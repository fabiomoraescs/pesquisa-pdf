"""Interface autenticada do OJS SEO, sem ativar a ferramenta no catálogo.

As rotas usam exclusivamente os serviços do domínio OJS. Enquanto a ``Tool``
``ojs_seo`` continuar ausente ou inativa, ``require_tool_access`` bloqueia a
entrada antes de qualquer leitura das tabelas OJS. Os testes criam uma Tool
efêmera somente no SQLite descartável para cobrir a interface futura.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Mapping
from uuid import UUID

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from .extensions import db
from .models import (
    OjsArticle,
    OjsArticleHtmlAnalysis,
    OjsHtmlAuditRun,
    OjsHtmlEvidenceReuse,
    OjsJournal,
    OjsSeoAuditRun,
)
from .ojs_seo_coordinator import OPERATION_OPEN, OPERATION_STATUS_LABELS, list_ojs_integrated_operations, request_ojs_integrated_analysis
from .ojs_html_client import html_finding_label, html_issue_message
from .ojs_seo_activation import OJS_SEO_DEVELOPMENT_WORKSPACE_PREFIX
from .ojs_seo_diagnostics import (
    FILTER_LABELS,
    OjsSeoDiagnosticFilterError,
    editorial_recommendations,
    get_ojs_article_diagnostic,
    list_ojs_article_diagnostics,
)
from .ojs_seo_dashboard import build_ojs_seo_consolidated_dashboard, build_ojs_seo_dashboard
from .ojs_seo_exports import OjsSeoExportError, build_ojs_seo_export, render_ojs_seo_export
from .ojs_seo_recommendations import (
    APPLICABILITY_LABELS,
    CERTAINTY_LABELS,
    NATURE_LABELS,
    PRIORITY_LABELS,
    ROLE_LABELS,
    SEVERITY_LABELS,
    OjsRecommendationFilterError,
    get_ojs_recommendation_detail,
    list_ojs_recommendations,
)
from .ojs_seo_services import (
    OjsSeoAuthorizationError,
    OjsSeoAuditCancellationError,
    OjsSeoNotFoundError,
    OjsSeoProjectDeletionError,
    OjsSeoServiceError,
    archive_ojs_seo_project,
    cancel_pending_ojs_seo_audit,
    can_request_ojs_seo_audit,
    delete_ojs_seo_project_permanently,
    get_ojs_journal,
    get_ojs_seo_audit_result,
    has_administrative_ojs_context,
    list_ojs_article_observations,
    list_archived_ojs_journals,
    list_ojs_journals,
    list_ojs_seo_audits,
    list_ojs_seo_subscriptions_for_actor,
    list_ojs_seo_subscription_usage_for_actor,
    register_ojs_journal,
    request_ojs_seo_audit,
    restore_ojs_seo_project,
)
from .ojs_seo_technical import (
    HTML_FILTER_LABELS,
    HTML_CAMPAIGN_OPEN,
    HTML_CAMPAIGN_STATUS_LABELS,
    HTML_SELECTION_CONTINUE,
    HTML_SELECTION_LABELS,
    HTML_SELECTION_REVALIDATE,
    HTML_SELECTION_RETRY_UNVERIFIED,
    can_request_ojs_html_audit,
    can_request_ojs_html_audit_campaign,
    get_ojs_html_analysis_page,
    get_ojs_html_audit,
    get_ojs_html_coverage,
    html_evidence_reuse_available,
    list_ojs_html_analysis_pages,
    list_ojs_html_audit_campaigns,
    list_ojs_html_audits,
    request_ojs_html_audit,
    request_ojs_html_audit_campaign,
    resume_ojs_html_audit_campaign,
)
from .pagination import PlatformPage, paginate_sequence, requested_page
from .services import require_tool_access


OJS_SEO_TOOL_ID = "ojs_seo"
OJS_SEO_PROJECT_MODULE = "ojs_seo"
OJS_AUDIT_STATUS_LABELS = {
    "pending": "Pendente",
    "running": "Em execução",
    "completed": "Concluída",
    "partial": "Parcial",
    "failed": "Falhou",
    "canceled": "Cancelada",
}
OJS_AUDIT_COMPLETION_REASON_LABELS = {
    "canceled_by_user": "Cancelada pelo usuário.",
    "project_archived": "Cancelada porque o periódico está arquivado.",
}
OJS_DISCOVERY_STATUS_LABELS = {
    "pending": "Pendente",
    "identified": "Identificado",
    "failed": "Falhou",
    "unavailable": "Indisponível",
}
OJS_COVERAGE_FIELDS = (
    ("title", "Título"),
    ("creator", "Autoria"),
    ("description", "Descrição"),
    ("subject", "Assunto"),
    ("language", "Idioma"),
    ("date", "Data"),
    ("type", "Tipo"),
    ("identifier", "Identificadores"),
)
OJS_DIAGNOSTIC_STATUS_LABELS = {
    "present": "Presente no OAI-PMH",
    "absent": "Ausente no OAI-PMH",
    "unverified": "Não verificado",
}
OJS_HTML_AUDIT_STATUS_LABELS = OJS_AUDIT_STATUS_LABELS


ojs_seo_bp = Blueprint("ojs_seo", __name__, url_prefix="/analytics/ojs-seo")


def _require_tool() -> None:
    require_tool_access(current_user, OJS_SEO_TOOL_ID)


def _wants_json_response() -> bool:
    """Reconhece o envio assíncrono sem alterar o fallback HTML.

    A autorização, o CSRF e a criação da auditoria continuam idênticos nos
    dois caminhos. A negociação de conteúdo só evita que o navegador precise
    recarregar a página para começar a acompanhar uma solicitação já gravada.
    """
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        and request.accept_mimetypes.best == "application/json"
    )


def _accepted_bibliographic_analysis_response(
    journal: OjsJournal,
    *,
    audit_run_id: str,
    operation_id: str | None = None,
):
    """Retorna referências para acompanhar uma auditoria já persistida.

    Nenhuma coleta é iniciada por esta resposta. O worker separado continua
    sendo a única entidade que reivindica e processa a fila OAI-PMH.
    """
    return jsonify({
        "audit_run_id": audit_run_id,
        "operation_id": operation_id,
        "status": "pending",
        "stage": "oai_pending",
        "message": "A solicitação foi registrada e aguarda o worker dos dados bibliográficos.",
        "progress_url": url_for("ojs_seo.journal_history_progress", journal_id=journal.id),
        "history_url": url_for("ojs_seo.journal_history", journal_id=journal.id),
    }), 202


def _not_found_from_service(error: OjsSeoServiceError) -> None:
    """Converte recursos fora do escopo em uma resposta neutra."""
    if isinstance(error, (OjsSeoNotFoundError, OjsSeoAuthorizationError)):
        abort(404)
    raise error


def _safe_list(value: object) -> list[dict[str, str]]:
    """Mantém apenas avisos já sanitizados e estruturados para o template."""
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        message = item.get("message")
        if isinstance(code, str) and isinstance(message, str):
            rows.append({"code": code[:80], "message": message[:500]})
    return rows


def _html_findings_for_display(value: object) -> tuple[dict[str, str], ...]:
    """Normaliza apenas a apresentação de evidências HTML históricas.

    Resultados gravados antes da correção podem conter uma mensagem genérica da
    camada de transporte OAI reutilizada. O dado histórico não é regravado: a
    tela contextualiza somente os códigos de transporte conhecidos como HTML.
    """
    if not isinstance(value, list):
        return ()
    rows: list[dict[str, str]] = []
    classifications = {"confirmed", "potential", "unverified"}
    severities = {"critical", "high", "medium", "low", "informative"}
    applicability = {"applicable", "context_dependent", "not_applicable", "not_verified"}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        code = item.get("code")
        classification = item.get("classification")
        evidence = item.get("evidence")
        recommendation = item.get("recommendation")
        severity = item.get("severity")
        applies = item.get("applicability")
        impact = item.get("impact")
        audience = item.get("audience")
        rows.append({
            "code": code[:80] if isinstance(code, str) else "html_finding",
            "label": html_finding_label(code),
            "classification": classification if isinstance(classification, str) and classification in classifications else "unverified",
            "severity": severity if isinstance(severity, str) and severity in severities else "informative",
            "applicability": applies if isinstance(applies, str) and applies in applicability else "not_verified",
            "evidence": html_issue_message(code, evidence),
            "recommendation": recommendation if isinstance(recommendation, str) else "",
            "impact": impact if isinstance(impact, str) and impact.strip()
            else "O impacto não foi classificado nesta evidência histórica.",
            "audience": audience if isinstance(audience, str) and audience.strip()
            else "Não informado nesta evidência histórica.",
        })
    return tuple(rows)


def _audit_view(run: OjsSeoAuditRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    metrics = run.metrics_json if isinstance(run.metrics_json, dict) else {}
    collection = metrics.get("collection") if isinstance(metrics.get("collection"), dict) else {}
    coverage = metrics.get("coverage") if isinstance(metrics.get("coverage"), dict) else {}
    active_records = _nonnegative_int(collection.get("active_records"), run.record_count - run.deleted_record_count)
    return {
        "run": run,
        "status_label": OJS_AUDIT_STATUS_LABELS.get(run.status, "Estado não informado"),
        "collection": collection,
        "coverage": coverage,
        "active_records": max(0, active_records),
        "warnings": _safe_list(run.warnings_json),
        "errors": _safe_list(run.errors_json),
        "duration_seconds": _duration_seconds(run),
        "completion_reason_label": OJS_AUDIT_COMPLETION_REASON_LABELS.get(
            run.completion_reason, run.completion_reason,
        ),
    }


def _html_audit_view(run: OjsHtmlAuditRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    metrics = run.metrics_json if isinstance(run.metrics_json, dict) else {}
    parameters = run.parameters_json if isinstance(run.parameters_json, dict) else {}
    selection_mode = parameters.get("selection_mode")
    return {
        "run": run,
        "status_label": OJS_HTML_AUDIT_STATUS_LABELS.get(run.status, "Estado não informado"),
        "metrics": metrics,
        "selection_mode": selection_mode,
        "selection_label": HTML_SELECTION_LABELS.get(selection_mode, "Análise técnica"),
        # analyzed_page_count é o nome histórico do campo e inclui toda
        # tentativa persistida. Os indicadores novos distinguem tentativas de
        # páginas cujo HTML foi efetivamente recuperado.
        "processed_page_count": _nonnegative_int(metrics.get("records_processed"), run.analyzed_page_count),
        "verified_page_count": _nonnegative_int(
            metrics.get("verified_pages"), max(0, run.analyzed_page_count - run.unverified_page_count),
        ),
        "warnings": _safe_list(run.warnings_json),
        "errors": _safe_list(run.errors_json),
        "duration_seconds": _duration_seconds(run),
    }


def _html_campaign_view(campaign) -> dict[str, Any] | None:
    if campaign is None:
        return None
    return {
        "campaign": campaign,
        "status_label": HTML_CAMPAIGN_STATUS_LABELS.get(campaign.status, "Estado não informado"),
        "warnings": _safe_list(campaign.warnings_json),
        "errors": _safe_list(campaign.errors_json),
        "duration_seconds": _duration_seconds(campaign),
    }


def _campaign_matches_current_reference(campaign, current_reference_id: str | None) -> bool:
    if campaign is None or not current_reference_id:
        return False
    if campaign.source_oai_audit_run_id == current_reference_id:
        return True
    parameters = campaign.parameters_json if isinstance(campaign.parameters_json, dict) else {}
    return parameters.get("compatible_with_oai_audit_run_id") == current_reference_id


def _integrated_operation_view(operation) -> dict[str, Any] | None:
    if operation is None:
        return None
    return {
        "operation": operation,
        "status_label": OPERATION_STATUS_LABELS.get(operation.status, "Estado não informado"),
        "warnings": _safe_list(operation.warnings_json),
        "errors": _safe_list(operation.errors_json),
    }


_HISTORY_PROGRESS_ACTIVE_STATUSES = frozenset({
    "oai_pending", "oai_running", "html_pending", "html_running",
})


def _history_progress_state(
    *,
    active_operation,
    audits: list[OjsSeoAuditRun],
    html_audits: list[OjsHtmlAuditRun],
    active_campaign,
) -> dict[str, Any]:
    """Expõe apenas o estado persistido que justifica acompanhamento visual.

    A ordem é intencional: uma operação integrada descreve as duas etapas e
    prevalece sobre as execuções que a compõem. As opções avançadas continuam
    podendo criar uma execução isolada, por isso as filas OAI e HTML também
    são consideradas quando não há coordenador aberto.
    """
    if active_operation is not None and active_operation.status in _HISTORY_PROGRESS_ACTIVE_STATUSES:
        status = active_operation.status
        return {
            "active": True,
            "stage": status,
            "label": OPERATION_STATUS_LABELS.get(status, "Processamento em andamento"),
            "message": {
                "oai_pending": "A solicitação foi registrada e aguarda o worker dos dados bibliográficos.",
                "oai_running": "O worker está verificando os dados bibliográficos do periódico.",
                "html_pending": "Os dados bibliográficos foram registrados; a análise das páginas aguarda o worker HTML.",
                "html_running": "O worker HTML está analisando as páginas dos artigos em lotes seguros.",
            }.get(status, "Há uma análise em andamento."),
        }

    active_audit = next((run for run in audits if run.status in {"pending", "running"}), None)
    if active_audit is not None:
        status = active_audit.status
        return {
            "active": True,
            "stage": f"oai_{status}",
            "label": "Aguardando processamento" if status == "pending" else "Verificando dados bibliográficos",
            "message": "A análise dos dados bibliográficos aguarda o worker." if status == "pending"
            else "O worker está verificando os dados bibliográficos do periódico.",
        }

    if active_campaign is not None and active_campaign.status in {"pending", "running"}:
        status = active_campaign.status
        return {
            "active": True,
            "stage": f"html_{status}",
            "label": "Aguardando análise das páginas" if status == "pending" else "Analisando páginas dos artigos",
            "message": "A campanha está registrada e aguarda o worker HTML." if status == "pending"
            else "O worker HTML está analisando as páginas dos artigos em lotes seguros.",
        }

    active_html_audit = next((run for run in html_audits if run.status in {"pending", "running"}), None)
    if active_html_audit is not None:
        status = active_html_audit.status
        return {
            "active": True,
            "stage": f"html_{status}",
            "label": "Aguardando análise das páginas" if status == "pending" else "Analisando páginas dos artigos",
            "message": "A análise das páginas aguarda o worker HTML." if status == "pending"
            else "O worker HTML está analisando as páginas dos artigos.",
        }

    return {"active": False, "stage": None, "label": None, "message": None}


def _json_datetime(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    instant = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return instant.isoformat()


def _latest_progress_datetime(*values: datetime | None) -> datetime | None:
    candidates = [value for value in values if isinstance(value, datetime)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda value: value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc),
    )


def _history_progress_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    """Serializa o mesmo contexto de leitura usado por ``journal_history``.

    Não recalcula cobertura nem consulta fontes externas: o JSON e o HTML
    compartilham ``get_ojs_html_coverage`` por meio de ``_journal_page_context``.
    """
    operation_view = context.get("integrated_operation") or context.get("last_integrated_operation")
    campaign_view = context.get("html_campaign")
    if campaign_view is None:
        campaign_history = context.get("html_campaign_history") or ()
        campaign_view = campaign_history[0] if campaign_history else None
    audit_view = context.get("audit")
    html_audit_view = context.get("html_audit")
    coverage = context.get("html_coverage")

    operation = operation_view["operation"] if operation_view else None
    campaign = campaign_view["campaign"] if campaign_view else None
    audit = audit_view["run"] if audit_view else None
    html_audit = html_audit_view["run"] if html_audit_view else None
    has_pending = bool(coverage and (
        coverage.unverified_article_count > 0 or coverage.unprocessed_article_count > 0
    ))
    has_pending = has_pending or bool(campaign and campaign.status in {
        "completed_with_pending", "interrupted", "failed",
    })
    has_pending = has_pending or bool(operation and operation.status in {
        "completed_with_pending", "interrupted", "failed",
    })
    last_updated_at = _latest_progress_datetime(
        coverage.last_execution_at if coverage else None,
        operation.updated_at if operation else None,
        campaign.updated_at if campaign else None,
        campaign.last_progress_at if campaign else None,
        audit.completed_at if audit else None,
        audit.started_at if audit else None,
        html_audit.completed_at if html_audit else None,
        html_audit.started_at if html_audit else None,
    )

    return {
        "reference": {
            "oai_audit_run_id": coverage.source_oai_audit_run_id if coverage else None,
        },
        "monitoring": context["history_progress"],
        "audit": None if audit is None else {
            "id": audit.id,
            "status": audit.status,
            "status_label": OJS_AUDIT_STATUS_LABELS.get(audit.status, "Estado não informado"),
            "record_count": audit.record_count,
            "errors": _safe_list(audit.errors_json),
        },
        "operation": None if operation is None else {
            "id": operation.id,
            "status": operation.status,
            "status_label": OPERATION_STATUS_LABELS.get(operation.status, "Estado não informado"),
            "updated_at": _json_datetime(operation.updated_at),
        },
        "campaign": None if campaign is None else {
            "id": campaign.id,
            "status": campaign.status,
            "status_label": HTML_CAMPAIGN_STATUS_LABELS.get(campaign.status, "Estado não informado"),
            "batch_count": campaign.batch_count,
            "last_progress_at": _json_datetime(campaign.last_progress_at),
            "updated_at": _json_datetime(campaign.updated_at),
        },
        "coverage": None if coverage is None else {
            "eligible_article_count": coverage.eligible_article_count,
            "verified_article_count": coverage.verified_article_count,
            "reused_evidence_article_count": coverage.reused_evidence_article_count,
            "unverified_article_count": coverage.unverified_article_count,
            "unprocessed_article_count": coverage.unprocessed_article_count,
            "verified_percentage": coverage.verified_percentage,
            "available_coverage_percentage": coverage.available_coverage_percentage,
            "last_execution_at": _json_datetime(coverage.last_execution_at),
        },
        "has_pending": has_pending,
        "last_updated_at": _json_datetime(last_updated_at),
    }


def _coverage_rows(audit: dict[str, Any] | None) -> tuple[dict[str, Any], ...]:
    if audit is None:
        return ()
    total = audit["active_records"]
    coverage = audit["coverage"]
    rows: list[dict[str, Any]] = []
    for field, label in OJS_COVERAGE_FIELDS:
        count = min(total, _nonnegative_int(coverage.get(field), 0))
        rows.append({
            "field": field,
            "label": label,
            "count": count,
            "total": total,
            "percentage": round((count / total * 100) if total else 0),
        })
    return tuple(rows)


def _metrics_audit_view(audits: list[OjsSeoAuditRun]) -> tuple[dict[str, Any] | None, str | None]:
    """Seleciona dados de coleta sem confundir execução operacional e métricas.

    A execução mais recente continua sendo exibida como "Última auditoria".
    Para os indicadores, uma pendência, execução ou falha nunca substitui a
    última coleta concluída. Na ausência desta, uma coleta parcial é exibida
    apenas com o rótulo explícito correspondente.
    """
    completed = next((run for run in audits if run.status == "completed"), None)
    if completed is not None:
        return _audit_view(completed), "completed"
    partial = next((run for run in audits if run.status == "partial"), None)
    if partial is not None:
        return _audit_view(partial), "partial"
    return None, None


def _journal_page_context(journal: OjsJournal) -> dict[str, Any]:
    """Monta o contexto de leitura comum às quatro áreas do periódico.

    Esta função apenas organiza dados já persistidos. Ela não solicita OAI-PMH
    nem HTML e mantém a separação entre o estado operacional mais recente e a
    última coleta utilizável para indicadores.
    """
    archived = journal.analytics_project.status == "archived"
    audits = list_ojs_seo_audits(current_user, journal.id, include_inactive=True)
    latest_audit = _audit_view(audits[0] if audits else None)
    metrics_audit, metrics_source = _metrics_audit_view(audits)
    html_audits = list_ojs_html_audits(current_user, journal.id, include_inactive=True)
    html_campaigns = list_ojs_html_audit_campaigns(current_user, journal.id, include_inactive=True)
    try:
        integrated_operations = list_ojs_integrated_operations(current_user, journal.id, include_inactive=True)
        integrated_operations_available = True
    except OperationalError:
        # A migration da operação integrada é deliberadamente aplicada em uma
        # etapa administrativa separada. Enquanto ela não existe, o histórico
        # legado continua consultável e nenhuma rota nova tenta gravar dados.
        db.session.rollback()
        integrated_operations = []
        integrated_operations_available = False
    latest_operation = integrated_operations[0] if integrated_operations else None
    active_operation = next(
        (operation for operation in integrated_operations if operation.status in OPERATION_OPEN),
        None,
    )
    # Uma campanha antiga só pode permanecer ativa para a referência atual se
    # o coordenador tiver gravado explicitamente a compatibilidade após uma
    # reconciliação sem mudanças. Campanhas de outros universos ficam somente
    # no histórico.
    current_html_reference_id = metrics_audit["run"].id if metrics_audit is not None else None
    active_html_campaign = next(
        (
            campaign for campaign in html_campaigns
            if campaign.status in HTML_CAMPAIGN_OPEN
            and _campaign_matches_current_reference(campaign, current_html_reference_id)
        ),
        None,
    )
    try:
        coverage_reference = None
        if latest_operation is not None and isinstance(latest_operation.parameters_json, dict):
            value = latest_operation.parameters_json.get("html_coverage_reference_oai_audit_run_id")
            coverage_reference = value if isinstance(value, str) and value else None
        html_coverage = get_ojs_html_coverage(
            current_user, journal.id, source_oai_audit_run_id=coverage_reference, include_inactive=True,
        )
    except OjsSeoServiceError:
        # Sem referência bibliográfica concluída/parcial, não há universo seguro
        # para consolidar a cobertura HTML. O histórico continua consultável.
        html_coverage = None

    recommendation_summary: dict[str, int] | None = None
    if metrics_audit is not None:
        try:
            recommendations = list_ojs_recommendations(
                current_user,
                str(journal.id),
                audit_run_id=str(metrics_audit["run"].id),
                include_inactive=True,
            )
            recommendation_summary = {
                "problem_types": recommendations.indicators.problem_types,
                "high_priority": recommendations.indicators.high_priority_recommendations,
                "needs_review": recommendations.indicators.review_recommendations,
            }
        except OjsSeoServiceError:
            # A visão geral nunca inventa recomendações: em caso de uma
            # referência histórica inválida, a área específica mostrará o erro
            # protegido já usado pelo serviço.
            recommendation_summary = None

    article_count = int(db.session.scalar(
        select(func.count()).select_from(OjsArticle).where(OjsArticle.journal_id == journal.id)
    ) or 0)
    html_continue_available = not archived and can_request_ojs_html_audit(
        current_user, journal.id, selection_mode=HTML_SELECTION_CONTINUE,
    )
    html_retry_available = not archived and can_request_ojs_html_audit(
        current_user, journal.id, selection_mode=HTML_SELECTION_RETRY_UNVERIFIED,
    )
    html_revalidate_available = not archived and can_request_ojs_html_audit(
        current_user, journal.id, selection_mode=HTML_SELECTION_REVALIDATE,
    )
    html_campaign_request_available = not archived and can_request_ojs_html_audit_campaign(
        current_user, journal.id,
    )
    history_progress = _history_progress_state(
        active_operation=active_operation,
        audits=audits,
        html_audits=html_audits,
        active_campaign=active_html_campaign,
    )
    return {
        "audit": latest_audit,
        "metrics_audit": metrics_audit,
        "metrics_source": metrics_source,
        "coverage_rows": _coverage_rows(metrics_audit),
        "audits": audits,
        "article_count": article_count,
        "archived": archived,
        "audit_request_available": not archived and can_request_ojs_seo_audit(current_user, journal.id),
        "html_audit": _html_audit_view(html_audits[0]) if html_audits else None,
        "html_audits": html_audits,
        "html_audit_history": tuple(_html_audit_view(run) for run in html_audits),
        "html_campaign": _html_campaign_view(active_html_campaign),
        "html_campaign_history": tuple(_html_campaign_view(campaign) for campaign in html_campaigns),
        # Apenas uma operação aberta bloqueia o botão inteligente. A última
        # conclusão continua no histórico, mas não pode impedir uma futura
        # atualização sob demanda.
        "integrated_operation": _integrated_operation_view(active_operation),
        "last_integrated_operation": _integrated_operation_view(latest_operation),
        "integrated_operation_history": tuple(_integrated_operation_view(item) for item in integrated_operations),
        "integrated_operations_available": integrated_operations_available,
        "integrated_action_available": (
            integrated_operations_available and not archived
            and (latest_operation is None or latest_operation.status not in OPERATION_OPEN)
            and can_request_ojs_seo_audit(current_user, journal.id)
        ),
        "html_coverage": html_coverage,
        "html_continue_available": html_continue_available,
        "html_retry_available": html_retry_available,
        "html_revalidate_available": html_revalidate_available,
        "html_campaign_request_available": html_campaign_request_available,
        "history_progress": history_progress,
        "recommendations_available": metrics_audit is not None,
        "recommendation_summary": recommendation_summary,
    }


def _latest_html_analysis_for_reference(
    article_id: str, source_audit_id: str,
) -> tuple[OjsArticleHtmlAnalysis | None, bool]:
    """Obtém evidência direta ou um vínculo histórico explicitamente compatível."""
    current = db.session.scalar(
        select(OjsArticleHtmlAnalysis)
        .join(OjsHtmlAuditRun, OjsHtmlAuditRun.id == OjsArticleHtmlAnalysis.html_audit_run_id)
        .where(
            OjsArticleHtmlAnalysis.article_id == article_id,
            OjsHtmlAuditRun.source_oai_audit_run_id == source_audit_id,
        )
        .order_by(
            OjsArticleHtmlAnalysis.checked_at.desc(),
            OjsArticleHtmlAnalysis.created_at.desc(),
            OjsArticleHtmlAnalysis.id.desc(),
        )
        .limit(1)
    )
    if current is not None:
        return current, False
    if not html_evidence_reuse_available():
        return None, False
    reused = db.session.scalar(
        select(OjsArticleHtmlAnalysis)
        .join(OjsHtmlEvidenceReuse, OjsHtmlEvidenceReuse.source_html_analysis_id == OjsArticleHtmlAnalysis.id)
        .where(
            OjsHtmlEvidenceReuse.article_id == article_id,
            OjsHtmlEvidenceReuse.target_oai_audit_run_id == source_audit_id,
            OjsArticleHtmlAnalysis.status == "analyzed",
        )
        .order_by(
            OjsHtmlEvidenceReuse.reused_at.desc(), OjsArticleHtmlAnalysis.checked_at.desc(),
            OjsArticleHtmlAnalysis.id.desc(),
        )
        .limit(1)
    )
    return reused, reused is not None


def _nonnegative_int(value: object, fallback: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else max(0, fallback)


def _duration_seconds(run: OjsSeoAuditRun) -> int | None:
    if run.started_at is None:
        return None
    end = run.completed_at
    if end is None and run.status == "running":
        end = datetime.now(timezone.utc)
    if end is None:
        return None
    start = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
    finish = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
    return max(0, int((finish - start).total_seconds()))


def _journal_for_current_user(journal_id: UUID, *, include_inactive: bool = False) -> OjsJournal:
    try:
        return get_ojs_journal(current_user, str(journal_id), include_inactive=include_inactive)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")


def _subscriptions_for_form():
    try:
        return list_ojs_seo_subscriptions_for_actor(current_user)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")


def _subscription_usage_for_current_user():
    try:
        return list_ojs_seo_subscription_usage_for_actor(current_user)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")


def _subscription_display_labels(subscription_usage: list[Any]) -> dict[str, dict[str, str]]:
    """Cria rótulos compactos sem alterar a identificação persistida.

    O provisionamento local identifica workspaces de desenvolvimento com um
    prefixo canônico. A interface reconhece esse marcador estrutural, em vez
    de cortar texto por tamanho ou posição, e mantém o nome completo e o plano
    no tooltip acessível. Demais workspaces continuam com o nome escolhido.
    """
    labels: dict[str, dict[str, str]] = {}
    development_label = OJS_SEO_DEVELOPMENT_WORKSPACE_PREFIX.removesuffix(" — ")
    for usage in subscription_usage:
        subscription = usage.subscription
        workspace_name = subscription.workspace.name
        display_name = development_label if workspace_name.startswith(
            OJS_SEO_DEVELOPMENT_WORKSPACE_PREFIX
        ) else workspace_name
        full_name = f"{workspace_name} · {subscription.plan.name}"
        labels[subscription.id] = {"display": display_name, "full": full_name}
    return labels


@ojs_seo_bp.get("")
@login_required
def home():
    _require_tool()
    journals = list_ojs_journals(current_user)
    archived_journals = list_archived_ojs_journals(current_user)
    consolidated_dashboard = build_ojs_seo_consolidated_dashboard(
        current_user,
        active_journals=journals,
        archived_journals=archived_journals,
    )
    subscription_usage = _subscription_usage_for_current_user()
    return render_template(
        "platform/ojs_seo/index.html",
        consolidated_dashboard=consolidated_dashboard,
        subscription_usage=subscription_usage,
        subscription_display_labels=_subscription_display_labels(subscription_usage),
        audit_status_labels=OJS_AUDIT_STATUS_LABELS,
        discovery_status_labels=OJS_DISCOVERY_STATUS_LABELS,
    )


@ojs_seo_bp.get("/journals/archived")
@login_required
def archived_journals():
    """Lista somente os periódicos arquivados do escopo autorizado."""
    _require_tool()
    journals = list_archived_ojs_journals(current_user)
    pagination = paginate_sequence(journals, requested_page(request.args.get("page")))
    return render_template(
        "platform/ojs_seo/archived_journals.html",
        journals=pagination.items,
        journals_pagination=pagination,
        audit_status_labels=OJS_AUDIT_STATUS_LABELS,
        discovery_status_labels=OJS_DISCOVERY_STATUS_LABELS,
    )


@ojs_seo_bp.get("/journals/new")
@login_required
def new_journal():
    _require_tool()
    administrative_registration = has_administrative_ojs_context(current_user)
    subscriptions = _subscriptions_for_form()
    subscription_usage = _subscription_usage_for_current_user()
    return render_template(
        "platform/ojs_seo/journal_new.html",
        subscriptions=subscriptions,
        administrative_registration=administrative_registration,
        quota_exhausted=any(
            usage.contracted_limit > 0 and usage.available_slots == 0
            for usage in subscription_usage
        ) and not subscriptions and not administrative_registration,
    )


@ojs_seo_bp.post("/journals")
@login_required
def create_journal():
    _require_tool()
    subscription_id = request.form.get("workspace_subscription_id", "").strip()
    registration_options: dict[str, Any] = {}
    client_factory = current_app.extensions.get("ojs_seo_client_factory")
    endpoint_validator = current_app.extensions.get("ojs_seo_endpoint_validator")
    if client_factory is not None:
        registration_options["client_factory"] = client_factory
    if endpoint_validator is not None:
        registration_options["endpoint_validator"] = endpoint_validator
    try:
        journal = register_ojs_journal(
            current_user,
            workspace_subscription_id=subscription_id,
            public_url=request.form.get("public_url", ""),
            project_name=request.form.get("project_name", ""),
            project_description="",
            **registration_options,
        )
    except OjsSeoServiceError as exc:
        db.session.rollback()
        if isinstance(exc, OjsSeoAuthorizationError):
            abort(404)
        flash(exc.safe_message, "danger")
        return redirect(url_for("ojs_seo.new_journal"))
    return redirect(url_for("ojs_seo.journal_detail", journal_id=journal.id))


@ojs_seo_bp.route("/journals/<uuid:journal_id>/archive", methods=("GET", "POST"))
@login_required
def archive_journal(journal_id: UUID):
    """Arquiva sem apagar dados e sem liberar a reserva comercial."""
    _require_tool()
    journal = _journal_for_current_user(journal_id)
    if request.method == "GET":
        return render_template(
            "platform/ojs_seo/journal_archive.html",
            project=journal.analytics_project,
            journal=journal,
        )
    if request.form.get("confirm") != "yes":
        abort(400)
    try:
        archive_ojs_seo_project(current_user, journal.id)
    except (OjsSeoAuthorizationError, OjsSeoNotFoundError):
        db.session.rollback()
        abort(404)
    except OjsSeoServiceError as error:
        db.session.rollback()
        flash(error.safe_message, "danger")
        return redirect(url_for("ojs_seo.home", _anchor="ojs-seo-consolidated-title"))
    except Exception:
        current_app.logger.exception("Falha ao arquivar periódico OJS SEO")
        db.session.rollback()
        flash("Não foi possível arquivar o periódico. Nenhum dado foi alterado.", "danger")
        return redirect(url_for("ojs_seo.home", _anchor="ojs-seo-consolidated-title"))
    flash("Periódico arquivado. O histórico foi preservado e a vaga comercial continua ocupada.", "success")
    return redirect(url_for("ojs_seo.home", _anchor="ojs-seo-consolidated-title"))


@ojs_seo_bp.post("/journals/<uuid:journal_id>/restore")
@login_required
def restore_journal(journal_id: UUID):
    """Restaura um periódico arquivado quando a mesma assinatura ainda é válida."""
    _require_tool()
    journal = _journal_for_current_user(journal_id, include_inactive=True)
    project = journal.analytics_project
    if project.status != "archived" or project.archived_at is None:
        abort(404)
    if request.form.get("confirm") != "yes":
        abort(400)
    try:
        restore_ojs_seo_project(current_user, journal.id)
    except (OjsSeoAuthorizationError, OjsSeoNotFoundError):
        db.session.rollback()
        abort(404)
    except OjsSeoServiceError as error:
        db.session.rollback()
        flash(error.safe_message, "danger")
        return redirect(url_for("ojs_seo.archived_journals", _anchor="ojs-seo-archived-journal-list"))
    except Exception:
        current_app.logger.exception("Falha ao restaurar periódico OJS SEO")
        db.session.rollback()
        flash("Não foi possível restaurar o periódico. Nenhum dado foi alterado.", "danger")
        return redirect(url_for("ojs_seo.archived_journals", _anchor="ojs-seo-archived-journal-list"))
    flash("Periódico restaurado.", "success")
    return redirect(url_for("ojs_seo.archived_journals", _anchor="ojs-seo-archived-journal-list"))


@ojs_seo_bp.route("/journals/<uuid:journal_id>/delete", methods=("GET", "POST"))
@login_required
def delete_journal(journal_id: UUID):
    """Confirma e executa a exclusão permanente de um periódico arquivado."""
    _require_tool()
    journal = _journal_for_current_user(journal_id, include_inactive=True)
    project = journal.analytics_project
    if project.status != "archived" or project.archived_at is None:
        abort(400)
    if request.method == "POST":
        try:
            delete_ojs_seo_project_permanently(
                current_user, journal.id, request.form.get("confirmation", ""),
            )
        except (OjsSeoAuthorizationError, OjsSeoNotFoundError):
            db.session.rollback()
            abort(404)
        except OjsSeoProjectDeletionError as error:
            db.session.rollback()
            flash(error.safe_message, "danger")
            return render_template(
                "platform/ojs_seo/journal_delete.html",
                project=project,
                journal=journal,
            ), 400
        except Exception:
            current_app.logger.exception("Falha ao excluir periódico OJS SEO permanentemente")
            db.session.rollback()
            flash("Não foi possível excluir o periódico permanentemente. Nenhum dado foi removido.", "danger")
            return render_template(
                "platform/ojs_seo/journal_delete.html",
                project=project,
                journal=journal,
            ), 500
        flash("Periódico excluído permanentemente. A vaga comercial foi liberada.", "success")
        return redirect(url_for("ojs_seo.archived_journals", _anchor="ojs-seo-archived-journal-list"))
    return render_template(
        "platform/ojs_seo/journal_delete.html",
        project=project,
        journal=journal,
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>")
@login_required
def journal_detail(journal_id: UUID):
    _require_tool()
    journal = _journal_for_current_user(journal_id, include_inactive=True)
    dashboard = build_ojs_seo_dashboard(current_user, str(journal.id), include_inactive=True)
    return render_template(
        "platform/ojs_seo/journal_detail.html",
        project=journal.analytics_project,
        journal=journal,
        audit_status_labels=OJS_AUDIT_STATUS_LABELS,
        discovery_status_labels=OJS_DISCOVERY_STATUS_LABELS,
        html_audit_status_labels=OJS_HTML_AUDIT_STATUS_LABELS,
        html_campaign_status_labels=HTML_CAMPAIGN_STATUS_LABELS,
        operation_status_labels=OPERATION_STATUS_LABELS,
        role_labels=ROLE_LABELS,
        dashboard=dashboard,
        **_journal_page_context(journal),
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/history")
@login_required
def journal_history(journal_id: UUID):
    """Concentra solicitações e histórico sem mudar os relatórios legados."""
    _require_tool()
    journal = _journal_for_current_user(journal_id, include_inactive=True)
    return render_template(
        "platform/ojs_seo/journal_history.html",
        project=journal.analytics_project,
        journal=journal,
        audit_status_labels=OJS_AUDIT_STATUS_LABELS,
        html_audit_status_labels=OJS_HTML_AUDIT_STATUS_LABELS,
        **_journal_page_context(journal),
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/history/progress")
@login_required
def journal_history_progress(journal_id: UUID):
    """Retorna somente progresso persistido, sem iniciar coleta ou worker."""
    _require_tool()
    journal = _journal_for_current_user(journal_id, include_inactive=True)
    payload = jsonify(_history_progress_payload(_journal_page_context(journal)))
    payload.headers["Cache-Control"] = "no-store"
    return payload


@ojs_seo_bp.get("/journals/<uuid:journal_id>/exports/<string:dataset>")
@login_required
def export_results(journal_id: UUID, dataset: str):
    """Entrega resultados persistidos sem iniciar análises ou modificar dados."""
    _require_tool()
    # A consulta antecipada aplica a mesma autorização da navegação e evita
    # que um parâmetro de exportação seja a primeira operação no periódico.
    _journal_for_current_user(journal_id, include_inactive=True)
    try:
        bundle = build_ojs_seo_export(
            current_user, str(journal_id), dataset=dataset, filters=request.args,
        )
        exported = render_ojs_seo_export(bundle, export_format=request.args.get("format", "xlsx"))
    except OjsSeoExportError:
        abort(400)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    response = send_file(
        BytesIO(exported.content),
        mimetype=exported.mimetype,
        as_attachment=True,
        download_name=exported.download_name,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@ojs_seo_bp.post("/journals/<uuid:journal_id>/integrated-analyses")
@login_required
def create_integrated_analysis(journal_id: UUID):
    """Registra uma análise coordenada; a requisição nunca inicia HTTP."""
    _require_tool()
    journal = _journal_for_current_user(journal_id)
    try:
        operation = request_ojs_integrated_analysis(current_user, journal.id)
        db.session.commit()
    except OjsSeoServiceError as exc:
        db.session.rollback()
        if isinstance(exc, OjsSeoAuthorizationError):
            abort(404)
        if _wants_json_response():
            return jsonify({"message": exc.safe_message}), 409
        flash(exc.safe_message, "danger")
        return redirect(url_for("ojs_seo.journal_history", journal_id=journal.id))
    except OperationalError:
        db.session.rollback()
        if _wants_json_response():
            return jsonify({"message": "A operação integrada ainda requer a migration OJS correspondente neste ambiente."}), 503
        flash("A operação integrada ainda requer a migration OJS correspondente neste ambiente.", "danger")
        return redirect(url_for("ojs_seo.journal_history", journal_id=journal.id))
    if _wants_json_response():
        return _accepted_bibliographic_analysis_response(
            journal,
            audit_run_id=str(operation.oai_audit_run_id),
            operation_id=str(operation.id),
        )
    label = "Análise do periódico" if operation.mode == "initial" else "Atualização dos dados"
    flash(f"{label} registrada. O worker bibliográfico processará a primeira etapa; a análise das páginas será liberada automaticamente quando aplicável.", "success")
    return redirect(url_for("ojs_seo.journal_history", journal_id=journal.id, _anchor="ojs-integrated-operation"))


@ojs_seo_bp.post("/journals/<uuid:journal_id>/audits")
@login_required
def create_audit(journal_id: UUID):
    _require_tool()
    journal = _journal_for_current_user(journal_id)
    try:
        run = request_ojs_seo_audit(current_user, journal.id)
        db.session.commit()
    except OjsSeoServiceError as exc:
        db.session.rollback()
        if isinstance(exc, OjsSeoAuthorizationError):
            abort(404)
        if _wants_json_response():
            return jsonify({"message": exc.safe_message}), 409
        flash(exc.safe_message, "danger")
        return redirect(url_for("ojs_seo.journal_detail", journal_id=journal.id))
    if _wants_json_response():
        return _accepted_bibliographic_analysis_response(journal, audit_run_id=str(run.id))
    flash("Auditoria OAI-PMH/Dublin Core solicitada. Ela será processada pelo worker OJS quando estiver em execução.", "success")
    return redirect(url_for("ojs_seo.audit_detail", journal_id=journal.id, audit_id=run.id))


@ojs_seo_bp.post("/journals/<uuid:journal_id>/technical-audits")
@login_required
def create_html_audit(journal_id: UUID):
    """Enfileira a análise HTML; a requisição Flask jamais executa HTTP."""
    _require_tool()
    journal = _journal_for_current_user(journal_id)
    try:
        run = request_ojs_html_audit(
            current_user,
            journal.id,
            selection_mode=request.form.get("selection_mode", HTML_SELECTION_CONTINUE),
        )
        db.session.commit()
    except OjsSeoServiceError as exc:
        db.session.rollback()
        if isinstance(exc, OjsSeoAuthorizationError):
            abort(404)
        flash(exc.safe_message, "danger")
        return redirect(url_for("ojs_seo.journal_detail", journal_id=journal.id, _anchor="ojs-seo-html-title"))
    flash("Análise SEO técnico solicitada. Ela será processada pelo worker HTML OJS em execução separada.", "success")
    return redirect(url_for("ojs_seo.html_audit_detail", journal_id=journal.id, audit_id=run.id))


@ojs_seo_bp.post("/journals/<uuid:journal_id>/technical-campaigns")
@login_required
def create_html_audit_campaign(journal_id: UUID):
    """Registra uma cobertura HTML completa; o worker executa os lotes."""
    _require_tool()
    journal = _journal_for_current_user(journal_id)
    try:
        campaign = request_ojs_html_audit_campaign(current_user, journal.id)
        db.session.commit()
    except OjsSeoServiceError as exc:
        db.session.rollback()
        if isinstance(exc, OjsSeoAuthorizationError):
            abort(404)
        flash(exc.safe_message, "danger")
        return redirect(url_for("ojs_seo.journal_history", journal_id=journal.id))
    flash(
        "Análise completa das páginas registrada. Um worker HTML ativo executará os lotes sucessivos dentro dos limites de segurança.",
        "success",
    )
    return redirect(url_for("ojs_seo.journal_history", journal_id=journal.id, _anchor="ojs-html-campaign-status"))


@ojs_seo_bp.post("/journals/<uuid:journal_id>/technical-campaigns/<uuid:campaign_id>/resume")
@login_required
def resume_html_audit_campaign(journal_id: UUID, campaign_id: UUID):
    """Retoma somente uma campanha interrompida e ainda compatível com a fonte."""
    _require_tool()
    journal = _journal_for_current_user(journal_id)
    try:
        resume_ojs_html_audit_campaign(current_user, journal.id, str(campaign_id))
        db.session.commit()
    except OjsSeoServiceError as exc:
        db.session.rollback()
        if isinstance(exc, OjsSeoAuthorizationError):
            abort(404)
        flash(exc.safe_message, "danger")
        return redirect(url_for("ojs_seo.journal_history", journal_id=journal.id))
    flash("A análise completa foi recolocada na fila. O worker HTML retomará o próximo lote elegível.", "success")
    return redirect(url_for("ojs_seo.journal_history", journal_id=journal.id, _anchor="ojs-html-campaign-status"))


@ojs_seo_bp.route("/journals/<uuid:journal_id>/audits/<uuid:audit_id>/cancel", methods=("GET", "POST"))
@login_required
def cancel_audit(journal_id: UUID, audit_id: UUID):
    """Confirma e cancela somente uma auditoria que ainda esteja pendente."""
    _require_tool()
    journal = _journal_for_current_user(journal_id, include_inactive=True)
    if journal.analytics_project.status == "archived":
        # A área de periódicos arquivados é histórica e somente de leitura.
        # O arquivamento normal já recusa auditorias abertas; esta proteção
        # também cobre registros residuais criados por uma corrida anterior.
        abort(404)
    try:
        run = get_ojs_seo_audit_result(current_user, str(audit_id), include_inactive=True)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    if run.journal_id != journal.id:
        abort(404)
    if request.method == "POST":
        try:
            cancel_pending_ojs_seo_audit(current_user, run.id)
        except (OjsSeoAuthorizationError, OjsSeoNotFoundError):
            db.session.rollback()
            abort(404)
        except OjsSeoAuditCancellationError as error:
            db.session.rollback()
            flash(error.safe_message, "danger")
            return redirect(url_for("ojs_seo.journal_detail", journal_id=journal.id, _anchor="ojs-seo-history-title"))
        except OjsSeoServiceError as error:
            db.session.rollback()
            flash(error.safe_message, "danger")
            return redirect(url_for("ojs_seo.journal_detail", journal_id=journal.id, _anchor="ojs-seo-history-title"))
        flash("Auditoria pendente cancelada.", "success")
        return redirect(url_for("ojs_seo.journal_detail", journal_id=journal.id, _anchor="ojs-seo-history-title"))
    if run.status != "pending":
        abort(404)
    return render_template(
        "platform/ojs_seo/audit_cancel.html",
        project=journal.analytics_project,
        journal=journal,
        audit=_audit_view(run),
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/audits/<uuid:audit_id>")
@login_required
def audit_detail(journal_id: UUID, audit_id: UUID):
    _require_tool()
    journal = _journal_for_current_user(journal_id, include_inactive=True)
    try:
        run = get_ojs_seo_audit_result(current_user, str(audit_id), include_inactive=True)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    if run.journal_id != journal.id:
        abort(404)
    audit = _audit_view(run)
    return render_template(
        "platform/ojs_seo/audit_detail.html",
        project=journal.analytics_project,
        journal=journal,
        audit=audit,
        coverage_rows=_coverage_rows(audit),
        audit_status_labels=OJS_AUDIT_STATUS_LABELS,
        archived=journal.analytics_project.status == "archived",
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/audits/<uuid:audit_id>/status")
@login_required
def audit_status(journal_id: UUID, audit_id: UUID):
    """Consulta protegida de estado; a página não inicia polling automático."""
    _require_tool()
    journal = _journal_for_current_user(journal_id, include_inactive=True)
    try:
        run = get_ojs_seo_audit_result(current_user, str(audit_id), include_inactive=True)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    if run.journal_id != journal.id:
        abort(404)
    return jsonify({
        "id": run.id,
        "status": run.status,
        "status_label": OJS_AUDIT_STATUS_LABELS.get(run.status, "Estado não informado"),
        "completed": run.status in {"completed", "partial", "failed", "canceled"},
    })


@ojs_seo_bp.get("/journals/<uuid:journal_id>/technical-audits/<uuid:audit_id>")
@login_required
def html_audit_detail(journal_id: UUID, audit_id: UUID):
    _require_tool()
    journal = _journal_for_current_user(journal_id, include_inactive=True)
    try:
        run = get_ojs_html_audit(current_user, journal.id, str(audit_id), include_inactive=True)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    return render_template(
        "platform/ojs_seo/html_audit_detail.html",
        project=journal.analytics_project,
        journal=journal,
        audit=_html_audit_view(run),
        audit_status_labels=OJS_HTML_AUDIT_STATUS_LABELS,
        archived=journal.analytics_project.status == "archived",
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/technical-audits/<uuid:audit_id>/records")
@login_required
def html_audit_records(journal_id: UUID, audit_id: UUID):
    _require_tool()
    try:
        listing = list_ojs_html_analysis_pages(
            current_user, str(journal_id), str(audit_id),
            page=requested_page(request.args.get("page")), selected_filter=request.args.get("filter", "all"),
            search=request.args.get("q", ""), include_inactive=True,
        )
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    return render_template(
        "platform/ojs_seo/html_records.html",
        project=listing.journal.analytics_project,
        journal=listing.journal,
        audit=_html_audit_view(listing.audit_run),
        pages=listing.pages.items,
        pages_pagination=listing.pages,
        selected_filter=listing.selected_filter,
        search_query=listing.search,
        html_filter_labels=HTML_FILTER_LABELS,
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/technical-audits/<uuid:audit_id>/records/<uuid:analysis_id>")
@login_required
def html_analysis_detail(journal_id: UUID, audit_id: UUID, analysis_id: UUID):
    _require_tool()
    try:
        page = get_ojs_html_analysis_page(
            current_user, str(journal_id), str(audit_id), str(analysis_id), include_inactive=True,
        )
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    return render_template(
        "platform/ojs_seo/html_analysis_detail.html",
        project=page.article.journal.analytics_project,
        journal=page.article.journal,
        audit=_html_audit_view(page.analysis.html_audit_run),
        page=page,
        display_findings=_html_findings_for_display(page.analysis.findings_json),
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/recommendations")
@login_required
def recommendations(journal_id: UUID):
    """Exibe recomendações derivadas, sem executar nova coleta HTTP."""
    _require_tool()
    try:
        listing = list_ojs_recommendations(
            current_user, str(journal_id), page=requested_page(request.args.get("page")),
            priority=request.args.get("priority", "all"), severity=request.args.get("severity", "all"),
            certainty=request.args.get("certainty", "all"), nature=request.args.get("nature", "all"),
            role=request.args.get("role", "all"), audit_run_id=request.args.get("audit"),
            include_inactive=True,
        )
    except OjsRecommendationFilterError:
        abort(400)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    return render_template(
        "platform/ojs_seo/recommendations.html",
        project=listing.journal.analytics_project,
        journal=listing.journal,
        diagnostic_reference=listing.reference,
        recommendations=listing.recommendations.items,
        recommendations_pagination=listing.recommendations,
        indicators=listing.indicators,
        priority_labels=PRIORITY_LABELS,
        severity_labels=SEVERITY_LABELS,
        certainty_labels=CERTAINTY_LABELS,
        applicability_labels=APPLICABILITY_LABELS,
        nature_labels=NATURE_LABELS,
        role_labels=ROLE_LABELS,
        selected_priority=listing.selected_priority,
        selected_severity=listing.selected_severity,
        selected_certainty=listing.selected_certainty,
        selected_nature=listing.selected_nature,
        selected_role=listing.selected_role,
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/recommendations/<uuid:source_audit_id>/<string:recommendation_code>")
@login_required
def recommendation_detail(journal_id: UUID, source_audit_id: UUID, recommendation_code: str):
    """Detalha uma recomendação somente no universo OAI-PMH que a fundamenta."""
    _require_tool()
    try:
        detail = get_ojs_recommendation_detail(
            current_user, str(journal_id), str(source_audit_id), recommendation_code,
            page=requested_page(request.args.get("page")), include_inactive=True,
        )
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    return render_template(
        "platform/ojs_seo/recommendation_detail.html",
        project=detail.journal.analytics_project,
        journal=detail.journal,
        diagnostic_reference=detail.reference,
        recommendation=detail.recommendation,
        articles=detail.articles.items,
        articles_pagination=detail.articles,
        priority_labels=PRIORITY_LABELS,
        severity_labels=SEVERITY_LABELS,
        certainty_labels=CERTAINTY_LABELS,
        applicability_labels=APPLICABILITY_LABELS,
        nature_labels=NATURE_LABELS,
        role_labels=ROLE_LABELS,
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/articles")
@login_required
def articles(journal_id: UUID):
    _require_tool()
    try:
        listing = list_ojs_article_diagnostics(
            current_user,
            str(journal_id),
            page=requested_page(request.args.get("page")),
            selected_filter=request.args.get("filter", "all"),
            search=request.args.get("q", ""),
            record_state=request.args.get("state", "active"),
            audit_run_id=request.args.get("audit"),
            include_inactive=True,
        )
    except OjsSeoDiagnosticFilterError:
        abort(400)
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    return render_template(
        "platform/ojs_seo/articles.html",
        project=listing.journal.analytics_project,
        journal=listing.journal,
        articles=listing.articles.items,
        articles_pagination=listing.articles,
        diagnostic_reference=listing.reference,
        diagnostic_summary=listing.summary,
        diagnostic_filter_labels=FILTER_LABELS,
        selected_filter=listing.selected_filter,
        search_query=listing.search,
        record_state=listing.record_state,
    )


@ojs_seo_bp.get("/journals/<uuid:journal_id>/articles/<uuid:article_id>")
@login_required
def article_detail(journal_id: UUID, article_id: UUID):
    _require_tool()
    try:
        diagnostic = get_ojs_article_diagnostic(
            current_user,
            str(journal_id),
            str(article_id),
            audit_run_id=request.args.get("audit"),
            include_inactive=True,
        )
        observations = list_ojs_article_observations(
            current_user, diagnostic.reference.journal.id, diagnostic.article.id, include_inactive=True,
        )
    except OjsSeoServiceError as exc:
        _not_found_from_service(exc)
        raise AssertionError("unreachable")
    html_analysis, html_evidence_reused = _latest_html_analysis_for_reference(
        diagnostic.article.id, diagnostic.reference.audit_run.id,
    )
    return render_template(
        "platform/ojs_seo/article_detail.html",
        project=diagnostic.reference.journal.analytics_project,
        journal=diagnostic.reference.journal,
        article=diagnostic.article,
        diagnostic=diagnostic,
        recommendations=editorial_recommendations(diagnostic),
        observations=observations,
        html_analysis=html_analysis,
        html_evidence_reused=html_evidence_reused,
        diagnostic_status_labels=OJS_DIAGNOSTIC_STATUS_LABELS,
    )


__all__ = ["ojs_seo_bp"]
