"""Exportações somente de leitura do OJS SEO.

Este módulo transforma as evidências que já fundamentam as telas do OJS SEO
em tabelas portáveis. Ele não inicia coletas, não cria campanhas e não
persiste artefatos: o arquivo é montado em memória e entregue pela rota.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO, StringIO
import re
from typing import Any, Iterable, Mapping, Sequence
import unicodedata

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select

from .extensions import db
from .models import (
    OjsArticleHtmlAnalysis,
    OjsHtmlAuditCampaign,
    OjsHtmlAuditRun,
    OjsHtmlEvidenceReuse,
    OjsSeoAnalysisOperation,
    OjsSeoAuditRun,
    OjsJournal,
    User,
)
from .ojs_html_client import html_finding_label
from .ojs_seo_dashboard import OjsSeoDashboard, build_ojs_seo_dashboard
from .ojs_seo_diagnostics import (
    OjsArticleDiagnostic,
    OjsDiagnosticReference,
    OjsSeoDiagnosticFilterError,
    list_all_ojs_article_diagnostics,
    resolve_ojs_diagnostic_reference,
)
from .ojs_seo_recommendations import (
    APPLICABILITY_LABELS,
    CERTAINTY_LABELS,
    NATURE_LABELS,
    PRIORITY_LABELS,
    ROLE_LABELS,
    SEVERITY_LABELS,
    OjsRecommendation,
    OjsRecommendationFilterError,
    filter_ojs_recommendations,
    get_ojs_recommendation_snapshot,
)
from .ojs_seo_services import get_ojs_journal
from .ojs_seo_technical import html_evidence_reuse_available


class OjsSeoExportError(ValueError):
    """Parâmetro de exportação inválido ou combinação não suportada."""


DATASET_LABELS: dict[str, str] = {
    "consolidated": "Diagnóstico consolidado",
    "overview": "Visão geral",
    "bibliographic": "Diagnóstico bibliográfico",
    "html": "Diagnóstico das páginas HTML",
    "comparison": "Comparação OAI-PMH × HTML",
    "recommendations": "Problemas e recomendações",
    "articles": "Publicações afetadas",
    "history": "Histórico de análises",
}
_FORMATS = frozenset({"xlsx", "csv"})
_AUDIT_STATUS_LABELS = {
    "pending": "Pendente", "running": "Em execução", "completed": "Concluída",
    "partial": "Parcial", "failed": "Falhou", "canceled": "Cancelada",
}
_HTML_STATUS_LABELS = _AUDIT_STATUS_LABELS
_CAMPAIGN_STATUS_LABELS = {
    "pending": "Pendente", "running": "Em execução", "completed": "Concluída",
    "completed_with_pending": "Concluída com pendências", "interrupted": "Interrompida",
    "failed": "Falhou", "canceled": "Cancelada",
}
_OPERATION_STATUS_LABELS = {
    "oai_pending": "Aguardando processamento bibliográfico",
    "oai_running": "Analisando dados bibliográficos",
    "html_pending": "Aguardando análise das páginas",
    "html_running": "Analisando páginas",
    "completed": "Concluída", "completed_with_pending": "Concluída com pendências",
    "interrupted": "Interrompida", "failed": "Falhou", "canceled": "Cancelada",
}
_SEVERITY_ORDER = {"informative": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_PRIORITY_ORDER = {"low": 0, "medium": 1, "review": 2, "high": 3}


@dataclass(frozen=True, slots=True)
class OjsSeoExportTable:
    key: str
    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True, slots=True)
class OjsSeoExportBundle:
    journal: OjsJournal
    generated_at: datetime
    dataset: str
    filters: Mapping[str, str]
    tables: tuple[OjsSeoExportTable, ...]


@dataclass(frozen=True, slots=True)
class OjsSeoExportFile:
    content: bytes
    mimetype: str
    download_name: str


def build_ojs_seo_export(
    actor: User,
    journal_id: str,
    *,
    dataset: object,
    filters: Mapping[str, object] | None = None,
) -> OjsSeoExportBundle:
    """Monta os conjuntos exportáveis autorizados para um único periódico."""
    dataset_key = _validated_dataset(dataset)
    normalized_filters = _normalized_filters(filters)
    journal = get_ojs_journal(actor, journal_id, include_inactive=True)
    generated_at = datetime.now(timezone.utc)
    needs_dashboard = dataset_key in {"consolidated", "overview"}
    needs_diagnostics = dataset_key in {"consolidated", "bibliographic", "html", "articles"}
    needs_recommendations = dataset_key in {"consolidated", "comparison", "recommendations", "articles"}
    try:
        reference = resolve_ojs_diagnostic_reference(
            actor, journal.id, audit_run_id=normalized_filters["audit_run_id"] or None, include_inactive=True,
        )
        diagnostics: tuple[OjsArticleDiagnostic, ...] = ()
        if needs_diagnostics:
            reference, diagnostics = list_all_ojs_article_diagnostics(
                actor,
                journal.id,
                selected_filter=normalized_filters["article_filter"],
                search=normalized_filters["search"],
                record_state=normalized_filters["record_state"],
                audit_run_id=normalized_filters["audit_run_id"] or None,
                include_inactive=True,
            )
        recommendations: tuple[OjsRecommendation, ...] = ()
        if needs_recommendations:
            snapshot = get_ojs_recommendation_snapshot(
                actor,
                journal.id,
                audit_run_id=reference.audit_run.id if reference else None,
                include_inactive=True,
            )
            recommendations = filter_ojs_recommendations(
                snapshot,
                priority=normalized_filters["priority"],
                severity=normalized_filters["severity"],
                certainty=normalized_filters["certainty"],
                nature=normalized_filters["nature"],
                role=normalized_filters["role"],
            )
    except (OjsSeoDiagnosticFilterError, OjsRecommendationFilterError) as exc:
        raise OjsSeoExportError("Filtro de exportação inválido.") from exc
    dashboard = build_ojs_seo_dashboard(actor, journal.id, include_inactive=True) if needs_dashboard else None
    table_builders = {
        "overview": lambda: _overview_table(dashboard),
        "bibliographic": lambda: _bibliographic_table(diagnostics),
        "html": lambda: _html_table(journal, reference, diagnostics),
        "comparison": lambda: _comparison_table(recommendations),
        "recommendations": lambda: _recommendations_table(journal, reference, recommendations),
        "articles": lambda: _affected_articles_table(reference, recommendations, diagnostics),
        "history": lambda: _history_table(journal),
    }
    metadata = _metadata_table(journal, generated_at, dataset_key, normalized_filters, reference, dashboard)
    if dataset_key == "consolidated":
        detail_tables = tuple(table_builders[key]() for key in (
            "overview", "bibliographic", "html", "comparison", "recommendations", "articles", "history",
        ))
        # O consolidado não cria abas técnicas vazias. A aba inicial sempre
        # explica quando não existe referência bibliográfica; os indicadores
        # de visão geral continuam disponíveis quando forem calculáveis.
        tables = (metadata,) + tuple(table for table in detail_tables if table.rows)
    else:
        tables = (metadata, _with_empty_notice(table_builders[dataset_key]()))
    return OjsSeoExportBundle(journal, generated_at, dataset_key, normalized_filters, tables)


def render_ojs_seo_export(bundle: OjsSeoExportBundle, *, export_format: object) -> OjsSeoExportFile:
    """Serializa um conjunto já consultado, sem nova leitura ou escrita no banco."""
    format_key = _validated_format(export_format, bundle.dataset)
    if format_key == "csv":
        # CSV é tabular; o arquivo contém o conjunto solicitado, nunca uma
        # mistura de tabelas de esquemas diferentes.
        table = bundle.tables[-1]
        return OjsSeoExportFile(
            _render_csv(table),
            "text/csv; charset=utf-8",
            _download_name(bundle, table.key, "csv"),
        )
    return OjsSeoExportFile(
        _render_xlsx(bundle),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _download_name(bundle, bundle.dataset, "xlsx"),
    )


def _validated_dataset(value: object) -> str:
    if not isinstance(value, str) or value not in DATASET_LABELS:
        raise OjsSeoExportError("Conjunto de dados de exportação inválido.")
    return value


def _validated_format(value: object, dataset: str) -> str:
    if not isinstance(value, str) or value not in _FORMATS:
        raise OjsSeoExportError("Formato de exportação inválido.")
    if value == "csv" and dataset == "consolidated":
        raise OjsSeoExportError("A exportação consolidada está disponível somente em XLSX.")
    return value


def _normalized_filters(filters: Mapping[str, object] | None) -> dict[str, str]:
    source = filters or {}
    # Cada valor é reaproveitado pelos serviços que já validam sua semântica.
    # O limite impede que parâmetros de URL anormalmente grandes cheguem a
    # buscas textuais ou ao arquivo de rastreabilidade.
    def text(key: str, default: str = "") -> str:
        value = source.get(key, default)
        return " ".join(value.split())[:200] if isinstance(value, str) else default

    return {
        "audit_run_id": text("audit"),
        "article_filter": text("filter", "all") or "all",
        "search": text("q"),
        "record_state": text("state", "active") or "active",
        "priority": text("priority", "all") or "all",
        "severity": text("severity", "all") or "all",
        "certainty": text("certainty", "all") or "all",
        "nature": text("nature", "all") or "all",
        "role": text("role", "all") or "all",
    }


def _metadata_table(
    journal: OjsJournal,
    generated_at: datetime,
    dataset: str,
    filters: Mapping[str, str],
    reference: OjsDiagnosticReference | None,
    dashboard: OjsSeoDashboard | None,
) -> OjsSeoExportTable:
    filter_display = "; ".join(
        f"{key}={value}" for key, value in filters.items()
        if value and value not in {"all", "active"}
    ) or "Sem filtros adicionais"
    source_reference = reference.audit_run if reference else None
    coverage_reference = dashboard.coverage_reference.audit_run if dashboard and dashboard.coverage_reference else None
    rows: tuple[tuple[object, object], ...] = (
        ("Periódico", journal.repository_name),
        ("URL pública cadastrada", journal.public_url),
        ("Gerado em (UTC)", generated_at),
        ("Escopo", DATASET_LABELS[dataset]),
        ("Filtros aplicados", filter_display),
        ("Referência bibliográfica do diagnóstico", source_reference.id if source_reference else "Indisponível"),
        ("Data da referência bibliográfica", source_reference.completed_at or source_reference.created_at if source_reference else "Indisponível"),
        ("Referência usada pela cobertura HTML", coverage_reference.id if coverage_reference else "Indisponível"),
        ("Nota metodológica", "Evidências diretas foram verificadas nesta referência; evidências reaproveitadas mantêm a origem e a data da verificação anterior; páginas não verificadas não comprovam problema de SEO."),
    )
    if source_reference is None:
        rows += (("Disponibilidade dos dados", "Não há auditoria bibliográfica persistida. Abas detalhadas sem dados não foram incluídas nesta exportação consolidada."),)
    return OjsSeoExportTable("information", "Informações gerais", ("Campo", "Valor"), rows)


def _with_empty_notice(table: OjsSeoExportTable) -> OjsSeoExportTable:
    """Mantém exportações tabulares honestas quando o escopo não tem linhas."""
    if table.rows:
        return table
    return OjsSeoExportTable(
        table.key,
        table.title,
        table.columns,
        (("Sem dados persistidos para o escopo selecionado.",) + ("",) * (len(table.columns) - 1),),
    )


def _overview_table(dashboard: OjsSeoDashboard | None) -> OjsSeoExportTable:
    if dashboard is None:
        raise AssertionError("Dashboard necessário para a exportação da visão geral.")
    rows: list[tuple[object, ...]] = []
    for metric in dashboard.bibliography:
        rows.append(("Indicador bibliográfico", metric.label, metric.value, metric.description))
    coverage = dashboard.coverage
    if coverage is not None:
        rows.extend((
            ("Cobertura HTML", "Artigos elegíveis", coverage.eligible_article_count, "Universo de páginas públicas elegíveis da referência HTML."),
            ("Cobertura HTML", "Verificadas diretamente", coverage.verified_article_count, "Páginas efetivamente verificadas nesta referência."),
            ("Cobertura HTML", "Evidências reaproveitadas", coverage.reused_evidence_article_count, "Evidências históricas compatíveis; não representam nova consulta HTML."),
            ("Cobertura HTML", "Páginas não verificadas", coverage.unverified_article_count, "Houve tentativa, mas o HTML não pôde ser analisado."),
            ("Cobertura HTML", "Artigos não processados", coverage.unprocessed_article_count, "Sem tentativa e sem evidência aplicável."),
            ("Cobertura HTML", "Cobertura verificada diretamente (%)", coverage.verified_percentage, "Percentual de 0 a 100: verificadas diretamente ÷ elegíveis."),
            ("Cobertura HTML", "Cobertura com evidência disponível (%)", coverage.available_coverage_percentage, "Percentual de 0 a 100: diretas + reaproveitadas ÷ elegíveis."),
        ))
    rows.extend((
        ("Problemas", "Publicações com problema confirmado", dashboard.confirmed_articles, "Publicações distintas; não equivale ao número de ocorrências."),
        ("Problemas", "Publicações com possível problema", dashboard.potential_articles, "Exigem conferência; não são problemas confirmados."),
        ("Recomendações", "Tipos de recomendação", len(dashboard.recommendations), "Categorias consolidadas da referência."),
        ("Recomendações", "Prioridades altas", dashboard.high_priority_recommendation_count, "Recomendações com prioridade alta."),
    ))
    for heading, distribution in (
        ("Distribuição por gravidade", dashboard.problems_by_severity),
        ("Distribuição por origem", dashboard.problems_by_nature),
        ("Distribuição por evidência", dashboard.problems_by_certainty),
        ("Distribuição por tipo", dashboard.problem_types),
    ):
        for item in distribution:
            rows.append((heading, item.label, item.value, item.description))
    return OjsSeoExportTable(
        "overview", "Visão geral", ("Grupo", "Indicador", "Valor", "Contexto metodológico"), tuple(rows),
    )


def _bibliographic_table(diagnostics: Sequence[OjsArticleDiagnostic]) -> OjsSeoExportTable:
    rows: list[tuple[object, ...]] = []
    for item in diagnostics:
        fields = {field.code: field for field in item.fields}
        def values(code: str) -> str:
            return "; ".join(fields[code].values) if fields[code].values else ""
        absent = "; ".join(field.label for field in item.fields if field.status == "absent")
        unverified = "; ".join(field.label for field in item.fields if field.status == "unverified")
        rows.append((
            item.article.oai_identifier, values("title"), values("creator"), values("date"),
            item.observation.doi_observed or "", "; ".join(item.public_urls), values("subject"),
            values("language"), values("description"), values("type"),
            "Ativo" if item.observation.record_status == "active" else "Excluído pela fonte",
            absent or "Nenhum campo ausente identificado", unverified or "Nenhum campo não verificado",
            "; ".join(item.missing_labels) or "Nenhuma lacuna identificada", item.reference.audit_run.id,
            item.observation.observed_at,
        ))
    return OjsSeoExportTable(
        "bibliographic", "Diagnóstico bibliográfico",
        ("Identificador OAI-PMH", "Título", "Autoria", "Data", "DOI", "URL pública", "Assuntos", "Idioma", "Descrição/resumo", "Tipo documental", "Situação na fonte", "Metadados ausentes", "Campos não verificados", "Problemas bibliográficos", "Referência bibliográfica", "Observado em"),
        tuple(rows),
    )


def _html_table(
    journal: OjsJournal,
    reference: OjsDiagnosticReference | None,
    diagnostics: Sequence[OjsArticleDiagnostic],
) -> OjsSeoExportTable:
    if reference is None:
        return OjsSeoExportTable("html", "Diagnóstico HTML", _html_columns(), ())
    direct, reused = _selected_html_evidence(journal, reference)
    rows: list[tuple[object, ...]] = []
    for item in diagnostics:
        analysis, reuse = direct.get(item.article.id, (None, None))
        evidence_kind: str
        status: str
        source_audit: object = ""
        reuse_reference: object = ""
        if analysis is not None and analysis.status == "analyzed":
            evidence_kind, status = "Verificação direta", "Verificada diretamente"
            source_audit = analysis.html_audit_run_id
        elif item.article.id in reused:
            analysis, reuse = reused[item.article.id]
            evidence_kind, status = "Evidência histórica reaproveitada", "Evidência reaproveitada"
            source_audit = analysis.html_audit_run_id
            reuse_reference = reuse.source_oai_audit_run_id
        elif analysis is not None:
            evidence_kind, status = "Tentativa direta", "Não verificada"
            source_audit = analysis.html_audit_run_id
        elif item.observation.record_status != "active" or not item.public_urls:
            evidence_kind, status = "Sem evidência HTML aplicável", "Não elegível"
        else:
            evidence_kind, status = "Sem evidência HTML", "Não processada"
        rows.append((
            item.article.oai_identifier, _diagnostic_title(item), "; ".join(item.public_urls), status,
            evidence_kind, analysis.checked_at if analysis else "", analysis.source_url if analysis else "",
            analysis.final_url if analysis else "", analysis.http_status if analysis else "",
            _finding_labels(analysis.findings_json) if analysis else "", analysis.confirmed_problem_count if analysis else 0,
            analysis.potential_problem_count if analysis else 0, analysis.unverified_count if analysis else 0,
            source_audit, reuse_reference, reference.audit_run.id,
        ))
    return OjsSeoExportTable("html", "Diagnóstico HTML", _html_columns(), tuple(rows))


def _html_columns() -> tuple[str, ...]:
    return (
        "Identificador OAI-PMH", "Publicação", "URL bibliográfica", "Situação da verificação",
        "Origem da evidência", "Data da evidência", "URL analisada", "URL final", "HTTP",
        "Elementos/problemas HTML", "Problemas confirmados", "Problemas potenciais", "Itens não verificados",
        "Execução HTML de origem", "Referência OAI de origem da reutilização", "Referência OAI atual",
    )


def _selected_html_evidence(
    journal: OjsJournal, reference: OjsDiagnosticReference,
) -> tuple[dict[str, tuple[OjsArticleHtmlAnalysis, None]], dict[str, tuple[OjsArticleHtmlAnalysis, OjsHtmlEvidenceReuse]]]:
    """Seleciona evidências com a mesma precedência usada nas recomendações."""
    direct_rows = db.session.scalars(
        select(OjsArticleHtmlAnalysis)
        .join(OjsHtmlAuditRun, OjsHtmlAuditRun.id == OjsArticleHtmlAnalysis.html_audit_run_id)
        .where(
            OjsArticleHtmlAnalysis.journal_id == journal.id,
            OjsHtmlAuditRun.journal_id == journal.id,
            OjsHtmlAuditRun.source_oai_audit_run_id == reference.audit_run.id,
            OjsHtmlAuditRun.status.in_(("completed", "partial")),
        )
        .order_by(OjsArticleHtmlAnalysis.checked_at.desc(), OjsArticleHtmlAnalysis.id.desc())
    ).all()
    direct: dict[str, tuple[OjsArticleHtmlAnalysis, None]] = {}
    fallback: dict[str, tuple[OjsArticleHtmlAnalysis, None]] = {}
    for analysis in direct_rows:
        if analysis.status == "analyzed":
            direct.setdefault(analysis.article_id, (analysis, None))
        elif analysis.status in {"not_verified", "skipped"}:
            fallback.setdefault(analysis.article_id, (analysis, None))
    for article_id, item in fallback.items():
        direct.setdefault(article_id, item)

    reused: dict[str, tuple[OjsArticleHtmlAnalysis, OjsHtmlEvidenceReuse]] = {}
    if html_evidence_reuse_available():
        reuse_rows = db.session.execute(
            select(OjsHtmlEvidenceReuse, OjsArticleHtmlAnalysis)
            .join(OjsArticleHtmlAnalysis, OjsArticleHtmlAnalysis.id == OjsHtmlEvidenceReuse.source_html_analysis_id)
            .where(
                OjsHtmlEvidenceReuse.journal_id == journal.id,
                OjsHtmlEvidenceReuse.target_oai_audit_run_id == reference.audit_run.id,
                OjsHtmlEvidenceReuse.target_observation_id.is_not(None),
                OjsArticleHtmlAnalysis.journal_id == journal.id,
                OjsArticleHtmlAnalysis.status == "analyzed",
            )
            .order_by(OjsHtmlEvidenceReuse.reused_at.desc(), OjsArticleHtmlAnalysis.checked_at.desc())
        ).all()
        for reuse, analysis in reuse_rows:
            reused.setdefault(reuse.article_id, (analysis, reuse))
    return direct, reused


def _comparison_table(recommendations: Sequence[OjsRecommendation]) -> OjsSeoExportTable:
    comparison_codes = {
        "title_oai_html_divergence": "title",
        "authors_oai_html_divergence": "authors",
        "publication_date_oai_html_divergence": "publication_date",
        "doi_oai_html_divergence": "doi",
        "journal_oai_html_divergence": "journal",
    }
    analysis_ids = {
        article.analysis_id
        for recommendation in recommendations if recommendation.nature == "comparison"
        for article in recommendation.articles if article.analysis_id
    }
    analyses = {
        analysis.id: analysis
        for analysis in db.session.scalars(select(OjsArticleHtmlAnalysis).where(
            OjsArticleHtmlAnalysis.id.in_(analysis_ids),
        )).all()
    } if analysis_ids else {}
    rows: list[tuple[object, ...]] = []
    for recommendation in recommendations:
        if recommendation.nature != "comparison":
            continue
        for article in recommendation.articles:
            oai_value, html_value = _comparison_values(
                analyses.get(article.analysis_id), comparison_codes.get(recommendation.code),
            )
            rows.append((
                article.title, article.oai_identifier, recommendation.label, oai_value,
                html_value, CERTAINTY_LABELS[article.classification],
                SEVERITY_LABELS[article.severity], article.evidence, article.evidence_date,
                article.source_audit_id, "Evidência histórica reaproveitada" if article.evidence_reused else "Verificação direta",
            ))
    return OjsSeoExportTable(
        "comparison", "Comparação OAI × HTML",
        ("Publicação", "Identificador OAI-PMH", "Campo/comparação", "Valor OAI-PMH", "Valor HTML", "Situação da evidência", "Gravidade", "Evidência registrada", "Data de referência", "Referência bibliográfica", "Origem HTML"),
        tuple(rows),
    )


def _comparison_values(analysis: OjsArticleHtmlAnalysis | None, field: str | None) -> tuple[str, str]:
    if analysis is None or not field or not isinstance(analysis.facts_json, dict):
        return "Indisponível na evidência persistida", "Indisponível na evidência persistida"
    consistency = analysis.facts_json.get("oai_html_consistency")
    comparisons = consistency.get("comparisons") if isinstance(consistency, dict) else None
    if not isinstance(comparisons, list):
        return "Indisponível na evidência persistida", "Indisponível na evidência persistida"
    row = next((item for item in comparisons if isinstance(item, dict) and item.get("field") == field), None)
    if row is None:
        return "Indisponível na evidência persistida", "Indisponível na evidência persistida"
    def values(key: str) -> str:
        source = row.get(key)
        return "; ".join(value for value in source if isinstance(value, str)) if isinstance(source, list) else ""
    return values("oai_values") or "Indisponível na evidência persistida", values("html_values") or "Indisponível na evidência persistida"


def _recommendations_table(
    journal: OjsJournal,
    reference: OjsDiagnosticReference | None,
    recommendations: Sequence[OjsRecommendation],
) -> OjsSeoExportTable:
    rows: list[tuple[object, ...]] = []
    for recommendation in recommendations:
        for article in recommendation.articles:
            rows.append((
                journal.repository_name, article.title, article.oai_identifier, article.source_url or "",
                recommendation.label, NATURE_LABELS[recommendation.nature], SEVERITY_LABELS[article.severity],
                PRIORITY_LABELS[recommendation.priority], CERTAINTY_LABELS[article.classification],
                APPLICABILITY_LABELS[article.applicability], article.evidence, recommendation.intervention,
                "; ".join(ROLE_LABELS[role] for role in recommendation.roles), article.source_audit_id,
                article.html_audit_id or "", article.evidence_date,
                "Evidência HTML reaproveitada" if article.evidence_reused else "Evidência da referência atual",
                recommendation.occurrence_count, recommendation.article_count,
            ))
    return OjsSeoExportTable(
        "recommendations", "Problemas e recomendações",
        ("Periódico", "Publicação", "Identificador OAI-PMH", "URL pública", "Tipo de problema", "Origem", "Gravidade", "Prioridade", "Situação da evidência", "Aplicabilidade", "Descrição objetiva/evidência", "Orientação de correção", "Responsáveis sugeridos", "Referência bibliográfica", "Execução HTML", "Data da evidência", "Origem da evidência HTML", "Ocorrências consolidadas do tipo", "Publicações afetadas pelo tipo"),
        tuple(rows),
    )


def _affected_articles_table(
    reference: OjsDiagnosticReference | None,
    recommendations: Sequence[OjsRecommendation],
    diagnostics: Sequence[OjsArticleDiagnostic],
) -> OjsSeoExportTable:
    diagnostic_by_article = {item.article.id: item for item in diagnostics}
    grouped: dict[str, dict[str, Any]] = {}
    for recommendation in recommendations:
        for article in recommendation.articles:
            row = grouped.setdefault(article.article_id, {
                "identifier": article.oai_identifier, "title": article.title, "url": article.source_url or "",
                "confirmed": 0, "potential": 0, "severity": article.severity, "priority": recommendation.priority,
                "reused": False, "direct": False, "date": article.evidence_date,
            })
            row["confirmed" if article.classification == "confirmed" else "potential"] += 1
            if _SEVERITY_ORDER.get(article.severity, 0) > _SEVERITY_ORDER.get(row["severity"], 0):
                row["severity"] = article.severity
            if _PRIORITY_ORDER.get(recommendation.priority, 0) > _PRIORITY_ORDER.get(row["priority"], 0):
                row["priority"] = recommendation.priority
            row["reused"] = row["reused"] or article.evidence_reused
            row["direct"] = row["direct"] or bool(article.analysis_id and not article.evidence_reused)
            row["date"] = max(row["date"], article.evidence_date)
    rows = []
    for article_id, item in sorted(
        grouped.items(), key=lambda pair: (str(pair[1]["title"]).casefold(), pair[1]["identifier"]),
    ):
        diagnostic = diagnostic_by_article.get(article_id)
        fields = {field.code: field for field in diagnostic.fields} if diagnostic else {}
        creators = "; ".join(fields["creator"].values) if fields.get("creator") else ""
        doi = diagnostic.observation.doi_observed if diagnostic else ""
        public_url = item["url"] or ("; ".join(diagnostic.public_urls) if diagnostic else "")
        html_status = (
            "Evidência HTML reaproveitada" if item["reused"] else "Verificada diretamente" if item["direct"]
            else "Sem evidência HTML aplicável para esta recomendação"
        )
        rows.append((
            item["identifier"], item["title"], creators, doi or "", public_url, item["confirmed"], item["potential"],
            SEVERITY_LABELS.get(item["severity"], item["severity"]), PRIORITY_LABELS.get(item["priority"], item["priority"]),
            html_status, item["date"], reference.audit_run.id if reference else "Indisponível",
        ))
    return OjsSeoExportTable(
        "articles", "Publicações afetadas",
        ("Identificador OAI-PMH", "Título", "Autoria", "DOI", "URL pública", "Problemas confirmados", "Possíveis problemas", "Maior gravidade", "Maior prioridade", "Situação da verificação HTML", "Data de referência", "Referência bibliográfica"),
        tuple(rows),
    )


def _history_table(journal: OjsJournal) -> OjsSeoExportTable:
    rows: list[tuple[object, ...]] = []
    audit_runs = db.session.scalars(select(OjsSeoAuditRun).where(
        OjsSeoAuditRun.journal_id == journal.id,
    ).order_by(OjsSeoAuditRun.created_at.desc(), OjsSeoAuditRun.id.desc())).all()
    for run in audit_runs:
        rows.append((
            "Auditoria bibliográfica", run.id, _AUDIT_STATUS_LABELS.get(run.status, run.status), run.created_at,
            run.started_at, run.completed_at, run.record_count, max(0, run.record_count - run.deleted_record_count),
            run.deleted_record_count, "", "", "", "", "", "", "", "", "", "",
            run.completion_reason or "",
        ))
    campaigns = db.session.scalars(select(OjsHtmlAuditCampaign).where(
        OjsHtmlAuditCampaign.journal_id == journal.id,
    ).order_by(OjsHtmlAuditCampaign.created_at.desc(), OjsHtmlAuditCampaign.id.desc())).all()
    for campaign in campaigns:
        rows.append((
            "Campanha HTML", campaign.id, _CAMPAIGN_STATUS_LABELS.get(campaign.status, campaign.status), campaign.created_at,
            campaign.started_at, campaign.completed_at, "", "", "", campaign.source_oai_audit_run_id,
            campaign.batch_count, "", "", "", "", "", "", "", "", campaign.id, campaign.completion_reason or "",
        ))
    html_runs = db.session.scalars(select(OjsHtmlAuditRun).where(
        OjsHtmlAuditRun.journal_id == journal.id,
    ).order_by(OjsHtmlAuditRun.created_at.desc(), OjsHtmlAuditRun.id.desc())).all()
    for run in html_runs:
        metrics = run.metrics_json if isinstance(run.metrics_json, dict) else {}
        verified = metrics.get("verified_pages")
        if not isinstance(verified, int) or isinstance(verified, bool) or verified < 0:
            verified = max(0, run.analyzed_page_count - run.unverified_page_count)
        direct_coverage = round(verified * 100 / run.eligible_article_count, 2) if run.eligible_article_count else ""
        rows.append((
            "Execução HTML", run.id, _HTML_STATUS_LABELS.get(run.status, run.status), run.created_at,
            run.started_at, run.completed_at, "", "", "", run.source_oai_audit_run_id,
            "", run.eligible_article_count, run.analyzed_page_count,
            verified, run.unverified_page_count,
            "Indisponível — não persistido pela execução", direct_coverage,
            run.confirmed_problem_count, run.potential_problem_count,
            run.campaign_id or "", run.completion_reason or "",
        ))
    operations = db.session.scalars(select(OjsSeoAnalysisOperation).where(
        OjsSeoAnalysisOperation.journal_id == journal.id,
    ).order_by(OjsSeoAnalysisOperation.created_at.desc(), OjsSeoAnalysisOperation.id.desc())).all()
    for operation in operations:
        rows.append((
            "Operação integrada", operation.id, _OPERATION_STATUS_LABELS.get(operation.status, operation.status), operation.created_at,
            operation.started_at, operation.completed_at, "", "", "", operation.oai_audit_run_id or "",
            "", "", "", "", "", "", "", "", "", operation.html_campaign_id or "", operation.completion_reason or "",
        ))
    rows.sort(key=lambda row: (row[3] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return OjsSeoExportTable(
        "history", "Histórico de análises",
        ("Tipo", "Identificador da análise", "Situação", "Solicitada em", "Iniciada em", "Concluída em", "Registros considerados", "Publicações ativas", "Registros excluídos", "Referência OAI-PMH", "Lotes HTML", "Páginas elegíveis", "Páginas processadas", "Verificadas diretamente", "Não verificadas", "Evidências reaproveitadas", "Cobertura direta (%)", "Problemas confirmados", "Problemas potenciais", "Campanha HTML vinculada", "Conclusão/falha disponível"),
        tuple(rows),
    )


def _diagnostic_title(diagnostic: OjsArticleDiagnostic) -> str:
    title = next(field for field in diagnostic.fields if field.code == "title")
    return "; ".join(title.values) if title.values else diagnostic.article.oai_identifier


def _finding_labels(findings: object) -> str:
    if not isinstance(findings, list):
        return ""
    labels: list[str] = []
    for finding in findings:
        code = finding.get("code") if isinstance(finding, dict) else None
        if isinstance(code, str):
            label = html_finding_label(code)
            if label not in labels:
                labels.append(label)
    return "; ".join(labels)


def _render_csv(table: OjsSeoExportTable) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([_safe_spreadsheet_value(column) for column in table.columns])
    for row in table.rows:
        writer.writerow([_safe_spreadsheet_value(_display_value(value)) for value in row])
    return output.getvalue().encode("utf-8-sig")


def _render_xlsx(bundle: OjsSeoExportBundle) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for table in bundle.tables:
        worksheet = workbook.create_sheet(_sheet_title(table.title, workbook.sheetnames))
        worksheet.append([_safe_xlsx_value(column) for column in table.columns])
        for row in table.rows:
            worksheet.append([_safe_xlsx_value(value) for value in row])
        _style_worksheet(worksheet, len(table.columns))
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _style_worksheet(worksheet, column_count: int) -> None:
    header_fill = PatternFill("solid", fgColor="17365D")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", wrap_text=False)
    worksheet.row_dimensions[1].height = 15
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    for column_index in range(1, column_count + 1):
        values = [str(worksheet.cell(row=row, column=column_index).value or "") for row in range(1, min(worksheet.max_row, 200) + 1)]
        width = min(52, max(12, max((len(value) for value in values), default=12) + 2))
        worksheet.column_dimensions[get_column_letter(column_index)].width = width
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=False)
            if isinstance(cell.value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm"
    for row_index in range(2, worksheet.max_row + 1):
        worksheet.row_dimensions[row_index].height = 15
    for row in range(2, worksheet.max_row + 1):
        for column_index in range(1, column_count + 1):
            header = worksheet.cell(row=1, column=column_index).value
            cell = worksheet.cell(row=row, column=column_index)
            if header and "URL" in str(header) and isinstance(cell.value, str) and cell.value.startswith("https://"):
                cell.hyperlink = cell.value
                cell.style = "Hyperlink"


def _safe_spreadsheet_value(value: object) -> object:
    if isinstance(value, datetime):
        # XLSX não admite datetimes com fuso; preservamos o instante em UTC e
        # mantemos o tipo de data para filtros e ordenação em planilhas.
        instant = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return instant.astimezone(timezone.utc).replace(tzinfo=None)
    if not isinstance(value, str):
        return value
    # Fórmulas só podem ser introduzidas por valores textuais persistidos. Um
    # apóstrofo instrui Excel/LibreOffice a mantê-los como texto literal.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _safe_xlsx_value(value: object) -> object:
    """Prepara somente a célula XLSX sem alterar CSV nem o dado persistido."""
    if isinstance(value, str):
        # O Excel não deve receber quebras internas: elas forçam alturas de
        # linha artificiais mesmo quando a quebra automática está desligada.
        value = re.sub(r"[\r\n]+", " ", value)
    return _safe_spreadsheet_value(value)


def _display_value(value: object) -> object:
    if isinstance(value, datetime):
        instant = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return instant.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return value


def _download_name(bundle: OjsSeoExportBundle, dataset: str, extension: str) -> str:
    label = _slug(DATASET_LABELS[dataset])
    journal = _slug(bundle.journal.repository_name) or "periodico"
    date = bundle.generated_at.astimezone(timezone.utc).date().isoformat()
    return f"analysis_ojsseo_{label}_{journal}_{date}.{extension}"


def _slug(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60]


def _sheet_title(title: str, existing: Iterable[str]) -> str:
    base = re.sub(r"[\\\\/:*?\[\]]", " ", title).strip()[:31] or "Dados"
    names = set(existing)
    candidate, suffix = base, 2
    while candidate in names:
        postfix = f" {suffix}"
        candidate = base[:31 - len(postfix)] + postfix
        suffix += 1
    return candidate


__all__ = [
    "DATASET_LABELS", "OjsSeoExportBundle", "OjsSeoExportError", "OjsSeoExportFile", "OjsSeoExportTable",
    "build_ojs_seo_export", "render_ojs_seo_export",
]
