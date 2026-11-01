"""Coordenação durável e determinística das análises OJS SEO.

Não contém cliente HTTP, scheduler ou thread. As rotas apenas registram uma
operação; os workers OAI-PMH e HTML chamam as transições curtas deste módulo
depois de confirmar seus próprios claims.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import OjsArticleObservation, OjsHtmlAuditCampaign, OjsJournal, OjsSeoAnalysisOperation, OjsSeoAuditRun, User, utcnow
from .ojs_seo_services import OjsSeoServiceError, get_ojs_journal, request_ojs_seo_audit
from .ojs_seo_technical import (
    get_html_coverage_for_journal_reference,
    reuse_compatible_html_evidence,
    start_ojs_html_campaign_for_reference,
)
from .services import record_audit


OPERATION_OPEN = frozenset({"oai_pending", "oai_running", "html_pending", "html_running", "interrupted"})
OPERATION_STATUS_LABELS = {
    "oai_pending": "Aguardando processamento",
    "oai_running": "Verificando dados bibliográficos",
    "html_pending": "Aguardando análise das páginas",
    "html_running": "Analisando páginas dos artigos",
    "completed": "Concluída",
    "completed_with_pending": "Concluída com pendências",
    "interrupted": "Interrompida",
    "failed": "Falhou",
    "canceled": "Cancelada",
}


def request_ojs_integrated_analysis(actor: User, journal_id: str) -> OjsSeoAnalysisOperation:
    """Registra uma única operação, sempre começando por uma coleta OAI.

    O cliente OAI atual não expõe uma coleta incremental confiável por
    datestamp. Portanto a atualização usa reconciliação completa por
    identificador OAI e ``metadata_hash``; isso detecta fluxo contínuo, novos
    registros e tombstones explícitos sem depender de títulos.
    """
    journal = get_ojs_journal(actor, journal_id)
    previous = _latest_usable_run(journal.id)
    if _open_operation(journal.id) is not None:
        raise OjsSeoServiceError("ojs_integrated_operation_open", "Já existe uma análise ou atualização aguardando processamento para este periódico.")
    operation = OjsSeoAnalysisOperation(
        journal_id=journal.id,
        mode="update" if previous is not None else "initial",
        status="oai_pending",
        parameters_json={
            "bibliographic_strategy": "full_reconciliation",
            "incremental_datestamp": "unavailable_in_current_client",
            "previous_oai_audit_run_id": previous.id if previous else None,
            "html_evidence_reuse": "eligible_after_explicit_compatibility_check",
        },
    )
    db.session.add(operation)
    try:
        db.session.flush()
        run = request_ojs_seo_audit(actor, journal.id)
        parameters = dict(run.parameters_json or {})
        parameters["integrated_operation_id"] = operation.id
        parameters["collection_strategy"] = "full_reconciliation"
        run.parameters_json = parameters
        operation.oai_audit_run_id = run.id
        record_audit(actor, "ojs_seo_integrated_analysis_requested", "ojs_seo_analysis_operation", operation.id, {}, {
            "journal_id": journal.id, "mode": operation.mode, "oai_audit_run_id": run.id,
        })
        return operation
    except IntegrityError as exc:
        raise OjsSeoServiceError("ojs_integrated_operation_open", "Já existe uma análise ou atualização aguardando processamento para este periódico.") from exc


def list_ojs_integrated_operations(actor: User, journal_id: str, *, include_inactive: bool = False) -> list[OjsSeoAnalysisOperation]:
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    return list(db.session.scalars(select(OjsSeoAnalysisOperation).where(
        OjsSeoAnalysisOperation.journal_id == journal.id,
    ).order_by(OjsSeoAnalysisOperation.created_at.desc(), OjsSeoAnalysisOperation.id.desc())))


def mark_integrated_oai_running(audit_run_id: str, *, now: datetime | None = None) -> None:
    operation = _operation_for_oai_run(audit_run_id)
    if operation is None or operation.status != "oai_pending":
        return
    instant = now or utcnow()
    operation.status = "oai_running"
    operation.started_at = operation.started_at or instant
    operation.updated_at = instant


def advance_integrated_after_oai(journal: OjsJournal, run: OjsSeoAuditRun, *, now: datetime | None = None) -> OjsSeoAnalysisOperation | None:
    """Libera HTML apenas após persistir a referência bibliográfica."""
    operation = _operation_for_oai_run(run.id)
    if operation is None or operation.journal_id != journal.id:
        return None
    instant = now or utcnow()
    if run.status not in {"completed", "partial"}:
        _finish(operation, "failed", run.completion_reason or "bibliographic_audit_failed", instant)
        return operation

    previous_id = _string_parameter(operation, "previous_oai_audit_run_id")
    delta = _reconciliation_delta(journal.id, previous_id, run.id)
    parameters = dict(operation.parameters_json or {})
    parameters["reconciliation"] = delta
    parameters["source_oai_audit_run_id"] = run.id
    operation.parameters_json = parameters

    # Sem alteração bibliográfica, a cobertura anterior continua sendo a única
    # fonte HTML aplicável. Em vez de reiniciar a seleção na nova referência,
    # a operação continua somente os artigos pendentes ou as retentativas da
    # referência anterior, que é registrada explicitamente na campanha.
    if previous_id and not any(delta[key] for key in ("new", "modified", "deleted")):
        prior = get_html_coverage_for_journal_reference(journal, previous_id)
        parameters["html_coverage_reference_oai_audit_run_id"] = previous_id
        parameters["html_continuation_reference_oai_audit_run_id"] = previous_id
        parameters["html_revalidation"] = "not_requested"
        operation.parameters_json = parameters
        if prior.unprocessed_article_count or prior.retryable_unverified_count:
            try:
                campaign = start_ojs_html_campaign_for_reference(
                    journal,
                    previous_id,
                    compatible_with_oai_audit_run_id=run.id,
                    requested_by="integrated_unchanged_continuation",
                )
            except OjsSeoServiceError as exc:
                _append_error(operation, exc.code, exc.safe_message)
                _finish(operation, "interrupted", exc.code, instant)
                return operation
            if campaign is not None:
                operation.html_campaign_id = campaign.id
                operation.status = "html_pending"
                operation.updated_at = instant
                return operation
        if prior.unverified_article_count or prior.unprocessed_article_count:
            _finish(operation, "completed_with_pending", "no_more_eligible_html_attempts", instant)
        else:
            _finish(operation, "completed", "no_bibliographic_changes", instant)
        return operation

    # Com uma referência nova, reaproveitamos somente evidências que satisfazem
    # todos os critérios documentados. Os vínculos preservam a origem e não
    # criam observações HTTP novas; o restante entra na campanha atual.
    reused_count = 0
    if previous_id:
        try:
            reused_count = reuse_compatible_html_evidence(
                journal,
                source_oai_audit_run_id=previous_id,
                target_oai_audit_run_id=run.id,
            )
        except OjsSeoServiceError as exc:
            _append_error(operation, exc.code, exc.safe_message)
            _finish(operation, "interrupted", exc.code, instant)
            return operation
    parameters["html_coverage_reference_oai_audit_run_id"] = run.id
    parameters["html_evidence_reuse"] = {
        "created_count": reused_count,
        "criteria": "stable_article_metadata_and_url_match",
        "source_oai_audit_run_id": previous_id,
        "target_oai_audit_run_id": run.id,
    }
    operation.parameters_json = parameters

    try:
        campaign = start_ojs_html_campaign_for_reference(journal, run.id)
    except OjsSeoServiceError as exc:
        _append_error(operation, exc.code, exc.safe_message)
        _finish(operation, "interrupted", exc.code, instant)
        return operation
    if campaign is None:
        coverage = get_html_coverage_for_journal_reference(journal, run.id)
        final_status = (
            "completed_with_pending"
            if run.status == "partial" or coverage.unverified_article_count or coverage.unprocessed_article_count
            else "completed"
        )
        _finish(operation, final_status, "no_html_revalidation_needed", instant)
        return operation
    operation.html_campaign_id = campaign.id
    operation.status = "html_pending"
    operation.updated_at = instant
    return operation


def advance_integrated_after_html_campaign(journal: OjsJournal, campaign: OjsHtmlAuditCampaign, *, now: datetime | None = None) -> OjsSeoAnalysisOperation | None:
    operation = _operation_for_campaign(campaign.id)
    if operation is None or operation.journal_id != journal.id:
        return None
    instant = now or utcnow()
    status = campaign.status
    if status == "pending":
        operation.status = "html_pending"
    elif status == "running":
        operation.status = "html_running"
        operation.started_at = operation.started_at or instant
    elif status == "completed":
        # Uma referência bibliográfica parcial não vira uma conclusão plena
        # apenas porque os lotes HTML disponíveis terminaram. A interface
        # precisa conservar essa ressalva da fonte que delimitou o universo.
        source = db.session.get(OjsSeoAuditRun, operation.oai_audit_run_id)
        final_status = "completed_with_pending" if source is not None and source.status == "partial" else "completed"
        _finish(operation, final_status, campaign.completion_reason or "all_eligible_pages_verified", instant)
    elif status == "completed_with_pending":
        _finish(operation, "completed_with_pending", campaign.completion_reason or "no_more_eligible_attempts", instant)
    elif status == "interrupted":
        _finish(operation, "interrupted", campaign.completion_reason or "html_campaign_interrupted", instant)
    else:
        _finish(operation, "failed", campaign.completion_reason or "html_campaign_failed", instant)
    operation.updated_at = instant
    return operation


def interrupt_integrated_for_oai_run(audit_run_id: str, code: str, *, now: datetime | None = None) -> None:
    operation = _operation_for_oai_run(audit_run_id)
    if operation is not None and operation.status in {"oai_pending", "oai_running"}:
        _finish(operation, "failed", code, now or utcnow())


def _latest_usable_run(journal_id: str, *, excluding_id: str | None = None) -> OjsSeoAuditRun | None:
    statement = select(OjsSeoAuditRun).where(
        OjsSeoAuditRun.journal_id == journal_id,
        OjsSeoAuditRun.status.in_(("completed", "partial")),
    )
    if excluding_id:
        statement = statement.where(OjsSeoAuditRun.id != excluding_id)
    return db.session.scalar(statement.order_by(
        OjsSeoAuditRun.completed_at.desc(), OjsSeoAuditRun.created_at.desc(), OjsSeoAuditRun.id.desc(),
    ).limit(1))


def _reconciliation_delta(journal_id: str, previous_id: str | None, current_id: str) -> dict[str, int]:
    current = {
        article_id: (metadata_hash, status)
        for article_id, metadata_hash, status in db.session.execute(select(
        OjsArticleObservation.article_id, OjsArticleObservation.metadata_hash, OjsArticleObservation.record_status,
    ).where(OjsArticleObservation.journal_id == journal_id, OjsArticleObservation.audit_run_id == current_id)).all()
    }
    if not previous_id:
        return {"new": sum(1 for _hash, status in current.values() if status == "active"), "modified": 0, "deleted": sum(1 for _hash, status in current.values() if status == "deleted"), "unchanged": 0}
    previous = {
        article_id: (metadata_hash, status)
        for article_id, metadata_hash, status in db.session.execute(select(
        OjsArticleObservation.article_id, OjsArticleObservation.metadata_hash, OjsArticleObservation.record_status,
    ).where(OjsArticleObservation.journal_id == journal_id, OjsArticleObservation.audit_run_id == previous_id)).all()
    }
    new = modified = deleted = unchanged = 0
    for article_id, (metadata_hash, status) in current.items():
        prior = previous.get(article_id)
        if prior is None:
            if status == "deleted": deleted += 1
            else: new += 1
        elif status == "deleted" and prior[1] != "deleted":
            deleted += 1
        elif prior[0] != metadata_hash or prior[1] != status:
            modified += 1
        else:
            unchanged += 1
    return {"new": new, "modified": modified, "deleted": deleted, "unchanged": unchanged}


def _open_operation(journal_id: str) -> OjsSeoAnalysisOperation | None:
    return db.session.scalar(select(OjsSeoAnalysisOperation).where(
        OjsSeoAnalysisOperation.journal_id == journal_id,
        OjsSeoAnalysisOperation.status.in_(OPERATION_OPEN),
    ).order_by(OjsSeoAnalysisOperation.created_at.desc()).limit(1))


def _operation_for_oai_run(run_id: str) -> OjsSeoAnalysisOperation | None:
    return db.session.scalar(select(OjsSeoAnalysisOperation).where(OjsSeoAnalysisOperation.oai_audit_run_id == run_id))


def _operation_for_campaign(campaign_id: str) -> OjsSeoAnalysisOperation | None:
    return db.session.scalar(select(OjsSeoAnalysisOperation).where(OjsSeoAnalysisOperation.html_campaign_id == campaign_id))


def _finish(operation: OjsSeoAnalysisOperation, status: str, reason: str, instant: datetime) -> None:
    operation.status = status
    operation.completion_reason = reason[:80]
    operation.completed_at = instant
    operation.updated_at = instant


def _append_error(operation: OjsSeoAnalysisOperation, code: str, message: str) -> None:
    values = list(operation.errors_json or [])
    values.append({"code": code[:80], "message": " ".join(message.split())[:500]})
    operation.errors_json = values


def _string_parameter(operation: OjsSeoAnalysisOperation, key: str) -> str | None:
    value = operation.parameters_json.get(key) if isinstance(operation.parameters_json, dict) else None
    return value if isinstance(value, str) and value else None


__all__ = [
    "OPERATION_OPEN", "OPERATION_STATUS_LABELS", "advance_integrated_after_html_campaign",
    "advance_integrated_after_oai", "interrupt_integrated_for_oai_run", "list_ojs_integrated_operations",
    "mark_integrated_oai_running", "request_ojs_integrated_analysis",
]
