"""Recomendações editoriais consolidadas para um periódico OJS.

Esta camada é deliberadamente somente de leitura. Ela agrega uma única
referência OAI-PMH e a evidência HTML útil mais recente de cada artigo, sem
reescrever observações, resultados técnicos ou versões históricas de critérios.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Iterable, Literal, Mapping
import unicodedata

from sqlalchemy import select

from .extensions import db
from .models import (
    OjsArticle,
    OjsArticleHtmlAnalysis,
    OjsArticleObservation,
    OjsHtmlEvidenceReuse,
    OjsHtmlAuditRun,
    OjsJournal,
    User,
)
from .ojs_html_client import html_finding_label
from .ojs_seo_diagnostics import DIAGNOSTIC_FIELDS, OjsDiagnosticReference, resolve_ojs_diagnostic_reference
from .ojs_seo_services import OjsSeoNotFoundError, OjsSeoServiceError, get_ojs_journal
from .ojs_seo_technical import HtmlCoverage, get_ojs_html_coverage, html_evidence_reuse_available
from .pagination import PlatformPage, paginate_sequence


RecommendationPriority = Literal["high", "medium", "low", "review"]
RecommendationCertainty = Literal["confirmed", "potential", "unverified"]
RecommendationApplicability = Literal["applicable", "context_dependent", "not_applicable", "not_verified"]

PRIORITY_LABELS = {
    "high": "Alta",
    "medium": "Média",
    "low": "Baixa",
    "review": "Exige verificação",
}
CERTAINTY_LABELS = {
    "confirmed": "Confirmado",
    "potential": "Potencial",
    "unverified": "Não verificado",
}
APPLICABILITY_LABELS = {
    "applicable": "Aplicável ao tipo documental informado",
    "context_dependent": "Exige conferência editorial",
    "not_applicable": "Não aplicável",
    "not_verified": "Não verificado",
}
SEVERITY_LABELS = {
    "critical": "Crítica",
    "high": "Alta",
    "medium": "Média",
    "low": "Baixa",
    "informative": "Informativa",
}
NATURE_LABELS = {
    "bibliographic": "Bibliográfico OAI-PMH/Dublin Core",
    "technical": "SEO técnico HTML",
    "comparison": "Comparação OAI-PMH × HTML",
}
ROLE_LABELS = {
    "editorial": "Equipe editorial",
    "technical": "Administração técnica do OJS",
    "infrastructure": "Responsáveis pela infraestrutura",
}
_SEVERITY_ORDER = {"informative": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_CERTAINTY_ORDER = {"unverified": 0, "potential": 1, "confirmed": 2}
_COMPARISON_CODES = frozenset({
    "title_oai_html_divergence", "authors_oai_html_divergence",
    "publication_date_oai_html_divergence", "doi_oai_html_divergence",
    "journal_oai_html_divergence",
})
_POSSIBLY_GLOBAL_CODES = frozenset({
    "html_title_missing", "html_title_multiple", "meta_description_missing", "h1_missing",
    "canonical_missing", "canonical_multiple", "canonical_differs", "noindex_observed",
    "json_ld_invalid", "citation_title_missing", "citation_author_missing",
    "citation_journal_title_missing", "citation_publication_date_missing", "citation_doi_missing",
    "oai_description_missing", "oai_subject_missing", "oai_language_missing",
    "oai_type_missing", "oai_identifier_missing",
})

# A classificação usa apenas valores explicitamente informados em ``dc:type``.
# Não há inferência por título, URL ou outra heurística editorial. Valores
# genéricos, múltiplos ou desconhecidos permanecem em conferência humana.
_ARTICLE_DOCUMENT_TYPES = frozenset({
    "article", "artigo", "research article", "original article", "review article",
    "artigo original", "artigo de revisao", "case report", "relato de caso",
    "info eu repo semantics article",
})
_CONTEXTUAL_DOCUMENT_TYPES = frozenset({
    "editorial", "foreword", "preface", "presentation", "apresentacao",
    "dossier presentation", "apresentacao de dossie", "nota editorial",
    "nota dos organizadores", "editors note", "editor note",
    "info eu repo semantics editorial",
})
_FRIENDLY_MISSING_LABELS = {
    "title": "Publicações sem título identificado",
    "creator": "Publicações sem autores identificados",
    "description": "Publicações sem resumo ou descrição identificada",
    "language": "Publicações sem idioma identificado",
    "date": "Publicações sem data identificada",
    "identifier": "Publicações sem identificador bibliográfico",
}

_OAI_RECOMMENDATIONS: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "title": (
        "A ausência pode dificultar a identificação e a apresentação do registro em serviços que consultam Dublin Core.",
        "Verifique o preenchimento e a exposição do título do artigo no OJS.",
        ("editorial",),
        "Solicite nova auditoria bibliográfica e confirme a presença de dc:title.",
    ),
    "creator": (
        "A ausência pode reduzir a atribuição bibliográfica e a desambiguação de autoria em serviços que consultam Dublin Core.",
        "Revise o cadastro e a exposição dos autores no OJS.",
        ("editorial",),
        "Solicite nova auditoria bibliográfica e confirme a presença de dc:creator.",
    ),
    "description": (
        "A ausência pode reduzir o contexto bibliográfico disponível em serviços que utilizam a descrição Dublin Core; não comprova ausência do resumo no HTML público.",
        "Verifique se o resumo está preenchido e corretamente exposto nos metadados do artigo no OJS.",
        ("editorial",),
        "Solicite nova auditoria bibliográfica e confirme a presença de dc:description.",
    ),
    "subject": (
        "A ausência pode limitar a recuperação temática em serviços que utilizam dc:subject; ela não comprova perda de indexação nem ausência de palavras-chave em outras fontes.",
        "Revise o cadastro e a exposição das palavras-chave no OJS.",
        ("editorial",),
        "Solicite nova auditoria bibliográfica e confirme a presença de dc:subject.",
    ),
    "language": (
        "A ausência pode limitar filtros e a contextualização linguística em serviços que consultam Dublin Core.",
        "Verifique o preenchimento e a exposição do idioma bibliográfico no OJS.",
        ("editorial",),
        "Solicite nova auditoria bibliográfica e confirme a presença de dc:language.",
    ),
    "date": (
        "A ausência pode dificultar a ordenação e a contextualização cronológica do registro em serviços que consultam Dublin Core.",
        "Verifique o preenchimento e a exposição da data bibliográfica no OJS.",
        ("editorial",),
        "Solicite nova auditoria bibliográfica e confirme a presença de dc:date.",
    ),
    "type": (
        "A ausência pode dificultar a classificação documental e a avaliação de aplicabilidade de outros diagnósticos; ela não comprova, por si só, uma deficiência editorial do conteúdo.",
        "Verifique o preenchimento e a exposição do tipo documental no OJS.",
        ("editorial",),
        "Solicite nova auditoria bibliográfica e confirme a presença de dc:type.",
    ),
    "identifier": (
        "A ausência pode dificultar o vínculo e a identificação persistente do registro em serviços que dependem de identificadores Dublin Core.",
        "Verifique os identificadores expostos pelo OJS para este registro.",
        ("editorial",),
        "Solicite nova auditoria bibliográfica e confirme a presença de dc:identifier.",
    ),
    "doi": (
        "A ausência de DOI identificado pode dificultar vínculos persistentes em serviços que dependem desse identificador, mas não prova que o artigo não possua DOI em outra fonte.",
        "Se o artigo possuir DOI, confirme se ele está informado e exposto nos metadados do OJS.",
        ("editorial",),
        "Confira o DOI na fonte editorial e execute nova auditoria; a ausência no OAI-PMH não prova que o artigo não possua DOI.",
    ),
}


@dataclass(frozen=True, slots=True)
class RecommendationArticle:
    article_id: str
    oai_identifier: str
    title: str
    source_audit_id: str
    analysis_id: str | None
    html_audit_id: str | None
    evidence_date: datetime
    criteria_version: str | None
    evidence: str
    classification: RecommendationCertainty
    severity: str
    applicability: str
    source_url: str | None
    evidence_reused: bool = False


@dataclass(frozen=True, slots=True)
class _SelectedHtmlEvidence:
    analysis: OjsArticleHtmlAnalysis
    reused: bool


@dataclass(frozen=True, slots=True)
class OjsRecommendation:
    code: str
    label: str
    nature: str
    source_label: str
    article_count: int
    occurrence_count: int
    confirmed_article_count: int
    potential_article_count: int
    unverified_article_count: int
    certainty: RecommendationCertainty
    severity: str
    applicability: str
    priority: RecommendationPriority
    priority_reason: str
    scope: str
    roles: tuple[str, ...]
    impact: str
    intervention: str
    verification: str
    historical_criteria: bool
    articles: tuple[RecommendationArticle, ...]


@dataclass(frozen=True, slots=True)
class OjsRecommendationIndicators:
    problem_types: int
    confirmed_articles: int
    potential_articles: int
    high_priority_recommendations: int
    review_recommendations: int
    coverage: HtmlCoverage | None


@dataclass(frozen=True, slots=True)
class OjsRecommendationListing:
    journal: OjsJournal
    reference: OjsDiagnosticReference | None
    recommendations: PlatformPage[OjsRecommendation]
    indicators: OjsRecommendationIndicators
    selected_priority: str
    selected_severity: str
    selected_certainty: str
    selected_nature: str
    selected_role: str


@dataclass(frozen=True, slots=True)
class OjsRecommendationSnapshot:
    """Conjunto completo e somente de leitura de uma referência OAI-PMH.

    Listagens paginadas e o dashboard compartilham esta origem; assim os
    indicadores e as barras visuais não executam uma segunda interpretação
    das mesmas evidências.
    """

    journal: OjsJournal
    reference: OjsDiagnosticReference | None
    recommendations: tuple[OjsRecommendation, ...]
    indicators: OjsRecommendationIndicators


@dataclass(frozen=True, slots=True)
class OjsRecommendationDetail:
    journal: OjsJournal
    reference: OjsDiagnosticReference
    recommendation: OjsRecommendation
    articles: PlatformPage[RecommendationArticle]


class OjsRecommendationFilterError(ValueError):
    """Filtro de recomendações fora da lista de opções conhecidas."""


def _filter(value: object, options: Iterable[str], name: str) -> str:
    result = value if isinstance(value, str) else "all"
    if result not in {"all", *options}:
        raise OjsRecommendationFilterError(f"Filtro de {name} inválido.")
    return result


def _safe_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _oai_fields(observation: OjsArticleObservation) -> dict[str, tuple[str, ...] | None]:
    payload = observation.dublin_core_json if isinstance(observation.dublin_core_json, dict) else {}
    result: dict[str, tuple[str, ...] | None] = {}
    for field, _label in DIAGNOSTIC_FIELDS:
        raw = payload.get(field)
        result[field] = _safe_list(raw) if isinstance(raw, list) else None
    return result


def _normalized_document_type(value: str) -> str:
    """Normaliza somente a grafia de valores explícitos de ``dc:type``."""
    normalized = "".join(
        character for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    ).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", normalized)).strip()


def _document_type_label(values: tuple[str, ...] | None) -> str:
    if values is None:
        return "não verificado"
    if not values:
        return "ausente"
    return "; ".join(values[:3]) + ("; …" if len(values) > 3 else "")


def _subject_missing_context(
    document_types: tuple[str, ...] | None,
) -> tuple[str, str, RecommendationApplicability, str, str]:
    """Classifica a aplicabilidade de ``dc:subject`` sem adivinhar o tipo.

    A evidência da ausência é sempre a mesma. Somente a prioridade editorial
    muda quando o próprio registro declara, de modo inequívoco, um tipo de
    artigo científico. Tipos explicitamente editoriais, genéricos, múltiplos
    ou desconhecidos continuam como conferência humana.
    """
    label = _document_type_label(document_types)
    normalized = {_normalized_document_type(value) for value in document_types or ()}
    if normalized and normalized.issubset(_ARTICLE_DOCUMENT_TYPES):
        return (
            "oai_subject_missing",
            "Publicações sem assuntos identificados",
            "applicable",
            "medium",
            f"O dc:type informado foi {label}; a aplicabilidade de assuntos é tratada como pertinente a esse tipo documental.",
        )
    if normalized and normalized.issubset(_CONTEXTUAL_DOCUMENT_TYPES):
        reason = f"O dc:type informado foi {label}; a relevância de assuntos para esse tipo depende da política editorial."
    else:
        reason = (
            f"O dc:type está {label}; não há base documental suficiente para determinar a aplicabilidade de assuntos."
        )
    return (
        "oai_subject_missing_needs_review",
        "Publicações sem assuntos identificados — verificar necessidade de correção",
        "context_dependent",
        "low",
        f"{reason} Confira antes de priorizar uma intervenção.",
    )


def _bibliographic_missing_context(
    field: str, document_types: tuple[str, ...] | None,
) -> tuple[str, str, RecommendationApplicability, str, str]:
    """Retorna o agrupamento e a aplicabilidade de uma ausência Dublin Core."""
    if field == "subject":
        return _subject_missing_context(document_types)
    if field == "type":
        return (
            "oai_type_missing",
            "Tipo de publicação não identificado",
            "context_dependent",
            "low",
            "Sem dc:type, a aplicabilidade editorial dos diagnósticos dependentes de tipo documental precisa de conferência humana.",
        )
    label = _FRIENDLY_MISSING_LABELS.get(field, f"{dict(DIAGNOSTIC_FIELDS)[field]} não identificado")
    return (
        f"oai_{field}_missing",
        label,
        "applicable",
        "medium",
        "A ausência foi confirmada exclusivamente no registro Dublin Core desta auditoria.",
    )


def _title(article: OjsArticle, fields: Mapping[str, tuple[str, ...] | None]) -> str:
    values = fields.get("title")
    if values:
        return values[0]
    return article.title or article.oai_identifier


def _roles_from_audience(value: object, *, default: tuple[str, ...]) -> tuple[str, ...]:
    text = value.casefold() if isinstance(value, str) else ""
    roles: list[str] = []
    if "editorial" in text:
        roles.append("editorial")
    if "administra" in text or "técnic" in text or "tecnic" in text:
        roles.append("technical")
    if "infraestrutura" in text:
        roles.append("infrastructure")
    return tuple(dict.fromkeys(roles)) or default


def _criteria_version(analysis: OjsArticleHtmlAnalysis) -> str | None:
    facts = analysis.facts_json if isinstance(analysis.facts_json, dict) else {}
    comparison = facts.get("oai_html_consistency")
    if not isinstance(comparison, dict):
        return None
    version = comparison.get("criteria_version")
    return version if isinstance(version, str) and version else None


def _selected_html_evidence(journal: OjsJournal, reference: OjsDiagnosticReference) -> dict[str, _SelectedHtmlEvidence]:
    """Escolhe a evidência direta ou o reaproveitamento histórico rastreado.

    ``not_verified`` posterior não apaga a evidência HTML anterior; o resultado
    útil é escolhido em uma consulta limitada à mesma referência OAI-PMH.
    """
    rows = db.session.scalars(
        select(OjsArticleHtmlAnalysis)
        .join(OjsHtmlAuditRun, OjsHtmlAuditRun.id == OjsArticleHtmlAnalysis.html_audit_run_id)
        .where(
            OjsArticleHtmlAnalysis.journal_id == journal.id,
            OjsArticleHtmlAnalysis.status == "analyzed",
            OjsHtmlAuditRun.journal_id == journal.id,
            OjsHtmlAuditRun.source_oai_audit_run_id == reference.audit_run.id,
            OjsHtmlAuditRun.status.in_(("completed", "partial")),
        )
        .order_by(OjsArticleHtmlAnalysis.checked_at.desc(), OjsArticleHtmlAnalysis.id.desc())
    ).all()
    selected: dict[str, _SelectedHtmlEvidence] = {}
    for analysis in rows:
        selected.setdefault(analysis.article_id, _SelectedHtmlEvidence(analysis, False))
    if not html_evidence_reuse_available():
        return selected
    reused_rows = db.session.execute(
        select(OjsHtmlEvidenceReuse, OjsArticleHtmlAnalysis)
        .join(OjsArticleHtmlAnalysis, OjsArticleHtmlAnalysis.id == OjsHtmlEvidenceReuse.source_html_analysis_id)
        .where(
            OjsHtmlEvidenceReuse.journal_id == journal.id,
            OjsHtmlEvidenceReuse.target_oai_audit_run_id == reference.audit_run.id,
            OjsArticleHtmlAnalysis.journal_id == journal.id,
            OjsArticleHtmlAnalysis.status == "analyzed",
        )
        .order_by(
            OjsHtmlEvidenceReuse.reused_at.desc(), OjsArticleHtmlAnalysis.checked_at.desc(),
            OjsArticleHtmlAnalysis.id.desc(),
        )
    ).all()
    for reuse, analysis in reused_rows:
        selected.setdefault(reuse.article_id, _SelectedHtmlEvidence(analysis, True))
    return selected


def _append_bucket(
    buckets: dict[str, dict],
    *,
    code: str,
    label: str,
    nature: str,
    source_label: str,
    article: RecommendationArticle,
    impact: str,
    intervention: str,
    verification: str,
    roles: tuple[str, ...],
    historical_criteria: bool = False,
) -> None:
    bucket = buckets.setdefault(code, {
        "code": code, "label": label, "nature": nature, "source_label": source_label,
        "articles": {}, "occurrence_count": 0, "impacts": [], "interventions": [],
        "verifications": [], "roles": [], "historical_criteria": False,
    })
    bucket["occurrence_count"] += 1
    bucket["articles"].setdefault(article.article_id, article)
    bucket["impacts"].append(impact)
    bucket["interventions"].append(intervention)
    bucket["verifications"].append(verification)
    bucket["roles"].extend(roles)
    bucket["historical_criteria"] = bucket["historical_criteria"] or historical_criteria


def _distinct_text(values: Iterable[str], fallback: str) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _priority(
    *, certainty: str, severity: str, applicability: str, article_count: int, code: str,
) -> tuple[RecommendationPriority, str, str]:
    global_candidate = code in _POSSIBLY_GLOBAL_CODES and article_count >= 3
    scope = "Possível intervenção global" if global_candidate else "Verificação individual"
    if certainty != "confirmed" or applicability in {"context_dependent", "not_verified", "not_applicable"}:
        contextual_scope = (
            "Aplicabilidade editorial a conferir"
            if code in {"oai_subject_missing_needs_review", "oai_type_missing"}
            else scope
        )
        return "review", "A evidência exige confirmação humana ou depende do contexto editorial.", contextual_scope
    if severity in {"critical", "high"}:
        return "high", "Problema confirmado com gravidade técnica ou editorial elevada.", scope
    if severity == "medium" and global_candidate:
        return "high", "Problema confirmado, de gravidade média e recorrente; pode exigir revisão de configuração compartilhada.", scope
    if severity == "medium":
        return "medium", "Problema confirmado que deve ser corrigido após os itens de prioridade alta.", scope
    return "low", "Achado confirmado de menor gravidade; acompanhe-o no contexto editorial do periódico.", scope


def _recommendations_for_reference(journal: OjsJournal, reference: OjsDiagnosticReference) -> tuple[OjsRecommendation, ...]:
    """Agrega observações e análises em consultas em lote, sem N+1."""
    rows = db.session.execute(
        select(OjsArticle, OjsArticleObservation)
        .join(OjsArticleObservation, OjsArticleObservation.article_id == OjsArticle.id)
        .where(
            OjsArticle.journal_id == journal.id,
            OjsArticleObservation.journal_id == journal.id,
            OjsArticleObservation.audit_run_id == reference.audit_run.id,
            OjsArticleObservation.record_status == "active",
        )
        .order_by(OjsArticle.oai_identifier.asc(), OjsArticle.id.asc())
    ).all()
    selected_html = _selected_html_evidence(journal, reference)
    buckets: dict[str, dict] = {}
    for article, observation in rows:
        fields = _oai_fields(observation)
        title = _title(article, fields)
        for field, label in DIAGNOSTIC_FIELDS:
            values = fields[field]
            if values is not None and not values:
                evidence = f"O registro Dublin Core desta auditoria não apresentou dc:{field}."
                impact, action, roles, verification = _OAI_RECOMMENDATIONS[field]
                code, recommendation_label, applicability, severity, context_note = _bibliographic_missing_context(
                    field, fields["type"],
                )
                if context_note:
                    evidence = f"{evidence} {context_note}"
                occurrence = RecommendationArticle(
                    article.id, article.oai_identifier, title, reference.audit_run.id, None, None,
                    observation.observed_at, None, evidence, "confirmed", severity, applicability, None,
                )
                _append_bucket(
                    buckets, code=code, label=recommendation_label,
                    nature="bibliographic", source_label=NATURE_LABELS["bibliographic"], article=occurrence,
                    impact=impact, intervention=action, verification=verification, roles=roles,
                )
        identifier = fields["identifier"]
        doi = observation.doi_observed.strip() if isinstance(observation.doi_observed, str) else ""
        if not doi and identifier is not None:
            impact, action, roles, verification = _OAI_RECOMMENDATIONS["doi"]
            occurrence = RecommendationArticle(
                article.id, article.oai_identifier, title, reference.audit_run.id, None, None,
                observation.observed_at, None,
                "O material OAI-PMH desta auditoria não permitiu identificar um DOI.",
                "potential", "low", "context_dependent", None,
            )
            _append_bucket(
                buckets, code="oai_doi_not_identified", label="DOI não identificado nos dados bibliográficos",
                nature="bibliographic", source_label=NATURE_LABELS["bibliographic"], article=occurrence,
                impact=impact, intervention=action, verification=verification, roles=roles,
            )

        selected_evidence = selected_html.get(article.id)
        if selected_evidence is None or not isinstance(selected_evidence.analysis.findings_json, list):
            continue
        analysis = selected_evidence.analysis
        version = _criteria_version(analysis)
        for finding in analysis.findings_json:
            if not isinstance(finding, Mapping) or not isinstance(finding.get("code"), str):
                continue
            code = finding["code"]
            classification = finding.get("classification") if finding.get("classification") in CERTAINTY_LABELS else "unverified"
            severity = finding.get("severity") if finding.get("severity") in SEVERITY_LABELS else "informative"
            applicability = finding.get("applicability") if finding.get("applicability") in {
                "applicable", "context_dependent", "not_applicable", "not_verified",
            } else "not_verified"
            # A ausência de versão também é uma evidência histórica cujo
            # critério de comparação não pode ser atribuído à versão atual.
            # Isso preserva o registro original, mas evita apresentá-lo como
            # confirmação contemporânea sem nova verificação.
            historical = code in _COMPARISON_CODES and version != "ojs-html-comparison-v3"
            evidence = finding.get("evidence") if isinstance(finding.get("evidence"), str) else "Evidência técnica registrada."
            if historical:
                classification = "potential"
                applicability = "context_dependent"
                version_label = version or "uma versão de critérios não informada"
                evidence = f"{evidence} Esta comparação histórica usa {version_label}; confirme-a pelos critérios atuais antes de tratá-la como problema comprovado."
            if selected_evidence.reused:
                evidence = (
                    f"Evidência HTML reaproveitada de uma verificação compatível em "
                    f"{analysis.checked_at.isoformat()}. {evidence}"
                )
            nature = "comparison" if code in _COMPARISON_CODES else "technical"
            occurrence = RecommendationArticle(
                article.id, article.oai_identifier, title, reference.audit_run.id, analysis.id,
                analysis.html_audit_run_id, analysis.checked_at, version, evidence, classification,
                severity, applicability, analysis.final_url or analysis.source_url, selected_evidence.reused,
            )
            _append_bucket(
                buckets, code=code, label=html_finding_label(code), nature=nature,
                source_label=NATURE_LABELS[nature], article=occurrence,
                impact=(finding.get("impact") if isinstance(finding.get("impact"), str) else "O impacto depende do contexto editorial e técnico da página."),
                intervention=(finding.get("recommendation") if isinstance(finding.get("recommendation"), str) else "Revise a evidência técnica antes de definir uma intervenção."),
                verification="Execute nova análise HTML e compare a evidência registrada com a resposta atual da página.",
                roles=_roles_from_audience(finding.get("audience"), default=("technical",)),
                historical_criteria=historical,
            )

    recommendations: list[OjsRecommendation] = []
    for bucket in buckets.values():
        articles = tuple(sorted(bucket["articles"].values(), key=lambda item: (item.oai_identifier, item.article_id)))
        certainty = max((item.classification for item in articles), key=lambda item: _CERTAINTY_ORDER[item])
        # Uma evidência potencial não pode elevar a prioridade de uma evidência
        # confirmada do mesmo grupo. Quando há confirmação, a gravidade exibida
        # e usada na prioridade é a maior entre os artigos efetivamente
        # confirmados; sem confirmação, usa-se a maior gravidade disponível.
        severity_pool = [item.severity for item in articles if item.classification == "confirmed"]
        severity = max(severity_pool or [item.severity for item in articles], key=lambda item: _SEVERITY_ORDER[item])
        applicability = "applicable" if all(item.applicability == "applicable" for item in articles) else "context_dependent"
        priority, priority_reason, scope = _priority(
            certainty=certainty, severity=severity, applicability=applicability,
            article_count=len(articles), code=bucket["code"],
        )
        roles = tuple(dict.fromkeys(role for role in bucket["roles"] if role in ROLE_LABELS)) or ("technical",)
        recommendations.append(OjsRecommendation(
            code=bucket["code"], label=bucket["label"], nature=bucket["nature"], source_label=bucket["source_label"],
            article_count=len(articles), occurrence_count=bucket["occurrence_count"],
            confirmed_article_count=sum(item.classification == "confirmed" for item in articles),
            potential_article_count=sum(item.classification == "potential" for item in articles),
            unverified_article_count=sum(item.classification == "unverified" for item in articles),
            certainty=certainty, severity=severity, applicability=applicability, priority=priority,
            priority_reason=priority_reason, scope=scope, roles=roles,
            impact=_distinct_text(bucket["impacts"], "Impacto a confirmar no contexto editorial."),
            intervention=_distinct_text(bucket["interventions"], "Revise as evidências antes de realizar uma alteração."),
            verification=_distinct_text(bucket["verifications"], "Execute nova auditoria e compare as evidências."),
            historical_criteria=bool(bucket["historical_criteria"]), articles=articles,
        ))
    return tuple(sorted(recommendations, key=lambda item: (
        {"high": 0, "medium": 1, "low": 2, "review": 3}[item.priority],
        -item.article_count, item.label.casefold(), item.code,
    )))


def _matches(recommendation: OjsRecommendation, *, priority: str, severity: str, certainty: str, nature: str, role: str) -> bool:
    return (
        (priority == "all" or recommendation.priority == priority)
        and (severity == "all" or recommendation.severity == severity)
        and (certainty == "all" or recommendation.certainty == certainty)
        and (nature == "all" or recommendation.nature == nature)
        and (role == "all" or role in recommendation.roles)
    )


def filter_ojs_recommendations(
    snapshot: OjsRecommendationSnapshot,
    *,
    priority: object = "all",
    severity: object = "all",
    certainty: object = "all",
    nature: object = "all",
    role: object = "all",
) -> tuple[OjsRecommendation, ...]:
    """Aplica os filtros canônicos das recomendações a uma referência pronta.

    Consumidores sem paginação, como a exportação, usam esta função para
    manter os mesmos valores aceitos e a mesma semântica da tabela da
    interface. Ela não consulta rede nem modifica evidências.
    """
    priority_value = _filter(priority, PRIORITY_LABELS, "prioridade")
    severity_value = _filter(severity, SEVERITY_LABELS, "gravidade")
    certainty_value = _filter(certainty, CERTAINTY_LABELS, "certeza")
    nature_value = _filter(nature, NATURE_LABELS, "natureza")
    role_value = _filter(role, ROLE_LABELS, "responsável")
    return tuple(item for item in snapshot.recommendations if _matches(
        item,
        priority=priority_value,
        severity=severity_value,
        certainty=certainty_value,
        nature=nature_value,
        role=role_value,
    ))


def _indicators(recommendations: tuple[OjsRecommendation, ...], coverage: HtmlCoverage | None) -> OjsRecommendationIndicators:
    confirmed = {article.article_id for item in recommendations for article in item.articles if article.classification == "confirmed"}
    potential = {article.article_id for item in recommendations for article in item.articles if article.classification == "potential"}
    return OjsRecommendationIndicators(
        len(recommendations), len(confirmed), len(potential),
        sum(item.priority == "high" for item in recommendations),
        sum(item.priority == "review" for item in recommendations), coverage,
    )


def list_ojs_recommendations(
    actor: User, journal_id: str, *, page: int = 1, priority: object = "all", severity: object = "all",
    certainty: object = "all", nature: object = "all", role: object = "all", audit_run_id: str | None = None,
    include_inactive: bool = False,
) -> OjsRecommendationListing:
    """Lista recomendações filtradas, depois de validar o periódico do ator."""
    priority_value = _filter(priority, PRIORITY_LABELS, "prioridade")
    severity_value = _filter(severity, SEVERITY_LABELS, "gravidade")
    certainty_value = _filter(certainty, CERTAINTY_LABELS, "certeza")
    nature_value = _filter(nature, NATURE_LABELS, "natureza")
    role_value = _filter(role, ROLE_LABELS, "responsável")
    snapshot = get_ojs_recommendation_snapshot(
        actor, journal_id, audit_run_id=audit_run_id, include_inactive=include_inactive,
    )
    selected = filter_ojs_recommendations(
        snapshot,
        priority=priority_value,
        severity=severity_value,
        certainty=certainty_value,
        nature=nature_value,
        role=role_value,
    )
    return OjsRecommendationListing(
        snapshot.journal, snapshot.reference, paginate_sequence(selected, page), snapshot.indicators,
        priority_value, severity_value, certainty_value, nature_value, role_value,
    )


def get_ojs_recommendation_snapshot(
    actor: User,
    journal_id: str,
    *,
    audit_run_id: str | None = None,
    include_inactive: bool = False,
) -> OjsRecommendationSnapshot:
    """Agrupa uma referência sem paginação, para consumidores de leitura.

    O dashboard usa esta função para apresentar barras por certeza, gravidade,
    origem e prioridade a partir das mesmas recomendações que a área “O que
    corrigir” lista. A função não faz rede, não persiste dados e não altera
    classificações históricas.
    """
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    reference = resolve_ojs_diagnostic_reference(
        actor, journal.id, audit_run_id=audit_run_id, include_inactive=include_inactive,
    )
    if reference is None:
        return OjsRecommendationSnapshot(
            journal, None, (), OjsRecommendationIndicators(0, 0, 0, 0, 0, None),
        )
    recommendations = _recommendations_for_reference(journal, reference)
    try:
        coverage = get_ojs_html_coverage(
            actor, journal.id, source_oai_audit_run_id=reference.audit_run.id,
            include_inactive=include_inactive,
        )
    except OjsSeoServiceError:
        # Cobertura técnica pode não existir para uma referência bibliográfica;
        # isso não invalida recomendações OAI-PMH já fundamentadas.
        coverage = None
    return OjsRecommendationSnapshot(
        journal, reference, recommendations, _indicators(recommendations, coverage),
    )


def get_ojs_recommendation_detail(
    actor: User, journal_id: str, source_audit_id: str, code: str, *, page: int = 1,
    include_inactive: bool = False,
) -> OjsRecommendationDetail:
    journal = get_ojs_journal(actor, journal_id, include_inactive=include_inactive)
    reference = resolve_ojs_diagnostic_reference(
        actor, journal.id, audit_run_id=source_audit_id, include_inactive=include_inactive,
    )
    if reference is None:
        raise OjsSeoNotFoundError("ojs_recommendation_not_found", "A recomendação não foi encontrada.")
    recommendation = next((item for item in _recommendations_for_reference(journal, reference) if item.code == code), None)
    if recommendation is None:
        raise OjsSeoNotFoundError("ojs_recommendation_not_found", "A recomendação não foi encontrada.")
    return OjsRecommendationDetail(journal, reference, recommendation, paginate_sequence(recommendation.articles, page))


__all__ = [
    "APPLICABILITY_LABELS", "CERTAINTY_LABELS", "NATURE_LABELS", "PRIORITY_LABELS", "ROLE_LABELS", "SEVERITY_LABELS",
    "OjsRecommendation", "OjsRecommendationDetail", "OjsRecommendationFilterError", "OjsRecommendationSnapshot",
    "OjsRecommendationIndicators", "OjsRecommendationListing", "RecommendationArticle",
    "filter_ojs_recommendations", "get_ojs_recommendation_detail", "get_ojs_recommendation_snapshot", "list_ojs_recommendations",
]
