"""Executor durável e worker exclusivo das auditorias OJS SEO.

O worker separa deliberadamente três fases: (A) claim curto e confirmado no
banco; (B) coleta OAI-PMH sem transação aberta; e (C) persistência atômica.
Claims usam token e lease persistidos, mais um índice parcial que admite no
máximo uma auditoria ``running`` por periódico, inclusive entre processos,
durante a vigência do lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
from typing import Callable
from uuid import uuid4

from flask import Flask, current_app
from sqlalchemy import exists, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import aliased

from .extensions import db
from .models import AnalyticsProject, OjsHtmlAuditRun, OjsJournal, OjsSeoAuditRun, utcnow
from .ojs_oai_client import (
    OaiClientError,
    OaiCollectionResult,
    OaiIssue,
    OaiPmhAuditResult,
    OaiPmhClient,
)
from .ojs_seo_persistence import OjsPersistenceError, persist_oai_collection
from .ojs_seo_coordinator import advance_integrated_after_oai, interrupt_integrated_for_oai_run, mark_integrated_oai_running
from .ojs_seo_services import journal_has_active_ojs_execution_authorization


OJS_SEO_WORKER_VERSION = "ojs-seo-worker-v1"
OJS_SEO_PENDING = "pending"
OJS_SEO_RUNNING = "running"
OJS_SEO_FINAL = frozenset({"completed", "partial", "failed", "canceled"})
DEFAULT_OJS_SEO_LEASE_SECONDS = 300
LEASE_SAFETY_MARGIN_SECONDS = 30
MAX_CLAIM_CANDIDATES = 32


class OjsSeoExecutorError(RuntimeError):
    """Falha segura do executor, sem URL, XML, token ou conteúdo OAI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


class OjsSeoClaimLostError(OjsSeoExecutorError):
    """O worker perdeu o lease antes de persistir o resultado externo."""


@dataclass(frozen=True, slots=True)
class OjsSeoAuditClaim:
    """Posse temporária e verificável de uma única auditoria persistida."""

    audit_run_id: str
    journal_id: str
    token: str
    claimed_at: datetime
    lease_expires_at: datetime
    attempt_count: int


ClientFactory = Callable[[str], OaiPmhClient]


def claim_next_ojs_seo_audit(*, lease_seconds: int = DEFAULT_OJS_SEO_LEASE_SECONDS) -> OjsSeoAuditClaim | None:
    """Reivindica e confirma no máximo uma auditoria pendente.

    O compare-and-set e o índice parcial evitam claims concorrentes. A função
    confirma apenas a transação A; não inicia coleta HTTP e não cria artigos.
    """
    _validate_lease_seconds(lease_seconds)
    pending_without_running = _pending_without_running_predicate()
    candidate_rows = db.session.execute(
        select(OjsSeoAuditRun.id, OjsSeoAuditRun.journal_id)
        .where(OjsSeoAuditRun.status == OJS_SEO_PENDING, pending_without_running)
        .order_by(OjsSeoAuditRun.created_at.asc(), OjsSeoAuditRun.id.asc())
        .limit(MAX_CLAIM_CANDIDATES)
    ).all()
    # A consulta abriu uma transação curta. Ela será encerrada antes de cada
    # CAS para que SQLite não retenha uma leitura durante a escrita do worker.
    db.session.rollback()
    for run_id, journal_id in candidate_rows:
        claim = _claim_audit_by_id(run_id, journal_id, lease_seconds=lease_seconds)
        if claim is not None:
            return claim
        # Um CAS recusado pode significar uma disputa legítima de claim, mas
        # também pode expor um registro pendente que nunca poderá executar.
        # Esta finalização é ela própria condicional e, portanto, não altera
        # uma auditoria que tenha sido reivindicada por outro processo.
        _finalize_rejected_pending_audit(run_id, journal_id)
    return None


