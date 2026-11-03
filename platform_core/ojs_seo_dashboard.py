"""Agregações somente de leitura para os painéis do OJS SEO.

O dashboard não cria diagnósticos, não dispara workers e não reclassifica
evidências. Cada número parte da referência OAI-PMH selecionada pelos serviços
existentes e mantém explícita a diferença entre uma verificação HTML direta e
uma evidência histórica reutilizada de forma compatível.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import select

from .extensions import db
from .models import OjsHtmlAuditCampaign, OjsSeoAnalysisOperation, OjsSeoAuditRun, OjsJournal, User
from .ojs_seo_diagnostics import DIAGNOSTIC_FIELDS, OjsDiagnosticReference, resolve_ojs_diagnostic_reference
from .ojs_seo_recommendations import OjsRecommendation, OjsRecommendationSnapshot, get_ojs_recommendation_snapshot
from .ojs_seo_services import OjsSeoServiceError, get_ojs_journal, list_archived_ojs_journals, list_ojs_journals
from .ojs_seo_technical import HtmlCoverage, get_ojs_html_coverage


_HISTORY_STATUSES = ("completed", "partial")
_SEVERITY_ORDER = ("critical", "high", "medium", "low", "informative")
_CERTAINTY_ORDER = ("confirmed", "potential", "unverified")
_NATURE_ORDER = ("bibliographic", "technical", "comparison")


@dataclass(frozen=True, slots=True)
class OjsDashboardMetricComparison:
    """Variação puramente descritiva entre duas auditorias comparáveis."""

    previous_value: int
    delta: int
    percentage: float
    direction: str
    label: str

    @property
    def display(self) -> str:
        sign = "+" if self.percentage > 0 else "−" if self.percentage < 0 else ""
        value = abs(self.percentage)
        formatted = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{sign}{formatted}%"


@dataclass(frozen=True, slots=True)
class OjsDashboardMetric:
    key: str
    label: str
    value: int | str
    description: str
    article_filter: str | None = None
    series: tuple[int, ...] = ()
    sparkline_path: str | None = None
    sparkline_label: str | None = None
    comparison: OjsDashboardMetricComparison | None = None


@dataclass(frozen=True, slots=True)
class OjsDashboardBar:
    key: str
    label: str
    value: int
    total: int
    description: str
    recommendation: OjsRecommendation | None = None

    @property
    def percentage(self) -> int:
        return round(self.value / self.total * 100) if self.total else 0


@dataclass(frozen=True, slots=True)
class OjsDashboardHistoryPoint:
    audit_run_id: str
    recorded_at: datetime
    status: str
    records: int
    active_records: int
    deleted_records: int
    dois_identified: int
    active_records_relative_percentage: int


@dataclass(frozen=True, slots=True)
class OjsSeoDashboard:
    journal: OjsJournal
    reference: OjsDiagnosticReference | None
    coverage_reference: OjsDiagnosticReference | None
    bibliography: tuple[OjsDashboardMetric, ...]
    metadata_completeness: tuple[OjsDashboardBar, ...]
    coverage: HtmlCoverage | None
    coverage_segments: tuple[OjsDashboardBar, ...]
    recommendations: tuple[OjsRecommendation, ...]
    priority_recommendations: tuple[OjsRecommendation, ...]
    problem_types: tuple[OjsDashboardBar, ...]
    problems_by_severity: tuple[OjsDashboardBar, ...]
    problems_by_nature: tuple[OjsDashboardBar, ...]
    problems_by_certainty: tuple[OjsDashboardBar, ...]
    confirmed_articles: int
    potential_articles: int
    high_priority_recommendation_count: int
    review_recommendation_count: int
    history: tuple[OjsDashboardHistoryPoint, ...]
    latest_operation: OjsSeoAnalysisOperation | None
    latest_campaign: OjsHtmlAuditCampaign | None
    archived: bool


@dataclass(frozen=True, slots=True)
class OjsSeoConsolidatedDashboard:
    active_dashboards: tuple[OjsSeoDashboard, ...]
    archived_dashboards: tuple[OjsSeoDashboard, ...]
    active_journals: int
    eligible_articles: int
    direct_verified_pages: int
    reused_evidence_pages: int
    confirmed_articles: int
    high_priority_recommendations: int

    @property
    def available_evidence_pages(self) -> int:
        return self.direct_verified_pages + self.reused_evidence_pages

    @property
    def available_coverage_percentage(self) -> int:
        return round(self.available_evidence_pages / self.eligible_articles * 100) if self.eligible_articles else 0


def build_ojs_seo_dashboard(
    actor: User,
    journal_id: str,
    *,
    include_inactive: bool = False,
) -> OjsSeoDashboard:
    """Retorna um painel por periódico, autorizado e ancorado numa referência.

    A função é deliberadamente somente de leitura. Chamadas de páginas e APIs
    usam a mesma instância de ``HtmlCoverage`` que já fundamenta o histórico e
    as recomendações, evitando percentuais paralelos.
    """
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    return _build_for_journal(actor, journal, include_inactive=include_inactive)


def build_ojs_seo_consolidated_dashboard(
    actor: User,
    *,
    active_journals: Iterable[OjsJournal] | None = None,
    archived_journals: Iterable[OjsJournal] | None = None,
) -> OjsSeoConsolidatedDashboard:
    """Consolida somente periódicos que os serviços já autorizaram ao ator."""
    active_source = list_ojs_journals(actor) if active_journals is None else active_journals
    archived_source = list_archived_ojs_journals(actor) if archived_journals is None else archived_journals
    active = tuple(_build_for_journal(actor, journal) for journal in active_source)
    archived = tuple(
        _build_for_journal(actor, journal, include_inactive=True)
        for journal in archived_source
    )
    eligible = sum(item.coverage.eligible_article_count for item in active if item.coverage is not None)
    direct = sum(item.coverage.verified_article_count for item in active if item.coverage is not None)
    reused = sum(item.coverage.reused_evidence_article_count for item in active if item.coverage is not None)
    return OjsSeoConsolidatedDashboard(
        active_dashboards=active,
        archived_dashboards=archived,
        active_journals=len(active),
        eligible_articles=eligible,
        direct_verified_pages=direct,
        reused_evidence_pages=reused,
        confirmed_articles=sum(item.confirmed_articles for item in active),
        high_priority_recommendations=sum(item.high_priority_recommendation_count for item in active),
    )


def _build_for_journal(actor: User, journal: OjsJournal, *, include_inactive: bool = False) -> OjsSeoDashboard:
    reference = resolve_ojs_diagnostic_reference(actor, journal.id, include_inactive=include_inactive)
    latest_operation = _latest_operation(journal.id)
    coverage_reference = reference
    if latest_operation is not None and isinstance(latest_operation.parameters_json, dict):
        coverage_reference_id = latest_operation.parameters_json.get(
            "html_coverage_reference_oai_audit_run_id"
        )
        if isinstance(coverage_reference_id, str) and coverage_reference_id:
            # Atualizações sem alterações bibliográficas mantêm explicitamente
            # a referência HTML anterior. Nunca a apresentamos como coleta
            # recente nem misturamos suas contagens com uma nova referência.
            compatible_reference = resolve_ojs_diagnostic_reference(
                actor, journal.id, audit_run_id=coverage_reference_id,
                include_inactive=include_inactive,
            )
            if compatible_reference is not None:
                coverage_reference = compatible_reference
    snapshot = get_ojs_recommendation_snapshot(
        actor, journal.id, audit_run_id=reference.audit_run.id if reference else None,
        include_inactive=include_inactive,
    )
    coverage = (
        snapshot.indicators.coverage
        if coverage_reference is not None and reference is not None
        and coverage_reference.audit_run.id == reference.audit_run.id
        else None
    )
    if coverage is None and coverage_reference is not None:
        # A recomendação já usa esta mesma função. O fallback só atende bases
        # antigas que ainda não produziam recomendações técnicas.
        try:
            coverage = get_ojs_html_coverage(
                actor, journal.id, source_oai_audit_run_id=coverage_reference.audit_run.id,
                include_inactive=include_inactive,
            )
        except OjsSeoServiceError:
            # Uma instalação anterior à auditoria HTML pode ter a referência
            # bibliográfica preservada, mas ainda não dispor das tabelas de
            # cobertura. O painel permanece informativo sem inventar números.
            coverage = None
    recommendations = snapshot.recommendations
    history = _history(journal)
    return OjsSeoDashboard(
        journal=journal,
        reference=reference,
        coverage_reference=coverage_reference,
        bibliography=_bibliography_metrics(reference, coverage, history),
        metadata_completeness=_metadata_completeness(reference),
        coverage=coverage,
        coverage_segments=_coverage_segments(coverage),
        recommendations=recommendations,
        priority_recommendations=tuple(item for item in recommendations if item.priority == "high")[:4]
        or recommendations[:4],
        problem_types=tuple(
            OjsDashboardBar(
                key=item.code,
                label=item.label,
                value=item.occurrence_count,
                total=max(1, sum(row.occurrence_count for row in recommendations)),
                description=(
                    f"{item.article_count} publicação(ões) distinta(s) afetada(s); "
                    f"{item.occurrence_count} ocorrência(s) registrada(s)."
                ),
                recommendation=item,
            )
            for item in recommendations[:8]
        ),
        problems_by_severity=_recommendation_distribution(recommendations, "severity", _SEVERITY_ORDER),
        problems_by_nature=_recommendation_distribution(recommendations, "nature", _NATURE_ORDER),
        problems_by_certainty=_recommendation_distribution(recommendations, "certainty", _CERTAINTY_ORDER),
        confirmed_articles=snapshot.indicators.confirmed_articles,
        potential_articles=snapshot.indicators.potential_articles,
        high_priority_recommendation_count=snapshot.indicators.high_priority_recommendations,
        review_recommendation_count=snapshot.indicators.review_recommendations,
        history=history,
        latest_operation=latest_operation,
        latest_campaign=_latest_campaign(journal.id),
        archived=journal.analytics_project.status == "archived",
    )


def _safe_int(value: object, fallback: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback


def _active_records(reference: OjsDiagnosticReference) -> int:
    metrics = reference.audit_run.metrics_json if isinstance(reference.audit_run.metrics_json, dict) else {}
    collection = metrics.get("collection") if isinstance(metrics.get("collection"), dict) else {}
    return _safe_int(
        collection.get("active_records"),
        max(0, reference.audit_run.record_count - reference.audit_run.deleted_record_count),
    )


def _bibliography_metrics(
    reference: OjsDiagnosticReference | None,
    coverage: HtmlCoverage | None,
    history: tuple[OjsDashboardHistoryPoint, ...],
) -> tuple[OjsDashboardMetric, ...]:
    if reference is None:
        return ()
    run = reference.audit_run
    active = _active_records(reference)
    doi = min(active, _safe_int(run.doi_count))
    series_by_key = {
        "records": tuple(point.records for point in history),
        "active": tuple(point.active_records for point in history),
        "deleted": tuple(point.deleted_records for point in history),
        "doi": tuple(min(point.active_records, point.dois_identified) for point in history),
        "missing_doi": tuple(
            max(0, point.active_records - min(point.active_records, point.dois_identified))
            for point in history
        ),
    }

    def metric(
        key: str, label: str, value: int | str, description: str,
        article_filter: str | None = None,
    ) -> OjsDashboardMetric:
        series = series_by_key.get(key, ())
        # Só comparamos a referência exibida com sua antecessora imediata
        # quando ela também é o último ponto da série registrada.
        if not isinstance(value, int) or not series or series[-1] != value:
            series = ()
        return OjsDashboardMetric(
            key, label, value, description, article_filter,
            series=series,
            sparkline_path=_sparkline_path(series),
            sparkline_label=_sparkline_label(series, label),
            comparison=_metric_comparison(series),
        )

    return (
        metric("records", "Registros bibliográficos recuperados", run.record_count, "Inclui registros ativos e excluídos informados pela fonte."),
        metric("active", "Publicações ativas", active, "Registros ativos da auditoria bibliográfica de referência."),
        metric("deleted", "Registros excluídos da fonte", run.deleted_record_count, "Tombstones informados pela fonte; não são lacunas editoriais."),
        metric("eligible", "Artigos elegíveis para análise das páginas", coverage.eligible_article_count if coverage else "—", "Publicações ativas com URL pública HTTPS adequada."),
        metric("doi", "DOIs identificados", doi, "DOIs identificados nos dados bibliográficos recuperados."),
        metric("missing_doi", "Sem DOI identificado", max(0, active - doi), "Não prova que a publicação não possua DOI.", "missing_doi"),
    )


def _metric_comparison(series: tuple[int, ...]) -> OjsDashboardMetricComparison | None:
    """Retorna mudança factual sem atribuir sentido editorial à variação."""
    if len(series) < 2:
        return None
    previous, current = series[-2:]
    if previous == 0:
        # A fórmula percentual não é definida. Não exibimos 0% decorativo.
        return None
    delta = current - previous
    percentage = round(delta / previous * 100, 1)
    direction = "up" if delta > 0 else "down" if delta < 0 else "same"
    movement = "Aumentou" if delta > 0 else "Diminuiu" if delta < 0 else "Não mudou"
    return OjsDashboardMetricComparison(
        previous_value=previous,
        delta=delta,
        percentage=percentage,
        direction=direction,
        label=(
            f"{movement} {abs(percentage):.1f}% em relação à auditoria anterior. "
            "A variação é descritiva e não comprova melhoria editorial."
        ),
    )


def _sparkline_path(series: tuple[int, ...]) -> str | None:
    """Cria uma linha SVG leve somente para uma série histórica real."""
    if len(series) < 3:
        return None
    width, height, inset = 72, 28, 2
    minimum, maximum = min(series), max(series)
    span = maximum - minimum
    points = []
    for index, value in enumerate(series):
        x = inset + (width - inset * 2) * index / (len(series) - 1)
        y = height / 2 if span == 0 else height - inset - (value - minimum) / span * (height - inset * 2)
        points.append(f"{x:.2f},{y:.2f}")
    return "M " + " L ".join(points)


def _sparkline_label(series: tuple[int, ...], label: str) -> str | None:
    if len(series) < 3:
        return None
    values = ", ".join(str(value) for value in series)
    return f"Série histórica de {label.lower()} em {len(series)} auditorias: {values}."


def _metadata_completeness(reference: OjsDiagnosticReference | None) -> tuple[OjsDashboardBar, ...]:
    if reference is None:
        return ()
    active = _active_records(reference)
    metrics = reference.audit_run.metrics_json if isinstance(reference.audit_run.metrics_json, dict) else {}
    raw = metrics.get("coverage") if isinstance(metrics.get("coverage"), dict) else {}
    labels = dict(DIAGNOSTIC_FIELDS)
    rows: list[OjsDashboardBar] = []
    for key in ("title", "creator", "description", "subject", "language", "date", "type", "identifier"):
        present = min(active, _safe_int(raw.get(key)))
        missing = max(0, active - present)
        rows.append(OjsDashboardBar(
            key=key,
            label=labels[key],
            value=present,
            total=active,
            description=(
                f"{present} com {labels[key].lower()} no registro; {missing} sem valor identificado no OAI-PMH."
            ),
        ))
    return tuple(rows)


def _coverage_segments(coverage: HtmlCoverage | None) -> tuple[OjsDashboardBar, ...]:
    if coverage is None:
        return ()
    total = coverage.eligible_article_count
    return (
        OjsDashboardBar("verified", "Verificadas nesta referência", coverage.verified_article_count, total, "HTML recuperado e analisado para a referência atual."),
        OjsDashboardBar("reused", "Evidências históricas reaproveitadas", coverage.reused_evidence_article_count, total, "Evidências compatíveis com origem e data preservadas; não representam nova consulta HTML."),
        OjsDashboardBar("unverified", "Não verificadas", coverage.unverified_article_count, total, "Houve tentativa, mas não foi possível analisar o HTML."),
        OjsDashboardBar("unprocessed", "Não processadas", coverage.unprocessed_article_count, total, "Artigos elegíveis sem tentativa nem evidência compatível."),
    )


def _recommendation_distribution(
    recommendations: Iterable[OjsRecommendation], key: str, ordered_keys: Iterable[str],
) -> tuple[OjsDashboardBar, ...]:
    rows = tuple(recommendations)
    total = sum(item.occurrence_count for item in rows)
    labels = {
        "critical": "Crítica", "high": "Alta", "medium": "Média", "low": "Baixa", "informative": "Informativa",
        "bibliographic": "Dados bibliográficos", "technical": "Informações das páginas", "comparison": "Informações diferentes entre as fontes",
        "confirmed": "Confirmados", "potential": "Potenciais", "unverified": "Não verificados",
    }
    result: list[OjsDashboardBar] = []
    for item_key in ordered_keys:
        value = sum(
            recommendation.occurrence_count
            for recommendation in rows
            if getattr(recommendation, key) == item_key
        )
        # Mantemos também categorias vazias: o painel visual pode explicitar
        # que uma gravidade, origem ou certeza não ocorreu nesta referência,
        # sem criar um cálculo ou uma regra diagnóstica paralela.
        result.append(OjsDashboardBar(
            item_key, labels[item_key], value, total,
            "Ocorrências agrupadas por recomendação; uma publicação pode participar de mais de um achado.",
        ))
    return tuple(result)


def _history(journal: OjsJournal) -> tuple[OjsDashboardHistoryPoint, ...]:
    runs = list(db.session.scalars(
        select(OjsSeoAuditRun)
        .where(OjsSeoAuditRun.journal_id == journal.id, OjsSeoAuditRun.status.in_(_HISTORY_STATUSES))
        .order_by(OjsSeoAuditRun.completed_at.asc(), OjsSeoAuditRun.created_at.asc(), OjsSeoAuditRun.id.asc())
    ))
    raw = [
        OjsDashboardHistoryPoint(
            audit_run_id=run.id,
            recorded_at=run.completed_at or run.created_at,
            status=run.status,
            records=run.record_count,
            active_records=_active_records(OjsDiagnosticReference(journal, run, "completed" if run.status == "completed" else "partial")),
            deleted_records=run.deleted_record_count,
            dois_identified=run.doi_count,
            active_records_relative_percentage=0,
        )
        for run in runs
    ]
    maximum = max((point.active_records for point in raw), default=0)
    return tuple(
        OjsDashboardHistoryPoint(
            audit_run_id=point.audit_run_id,
            recorded_at=point.recorded_at,
            status=point.status,
            records=point.records,
            active_records=point.active_records,
            deleted_records=point.deleted_records,
            dois_identified=point.dois_identified,
            active_records_relative_percentage=(
                round(point.active_records / maximum * 100) if maximum else 0
            ),
        )
        for point in raw
    )


def _latest_operation(journal_id: str) -> OjsSeoAnalysisOperation | None:
    return db.session.scalar(select(OjsSeoAnalysisOperation).where(
        OjsSeoAnalysisOperation.journal_id == journal_id,
    ).order_by(OjsSeoAnalysisOperation.created_at.desc(), OjsSeoAnalysisOperation.id.desc()).limit(1))


def _latest_campaign(journal_id: str) -> OjsHtmlAuditCampaign | None:
    return db.session.scalar(select(OjsHtmlAuditCampaign).where(
        OjsHtmlAuditCampaign.journal_id == journal_id,
    ).order_by(OjsHtmlAuditCampaign.created_at.desc(), OjsHtmlAuditCampaign.id.desc()).limit(1))


__all__ = [
    "OjsDashboardBar", "OjsDashboardHistoryPoint", "OjsDashboardMetric", "OjsDashboardMetricComparison", "OjsSeoConsolidatedDashboard",
    "OjsSeoDashboard", "build_ojs_seo_consolidated_dashboard", "build_ojs_seo_dashboard",
]
