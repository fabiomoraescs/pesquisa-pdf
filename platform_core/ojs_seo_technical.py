"""Serviços e persistência do diagnóstico técnico HTML do OJS SEO.

As funções não executam HTTP. A solicitação fixa uma auditoria OAI concluída ou
parcial como fonte; o executor próprio depois busca somente a amostra já
selecionada e confirma todos os resultados em uma transação curta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Literal, Sequence
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import String, func, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import (
    OjsArticle,
    OjsArticleHtmlAnalysis,
    OjsArticleObservation,
    OjsHtmlAuditCampaign,
    OjsHtmlAuditRun,
    OjsHtmlEvidenceReuse,
    OjsJournal,
    OjsSeoAuditRun,
    User,
    utcnow,
)
from .ojs_html_client import HtmlAnalysisResult
from .ojs_seo_diagnostics import resolve_ojs_diagnostic_reference
from .ojs_seo_services import (
    OjsSeoNotFoundError,
    OjsSeoServiceError,
    get_ojs_journal,
    journal_has_active_ojs_execution_authorization,
    require_ojs_seo_access,
)
from .pagination import PAGE_SIZE, PlatformPage
from .services import record_audit


HTML_AUDIT_OPEN = frozenset({"pending", "running"})
HTML_AUDIT_FINAL = frozenset({"completed", "partial", "failed", "canceled"})
HTML_AUDIT_MAX_PAGES = 100
HTML_AUDIT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
HTML_AUDIT_MAX_ATTEMPTS_PER_ARTICLE = 2
HTML_SELECTION_CONTINUE = "continue"
HTML_SELECTION_RETRY_UNVERIFIED = "retry_unverified"
HTML_SELECTION_REVALIDATE = "revalidate"
HTML_SELECTION_FULL_CAMPAIGN = "full_campaign"
HTML_SELECTION_LABELS = {
    HTML_SELECTION_CONTINUE: "Continuar análise",
    HTML_SELECTION_RETRY_UNVERIFIED: "Retentar páginas não verificadas",
    HTML_SELECTION_REVALIDATE: "Revalidar páginas",
}
HTML_EVIDENCE_REUSE_CRITERIA_VERSION = "ojs-html-evidence-reuse-v1"
HTML_EVIDENCE_REUSE_REASON = "stable_article_metadata_and_url_match"
HTML_CAMPAIGN_OPEN = frozenset({"pending", "running", "interrupted"})
HTML_CAMPAIGN_FINAL = frozenset({"completed", "completed_with_pending", "failed", "canceled"})
HTML_CAMPAIGN_STATUS_LABELS = {
    "pending": "Aguardando processamento",
    "running": "Em andamento",
    "completed": "Concluída",
    "completed_with_pending": "Concluída com pendências",
    "interrupted": "Interrompida ou aguardando retomada",
    "failed": "Falhou",
    "canceled": "Cancelada",
}
# Códigos que representam uma rejeição de segurança, uma URL inadequada ou um
# conteúdo que não corresponde a uma página HTML. Eles não entram em uma nova
# tentativa automática da mesma campanha OAI-PMH.
HTML_TERMINAL_UNVERIFIED_CODES = frozenset({
    "invalid_html_url", "https_required", "embedded_credentials", "invalid_html_port",
    "https_port_required", "ip_literal_not_allowed", "invalid_host", "invalid_html_path",
    "non_public_dns_address", "empty_dns_answer", "redirect_loop", "redirect_without_location",
    "redirect_limit_reached", "compressed_response_not_allowed", "invalid_content_length",
    "response_too_large", "tls_certificate_failed", "tls_failed", "unexpected_content_type",
    "invalid_html",
})
HTML_FILTER_LABELS = {
    "all": "Todos os registros analisados",
    "confirmed": "Com problema confirmado",
    "potential": "Com problema potencial",
    "unverified": "Não verificados",
}


def html_evidence_reuse_available() -> bool:
    """Indica se a migration aditiva de reaproveitamento já está aplicada.

    A aplicação pode receber o código antes do upgrade local. Nesse intervalo,
    a cobertura conserva a semântica anterior em vez de consultar uma tabela
    inexistente. A verificação não é cacheada para enxergar um upgrade Alembic
    feito durante a execução do processo.
    """
    try:
        return inspect(db.session.get_bind()).has_table("ojs_html_evidence_reuses")
    except Exception:  # pragma: no cover - bind indisponível na inicialização
        return False


@dataclass(frozen=True, slots=True)
class HtmlAuditCandidate:
    article: OjsArticle
    observation: OjsArticleObservation
    source_url: str


@dataclass(frozen=True, slots=True)
class HtmlAnalysisPage:
    article: OjsArticle
    observation: OjsArticleObservation
    analysis: OjsArticleHtmlAnalysis


@dataclass(frozen=True, slots=True)
class HtmlAnalysisListing:
    journal: OjsJournal
    audit_run: OjsHtmlAuditRun
    pages: PlatformPage[HtmlAnalysisPage]
    selected_filter: str
    search: str


@dataclass(frozen=True, slots=True)
class HtmlCoverage:
    """Cobertura acumulada de uma única referência OAI-PMH."""

    source_oai_audit_run_id: str
    eligible_article_count: int
    excluded_article_count: int
    processed_article_count: int
    verified_article_count: int
    reused_evidence_article_count: int
    unverified_article_count: int
    unprocessed_article_count: int
    retryable_unverified_count: int
    exhausted_unverified_count: int
    last_execution_at: datetime | None

    @property
    def verified_percentage(self) -> int:
        return round(self.verified_article_count / self.eligible_article_count * 100) if self.eligible_article_count else 0

    @property
    def available_coverage_percentage(self) -> int:
        """Cobertura disponível, sem alegar uma nova requisição HTTP."""
        if not self.eligible_article_count:
            return 0
        return round(
            (self.verified_article_count + self.reused_evidence_article_count)
            / self.eligible_article_count * 100
        )


@dataclass(frozen=True, slots=True)
class _HtmlCoverageSelection:
    coverage: HtmlCoverage
    unprocessed: tuple[HtmlAuditCandidate, ...]
    retryable_unverified: tuple[HtmlAuditCandidate, ...]
    revalidation: tuple[HtmlAuditCandidate, ...]


def _normalized_search(value: object) -> str:
    return " ".join(value.split())[:200] if isinstance(value, str) else ""


def _validated_filter(value: object) -> str:
    candidate = value if isinstance(value, str) else "all"
    if candidate not in HTML_FILTER_LABELS:
        raise OjsSeoServiceError("invalid_html_diagnostic_filter", "O filtro de SEO técnico é inválido.")
    return candidate


def _source_reference(actor: User, journal_id: str):
    reference = resolve_ojs_diagnostic_reference(actor, journal_id)
    if reference is None:
        raise OjsSeoServiceError(
            "ojs_html_source_unavailable",
            "Uma auditoria OAI-PMH concluída ou parcial é necessária antes da análise técnica HTML.",
        )
    return reference


def _validated_selection_mode(value: object) -> str:
    mode = value if isinstance(value, str) else HTML_SELECTION_CONTINUE
    if mode not in HTML_SELECTION_LABELS:
        raise OjsSeoServiceError("invalid_html_selection_mode", "A ação de cobertura HTML é inválida.")
    return mode


def request_ojs_html_audit(
    actor: User,
    journal_id: str,
    *,
    selection_mode: object = HTML_SELECTION_CONTINUE,
) -> OjsHtmlAuditRun:
    """Cria uma execução pendente, sem rede e sem iniciar worker.

    A campanha é sempre vinculada à referência OAI-PMH atual. ``continue``
    seleciona somente artigos ainda não processados; ``retry_unverified``
    seleciona somente falhas não terminais que ainda não consumiram o limite
    explícito de tentativas; ``revalidate`` é a ação deliberada que ignora
    vínculos históricos compatíveis e pede uma nova consulta HTML.
    """
    mode = _validated_selection_mode(selection_mode)
    require_ojs_seo_access(actor)
    journal = get_ojs_journal(actor, journal_id)
    if not journal_has_active_ojs_execution_authorization(journal):
        raise OjsSeoServiceError(
            "ojs_subscription_ineligible", "O periódico não possui autorização ativa para uma nova análise OJS."
        )
    reference = _source_reference(actor, journal.id)
    if db.session.scalar(select(OjsSeoAuditRun.id).where(
        OjsSeoAuditRun.journal_id == journal.id,
        OjsSeoAuditRun.status.in_(("pending", "running")),
    ).limit(1)) is not None:
        raise OjsSeoServiceError(
            "ojs_oai_audit_open", "Aguarde a auditoria bibliográfica pendente ou em processamento antes da análise técnica."
        )
    if db.session.scalar(select(OjsHtmlAuditRun.id).where(
        OjsHtmlAuditRun.journal_id == journal.id,
        OjsHtmlAuditRun.status.in_(HTML_AUDIT_OPEN),
    ).limit(1)) is not None:
        raise OjsSeoServiceError(
            "ojs_html_audit_open", "Já existe uma análise técnica HTML pendente ou em processamento para este periódico."
        )
    if db.session.scalar(select(OjsHtmlAuditCampaign.id).where(
        OjsHtmlAuditCampaign.journal_id == journal.id,
        OjsHtmlAuditCampaign.status.in_(HTML_CAMPAIGN_OPEN),
    ).limit(1)) is not None:
        raise OjsSeoServiceError(
            "ojs_html_campaign_open", "Há uma análise completa ativa ou interrompida para este periódico."
        )
    force_revalidation = mode == HTML_SELECTION_REVALIDATE
    selection = _coverage_selection(
        journal, reference.audit_run.id, include_reused=not force_revalidation,
    )
    candidates = (
        selection.revalidation if mode == HTML_SELECTION_REVALIDATE
        else selection.unprocessed if mode == HTML_SELECTION_CONTINUE
        else selection.retryable_unverified
    )
    if not candidates:
        messages = {
            HTML_SELECTION_CONTINUE: "Não há artigos ainda não processados para esta referência bibliográfica.",
            HTML_SELECTION_RETRY_UNVERIFIED: "Não há páginas não verificadas elegíveis para uma nova tentativa nesta referência bibliográfica.",
            HTML_SELECTION_REVALIDATE: "Não há páginas elegíveis para uma nova verificação nesta referência bibliográfica.",
        }
        message = messages[mode]
        raise OjsSeoServiceError("ojs_html_no_candidates", message)
    run = OjsHtmlAuditRun(
        journal_id=journal.id,
        source_oai_audit_run_id=reference.audit_run.id,
        status="pending",
        parameters_json={
            "selection": "oai_identifier_ascending",
            "selection_mode": mode,
            "force_revalidation": force_revalidation,
            "max_pages": HTML_AUDIT_MAX_PAGES,
            "max_attempts_per_article": HTML_AUDIT_MAX_ATTEMPTS_PER_ARTICLE,
            "source_oai_audit_run_id": reference.audit_run.id,
            "coverage_before": {
                "eligible": selection.coverage.eligible_article_count,
                "verified": selection.coverage.verified_article_count,
                "reused": selection.coverage.reused_evidence_article_count,
                "unverified": selection.coverage.unverified_article_count,
                "unprocessed": selection.coverage.unprocessed_article_count,
            },
        },
    )
    db.session.add(run)
    try:
        db.session.flush()
    except IntegrityError as exc:
        # O índice parcial protege contra duas solicitações concorrentes entre
        # processos. O chamador mantém a responsabilidade pelo rollback.
        raise OjsSeoServiceError(
            "ojs_html_audit_open", "Já existe uma análise técnica HTML pendente ou em processamento para este periódico."
        ) from exc
    record_audit(actor, "ojs_seo_html_audit_requested", "ojs_html_audit_run", run.id, {}, {
        "journal_id": journal.id,
        "source_oai_audit_run_id": reference.audit_run.id,
        "selection_mode": mode,
        "candidate_count": len(candidates),
        "status": run.status,
    })
    return run


def can_request_ojs_html_audit(
    actor: User,
    journal_id: str,
    *,
    selection_mode: object = HTML_SELECTION_CONTINUE,
) -> bool:
    """Conveniência de interface; a rota sempre revalida no POST."""
    try:
        mode = _validated_selection_mode(selection_mode)
        journal = get_ojs_journal(actor, journal_id)
        if not journal_has_active_ojs_execution_authorization(journal):
            return False
        reference = _source_reference(actor, journal.id)
        force_revalidation = mode == HTML_SELECTION_REVALIDATE
        selection = _coverage_selection(
            journal, reference.audit_run.id, include_reused=not force_revalidation,
        )
    except OjsSeoServiceError:
        return False
    candidates = (
        selection.revalidation if mode == HTML_SELECTION_REVALIDATE
        else selection.unprocessed if mode == HTML_SELECTION_CONTINUE
        else selection.retryable_unverified
    )
    if not candidates:
        return False
    if db.session.scalar(select(OjsHtmlAuditCampaign.id).where(
        OjsHtmlAuditCampaign.journal_id == journal.id,
        OjsHtmlAuditCampaign.status.in_(HTML_CAMPAIGN_OPEN),
    ).limit(1)) is not None:
        return False
    return not any(
        db.session.scalar(select(model.id).where(
            model.journal_id == journal.id, model.status.in_(statuses),
        ).limit(1)) is not None
        for model, statuses in ((OjsSeoAuditRun, ("pending", "running")), (OjsHtmlAuditRun, HTML_AUDIT_OPEN))
    )


def request_ojs_html_audit_campaign(actor: User, journal_id: str) -> OjsHtmlAuditCampaign:
    """Registra uma campanha integral e seu primeiro lote, sem executar HTTP.

    O registro da campanha e do lote pendente ocorre na mesma transação do
    chamador. Um worker HTML ativo encadeia os lotes seguintes depois de cada
    persistência confirmada; a rota Flask apenas registra esta intenção.
    """
    require_ojs_seo_access(actor)
    journal = get_ojs_journal(actor, journal_id)
    if not journal_has_active_ojs_execution_authorization(journal):
        raise OjsSeoServiceError(
            "ojs_subscription_ineligible", "O periódico não possui autorização ativa para uma nova análise OJS."
        )
    reference = _source_reference(actor, journal.id)
    if db.session.scalar(select(OjsSeoAuditRun.id).where(
        OjsSeoAuditRun.journal_id == journal.id,
        OjsSeoAuditRun.status.in_(("pending", "running")),
    ).limit(1)) is not None:
        raise OjsSeoServiceError(
            "ojs_oai_audit_open", "Aguarde a auditoria bibliográfica pendente ou em processamento antes da análise técnica."
        )
    existing_campaign = _open_html_campaign(journal.id, reference.audit_run.id)
    if existing_campaign is not None:
        if existing_campaign.status == "interrupted":
            raise OjsSeoServiceError(
                "ojs_html_campaign_interrupted",
                "Há uma análise completa interrompida para esta referência. Retome-a quando o worker estiver disponível.",
            )
        raise OjsSeoServiceError(
            "ojs_html_campaign_open", "Já existe uma análise completa aguardando ou em processamento para esta referência."
        )
    if db.session.scalar(select(OjsHtmlAuditRun.id).where(
        OjsHtmlAuditRun.journal_id == journal.id,
        OjsHtmlAuditRun.status.in_(HTML_AUDIT_OPEN),
    ).limit(1)) is not None:
        raise OjsSeoServiceError(
            "ojs_html_audit_open", "Já existe uma análise técnica HTML pendente ou em processamento para este periódico."
        )
    # A campanha normal continua somente o que ainda precisa de trabalho nesta
    # referência. Evidências reaproveitadas contam como cobertura disponível e
    # não devem ser consultadas outra vez por padrão. A revalidação deliberada
    # usa ``request_ojs_html_audit(..., selection_mode="revalidate")``.
    selection = _coverage_selection(journal, reference.audit_run.id)
    if not selection.unprocessed and not selection.retryable_unverified:
        raise OjsSeoServiceError(
            "ojs_html_no_candidates", "Não há páginas elegíveis para uma nova análise completa nesta referência bibliográfica."
        )
    campaign = OjsHtmlAuditCampaign(
        journal_id=journal.id,
        source_oai_audit_run_id=reference.audit_run.id,
        status="pending",
        parameters_json={
            "selection": "oai_identifier_ascending",
            "selection_mode": HTML_SELECTION_FULL_CAMPAIGN,
            "max_pages_per_batch": HTML_AUDIT_MAX_PAGES,
            "max_attempts_per_article": HTML_AUDIT_MAX_ATTEMPTS_PER_ARTICLE,
            "source_oai_audit_run_id": reference.audit_run.id,
        },
    )
    db.session.add(campaign)
    try:
        db.session.flush()
        _enqueue_campaign_batch(campaign, journal, selection)
    except IntegrityError as exc:
        raise OjsSeoServiceError(
            "ojs_html_campaign_open", "Já existe uma análise completa aguardando ou em processamento para este periódico."
        ) from exc
    record_audit(actor, "ojs_seo_html_campaign_requested", "ojs_html_audit_campaign", campaign.id, {}, {
        "journal_id": journal.id,
        "source_oai_audit_run_id": reference.audit_run.id,
        "status": campaign.status,
        "batch_count": campaign.batch_count,
    })
    return campaign


def can_request_ojs_html_audit_campaign(actor: User, journal_id: str) -> bool:
    """Indica se uma nova campanha integral pode ser registrada com segurança."""
    try:
        journal = get_ojs_journal(actor, journal_id)
        if not journal_has_active_ojs_execution_authorization(journal):
            return False
        reference = _source_reference(actor, journal.id)
        if db.session.scalar(select(OjsSeoAuditRun.id).where(
            OjsSeoAuditRun.journal_id == journal.id,
            OjsSeoAuditRun.status.in_(("pending", "running")),
        ).limit(1)) is not None:
            return False
        if _open_html_campaign(journal.id, reference.audit_run.id) is not None:
            return False
        if db.session.scalar(select(OjsHtmlAuditRun.id).where(
            OjsHtmlAuditRun.journal_id == journal.id,
            OjsHtmlAuditRun.status.in_(HTML_AUDIT_OPEN),
        ).limit(1)) is not None:
            return False
        selection = _coverage_selection(journal, reference.audit_run.id)
        return bool(selection.unprocessed or selection.retryable_unverified)
    except OjsSeoServiceError:
        return False


def start_ojs_html_campaign_for_reference(
    journal: OjsJournal,
    source_oai_audit_run_id: str,
    *,
    compatible_with_oai_audit_run_id: str | None = None,
    force_revalidation: bool = False,
    requested_by: str = "integrated_operation",
) -> OjsHtmlAuditCampaign | None:
    """Cria o primeiro lote da campanha dentro da transação de um coordenador.

    Não faz HTTP e não revalida um usuário web: o chamador já deve ter
    confirmado a autorização antes de registrar a operação durável. Retorna
    ``None`` quando a referência não oferece páginas elegíveis.
    """
    source = db.session.scalar(select(OjsSeoAuditRun).where(
        OjsSeoAuditRun.id == source_oai_audit_run_id,
        OjsSeoAuditRun.journal_id == journal.id,
        OjsSeoAuditRun.status.in_(("completed", "partial")),
    ))
    if source is None:
        raise OjsSeoServiceError("invalid_html_audit_source", "A referência bibliográfica não está disponível para análise das páginas.")
    if compatible_with_oai_audit_run_id is not None:
        compatible = db.session.scalar(select(OjsSeoAuditRun.id).where(
            OjsSeoAuditRun.id == compatible_with_oai_audit_run_id,
            OjsSeoAuditRun.journal_id == journal.id,
            OjsSeoAuditRun.status.in_(("completed", "partial")),
        ))
        if compatible is None:
            raise OjsSeoServiceError(
                "invalid_html_compatible_reference",
                "A referência bibliográfica compatível não está disponível para a continuidade HTML.",
            )
    if not journal_has_active_ojs_execution_authorization(journal):
        raise OjsSeoServiceError("ojs_subscription_ineligible", "O periódico não possui autorização ativa para uma análise OJS.")
    if _open_html_campaign(journal.id, source.id) is not None:
        raise OjsSeoServiceError("ojs_html_campaign_open", "Já existe uma análise completa para esta referência bibliográfica.")
    if db.session.scalar(select(OjsHtmlAuditRun.id).where(
        OjsHtmlAuditRun.journal_id == journal.id,
        OjsHtmlAuditRun.status.in_(HTML_AUDIT_OPEN),
    ).limit(1)) is not None:
        raise OjsSeoServiceError("ojs_html_audit_open", "Já existe uma análise das páginas pendente ou em processamento.")
    selection = _coverage_selection(journal, source.id, include_reused=not force_revalidation)
    if not selection.unprocessed and not selection.retryable_unverified:
        return None
    campaign = OjsHtmlAuditCampaign(
        journal_id=journal.id,
        source_oai_audit_run_id=source.id,
        status="pending",
        parameters_json={
            "selection": "oai_identifier_ascending",
            "selection_mode": HTML_SELECTION_FULL_CAMPAIGN,
            "max_pages_per_batch": HTML_AUDIT_MAX_PAGES,
            "max_attempts_per_article": HTML_AUDIT_MAX_ATTEMPTS_PER_ARTICLE,
            "source_oai_audit_run_id": source.id,
            "compatible_with_oai_audit_run_id": compatible_with_oai_audit_run_id,
            "force_revalidation": force_revalidation,
            "requested_by": requested_by,
        },
    )
    db.session.add(campaign)
    db.session.flush()
    _enqueue_campaign_batch(campaign, journal, selection, force_revalidation=force_revalidation)
    return campaign


def resume_ojs_html_audit_campaign(actor: User, journal_id: str, campaign_id: str) -> OjsHtmlAuditCampaign:
    """Recoloca uma campanha interrompida na fila sem recriar seu histórico."""
    require_ojs_seo_access(actor)
    journal = get_ojs_journal(actor, journal_id)
    if not journal_has_active_ojs_execution_authorization(journal):
        raise OjsSeoServiceError(
            "ojs_subscription_ineligible", "O periódico não possui autorização ativa para retomar a análise OJS."
        )
    campaign = _get_html_campaign_for_journal(journal.id, campaign_id)
    if campaign.status != "interrupted":
        raise OjsSeoServiceError(
            "ojs_html_campaign_not_resumable", "Esta análise completa não está em condição segura para retomada."
        )
    reference = _source_reference(actor, journal.id)
    if not _campaign_accepts_current_reference(campaign, reference.audit_run.id):
        raise OjsSeoServiceError(
            "ojs_html_campaign_reference_changed",
            "A referência bibliográfica mudou. Inicie uma nova análise completa para o novo conjunto de artigos.",
        )
    result = db.session.execute(update(OjsHtmlAuditCampaign).where(
        OjsHtmlAuditCampaign.id == campaign.id,
        OjsHtmlAuditCampaign.journal_id == journal.id,
        OjsHtmlAuditCampaign.status == "interrupted",
    ).values(status="pending", completion_reason=None, completed_at=None, updated_at=utcnow()))
    if result.rowcount != 1:
        raise OjsSeoServiceError(
            "ojs_html_campaign_busy", "A análise completa foi alterada por outra operação. Atualize a página e tente novamente."
        )
    campaign = db.session.get(OjsHtmlAuditCampaign, campaign.id)
    if db.session.scalar(select(OjsHtmlAuditRun.id).where(
        OjsHtmlAuditRun.journal_id == journal.id,
        OjsHtmlAuditRun.status.in_(HTML_AUDIT_OPEN),
    ).limit(1)) is not None:
        raise OjsSeoServiceError(
            "ojs_html_audit_open", "Já existe uma análise técnica HTML pendente ou em processamento para este periódico."
        )
    force_revalidation = _campaign_forces_revalidation(campaign)
    selection = _coverage_selection(
        journal, campaign.source_oai_audit_run_id, include_reused=not force_revalidation,
    )
    if not selection.unprocessed and not selection.retryable_unverified:
        _finalize_campaign(campaign, selection.coverage, utcnow())
    else:
        _enqueue_campaign_batch(campaign, journal, selection, force_revalidation=force_revalidation)
    record_audit(actor, "ojs_seo_html_campaign_resumed", "ojs_html_audit_campaign", campaign.id, {}, {
        "journal_id": journal.id,
        "source_oai_audit_run_id": campaign.source_oai_audit_run_id,
        "status": campaign.status,
        "batch_count": campaign.batch_count,
    })
    return campaign


def list_ojs_html_audit_campaigns(
    actor: User, journal_id: str, *, include_inactive: bool = False,
) -> list[OjsHtmlAuditCampaign]:
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    return list(db.session.scalars(select(OjsHtmlAuditCampaign).where(
        OjsHtmlAuditCampaign.journal_id == journal.id,
    ).order_by(OjsHtmlAuditCampaign.created_at.desc(), OjsHtmlAuditCampaign.id.desc())))


def get_ojs_html_audit_campaign(
    actor: User, journal_id: str, campaign_id: str, *, include_inactive: bool = False,
) -> OjsHtmlAuditCampaign:
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    return _get_html_campaign_for_journal(journal.id, campaign_id)


def _get_html_campaign_for_journal(journal_id: str, campaign_id: str) -> OjsHtmlAuditCampaign:
    if not isinstance(campaign_id, str) or not campaign_id:
        raise OjsSeoNotFoundError("ojs_html_campaign_not_found", "A análise completa não foi encontrada.")
    campaign = db.session.scalar(select(OjsHtmlAuditCampaign).where(
        OjsHtmlAuditCampaign.id == campaign_id,
        OjsHtmlAuditCampaign.journal_id == journal_id,
    ))
    if campaign is None:
        raise OjsSeoNotFoundError("ojs_html_campaign_not_found", "A análise completa não foi encontrada.")
    return campaign


def _open_html_campaign(journal_id: str, source_oai_audit_run_id: str) -> OjsHtmlAuditCampaign | None:
    return db.session.scalar(select(OjsHtmlAuditCampaign).where(
        OjsHtmlAuditCampaign.journal_id == journal_id,
        OjsHtmlAuditCampaign.source_oai_audit_run_id == source_oai_audit_run_id,
        OjsHtmlAuditCampaign.status.in_(HTML_CAMPAIGN_OPEN),
    ).order_by(OjsHtmlAuditCampaign.created_at.desc(), OjsHtmlAuditCampaign.id.desc()).limit(1))


def _batch_parameters(campaign: OjsHtmlAuditCampaign, selection: _HtmlCoverageSelection, mode: str) -> dict:
    return {
        "selection": "oai_identifier_ascending",
        "selection_mode": mode,
        "campaign_id": campaign.id,
        "max_pages": HTML_AUDIT_MAX_PAGES,
        "max_attempts_per_article": HTML_AUDIT_MAX_ATTEMPTS_PER_ARTICLE,
        "source_oai_audit_run_id": campaign.source_oai_audit_run_id,
        "coverage_before": {
            "eligible": selection.coverage.eligible_article_count,
            "verified": selection.coverage.verified_article_count,
            "reused": selection.coverage.reused_evidence_article_count,
            "unverified": selection.coverage.unverified_article_count,
            "unprocessed": selection.coverage.unprocessed_article_count,
        },
    }


def _enqueue_campaign_batch(
    campaign: OjsHtmlAuditCampaign,
    journal: OjsJournal,
    selection: _HtmlCoverageSelection,
    *,
    force_revalidation: bool = False,
) -> OjsHtmlAuditRun:
    mode = (
        HTML_SELECTION_REVALIDATE
        if force_revalidation and selection.unprocessed
        else HTML_SELECTION_CONTINUE if selection.unprocessed else HTML_SELECTION_RETRY_UNVERIFIED
    )
    candidates = (
        selection.unprocessed
        if mode in {HTML_SELECTION_CONTINUE, HTML_SELECTION_REVALIDATE}
        else selection.retryable_unverified
    )
    if not candidates:
        raise OjsSeoServiceError("ojs_html_no_candidates", "Não há páginas elegíveis para o próximo lote da análise completa.")
    run = OjsHtmlAuditRun(
        journal_id=journal.id,
        source_oai_audit_run_id=campaign.source_oai_audit_run_id,
        campaign_id=campaign.id,
        status="pending",
        parameters_json=_batch_parameters(campaign, selection, mode),
    )
    campaign.status = "pending"
    campaign.updated_at = utcnow()
    campaign.completed_at = None
    campaign.batch_count += 1
    db.session.add(run)
    db.session.flush()
    return run


def _current_oai_reference_id(journal_id: str) -> str | None:
    completed = db.session.scalar(select(OjsSeoAuditRun.id).where(
        OjsSeoAuditRun.journal_id == journal_id,
        OjsSeoAuditRun.status == "completed",
    ).order_by(
        OjsSeoAuditRun.completed_at.is_(None).asc(), OjsSeoAuditRun.completed_at.desc(),
        OjsSeoAuditRun.created_at.desc(), OjsSeoAuditRun.id.desc(),
    ).limit(1))
    if completed is not None:
        return completed
    return db.session.scalar(select(OjsSeoAuditRun.id).where(
        OjsSeoAuditRun.journal_id == journal_id,
        OjsSeoAuditRun.status == "partial",
    ).order_by(
        OjsSeoAuditRun.completed_at.is_(None).asc(), OjsSeoAuditRun.completed_at.desc(),
        OjsSeoAuditRun.created_at.desc(), OjsSeoAuditRun.id.desc(),
    ).limit(1))


def _campaign_parameters(campaign: OjsHtmlAuditCampaign) -> dict:
    return campaign.parameters_json if isinstance(campaign.parameters_json, dict) else {}


def _campaign_forces_revalidation(campaign: OjsHtmlAuditCampaign) -> bool:
    return _campaign_parameters(campaign).get("force_revalidation") is True


def _campaign_accepts_current_reference(campaign: OjsHtmlAuditCampaign, current_reference_id: str | None) -> bool:
    """Permite somente a continuidade histórica explicitamente vinculada.

    Uma campanha normal ainda exige a sua própria referência. A exceção ocorre
    quando o coordenador comprovou que a nova coleta não mudou os registros e
    gravou a referência atual compatível no próprio registro da campanha.
    """
    if current_reference_id == campaign.source_oai_audit_run_id:
        return True
    compatible = _campaign_parameters(campaign).get("compatible_with_oai_audit_run_id")
    return isinstance(compatible, str) and compatible == current_reference_id


def _append_campaign_issue(campaign: OjsHtmlAuditCampaign, code: str, message: str) -> None:
    errors = list(campaign.errors_json) if isinstance(campaign.errors_json, list) else []
    errors.append({"code": _safe_code(code), "message": " ".join(message.split())[:500]})
    campaign.errors_json = errors


def _finalize_campaign(campaign: OjsHtmlAuditCampaign, coverage: HtmlCoverage, completed_at: datetime) -> None:
    all_available = (
        coverage.verified_article_count + coverage.reused_evidence_article_count
        == coverage.eligible_article_count
    )
    campaign.status = "completed" if all_available else "completed_with_pending"
    campaign.completion_reason = (
        "all_eligible_pages_verified"
        if campaign.status == "completed" and not coverage.reused_evidence_article_count
        else "all_eligible_pages_covered_with_reused_evidence"
        if campaign.status == "completed"
        else "no_more_eligible_attempts"
    )
    campaign.completed_at = completed_at
    campaign.updated_at = completed_at


def advance_ojs_html_audit_campaign(journal: OjsJournal, run: OjsHtmlAuditRun, *, completed_at: datetime | None = None) -> OjsHtmlAuditCampaign | None:
    """Confirma o próximo lote ou encerra a campanha junto do lote final.

    Deve ser chamada na mesma transação que persiste o ``run`` final. Não faz
    rede e não cria uma repetição quando um lote não obteve qualquer resultado.
    """
    if not run.campaign_id:
        return None
    campaign = db.session.get(OjsHtmlAuditCampaign, run.campaign_id)
    if campaign is None or campaign.journal_id != journal.id:
        raise OjsSeoServiceError("invalid_html_campaign_scope", "A campanha HTML não pertence ao periódico informado.")
    instant = completed_at or utcnow()
    if campaign.status not in {"pending", "running"}:
        return campaign
    if not _campaign_accepts_current_reference(campaign, _current_oai_reference_id(journal.id)):
        campaign.status = "interrupted"
        campaign.completion_reason = "source_reference_changed"
        campaign.completed_at = instant
        campaign.updated_at = instant
        _append_campaign_issue(campaign, "source_reference_changed", "A referência bibliográfica mudou antes do próximo lote.")
        return campaign
    force_revalidation = _campaign_forces_revalidation(campaign)
    selection = _coverage_selection(
        journal, campaign.source_oai_audit_run_id, include_reused=not force_revalidation,
    )
    if run.analyzed_page_count <= 0:
        campaign.status = "interrupted"
        campaign.completion_reason = "no_progress_batch"
        campaign.completed_at = instant
        campaign.updated_at = instant
        campaign.no_progress_batch_count += 1
        _append_campaign_issue(campaign, "no_progress_batch", "O último lote não iniciou uma página com segurança; a retomada requer nova solicitação.")
        return campaign
    campaign.last_progress_at = instant
    campaign.updated_at = instant
    campaign.no_progress_batch_count = 0
    if selection.unprocessed or selection.retryable_unverified:
        _enqueue_campaign_batch(campaign, journal, selection, force_revalidation=force_revalidation)
    else:
        _finalize_campaign(campaign, selection.coverage, instant)
    return campaign


def interrupt_ojs_html_audit_campaign(run: OjsHtmlAuditRun, code: str, message: str, *, completed_at: datetime | None = None) -> OjsHtmlAuditCampaign | None:
    """Preserva uma campanha quando o lote falha fora da transação de resultados."""
    if not run.campaign_id:
        return None
    campaign = db.session.get(OjsHtmlAuditCampaign, run.campaign_id)
    if campaign is None or campaign.status not in {"pending", "running"}:
        return campaign
    instant = completed_at or utcnow()
    campaign.status = "interrupted"
    campaign.completion_reason = _safe_code(code)
    campaign.completed_at = instant
    campaign.updated_at = instant
    _append_campaign_issue(campaign, code, message)
    return campaign


def get_ojs_html_audit(actor: User, journal_id: str, audit_run_id: str, *, include_inactive: bool = False) -> OjsHtmlAuditRun:
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    if not isinstance(audit_run_id, str) or not audit_run_id:
        raise OjsSeoNotFoundError("ojs_html_audit_not_found", "A análise técnica HTML não foi encontrada.")
    run = db.session.scalar(select(OjsHtmlAuditRun).where(
        OjsHtmlAuditRun.id == audit_run_id, OjsHtmlAuditRun.journal_id == journal.id,
    ))
    if run is None:
        raise OjsSeoNotFoundError("ojs_html_audit_not_found", "A análise técnica HTML não foi encontrada.")
    return run


def list_ojs_html_audits(actor: User, journal_id: str, *, include_inactive: bool = False) -> list[OjsHtmlAuditRun]:
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    return list(db.session.scalars(select(OjsHtmlAuditRun).where(
        OjsHtmlAuditRun.journal_id == journal.id,
    ).order_by(OjsHtmlAuditRun.created_at.desc(), OjsHtmlAuditRun.id.desc())))


def get_ojs_html_coverage(
    actor: User,
    journal_id: str,
    *,
    source_oai_audit_run_id: str | None = None,
    include_inactive: bool = False,
) -> HtmlCoverage:
    """Retorna cobertura acumulada sem misturar referências OAI-PMH."""
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    if source_oai_audit_run_id is None:
        source_oai_audit_run_id = _source_reference(actor, journal.id).audit_run.id
    return _coverage_selection(journal, source_oai_audit_run_id).coverage


def get_html_coverage_for_journal_reference(journal: OjsJournal, source_oai_audit_run_id: str) -> HtmlCoverage:
    """Leitura interna, sem autorização web, para encadear workers duráveis."""
    return _coverage_selection(journal, source_oai_audit_run_id).coverage


def list_html_audit_candidates(journal: OjsJournal, run: OjsHtmlAuditRun) -> tuple[int, tuple[HtmlAuditCandidate, ...]]:
    """Seleciona até cem páginas sem repetir cobertura já verificada."""
    if run.journal_id != journal.id or run.source_oai_audit_run_id is None:
        raise OjsSeoServiceError("invalid_html_audit_scope", "A análise técnica não pertence ao periódico informado.")
    mode = _validated_selection_mode(
        run.parameters_json.get("selection_mode") if isinstance(run.parameters_json, dict) else HTML_SELECTION_CONTINUE
    )
    selection = _coverage_selection(
        journal, run.source_oai_audit_run_id,
        include_reused=mode != HTML_SELECTION_REVALIDATE,
    )
    candidates = (
        selection.revalidation if mode == HTML_SELECTION_REVALIDATE
        else selection.unprocessed if mode == HTML_SELECTION_CONTINUE
        else selection.retryable_unverified
    )
    return selection.coverage.eligible_article_count, candidates[:HTML_AUDIT_MAX_PAGES]


def _coverage_selection(
    journal: OjsJournal,
    source_oai_audit_run_id: str,
    *,
    include_reused: bool = True,
) -> _HtmlCoverageSelection:
    """Monta a campanha por referência, em ordem estável e sem N+1.

    A leitura do histórico direto é limitada ao periódico e à mesma referência
    OAI. Reutilizações entre referências aparecem apenas por vínculo explícito
    e rastreável; elas nunca são transformadas em nova análise HTTP.
    """
    source = db.session.scalar(select(OjsSeoAuditRun).where(
        OjsSeoAuditRun.id == source_oai_audit_run_id,
        OjsSeoAuditRun.journal_id == journal.id,
        OjsSeoAuditRun.status.in_(("completed", "partial")),
    ))
    if source is None:
        raise OjsSeoServiceError(
            "invalid_html_audit_source", "A análise técnica não possui uma auditoria bibliográfica de referência válida."
        )
    rows = db.session.execute(select(OjsArticle, OjsArticleObservation).join(
        OjsArticleObservation, OjsArticleObservation.article_id == OjsArticle.id,
    ).where(
        OjsArticle.journal_id == journal.id,
        OjsArticleObservation.journal_id == journal.id,
        OjsArticleObservation.audit_run_id == source.id,
        OjsArticleObservation.record_status == "active",
    ).order_by(OjsArticle.oai_identifier.asc(), OjsArticle.id.asc())).all()
    history_rows = db.session.scalars(select(OjsArticleHtmlAnalysis).join(
        OjsHtmlAuditRun, OjsHtmlAuditRun.id == OjsArticleHtmlAnalysis.html_audit_run_id,
    ).where(
        OjsArticleHtmlAnalysis.journal_id == journal.id,
        OjsHtmlAuditRun.journal_id == journal.id,
        OjsHtmlAuditRun.source_oai_audit_run_id == source.id,
    ).order_by(
        OjsArticleHtmlAnalysis.checked_at.asc(), OjsArticleHtmlAnalysis.id.asc(),
    )).all()
    history: dict[str, list[OjsArticleHtmlAnalysis]] = {}
    for analysis in history_rows:
        history.setdefault(analysis.article_id, []).append(analysis)

    reuse_by_article: dict[str, OjsHtmlEvidenceReuse] = {}
    if include_reused and html_evidence_reuse_available():
        reuse_rows = db.session.execute(select(
            OjsHtmlEvidenceReuse, OjsArticleHtmlAnalysis,
        ).join(
            OjsArticleHtmlAnalysis,
            OjsArticleHtmlAnalysis.id == OjsHtmlEvidenceReuse.source_html_analysis_id,
        ).where(
            OjsHtmlEvidenceReuse.journal_id == journal.id,
            OjsHtmlEvidenceReuse.target_oai_audit_run_id == source.id,
            OjsArticleHtmlAnalysis.journal_id == journal.id,
            OjsArticleHtmlAnalysis.status == "analyzed",
        )).all()
        for reuse, analysis in reuse_rows:
            # A unicidade protege a tabela; as verificações adicionais evitam
            # que dados antigos ou importados atravessem o isolamento lógico.
            if (
                reuse.article_id not in reuse_by_article
                and reuse.source_html_audit_run_id == analysis.html_audit_run_id
            ):
                reuse_by_article[reuse.article_id] = reuse

    unprocessed: list[HtmlAuditCandidate] = []
    retryable: list[HtmlAuditCandidate] = []
    revalidation: list[HtmlAuditCandidate] = []
    eligible = excluded = processed = verified = reused = unverified = exhausted = 0
    for article, observation in rows:
        source_url = _public_url_from_observation(observation)
        if source_url is None:
            excluded += 1
            continue
        eligible += 1
        candidate = HtmlAuditCandidate(article, observation, source_url)
        # A revalidação é uma ação deliberada e pode selecionar uma página
        # já analisada. Ela nunca é usada pela continuidade automática, que
        # continua se baseando exclusivamente em ``unprocessed`` e
        # ``retryable``.
        revalidation.append(candidate)
        attempts = [item for item in history.get(article.id, ()) if item.status in {"analyzed", "not_verified"}]
        if not attempts:
            reuse = reuse_by_article.get(article.id)
            if reuse is not None and reuse.target_observation_id == observation.id:
                reused += 1
                continue
            unprocessed.append(candidate)
            continue
        processed += 1
        if any(item.status == "analyzed" for item in attempts):
            verified += 1
            continue
        unverified += 1
        if len(attempts) >= HTML_AUDIT_MAX_ATTEMPTS_PER_ARTICLE or _has_terminal_unverified_result(attempts):
            exhausted += 1
        else:
            retryable.append(candidate)

    last_execution_at = db.session.scalar(select(func.max(func.coalesce(
        OjsHtmlAuditRun.completed_at, OjsHtmlAuditRun.started_at, OjsHtmlAuditRun.created_at,
    ))).where(
        OjsHtmlAuditRun.journal_id == journal.id,
        OjsHtmlAuditRun.source_oai_audit_run_id == source.id,
        OjsHtmlAuditRun.status.in_(HTML_AUDIT_FINAL),
        OjsHtmlAuditRun.analyzed_page_count > 0,
    ))
    coverage = HtmlCoverage(
        source.id,
        eligible,
        excluded,
        processed,
        verified,
        reused,
        unverified,
        len(unprocessed),
        len(retryable),
        exhausted,
        last_execution_at,
    )
    return _HtmlCoverageSelection(
        coverage,
        tuple(unprocessed),
        tuple(retryable),
        tuple(revalidation),
    )


def reuse_compatible_html_evidence(
    journal: OjsJournal,
    *,
    source_oai_audit_run_id: str,
    target_oai_audit_run_id: str,
) -> int:
    """Vincula evidências HTML compatíveis, sem copiar nem reclassificar dados.

    A identidade precisa ser demonstrada pela mesma entidade ``OjsArticle``,
    pelo mesmo ``metadata_hash``, por observações ativas e por URL pública
    normalizada idêntica. O resultado histórico continua referenciado por seu
    próprio audit run e timestamp, portanto não é uma verificação nova da
    referência de destino.
    """
    if source_oai_audit_run_id == target_oai_audit_run_id or not html_evidence_reuse_available():
        return 0
    sources = {
        run.id: run
        for run in db.session.scalars(select(OjsSeoAuditRun).where(
            OjsSeoAuditRun.journal_id == journal.id,
            OjsSeoAuditRun.id.in_((source_oai_audit_run_id, target_oai_audit_run_id)),
            OjsSeoAuditRun.status.in_(("completed", "partial")),
        )).all()
    }
    if set(sources) != {source_oai_audit_run_id, target_oai_audit_run_id}:
        raise OjsSeoServiceError(
            "invalid_html_evidence_reuse_scope",
            "As referências bibliográficas necessárias para reaproveitar evidências não estão disponíveis.",
        )

    previous = {
        observation.article_id: observation
        for observation in db.session.scalars(select(OjsArticleObservation).where(
            OjsArticleObservation.journal_id == journal.id,
            OjsArticleObservation.audit_run_id == source_oai_audit_run_id,
            OjsArticleObservation.record_status == "active",
        )).all()
    }
    current = list(db.session.scalars(select(OjsArticleObservation).where(
        OjsArticleObservation.journal_id == journal.id,
        OjsArticleObservation.audit_run_id == target_oai_audit_run_id,
        OjsArticleObservation.record_status == "active",
    )))
    existing = set(db.session.scalars(select(OjsHtmlEvidenceReuse.article_id).where(
        OjsHtmlEvidenceReuse.journal_id == journal.id,
        OjsHtmlEvidenceReuse.target_oai_audit_run_id == target_oai_audit_run_id,
    )))
    analyses = db.session.scalars(select(OjsArticleHtmlAnalysis).join(
        OjsHtmlAuditRun, OjsHtmlAuditRun.id == OjsArticleHtmlAnalysis.html_audit_run_id,
    ).where(
        OjsArticleHtmlAnalysis.journal_id == journal.id,
        OjsArticleHtmlAnalysis.status == "analyzed",
        OjsHtmlAuditRun.journal_id == journal.id,
        OjsHtmlAuditRun.source_oai_audit_run_id == source_oai_audit_run_id,
        OjsHtmlAuditRun.status.in_(HTML_AUDIT_FINAL),
    ).order_by(
        OjsArticleHtmlAnalysis.checked_at.desc(), OjsArticleHtmlAnalysis.created_at.desc(),
        OjsArticleHtmlAnalysis.id.desc(),
    )).all()
    by_article: dict[str, OjsArticleHtmlAnalysis] = {}
    for analysis in analyses:
        by_article.setdefault(analysis.article_id, analysis)

    created = 0
    for target in current:
        if target.article_id in existing:
            continue
        source = previous.get(target.article_id)
        analysis = by_article.get(target.article_id)
        if source is None or analysis is None:
            continue
        if (
            not target.metadata_hash
            or target.metadata_hash != source.metadata_hash
            or analysis.source_observation_id != source.id
        ):
            continue
        target_url = _normalized_evidence_url(_public_url_from_observation(target))
        source_url = _normalized_evidence_url(analysis.source_url)
        if target_url is None or target_url != source_url:
            continue
        db.session.add(OjsHtmlEvidenceReuse(
            journal_id=journal.id,
            target_oai_audit_run_id=target_oai_audit_run_id,
            source_oai_audit_run_id=source_oai_audit_run_id,
            article_id=target.article_id,
            target_observation_id=target.id,
            source_observation_id=source.id,
            source_html_audit_run_id=analysis.html_audit_run_id,
            source_html_analysis_id=analysis.id,
            criteria_version=HTML_EVIDENCE_REUSE_CRITERIA_VERSION,
            reuse_reason=HTML_EVIDENCE_REUSE_REASON,
        ))
        created += 1
    return created


def _normalized_evidence_url(value: str | None) -> str | None:
    """Normaliza somente equivalências formais de uma URL HTTPS já segura."""
    if not isinstance(value, str) or not value or len(value) > 2_048 or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    netloc = host if port in {None, 443} else f"{host}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _has_terminal_unverified_result(attempts: Sequence[OjsArticleHtmlAnalysis]) -> bool:
    for attempt in attempts:
        if attempt.status != "not_verified" or not isinstance(attempt.findings_json, list):
            continue
        for finding in attempt.findings_json:
            if isinstance(finding, dict) and finding.get("code") in HTML_TERMINAL_UNVERIFIED_CODES:
                return True
    return False


def _public_url_from_observation(observation: OjsArticleObservation) -> str | None:
    payload = observation.dublin_core_json if isinstance(observation.dublin_core_json, dict) else {}
    values = payload.get("identifier") if isinstance(payload.get("identifier"), list) else []
    for value in values:
        if not isinstance(value, str) or len(value) > 2_048 or any(ord(char) < 32 for char in value):
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        # Um resolvedor DOI é um identificador bibliográfico, não a página
        # pública do artigo no periódico. Preferimos a URL editorial que o
        # OAI-PMH também possa ter informado, para não diagnosticar a página
        # de terceiros por engano.
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme.lower() == "https" and hostname and hostname not in {"doi.org", "www.doi.org"}
            and parsed.username is None
            and parsed.password is None
        ):
            return value
    return None


def persist_html_audit_results(
    journal: OjsJournal,
    run: OjsHtmlAuditRun,
    pages: Sequence[tuple[HtmlAuditCandidate, HtmlAnalysisResult]],
    *,
    eligible_article_count: int,
    final_status: Literal["completed", "partial", "failed"],
    completion_reason: str | None = None,
    warnings: Sequence[dict[str, str]] = (),
    errors: Sequence[dict[str, str]] = (),
    response_bytes: int = 0,
    completed_at: datetime | None = None,
) -> OjsHtmlAuditRun:
    """Persiste análises novas e finaliza o run; não confirma a transação."""
    managed = db.session.get(OjsHtmlAuditRun, run.id)
    if managed is None or managed.journal_id != journal.id or managed.status != "running":
        raise OjsSeoServiceError("html_audit_claim_lost", "A análise técnica não está mais disponível para persistência.")
    if final_status not in {"completed", "partial", "failed"}:
        raise OjsSeoServiceError("invalid_html_audit_status", "O estado final da análise técnica é inválido.")
    seen: set[str] = set()
    for candidate, result in pages:
        if candidate.article.journal_id != journal.id or candidate.observation.journal_id != journal.id:
            raise OjsSeoServiceError("html_analysis_scope_mismatch", "A evidência HTML não pertence ao periódico informado.")
        if candidate.article.id in seen:
            raise OjsSeoServiceError("duplicate_html_analysis", "A mesma página foi preparada duas vezes na análise técnica.")
        seen.add(candidate.article.id)
        db.session.add(OjsArticleHtmlAnalysis(
            journal_id=journal.id,
            article_id=candidate.article.id,
            source_observation_id=candidate.observation.id,
            html_audit_run_id=managed.id,
            status=result.status,
            source_url=result.source_url,
            final_url=result.final_url,
            http_status=result.http_status,
            content_type=result.content_type,
            response_time_ms=result.response_time_ms,
            redirect_count=result.redirect_count,
            facts_json=result.facts,
            findings_json=[{
                "code": item.code, "classification": item.classification, "severity": item.severity,
                "evidence": item.evidence, "recommendation": item.recommendation,
                "applicability": item.applicability, "impact": item.impact, "audience": item.audience,
            } for item in result.findings],
            evidence_hash=result.evidence_hash,
            confirmed_problem_count=result.confirmed_problem_count,
            potential_problem_count=result.potential_problem_count,
            unverified_count=result.unverified_count,
            checked_at=completed_at or utcnow(),
        ))
    completed = completed_at or utcnow()
    # O resumo do periódico comunica páginas afetadas, não a soma de regras
    # acionadas. Uma página pode ter várias evidências (por exemplo, canonical
    # e citation_doi ausentes), mas continua sendo uma única página na visão
    # operacional. Os totais de regras permanecem em cada análise individual.
    confirmed = sum(result.confirmed_problem_count > 0 for _candidate, result in pages)
    potential = sum(result.potential_problem_count > 0 for _candidate, result in pages)
    unverified = sum(result.status == "not_verified" for _candidate, result in pages)
    verified = sum(result.status == "analyzed" for _candidate, result in pages)
    managed.eligible_article_count = eligible_article_count
    managed.analyzed_page_count = len(pages)
    managed.confirmed_problem_count = confirmed
    managed.potential_problem_count = potential
    managed.unverified_page_count = unverified
    managed.metrics_json = {
        "eligible_articles": eligible_article_count,
        "records_processed": len(pages),
        "pages_analyzed": len(pages),
        "verified_pages": verified,
        "sample_limit": HTML_AUDIT_MAX_PAGES,
        "sampled": eligible_article_count > len(pages),
        "confirmed_problem_pages": confirmed,
        "potential_problem_pages": potential,
        "not_verified_pages": unverified,
        "response_bytes": max(0, response_bytes),
    }
    managed.warnings_json = _safe_issue_list(warnings)
    managed.errors_json = _safe_issue_list(errors)
    managed.status = final_status
    managed.completion_reason = _safe_code(completion_reason) if completion_reason else None
    managed.completed_at = completed
    managed.claim_token = None
    managed.claimed_at = None
    managed.lease_expires_at = None
    db.session.flush()
    return managed


def _safe_code(value: object) -> str:
    text = value if isinstance(value, str) else "html_audit_error"
    return "".join(character for character in text if character.isalnum() or character in {"_", "-"})[:80] or "html_audit_error"


def _safe_issue_list(values: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    safe: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        code, message = item.get("code"), item.get("message")
        if isinstance(code, str) and isinstance(message, str):
            safe.append({"code": _safe_code(code), "message": " ".join(message.split())[:500]})
    return safe


def list_ojs_html_analysis_pages(
    actor: User, journal_id: str, audit_run_id: str, *, page: int = 1,
    selected_filter: object = "all", search: object = "", include_inactive: bool = False,
) -> HtmlAnalysisListing:
    selected = _validated_filter(selected_filter)
    query = _normalized_search(search)
    run = get_ojs_html_audit(actor, journal_id, audit_run_id, include_inactive=include_inactive)
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    statement = select(OjsArticle, OjsArticleObservation, OjsArticleHtmlAnalysis).join(
        OjsArticleHtmlAnalysis, OjsArticleHtmlAnalysis.article_id == OjsArticle.id,
    ).join(
        OjsArticleObservation, OjsArticleObservation.id == OjsArticleHtmlAnalysis.source_observation_id,
    ).where(
        OjsArticleHtmlAnalysis.html_audit_run_id == run.id,
        OjsArticleHtmlAnalysis.journal_id == journal.id,
        OjsArticle.journal_id == journal.id,
        OjsArticleObservation.journal_id == journal.id,
    )
    if selected == "confirmed":
        statement = statement.where(OjsArticleHtmlAnalysis.confirmed_problem_count > 0)
    elif selected == "potential":
        statement = statement.where(OjsArticleHtmlAnalysis.potential_problem_count > 0)
    elif selected == "unverified":
        statement = statement.where(OjsArticleHtmlAnalysis.status == "not_verified")
    if query:
        pattern = f"%{query}%"
        statement = statement.where(or_(
            OjsArticle.oai_identifier.ilike(pattern), OjsArticle.title.ilike(pattern),
            OjsArticle.doi.ilike(pattern), cast_json_text(OjsArticleObservation.dublin_core_json).ilike(pattern),
        ))
    total = int(db.session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    pages = max(1, ceil(total / PAGE_SIZE))
    current = min(max(1, page), pages)
    rows = db.session.execute(statement.order_by(
        OjsArticle.oai_identifier.asc(), OjsArticle.id.asc(),
    ).limit(PAGE_SIZE).offset((current - 1) * PAGE_SIZE)).all()
    return HtmlAnalysisListing(
        journal, run, PlatformPage(tuple(HtmlAnalysisPage(*row) for row in rows), total, current), selected, query,
    )


def cast_json_text(column):
    """Busca portável sem carregar todos os metadados em Python."""
    return column.cast(String)


def get_ojs_html_analysis_page(
    actor: User, journal_id: str, audit_run_id: str, analysis_id: str, *, include_inactive: bool = False,
) -> HtmlAnalysisPage:
    run = get_ojs_html_audit(actor, journal_id, audit_run_id, include_inactive=include_inactive)
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    row = db.session.execute(select(OjsArticle, OjsArticleObservation, OjsArticleHtmlAnalysis).join(
        OjsArticleHtmlAnalysis, OjsArticleHtmlAnalysis.article_id == OjsArticle.id,
    ).join(
        OjsArticleObservation, OjsArticleObservation.id == OjsArticleHtmlAnalysis.source_observation_id,
    ).where(
        OjsArticleHtmlAnalysis.id == analysis_id,
        OjsArticleHtmlAnalysis.html_audit_run_id == run.id,
        OjsArticleHtmlAnalysis.journal_id == journal.id,
        OjsArticle.journal_id == journal.id,
        OjsArticleObservation.journal_id == journal.id,
    )).one_or_none()
    if row is None:
        raise OjsSeoNotFoundError("ojs_html_analysis_not_found", "O diagnóstico técnico do registro não foi encontrado.")
    return HtmlAnalysisPage(*row)


__all__ = [
    "HTML_AUDIT_FINAL", "HTML_AUDIT_MAX_ATTEMPTS_PER_ARTICLE", "HTML_AUDIT_MAX_PAGES", "HTML_AUDIT_MAX_RESPONSE_BYTES", "HTML_AUDIT_OPEN", "HTML_CAMPAIGN_FINAL", "HTML_CAMPAIGN_OPEN", "HTML_CAMPAIGN_STATUS_LABELS", "HTML_FILTER_LABELS",
    "HTML_EVIDENCE_REUSE_CRITERIA_VERSION", "HTML_SELECTION_CONTINUE", "HTML_SELECTION_FULL_CAMPAIGN", "HTML_SELECTION_LABELS", "HTML_SELECTION_RETRY_UNVERIFIED", "HTML_SELECTION_REVALIDATE", "HtmlAnalysisListing", "HtmlCoverage",
    "HtmlAnalysisPage", "HtmlAuditCandidate", "get_ojs_html_analysis_page", "get_ojs_html_audit", "get_ojs_html_coverage", "get_html_coverage_for_journal_reference", "html_evidence_reuse_available",
    "get_ojs_html_audit_campaign", "list_html_audit_candidates", "list_ojs_html_analysis_pages", "list_ojs_html_audit_campaigns", "list_ojs_html_audits", "persist_html_audit_results",
    "request_ojs_html_audit", "request_ojs_html_audit_campaign", "resume_ojs_html_audit_campaign", "reuse_compatible_html_evidence", "start_ojs_html_campaign_for_reference",
    "advance_ojs_html_audit_campaign", "interrupt_ojs_html_audit_campaign", "can_request_ojs_html_audit", "can_request_ojs_html_audit_campaign",
]