def claim_ojs_seo_audit(
    audit_run_id: str, *, lease_seconds: int = DEFAULT_OJS_SEO_LEASE_SECONDS
) -> OjsSeoAuditClaim | None:
    """Reivindica uma auditoria pendente específica para diagnóstico controlado."""
    _validate_lease_seconds(lease_seconds)
    if not isinstance(audit_run_id, str) or not audit_run_id:
        raise OjsSeoExecutorError("invalid_audit_run", "A auditoria indicada é inválida.")
    journal_id = db.session.scalar(select(OjsSeoAuditRun.journal_id).where(
        OjsSeoAuditRun.id == audit_run_id,
        OjsSeoAuditRun.status == OJS_SEO_PENDING,
    ))
    db.session.rollback()
    if journal_id is None:
        return None
    return _claim_audit_by_id(audit_run_id, journal_id, lease_seconds=lease_seconds)


def _claim_audit_by_id(
    audit_run_id: str,
    journal_id: str,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> OjsSeoAuditClaim | None:
    instant = _as_utc(now or utcnow())
    expires = instant + timedelta(seconds=lease_seconds)
    token = str(uuid4())
    running = aliased(OjsSeoAuditRun)
    no_running_for_journal = ~exists(select(running.id).where(
        running.journal_id == OjsSeoAuditRun.journal_id,
        running.status == OJS_SEO_RUNNING,
    )).correlate(OjsSeoAuditRun)
    no_html_running_for_journal = ~exists(select(OjsHtmlAuditRun.id).where(
        OjsHtmlAuditRun.journal_id == OjsSeoAuditRun.journal_id,
        OjsHtmlAuditRun.status == OJS_SEO_RUNNING,
    )).correlate(OjsSeoAuditRun)
    active_project_for_journal = exists(
        select(AnalyticsProject.id)
        .join(OjsJournal, OjsJournal.analytics_project_id == AnalyticsProject.id)
        .where(
            OjsJournal.id == OjsSeoAuditRun.journal_id,
            AnalyticsProject.module_key == "ojs_seo",
            AnalyticsProject.status == "active",
            AnalyticsProject.archived_at.is_(None),
        )
    ).correlate(OjsSeoAuditRun)
    try:
        # O índice parcial protege duas auditorias OAI. Este lock curto do
        # periódico também serializa o claim contra a análise HTML em outro
        # processo. PostgreSQL usa lock de linha; SQLite usa BEGIN IMMEDIATE.
        if db.session.get_bind().dialect.name == "sqlite":
            db.session.execute(text("BEGIN IMMEDIATE"))
        else:
            db.session.begin()
            if db.session.scalar(select(OjsJournal.id).where(OjsJournal.id == journal_id).with_for_update()) is None:
                db.session.rollback()
                return None
        result = db.session.execute(
            update(OjsSeoAuditRun)
            .where(
                OjsSeoAuditRun.id == audit_run_id,
                OjsSeoAuditRun.journal_id == journal_id,
                OjsSeoAuditRun.status == OJS_SEO_PENDING,
                no_running_for_journal,
                no_html_running_for_journal,
                active_project_for_journal,
            )
            .values(
                status=OJS_SEO_RUNNING,
                started_at=instant,
                completed_at=None,
                claim_token=token,
                claimed_at=instant,
                lease_expires_at=expires,
                attempt_count=OjsSeoAuditRun.attempt_count + 1,
                completion_reason=None,
            )
        )
        if result.rowcount != 1:
            db.session.rollback()
            return None
        mark_integrated_oai_running(audit_run_id, now=instant)
        db.session.commit()
    except (IntegrityError, OperationalError):
        db.session.rollback()
        return None
    return OjsSeoAuditClaim(audit_run_id, journal_id, token, instant, expires, 1)


def _finalize_rejected_pending_audit(
    audit_run_id: str,
    journal_id: str | None = None,
) -> OjsSeoAuditRun | None:
    """Finaliza somente pendências que o claim corretamente não pode assumir.

    Cada ``UPDATE`` funciona como compare-and-set entre processos: além de
    exigir ``pending`` e ausência de token/lease, ele revalida o vínculo
    periódico→projeto na própria instrução SQL. Assim, uma disputa que venceu
    um claim legítimo ou uma execução já ``running`` nunca é sobrescrita.

    Projetos arquivados recebem ``canceled`` em vez de voltarem a ser ativos;
    projetos fora do domínio OJS SEO recebem a falha terminal explícita que
    permite diagnosticar a inconsistência sem iniciar HTTP.
    """
    if not isinstance(audit_run_id, str) or not audit_run_id:
        return None
    instant = _as_utc(utcnow())
    journal_scope = [OjsSeoAuditRun.journal_id == journal_id] if journal_id else []
    for status, code, message, predicate in (
        (
            "failed",
            "invalid_ojs_project",
            "O periódico não está vinculado a um domínio OJS SEO válido.",
            _invalid_ojs_project_for_journal_predicate(),
        ),
        (
            "canceled",
            "project_archived",
            "A auditoria foi cancelada porque o periódico está arquivado.",
            _archived_ojs_project_for_journal_predicate(),
        ),
    ):
        try:
            result = db.session.execute(
                update(OjsSeoAuditRun)
                .where(
                    OjsSeoAuditRun.id == audit_run_id,
                    *journal_scope,
                    OjsSeoAuditRun.status == OJS_SEO_PENDING,
                    OjsSeoAuditRun.claim_token.is_(None),
                    OjsSeoAuditRun.claimed_at.is_(None),
                    OjsSeoAuditRun.lease_expires_at.is_(None),
                    predicate,
                )
                .values(
                    status=status,
                    completed_at=instant,
                    completion_reason=code,
                    claim_token=None,
                    claimed_at=None,
                    lease_expires_at=None,
                )
            )
            if result.rowcount != 1:
                db.session.rollback()
                continue
            run = db.session.get(OjsSeoAuditRun, audit_run_id)
            if run is None:
                db.session.rollback()
                return None
            prior_errors = list(run.errors_json) if isinstance(run.errors_json, list) else []
            prior_errors.append({"code": code, "message": message})
            run.errors_json = prior_errors
            interrupt_integrated_for_oai_run(run.id, code, now=instant)
            db.session.commit()
            return db.session.get(OjsSeoAuditRun, audit_run_id)
        except (IntegrityError, OperationalError):
            db.session.rollback()
            return None
    return None


def _invalid_ojs_project_for_journal_predicate():
    """Projeto inexistente ou associado a módulo diferente de ``ojs_seo``."""
    valid_ojs_project = exists(
        select(AnalyticsProject.id)
        .join(OjsJournal, OjsJournal.analytics_project_id == AnalyticsProject.id)
        .where(
            OjsJournal.id == OjsSeoAuditRun.journal_id,
            AnalyticsProject.module_key == "ojs_seo",
        )
    ).correlate(OjsSeoAuditRun)
    return ~valid_ojs_project


def _archived_ojs_project_for_journal_predicate():
    """Projeto OJS existente, porém fora do ciclo ativo por arquivamento."""
    return exists(
        select(AnalyticsProject.id)
        .join(OjsJournal, OjsJournal.analytics_project_id == AnalyticsProject.id)
        .where(
            OjsJournal.id == OjsSeoAuditRun.journal_id,
            AnalyticsProject.module_key == "ojs_seo",
            AnalyticsProject.status == "archived",
            AnalyticsProject.archived_at.is_not(None),
        )
    ).correlate(OjsSeoAuditRun)


def recover_expired_ojs_seo_audits(*, now: datetime | None = None) -> int:
    """Finaliza somente claims cujo lease já expirou.

    A execução não é reaberta automaticamente. Um processo antigo que termine
    após a expiração não pode persistir porque a transação B exige o mesmo
    token ainda válido. O cliente tem orçamento máximo inferior ao lease, com
    margem de segurança, mas esta recuperação não encerra um processo externo
    que tenha ficado preso além de seu prazo; uma garantia de término físico
    exigirá isolamento do job com deadline na evolução do worker.
    """
    instant = _as_utc(now or utcnow())
    try:
        stale_runs = list(db.session.scalars(
            select(OjsSeoAuditRun)
            .where(
                OjsSeoAuditRun.status == OJS_SEO_RUNNING,
                OjsSeoAuditRun.lease_expires_at.is_not(None),
                OjsSeoAuditRun.lease_expires_at <= instant,
            )
            .with_for_update()
        ))
        recovered = 0
        for run in stale_runs:
            # O lock da linha em PostgreSQL e a serialização do escritor em
            # SQLite garantem que uma finalização válida prevaleça sobre a
            # recuperação. A condição é repetida para evitar estado obsoleto.
            if run.status != OJS_SEO_RUNNING or run.lease_expires_at is None:
                continue
            if _as_utc(run.lease_expires_at) > instant:
                continue
            _mark_run_failed_in_session(
                run,
                code="worker_lease_expired",
                message="A auditoria foi interrompida antes da conclusão. Crie uma nova auditoria para tentar novamente.",
                completed_at=instant,
            )
            interrupt_integrated_for_oai_run(run.id, "worker_lease_expired", now=instant)
            recovered += 1
        db.session.commit()
        return recovered
    except OperationalError as exc:
        db.session.rollback()
        raise OjsSeoExecutorError(
            "database_unavailable", "Não foi possível recuperar auditorias interrompidas no momento."
        ) from exc


def process_next_ojs_seo_audit(
    *,
    client_factory: ClientFactory | None = None,
    lease_seconds: int = DEFAULT_OJS_SEO_LEASE_SECONDS,
) -> OjsSeoAuditRun | None:
    """Reivindica e processa uma auditoria pendente, sem paralelismo interno."""
    claim = claim_next_ojs_seo_audit(lease_seconds=lease_seconds)
    return process_claimed_ojs_seo_audit(
        claim, client_factory=client_factory, lease_seconds=lease_seconds
    ) if claim is not None else None


def process_ojs_seo_audit(
    audit_run_id: str,
    *,
    client_factory: ClientFactory | None = None,
    lease_seconds: int = DEFAULT_OJS_SEO_LEASE_SECONDS,
) -> OjsSeoAuditRun | None:
    """Processa uma auditoria pendente específica, sem aceitar URL externa."""
    claim = claim_ojs_seo_audit(audit_run_id, lease_seconds=lease_seconds)
    if claim is not None:
        return process_claimed_ojs_seo_audit(
            claim, client_factory=client_factory, lease_seconds=lease_seconds
        )
    # O claim conservador não aceita projetos fora do domínio OJS ou
    # arquivados. Sem esta transição explícita, uma auditoria residual desses
    # projetos ficaria pendente para sempre. Nenhum cliente HTTP é criado.
    return _finalize_rejected_pending_audit(audit_run_id)


def process_claimed_ojs_seo_audit(
    claim: OjsSeoAuditClaim,
    *,
    client_factory: ClientFactory | None = None,
    lease_seconds: int = DEFAULT_OJS_SEO_LEASE_SECONDS,
) -> OjsSeoAuditRun | None:
    """Executa a fase externa e confirma o resultado de um claim válido."""
    _validate_lease_seconds(lease_seconds)
    try:
        endpoint = _endpoint_for_claim(claim)
        client = client_factory(endpoint) if client_factory is not None else OaiPmhClient(endpoint)
        _assert_client_fits_lease(client, lease_seconds)
        audit_result = client.inspect()
        collection = _collection_from_audit_result(audit_result)
        return _persist_claimed_collection(claim, audit_result, collection, lease_seconds=lease_seconds)
    except OjsSeoClaimLostError:
        db.session.rollback()
        return db.session.get(OjsSeoAuditRun, claim.audit_run_id)
    except OaiClientError as exc:
        db.session.rollback()
        collection = _failed_collection(exc.code, exc.safe_message)
        return _persist_or_mark_failed(claim, collection, None, lease_seconds=lease_seconds)
    except OjsSeoExecutorError as exc:
        db.session.rollback()
        return _mark_claim_failed(claim, exc.code, exc.safe_message)
    except OjsPersistenceError:
        db.session.rollback()
        return _mark_claim_failed(
            claim,
            "persistence_failed",
            "Não foi possível registrar a auditoria de forma consistente. Nenhuma alteração bibliográfica foi salva.",
        )
    except Exception:
        db.session.rollback()
        current_app.logger.error(
            "ojs_seo audit_run=%s operation=collect status=unexpected_error", claim.audit_run_id
        )
        return _mark_claim_failed(
            claim,
            "unexpected_error",
            "Não foi possível concluir a auditoria OJS. Nenhuma alteração bibliográfica foi salva.",
        )


def _endpoint_for_claim(claim: OjsSeoAuditClaim) -> str:
    run = db.session.scalar(select(OjsSeoAuditRun).where(
        OjsSeoAuditRun.id == claim.audit_run_id,
        OjsSeoAuditRun.journal_id == claim.journal_id,
        OjsSeoAuditRun.status == OJS_SEO_RUNNING,
        OjsSeoAuditRun.claim_token == claim.token,
        OjsSeoAuditRun.lease_expires_at > _as_utc(utcnow()),
    ))
    if run is None:
        db.session.rollback()
        raise OjsSeoClaimLostError("claim_lost", "A auditoria não está mais disponível para este worker.")
    journal = db.session.get(OjsJournal, claim.journal_id)
    project = journal.analytics_project if journal is not None else None
    endpoint = journal.oai_endpoint_url if journal is not None else None
    execution_authorized = bool(
        journal is not None and journal_has_active_ojs_execution_authorization(journal)
    )
    db.session.rollback()
    if project is None or project.module_key != "ojs_seo":
        raise OjsSeoExecutorError("invalid_ojs_project", "O periódico não está vinculado a um domínio OJS SEO válido.")
    if not execution_authorized:
        raise OjsSeoExecutorError(
            "ojs_subscription_ineligible",
            "O periódico não possui autorização ativa para uma nova auditoria OJS.",
        )
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise OjsSeoExecutorError("oai_endpoint_unavailable", "O periódico não possui endpoint OAI-PMH confirmado.")
    return endpoint


def _persist_or_mark_failed(
    claim: OjsSeoAuditClaim,
    collection: OaiCollectionResult,
    audit_result: OaiPmhAuditResult | None,
    *,
    lease_seconds: int,
) -> OjsSeoAuditRun | None:
    try:
        return _persist_claimed_collection(claim, audit_result, collection, lease_seconds=lease_seconds)
    except OjsSeoClaimLostError:
        db.session.rollback()
        return db.session.get(OjsSeoAuditRun, claim.audit_run_id)
    except OjsPersistenceError:
        db.session.rollback()
        return _mark_claim_failed(
            claim,
            "persistence_failed",
            "Não foi possível registrar a auditoria de forma consistente. Nenhuma alteração bibliográfica foi salva.",
        )


def _persist_claimed_collection(
    claim: OjsSeoAuditClaim,
    audit_result: OaiPmhAuditResult | None,
    collection: OaiCollectionResult,
    *,
    lease_seconds: int,
) -> OjsSeoAuditRun | None:
    """Transação B: confirma posse, persiste tudo e finaliza a auditoria."""
    instant = _as_utc(utcnow())
    renewed_until = instant + timedelta(seconds=lease_seconds)
    try:
        possession = db.session.execute(
            update(OjsSeoAuditRun)
            .where(
                OjsSeoAuditRun.id == claim.audit_run_id,
                OjsSeoAuditRun.journal_id == claim.journal_id,
                OjsSeoAuditRun.status == OJS_SEO_RUNNING,
                OjsSeoAuditRun.claim_token == claim.token,
                OjsSeoAuditRun.lease_expires_at > instant,
            )
            .values(lease_expires_at=renewed_until)
        )
        if possession.rowcount != 1:
            db.session.rollback()
            raise OjsSeoClaimLostError("claim_lost", "A auditoria não está mais disponível para este worker.")

        run = db.session.get(OjsSeoAuditRun, claim.audit_run_id)
        journal = db.session.get(OjsJournal, claim.journal_id)
        if run is None or journal is None or run.journal_id != journal.id:
            raise OjsSeoExecutorError("invalid_audit_scope", "A auditoria ou o periódico não está mais disponível.")
        if journal.analytics_project is None or journal.analytics_project.module_key != "ojs_seo":
            raise OjsSeoExecutorError("invalid_ojs_project", "O periódico não está vinculado a um domínio OJS SEO válido.")

        _apply_discovery_result(journal, audit_result, instant)
        run.processor_version = OJS_SEO_WORKER_VERSION
        # ``persist_oai_collection`` conclui o estado e limpa token/lease no
        # mesmo flush. Não podemos limpar antes: ela abre um savepoint e o
        # banco validaria indevidamente um estado ``running`` sem posse.
        persist_oai_collection(db.session, journal, run, collection, observed_at=instant)
        advance_integrated_after_oai(journal, run, now=instant)
        db.session.commit()
        return db.session.get(OjsSeoAuditRun, claim.audit_run_id)
    except OjsSeoExecutorError:
        raise
    except Exception:
        db.session.rollback()
        raise


def _apply_discovery_result(
    journal: OjsJournal,
    audit_result: OaiPmhAuditResult | None,
    observed_at: datetime,
) -> None:
    if audit_result is None:
        return
    if audit_result.identity is not None:
        journal.repository_name = audit_result.identity.repository_name[:500]
        journal.oai_endpoint_validated_at = observed_at
        journal.last_identified_at = observed_at
        journal.discovery_status = "identified"
        journal.last_discovery_error = None
        return
    issues = (*audit_result.errors, *audit_result.warnings)
    message = _safe_issue_message(issues[0].message) if issues else "Não foi possível identificar o repositório OAI-PMH."
    if journal.discovery_status == "pending":
        journal.discovery_status = "failed"
    journal.last_discovery_error = message


def _collection_from_audit_result(audit_result: OaiPmhAuditResult) -> OaiCollectionResult:
    if audit_result.collection is not None:
        return audit_result.collection
    issue = audit_result.errors[0] if audit_result.errors else OaiIssue(
        "oai_discovery_failed", "Não foi possível concluir a descoberta OAI-PMH."
    )
    return _failed_collection(issue.code, issue.message, warnings=audit_result.warnings)


def _failed_collection(
    code: str,
    message: str,
    *,
    warnings: tuple[OaiIssue, ...] = (),
) -> OaiCollectionResult:
    issue = OaiIssue(_safe_code(code), _safe_issue_message(message))
    return OaiCollectionResult(
        status="failed",
        completion_reason=issue.code,
        pages_fetched=0,
        total_response_bytes=0,
        records=(),
        deleted_records=(),
        unique_identifiers=(),
        duplicate_identifiers=(),
        warnings=warnings,
        errors=(issue,),
        elapsed_seconds=0.0,
    )


def _mark_claim_failed(claim: OjsSeoAuditClaim, code: str, message: str) -> OjsSeoAuditRun | None:
    """Registra uma falha fora da transação B depois de rollback seguro."""
    instant = _as_utc(utcnow())
    try:
        run = db.session.scalar(
            select(OjsSeoAuditRun)
            .where(
                OjsSeoAuditRun.id == claim.audit_run_id,
                OjsSeoAuditRun.journal_id == claim.journal_id,
                OjsSeoAuditRun.status == OJS_SEO_RUNNING,
                OjsSeoAuditRun.claim_token == claim.token,
            )
            .with_for_update()
        )
        if run is None:
            db.session.rollback()
            return db.session.get(OjsSeoAuditRun, claim.audit_run_id)
        _mark_run_failed_in_session(run, code=code, message=message, completed_at=instant)
        interrupt_integrated_for_oai_run(run.id, code, now=instant)
        db.session.commit()
        return db.session.get(OjsSeoAuditRun, claim.audit_run_id)
    except (IntegrityError, OperationalError):
        db.session.rollback()
        return None


def _mark_run_failed_in_session(
    run: OjsSeoAuditRun,
    *,
    code: str,
    message: str,
    completed_at: datetime,
) -> None:
    prior_errors = list(run.errors_json) if isinstance(run.errors_json, list) else []
    prior_errors.append({"code": _safe_code(code), "message": _safe_issue_message(message)})
    run.status = "failed"
    run.completed_at = completed_at
    run.completion_reason = _safe_code(code)
    run.errors_json = prior_errors
    run.claim_token = None
    run.lease_expires_at = None


def _assert_client_fits_lease(client: OaiPmhClient, lease_seconds: int) -> None:
    config = getattr(client, "config", None)
    maximum = getattr(config, "max_execution_seconds", None)
    if isinstance(maximum, (int, float)) and maximum + LEASE_SAFETY_MARGIN_SECONDS > lease_seconds:
        raise OjsSeoExecutorError(
            "lease_too_short",
            "O prazo de posse do worker é menor que o limite seguro da coleta OAI-PMH.",
        )


def _pending_without_running_predicate():
    running = aliased(OjsSeoAuditRun)
    no_oai_running = ~exists(select(running.id).where(
        running.journal_id == OjsSeoAuditRun.journal_id,
        running.status == OJS_SEO_RUNNING,
    )).correlate(OjsSeoAuditRun)
    no_html_running = ~exists(select(OjsHtmlAuditRun.id).where(
        OjsHtmlAuditRun.journal_id == OjsSeoAuditRun.journal_id,
        OjsHtmlAuditRun.status == OJS_SEO_RUNNING,
    )).correlate(OjsSeoAuditRun)
    return no_oai_running & no_html_running


def _validate_lease_seconds(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 60:
        raise OjsSeoExecutorError("invalid_lease", "O prazo de posse do worker precisa ser de pelo menos 60 segundos.")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_code(value: object) -> str:
    text = str(value) if isinstance(value, str) else "executor_error"
    return "".join(character for character in text if character.isalnum() or character in {"_", "-"})[:80] or "executor_error"


def _safe_issue_message(value: object) -> str:
    if not isinstance(value, str):
        return "Não foi possível concluir a auditoria OJS."
    return " ".join(value.split())[:300] or "Não foi possível concluir a auditoria OJS."


def run_ojs_seo_worker(
    app: Flask,
    *,
    once: bool,
    poll_interval: float,
    lease_seconds: int,
    max_jobs: int = 1,
    audit_run_id: str | None = None,
    client_factory: ClientFactory | None = None,
) -> None:
    """Loop de processo separado, conservador e sem paralelismo adicional."""
    if poll_interval <= 0:
        raise ValueError("O intervalo de polling precisa ser positivo.")
    _validate_lease_seconds(lease_seconds)
    if not isinstance(max_jobs, int) or max_jobs <= 0:
        raise ValueError("O limite de auditorias precisa ser positivo.")
    if audit_run_id and not isinstance(audit_run_id, str):
        raise ValueError("A auditoria indicada é inválida.")

    processed = 0
    with app.app_context():
        while True:
            recovered = recover_expired_ojs_seo_audits()
            if recovered:
                current_app.logger.warning("ojs_seo worker recovered_expired_audits=%s", recovered)
            run = (
                process_ojs_seo_audit(
                    audit_run_id, client_factory=client_factory, lease_seconds=lease_seconds
                ) if audit_run_id else process_next_ojs_seo_audit(
                    client_factory=client_factory, lease_seconds=lease_seconds
                )
            )
            if run is not None:
                processed += 1
                current_app.logger.info("ojs_seo worker audit_run=%s status=%s", run.id, run.status)
            db.session.remove()
            if once or audit_run_id or processed >= max_jobs:
                return
            if run is None:
                time.sleep(poll_interval)


__all__ = [
    "DEFAULT_OJS_SEO_LEASE_SECONDS",
    "OJS_SEO_WORKER_VERSION",
    "OjsSeoAuditClaim",
    "OjsSeoClaimLostError",
    "OjsSeoExecutorError",
    "claim_next_ojs_seo_audit",
    "claim_ojs_seo_audit",
    "process_claimed_ojs_seo_audit",
    "process_next_ojs_seo_audit",
    "process_ojs_seo_audit",
    "recover_expired_ojs_seo_audits",
    "run_ojs_seo_worker",
]
