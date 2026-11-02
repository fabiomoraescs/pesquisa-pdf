"""Worker durável, serializado por periódico, para diagnóstico técnico HTML."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
from threading import Event
from typing import Callable
from uuid import uuid4

from flask import Flask, current_app
from sqlalchemy import exists, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import aliased

from .extensions import db
from .models import (
    AnalyticsProject,
    OjsArticle,
    OjsArticleObservation,
    OjsHtmlAuditCampaign,
    OjsHtmlAuditRun,
    OjsJournal,
    OjsSeoAuditRun,
    utcnow,
)
from .ojs_html_client import HtmlPageClient, analyze_html_page
from .ojs_seo_coordinator import advance_integrated_after_html_campaign
from .ojs_seo_services import journal_has_active_ojs_execution_authorization
from .ojs_seo_technical import (
    HTML_AUDIT_MAX_RESPONSE_BYTES,
    HtmlAuditCandidate,
    advance_ojs_html_audit_campaign,
    interrupt_ojs_html_audit_campaign,
    list_html_audit_candidates,
    persist_html_audit_results,
)
from .worker_runtime import graceful_worker_shutdown


OJS_HTML_WORKER_VERSION = "ojs-html-worker-v1"
DEFAULT_OJS_HTML_LEASE_SECONDS = 300
HTML_LEASE_SAFETY_MARGIN_SECONDS = 30
HTML_MAX_EXECUTION_SECONDS = 240
# Uma nova página pode acionar os timeouts de conexão/leitura do cliente. O
# worker não inicia a requisição quando já não há orçamento mínimo para uma
# tentativa completa; assim não grava "não verificado" para URL que sequer foi
# solicitada. Este valor não altera os timeouts nem o limite global.
HTML_MINIMUM_PAGE_BUDGET_SECONDS = 30
MAX_HTML_CLAIM_CANDIDATES = 32


class OjsHtmlExecutorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True, slots=True)
class OjsHtmlAuditClaim:
    audit_run_id: str
    journal_id: str
    token: str
    claimed_at: datetime
    lease_expires_at: datetime


HtmlClientFactory = Callable[[], HtmlPageClient]


def claim_next_ojs_html_audit(*, lease_seconds: int = DEFAULT_OJS_HTML_LEASE_SECONDS) -> OjsHtmlAuditClaim | None:
    _validate_lease_seconds(lease_seconds)
    rows = db.session.execute(select(OjsHtmlAuditRun.id, OjsHtmlAuditRun.journal_id).where(
        OjsHtmlAuditRun.status == "pending", _no_other_running_for_html_claim(), _active_ojs_project_predicate(),
        _active_campaign_for_html_claim(),
    ).order_by(OjsHtmlAuditRun.created_at.asc(), OjsHtmlAuditRun.id.asc()).limit(MAX_HTML_CLAIM_CANDIDATES)).all()
    db.session.rollback()
    for run_id, journal_id in rows:
        claim = claim_ojs_html_audit(run_id, lease_seconds=lease_seconds, journal_id=journal_id)
        if claim is not None:
            return claim
    return None


def claim_ojs_html_audit(
    audit_run_id: str, *, lease_seconds: int = DEFAULT_OJS_HTML_LEASE_SECONDS, journal_id: str | None = None,
) -> OjsHtmlAuditClaim | None:
    _validate_lease_seconds(lease_seconds)
    if not isinstance(audit_run_id, str) or not audit_run_id:
        raise OjsHtmlExecutorError("invalid_html_audit", "A análise técnica indicada é inválida.")
    known_journal = journal_id or db.session.scalar(select(OjsHtmlAuditRun.journal_id).where(
        OjsHtmlAuditRun.id == audit_run_id, OjsHtmlAuditRun.status == "pending",
    ))
    db.session.rollback()
    if not known_journal:
        return None
    now = _as_utc(utcnow())
    token = str(uuid4())
    try:
        if db.session.get_bind().dialect.name == "sqlite":
            db.session.execute(text("BEGIN IMMEDIATE"))
        else:
            db.session.begin()
            if db.session.scalar(select(OjsJournal.id).where(OjsJournal.id == known_journal).with_for_update()) is None:
                db.session.rollback()
                return None
        result = db.session.execute(update(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.id == audit_run_id,
            OjsHtmlAuditRun.journal_id == known_journal,
            OjsHtmlAuditRun.status == "pending",
            _no_other_running_for_html_claim(),
            _active_ojs_project_predicate(),
            _active_campaign_for_html_claim(),
        ).values(
            status="running", started_at=now, completed_at=None, claim_token=token, claimed_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempt_count=OjsHtmlAuditRun.attempt_count + 1, completion_reason=None,
        ))
    except (IntegrityError, OperationalError):
        db.session.rollback()
        return None
    if result.rowcount != 1:
        db.session.rollback()
        return None
    claimed_run = db.session.get(OjsHtmlAuditRun, audit_run_id)
    if claimed_run is not None and claimed_run.campaign_id:
        campaign = db.session.scalar(select(OjsHtmlAuditCampaign).where(
            OjsHtmlAuditCampaign.id == claimed_run.campaign_id,
            OjsHtmlAuditCampaign.journal_id == known_journal,
            OjsHtmlAuditCampaign.source_oai_audit_run_id == claimed_run.source_oai_audit_run_id,
            OjsHtmlAuditCampaign.status.in_(("pending", "running")),
        ))
        if campaign is None:
            db.session.rollback()
            return None
        campaign.status = "running"
        campaign.started_at = campaign.started_at or now
        campaign.updated_at = now
        advance_integrated_after_html_campaign(db.session.get(OjsJournal, known_journal), campaign, now=now)
    try:
        db.session.commit()
    except (IntegrityError, OperationalError):
        db.session.rollback()
        return None
    return OjsHtmlAuditClaim(audit_run_id, known_journal, token, now, now + timedelta(seconds=lease_seconds))


def _active_ojs_project_predicate():
    return exists(select(AnalyticsProject.id).join(
        OjsJournal, OjsJournal.analytics_project_id == AnalyticsProject.id,
    ).where(
        OjsJournal.id == OjsHtmlAuditRun.journal_id,
        AnalyticsProject.module_key == "ojs_seo", AnalyticsProject.status == "active",
        AnalyticsProject.archived_at.is_(None),
    )).correlate(OjsHtmlAuditRun)


def _active_campaign_for_html_claim():
    """Permite lotes legados sem campanha e protege campanhas finalizadas."""
    campaign_active = exists(select(OjsHtmlAuditCampaign.id).where(
        OjsHtmlAuditCampaign.id == OjsHtmlAuditRun.campaign_id,
        OjsHtmlAuditCampaign.journal_id == OjsHtmlAuditRun.journal_id,
        OjsHtmlAuditCampaign.source_oai_audit_run_id == OjsHtmlAuditRun.source_oai_audit_run_id,
        OjsHtmlAuditCampaign.status.in_(("pending", "running")),
    )).correlate(OjsHtmlAuditRun)
    return OjsHtmlAuditRun.campaign_id.is_(None) | campaign_active


def _no_other_running_for_html_claim():
    same_html = aliased(OjsHtmlAuditRun)
    html_running = exists(select(same_html.id).where(
        same_html.journal_id == OjsHtmlAuditRun.journal_id, same_html.status == "running",
    )).correlate(OjsHtmlAuditRun)
    oai_running = exists(select(OjsSeoAuditRun.id).where(
        OjsSeoAuditRun.journal_id == OjsHtmlAuditRun.journal_id, OjsSeoAuditRun.status == "running",
    )).correlate(OjsHtmlAuditRun)
    return ~html_running & ~oai_running


def process_next_ojs_html_audit(*, client_factory: HtmlClientFactory | None = None, lease_seconds: int = DEFAULT_OJS_HTML_LEASE_SECONDS) -> OjsHtmlAuditRun | None:
    claim = claim_next_ojs_html_audit(lease_seconds=lease_seconds)
    return process_claimed_ojs_html_audit(claim, client_factory=client_factory, lease_seconds=lease_seconds) if claim else None


def process_ojs_html_audit(
    audit_run_id: str, *, client_factory: HtmlClientFactory | None = None, lease_seconds: int = DEFAULT_OJS_HTML_LEASE_SECONDS,
) -> OjsHtmlAuditRun | None:
    claim = claim_ojs_html_audit(audit_run_id, lease_seconds=lease_seconds)
    return process_claimed_ojs_html_audit(claim, client_factory=client_factory, lease_seconds=lease_seconds) if claim else None


def process_claimed_ojs_html_audit(
    claim: OjsHtmlAuditClaim, *, client_factory: HtmlClientFactory | None = None, lease_seconds: int = DEFAULT_OJS_HTML_LEASE_SECONDS,
) -> OjsHtmlAuditRun | None:
    """Fase externa sem transação longa; cada falha de página é evidência própria."""
    _validate_lease_seconds(lease_seconds)
    try:
        repository_name, _run, candidates, eligible = _scope_and_candidates_for_claim(claim)
        client = client_factory() if client_factory is not None else HtmlPageClient()
        deadline = time.monotonic() + min(HTML_MAX_EXECUTION_SECONDS, lease_seconds - HTML_LEASE_SAFETY_MARGIN_SECONDS)
        results = []
        partial = False
        response_bytes = 0
        for snapshot in candidates:
            if not _has_page_budget(deadline):
                partial = True
                break
            fetched = client.fetch(snapshot.source_url, deadline=deadline)
            response_bytes += len(fetched.body or b"")
            result = analyze_html_page(
                fetched,
                oai_fields=snapshot.observation.dublin_core_json if isinstance(snapshot.observation.dublin_core_json, dict) else {},
                oai_doi=snapshot.observation.doi_observed,
                repository_name=repository_name,
                oai_dois=(
                    snapshot.observation.dois_observed_json
                    if isinstance(snapshot.observation.dois_observed_json, list) else ()
                ),
            )
            results.append((snapshot, result))
            if response_bytes >= HTML_AUDIT_MAX_RESPONSE_BYTES:
                partial = True
                break
        return _persist_claimed_results(
            claim, results, eligible_article_count=eligible,
            final_status="partial" if partial else "completed",
            completion_reason=("response_size_limit" if response_bytes >= HTML_AUDIT_MAX_RESPONSE_BYTES else "execution_time_limit") if partial else None,
            response_bytes=response_bytes,
            lease_seconds=lease_seconds,
        )
    except OjsHtmlExecutorError as exc:
        db.session.rollback()
        return _mark_claim_failed(claim, exc.code, exc.safe_message)
    except Exception:
        db.session.rollback()
        current_app.logger.error("ojs_seo html_audit_run=%s operation=collect status=unexpected_error", claim.audit_run_id)
        return _mark_claim_failed(claim, "unexpected_error", "Não foi possível concluir a análise técnica HTML. Nenhuma evidência foi salva.")


def _has_page_budget(deadline: float, *, now: float | None = None) -> bool:
    """Indica se ainda há tempo suficiente para iniciar uma página nova."""
    instant = time.monotonic() if now is None else now
    return deadline - instant >= HTML_MINIMUM_PAGE_BUDGET_SECONDS


def _scope_and_candidates_for_claim(claim: OjsHtmlAuditClaim):
    run = db.session.scalar(select(OjsHtmlAuditRun).where(
        OjsHtmlAuditRun.id == claim.audit_run_id, OjsHtmlAuditRun.journal_id == claim.journal_id,
        OjsHtmlAuditRun.status == "running", OjsHtmlAuditRun.claim_token == claim.token,
        OjsHtmlAuditRun.lease_expires_at > _as_utc(utcnow()),
    ))
    journal = db.session.get(OjsJournal, claim.journal_id)
    if run is None or journal is None or journal.analytics_project is None:
        db.session.rollback()
        raise OjsHtmlExecutorError("html_claim_lost", "A análise técnica não está mais disponível para este worker.")
    if journal.analytics_project.module_key != "ojs_seo" or journal.analytics_project.status != "active":
        db.session.rollback()
        raise OjsHtmlExecutorError("invalid_ojs_project", "O periódico não pertence a um projeto OJS SEO ativo.")
    if not journal_has_active_ojs_execution_authorization(journal):
        db.session.rollback()
        raise OjsHtmlExecutorError("ojs_subscription_ineligible", "O periódico não possui autorização ativa para uma análise técnica OJS.")
    eligible, candidates = list_html_audit_candidates(journal, run)
    snapshots = tuple(_snapshot_candidate(candidate) for candidate in candidates)
    # Fecha a transação de leitura antes de qualquer GET público. A persistência
    # posterior confirma novamente o token e o lease.
    db.session.rollback()
    return journal.repository_name, run, snapshots, eligible


def _snapshot_candidate(candidate: HtmlAuditCandidate) -> HtmlAuditCandidate:
    # O executor carrega apenas valores necessários; objetos são recarregados
    # na transação de persistência para não usar sessão expirável durante rede.
    article = OjsArticle(id=candidate.article.id, journal_id=candidate.article.journal_id, oai_identifier=candidate.article.oai_identifier)
    observation = OjsArticleObservation(
        id=candidate.observation.id, journal_id=candidate.observation.journal_id,
        article_id=candidate.observation.article_id, audit_run_id=candidate.observation.audit_run_id,
        record_status=candidate.observation.record_status,
        dublin_core_json=dict(candidate.observation.dublin_core_json or {}),
        doi_observed=candidate.observation.doi_observed,
    )
    return HtmlAuditCandidate(article, observation, candidate.source_url)


def _persist_claimed_results(
    claim: OjsHtmlAuditClaim,
    results, *, eligible_article_count: int, final_status: str, completion_reason: str | None, response_bytes: int, lease_seconds: int,
) -> OjsHtmlAuditRun | None:
    now = _as_utc(utcnow())
    possession = db.session.execute(update(OjsHtmlAuditRun).where(
        OjsHtmlAuditRun.id == claim.audit_run_id, OjsHtmlAuditRun.journal_id == claim.journal_id,
        OjsHtmlAuditRun.status == "running", OjsHtmlAuditRun.claim_token == claim.token,
        OjsHtmlAuditRun.lease_expires_at > now,
    ).values(lease_expires_at=now + timedelta(seconds=lease_seconds)))
    if possession.rowcount != 1:
        db.session.rollback()
        return db.session.get(OjsHtmlAuditRun, claim.audit_run_id)
    journal = db.session.get(OjsJournal, claim.journal_id)
    run = db.session.get(OjsHtmlAuditRun, claim.audit_run_id)
    if journal is None or run is None or journal.analytics_project is None or journal.analytics_project.status != "active":
        db.session.rollback()
        return _mark_claim_failed(claim, "invalid_ojs_project", "O periódico não está disponível para a análise técnica.")
    managed_results = []
    for snapshot, analysis in results:
        article = db.session.get(OjsArticle, snapshot.article.id)
        observation = db.session.get(OjsArticleObservation, snapshot.observation.id)
        if article is None or observation is None or article.journal_id != journal.id or observation.journal_id != journal.id:
            db.session.rollback()
            return _mark_claim_failed(claim, "html_source_changed", "A evidência bibliográfica da análise técnica não está mais disponível.")
        managed_results.append((HtmlAuditCandidate(article, observation, snapshot.source_url), analysis))
    try:
        run.processor_version = OJS_HTML_WORKER_VERSION
        persist_html_audit_results(
            journal, run, managed_results, eligible_article_count=eligible_article_count,
            final_status=final_status, completion_reason=completion_reason, response_bytes=response_bytes, completed_at=now,
        )
        # A criação do próximo lote (ou a conclusão da campanha) pertence à
        # mesma transação que confirma a evidência deste lote. Assim, uma
        # queda não deixa a campanha concluída sem dados, nem dados sem uma
        # transição de campanha rastreável.
        campaign = advance_ojs_html_audit_campaign(journal, run, completed_at=now)
        if campaign is not None:
            advance_integrated_after_html_campaign(journal, campaign, now=now)
        db.session.commit()
        return db.session.get(OjsHtmlAuditRun, claim.audit_run_id)
    except Exception:
        db.session.rollback()
        raise


def recover_expired_ojs_html_audits(*, now: datetime | None = None) -> int:
    instant = _as_utc(now or utcnow())
    try:
        runs = list(db.session.scalars(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.status == "running", OjsHtmlAuditRun.lease_expires_at.is_not(None),
            OjsHtmlAuditRun.lease_expires_at <= instant,
        )))
        for run in runs:
            _finalize_run(run, "failed", "worker_lease_expired", "A análise técnica foi interrompida antes da conclusão. Solicite uma nova análise.", instant)
            campaign = interrupt_ojs_html_audit_campaign(
                run,
                "worker_lease_expired",
                "O worker interrompeu o lote antes da conclusão; a campanha pode ser retomada com segurança.",
                completed_at=instant,
            )
            if campaign is not None:
                journal = db.session.get(OjsJournal, run.journal_id)
                if journal is not None:
                    advance_integrated_after_html_campaign(journal, campaign, now=instant)
        db.session.commit()
        return len(runs)
    except OperationalError as exc:
        db.session.rollback()
        raise OjsHtmlExecutorError("database_unavailable", "Não foi possível recuperar análises interrompidas agora.") from exc


def _mark_claim_failed(claim: OjsHtmlAuditClaim, code: str, message: str) -> OjsHtmlAuditRun | None:
    try:
        run = db.session.scalar(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.id == claim.audit_run_id, OjsHtmlAuditRun.journal_id == claim.journal_id,
            OjsHtmlAuditRun.status == "running", OjsHtmlAuditRun.claim_token == claim.token,
        ))
        if run is None:
            db.session.rollback()
            return db.session.get(OjsHtmlAuditRun, claim.audit_run_id)
        _finalize_run(run, "failed", code, message, _as_utc(utcnow()))
        campaign = interrupt_ojs_html_audit_campaign(run, code, message)
        if campaign is not None:
            journal = db.session.get(OjsJournal, run.journal_id)
            if journal is not None:
                advance_integrated_after_html_campaign(journal, campaign)
        db.session.commit()
        return db.session.get(OjsHtmlAuditRun, claim.audit_run_id)
    except (IntegrityError, OperationalError):
        db.session.rollback()
        return None


def _finalize_run(run: OjsHtmlAuditRun, status: str, code: str, message: str, completed_at: datetime) -> None:
    errors = list(run.errors_json) if isinstance(run.errors_json, list) else []
    errors.append({"code": _safe_code(code), "message": " ".join(message.split())[:500]})
    run.status, run.completion_reason, run.completed_at = status, _safe_code(code), completed_at
    run.errors_json = errors
    run.claim_token = run.claimed_at = run.lease_expires_at = None


def _safe_code(value: object) -> str:
    text = value if isinstance(value, str) else "html_executor_error"
    return "".join(character for character in text if character.isalnum() or character in {"_", "-"})[:80] or "html_executor_error"


def _validate_lease_seconds(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 60:
        raise OjsHtmlExecutorError("invalid_lease", "O prazo de posse do worker precisa ser de pelo menos 60 segundos.")
    if value <= HTML_LEASE_SAFETY_MARGIN_SECONDS:
        raise OjsHtmlExecutorError("lease_too_short", "O prazo de posse do worker é insuficiente para a análise técnica.")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def run_ojs_html_worker(
    app: Flask, *, once: bool, poll_interval: float, lease_seconds: int, max_jobs: int | None = None,
    audit_run_id: str | None = None, client_factory: HtmlClientFactory | None = None,
    until_idle: bool = False,
    stop_event: Event | None = None,
) -> None:
    if poll_interval <= 0 or (max_jobs is not None and max_jobs <= 0):
        raise ValueError("Os limites do worker HTML precisam ser positivos.")
    processed = 0
    with app.app_context(), graceful_worker_shutdown(stop_event) as stopping:
        while not stopping.is_set():
            recover_expired_ojs_html_audits()
            if stopping.is_set():
                db.session.remove()
                break
            run = process_ojs_html_audit(audit_run_id, client_factory=client_factory, lease_seconds=lease_seconds) if audit_run_id else process_next_ojs_html_audit(client_factory=client_factory, lease_seconds=lease_seconds)
            if run is not None:
                processed += 1
                current_app.logger.info("ojs_seo html_audit_run=%s status=%s", run.id, run.status)
            db.session.remove()
            if stopping.is_set():
                current_app.logger.info("ojs_seo html worker stopping_after_current_job=%s", run.id if run else None)
                return
            if once or audit_run_id or (not until_idle and max_jobs is not None and processed >= max_jobs):
                return
            if run is None:
                if until_idle:
                    return
                stopping.wait(poll_interval)


__all__ = [
    "DEFAULT_OJS_HTML_LEASE_SECONDS", "HTML_MINIMUM_PAGE_BUDGET_SECONDS", "OJS_HTML_WORKER_VERSION", "OjsHtmlAuditClaim", "OjsHtmlExecutorError",
    "claim_next_ojs_html_audit", "claim_ojs_html_audit", "process_claimed_ojs_html_audit",
    "process_next_ojs_html_audit", "process_ojs_html_audit", "recover_expired_ojs_html_audits", "run_ojs_html_worker",
]
