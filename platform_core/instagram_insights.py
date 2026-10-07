"""Motor determinístico, versionado e rastreável de achados Instagram.

Os achados desta camada são descritivos: eles registram associações observadas
em um recorte explícito, nunca recomendações nem relações de causalidade.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from statistics import median
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from .extensions import db
from .instagram_comments import (
    HUMAN_ASSESSMENT_SOURCES,
    INTENT_LABELS,
    RISK_LABELS,
    SENTIMENT_LABELS,
    effective_assessment,
)
from .instagram_dashboard import (
    PERIOD_DAYS,
    MediaRow,
    dashboard_period_bounds,
    selected_media_rows,
)
from .instagram_metrics import DERIVED_METRICS_VERSION, normalized_content_type
from .instagram_taxonomy import current_classifications
from .models import (
    AnalyticsProject,
    InstagramAccount,
    InstagramComment,
    InstagramCommentAssessment,
    InstagramInsightEvidence,
    InstagramInsightFinding,
    InstagramInsightRun,
    InstagramMedia,
    InstagramMediaClassification,
    InstagramMediaObservation,
    InstagramTaxonomyDimension,
)


INSTAGRAM_INSIGHTS_VERSION = "instagram-insights-v1"

MIN_GROUP_N = 3
MIN_COMPARISON_N = 3
MIN_COVERAGE = 0.70
PERFORMANCE_RELATIVE_DIFFERENCE = 0.20
RATE_DIFFERENCE_PP = 1.0
RECEPTION_DIFFERENCE_PP = 15.0
MIN_RECURRING_TOPIC_N = 3
MAX_FINDINGS = 24

SUPPORT_RANK = {"exploratory": 1, "moderate": 2, "consistent": 3}
EDITORIAL_DIMENSION_KEYS = frozenset({
    "communicational-objective", "editorial-type", "main-theme",
})
PERFORMANCE_METRICS = (
    ("reach", "Alcance", False),
    ("interactions", "Interações", False),
    ("saves", "Salvamentos", False),
    ("shares", "Compartilhamentos", False),
    ("comments_count", "Comentários", False),
    ("interaction_rate_by_reach", "Taxa de interação por alcance", True),
    ("saves_rate_by_reach", "Salvamentos por alcance", True),
    ("shares_rate_by_reach", "Compartilhamentos por alcance", True),
)


class InstagramInsightsError(ValueError):
    """Entrada ou estado incompatível com a geração de insights."""


class InstagramInsightsPersistenceError(RuntimeError):
    """Falha transacional que não deixa execução ou evidência parcial."""


@dataclass(frozen=True)
class EvidenceReference:
    source_type: str
    source_id: str
    role: str
    details: Mapping[str, object]


@dataclass(frozen=True)
class FindingDraft:
    rule_id: str
    rule_version: str
    kind: str
    title: str
    summary: str
    metric_key: str | None
    metric_version: str | None
    group_label: str
    comparison_label: str | None
    group_value: float | None
    comparison_value: float | None
    absolute_difference: float | None
    relative_difference: float | None
    difference_unit: str | None
    group_n: int
    comparison_n: int
    coverage: float
    support_level: str
    support_explanation: str
    details: Mapping[str, object]
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class CommentFact:
    comment: InstagramComment
    assessment: InstagramCommentAssessment | None


@dataclass(frozen=True)
class EvidenceView:
    source_type: str
    source_id: str
    role: str
    label: str
    detail: str
    media_id: str | None = None
    comment_id: str | None = None
    permalink: str | None = None


def insight_thresholds() -> dict[str, float | int]:
    """Snapshot serializável dos limiares usados por uma execução."""
    return {
        "min_group_n": MIN_GROUP_N,
        "min_comparison_n": MIN_COMPARISON_N,
        "min_coverage": MIN_COVERAGE,
        "performance_relative_difference": PERFORMANCE_RELATIVE_DIFFERENCE,
        "rate_difference_percentage_points": RATE_DIFFERENCE_PP,
        "reception_difference_percentage_points": RECEPTION_DIFFERENCE_PP,
        "min_recurring_topic_n": MIN_RECURRING_TOPIC_N,
    }


def _finite(value: float | int | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not isfinite(result):
        raise InstagramInsightsError("Um achado contém valor numérico inválido.")
    return result


def support_for(*, group_n: int, comparison_n: int, coverage: float,
                magnitude: float, threshold: float) -> tuple[str, str]:
    """Heurística descritiva central; não é teste estatístico nem p-valor."""
    minimum_n = min(group_n, comparison_n)
    ratio = abs(magnitude) / threshold if threshold > 0 else 0
    if minimum_n >= 8 and coverage >= .90 and ratio >= 2:
        level = "consistent"
    elif minimum_n >= 5 and coverage >= .80 and ratio >= 1:
        level = "moderate"
    else:
        level = "exploratory"
    explanation = (
        "Grau descritivo baseado no menor tamanho dos grupos, na cobertura e na magnitude observada; "
        "não representa significância estatística, p-valor ou causalidade."
    )
    return level, explanation


def _metric_value(row: MediaRow, metric_key: str) -> int | float | None:
    if metric_key in row.derived_metrics:
        return row.derived_metrics[metric_key]
    return getattr(row.observation, metric_key) if row.observation is not None else None


def _display_value(value: float | None, *, rate: bool = False) -> str:
    if value is None:
        return "—"
    if rate:
        return f"{value:.2f}%".replace(".", ",")
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", ".")
    return f"{value:.2f}".replace(".", ",")


def _unique_evidence(references: Iterable[EvidenceReference]) -> tuple[EvidenceReference, ...]:
    unique: dict[tuple[str, str, str], EvidenceReference] = {}
    for reference in references:
        unique[(reference.source_type, reference.source_id, reference.role)] = reference
    return tuple(unique[key] for key in sorted(unique))


def _media_evidence(rows: Iterable[MediaRow], role: str, metric_key: str,
                    classifications: Mapping[str, Sequence[InstagramMediaClassification]] | None = None) -> list[EvidenceReference]:
    references: list[EvidenceReference] = []
    for row in rows:
        value = _metric_value(row, metric_key)
        references.append(EvidenceReference(
            "media", row.media.id, f"{role}_media", {"metric_key": metric_key, "value": value},
        ))
        if row.observation is not None:
            references.append(EvidenceReference(
                "media_observation", row.observation.id, f"{role}_observation",
                {"metric_key": metric_key, "value": value, "observed_at": row.observation.observed_at.isoformat()},
            ))
        for classification in (classifications or {}).get(row.media.id, ()):
            references.append(EvidenceReference(
                "media_classification", classification.id, f"{role}_classification",
                {"dimension": classification.dimension.name, "category": classification.category.name},
            ))
    return references


def _comment_evidence(facts: Iterable[CommentFact], role: str) -> list[EvidenceReference]:
    references: list[EvidenceReference] = []
    for fact in facts:
        references.append(EvidenceReference(
            "comment", fact.comment.id, f"{role}_comment", {"media_id": fact.comment.instagram_media_id},
        ))
        if fact.assessment is not None:
            references.append(EvidenceReference(
                "comment_assessment", fact.assessment.id, f"{role}_assessment",
                {"source": fact.assessment.source, "comment_id": fact.comment.id},
            ))
    return references


def _meaningful_difference(*, absolute: float, relative: float | None, rate: bool) -> tuple[bool, float]:
    if rate:
        return abs(absolute) >= RATE_DIFFERENCE_PP, RATE_DIFFERENCE_PP
    if relative is not None:
        return abs(relative) >= PERFORMANCE_RELATIVE_DIFFERENCE, PERFORMANCE_RELATIVE_DIFFERENCE
    return abs(absolute) >= 1, 1.0


def _performance_finding(*, rule_id: str, kind: str, group_label: str,
                         comparison_label: str, group_rows: Sequence[MediaRow],
                         comparison_rows: Sequence[MediaRow], metric_key: str, metric_label: str,
                         rate: bool, group_classifications: Mapping[str, Sequence[InstagramMediaClassification]] | None = None,
                         comparison_classifications: Mapping[str, Sequence[InstagramMediaClassification]] | None = None) -> FindingDraft | None:
    group_values = [value for row in group_rows if (value := _metric_value(row, metric_key)) is not None]
    comparison_values = [value for row in comparison_rows if (value := _metric_value(row, metric_key)) is not None]
    group_n, comparison_n = len(group_values), len(comparison_values)
    total_publications = len(group_rows) + len(comparison_rows)
    coverage = (group_n + comparison_n) / total_publications if total_publications else 0
    if group_n < MIN_GROUP_N or comparison_n < MIN_COMPARISON_N or coverage < MIN_COVERAGE:
        return None
    group_value, comparison_value = float(median(group_values)), float(median(comparison_values))
    absolute = group_value - comparison_value
    relative = absolute / abs(comparison_value) if comparison_value != 0 else None
    meaningful, threshold = _meaningful_difference(absolute=absolute, relative=relative, rate=rate)
    if not meaningful:
        return None
    support, explanation = support_for(
        group_n=group_n, comparison_n=comparison_n, coverage=coverage,
        magnitude=absolute if rate else (relative if relative is not None else absolute), threshold=threshold,
    )
    direction = "superior" if absolute > 0 else "inferior"
    difference = f"{absolute:+.2f} p.p." if rate else _display_value(absolute)
    title = f"{group_label}: mediana {direction} em {metric_label}"
    summary = (
        f"No período analisado, {group_label} apresentou mediana de {_display_value(group_value, rate=rate)} em "
        f"{metric_label}, frente a {_display_value(comparison_value, rate=rate)} em {comparison_label} "
        f"(diferença {difference}). O padrão é descritivo e não causal."
    )
    evidence = _media_evidence(group_rows, "group", metric_key, group_classifications)
    evidence.extend(_media_evidence(comparison_rows, "comparison", metric_key, comparison_classifications))
    return FindingDraft(
        rule_id=rule_id, rule_version=f"{rule_id}-v1", kind=kind, title=title, summary=summary,
        metric_key=metric_key, metric_version=DERIVED_METRICS_VERSION if rate else None,
        group_label=group_label, comparison_label=comparison_label,
        group_value=group_value, comparison_value=comparison_value,
        absolute_difference=absolute, relative_difference=relative,
        difference_unit="percentage_points" if rate else "value",
        group_n=group_n, comparison_n=comparison_n, coverage=coverage,
        support_level=support, support_explanation=explanation,
        details={
            "group_publications": len(group_rows), "comparison_publications": len(comparison_rows),
            "metric_label": metric_label, "metric_is_rate": rate,
            "derived_metrics_version": DERIVED_METRICS_VERSION if rate else None,
        }, evidence=_unique_evidence(evidence),
    )


def _performance_by_editorial(rows: Sequence[MediaRow], project: AnalyticsProject) -> list[FindingDraft]:
    classifications = current_classifications(project.id, media_ids=tuple(row.media.id for row in rows))
    by_dimension: dict[str, list[InstagramMediaClassification]] = defaultdict(list)
    for classification in classifications:
        if (classification.dimension.selection_mode == "single"
                and classification.dimension.key in EDITORIAL_DIMENSION_KEYS):
            by_dimension[classification.dimension.id].append(classification)
    findings: list[FindingDraft] = []
    all_by_media: dict[str, list[InstagramMediaClassification]] = defaultdict(list)
    for classification in classifications:
        all_by_media[classification.instagram_media_id].append(classification)
    for dimension_id, assignments in sorted(by_dimension.items()):
        by_category: dict[str, list[InstagramMediaClassification]] = defaultdict(list)
        for assignment in assignments:
            by_category[assignment.instagram_taxonomy_category_id].append(assignment)
        for category_id, category_assignments in sorted(
            by_category.items(), key=lambda item: (item[1][0].category.name.casefold(), item[0])
        ):
            group_ids = {item.instagram_media_id for item in category_assignments}
            group = tuple(row for row in rows if row.media.id in group_ids)
            comparison = tuple(row for row in rows if row.media.id not in group_ids)
            group_classes = {row.media.id: tuple(
                item for item in all_by_media[row.media.id] if item.instagram_taxonomy_dimension_id == dimension_id
            ) for row in group}
            category = category_assignments[0].category
            dimension = category_assignments[0].dimension
            for metric_key, metric_label, rate in PERFORMANCE_METRICS:
                finding = _performance_finding(
                    rule_id="editorial_metric_difference", kind="content_performance",
                    group_label=f"{dimension.name}: {category.name}",
                    comparison_label="demais publicações elegíveis", group_rows=group,
                    comparison_rows=comparison, metric_key=metric_key, metric_label=metric_label,
                    rate=rate, group_classifications=group_classes,
                )
                if finding is not None:
                    findings.append(finding)
    return findings


def _performance_by_media_type(rows: Sequence[MediaRow]) -> list[FindingDraft]:
    by_type: dict[tuple[str, str], list[MediaRow]] = defaultdict(list)
    for row in rows:
        key, label = normalized_content_type(row.media.media_type_raw)
        if key != "unknown":
            by_type[(key, label)].append(row)
    findings: list[FindingDraft] = []
    for (_key, label), group in sorted(by_type.items(), key=lambda item: (item[0][1], item[0][0])):
        group_ids = {row.media.id for row in group}
        comparison = tuple(row for row in rows if row.media.id not in group_ids)
        for metric_key, metric_label, rate in PERFORMANCE_METRICS:
            finding = _performance_finding(
                rule_id="media_type_metric_difference", kind="media_type_performance",
                group_label=f"Tipo de mídia: {label}", comparison_label="demais publicações elegíveis",
                group_rows=group, comparison_rows=comparison, metric_key=metric_key,
                metric_label=metric_label, rate=rate,
            )
            if finding is not None:
                findings.append(finding)
    return findings


def _comment_facts(project: AnalyticsProject, media_ids: Sequence[str]) -> tuple[CommentFact, ...]:
    if not media_ids:
        return ()
    comments = db.session.scalars(
        select(InstagramComment)
        .join(InstagramMedia, InstagramMedia.id == InstagramComment.instagram_media_id)
        .where(InstagramComment.instagram_media_id.in_(tuple(media_ids)))
        .options(joinedload(InstagramComment.instagram_media), selectinload(InstagramComment.assessments))
        .order_by(InstagramComment.id)
    ).unique().all()
    return tuple(CommentFact(comment, effective_assessment(comment.assessments)) for comment in comments)


def _reception_finding(*, rule_id: str, kind: str, group_label: str,
                       facts: Sequence[CommentFact], predicate: Callable[[InstagramCommentAssessment], bool],
                       known: Callable[[InstagramCommentAssessment], bool], title_suffix: str,
                       group_media_count: int | None = None, comparison_facts: Sequence[CommentFact] | None = None,
                       comparison_media_count: int | None = None,
                       group_evidence: Sequence[EvidenceReference] | None = None,
                       comparison_evidence: Sequence[EvidenceReference] | None = None) -> FindingDraft | None:
    comparison_facts = facts if comparison_facts is None else comparison_facts
    population = tuple(facts) + (tuple(comparison_facts) if comparison_facts is not facts else ())
    known_group = tuple(fact for fact in facts if fact.assessment is not None and known(fact.assessment))
    known_comparison = tuple(
        fact for fact in comparison_facts if fact.assessment is not None and known(fact.assessment)
    )
    target_group = tuple(fact for fact in known_group if predicate(fact.assessment))
    target_comparison = tuple(fact for fact in known_comparison if predicate(fact.assessment))
    if comparison_facts is facts:
        # Para concentrações gerais, "comparação" é o restante dos avaliados.
        comparison_group = tuple(fact for fact in known_group if not predicate(fact.assessment))
        group_n, comparison_n = len(target_group), len(comparison_group)
        known_total = len(known_group)
        group_value = len(target_group) / known_total * 100 if known_total else 0
        comparison_value = len(comparison_group) / known_total * 100 if known_total else 0
        evidence_group, evidence_comparison = target_group, comparison_group
        support_group_n = support_comparison_n = known_total
    else:
        # Nas comparações editoriais, a amostra é o número de comentários
        # avaliados em cada conjunto, não somente a quantidade de ocorrências
        # do evento. Assim, 0% no comparador continua sendo um dado válido.
        group_n, comparison_n = len(known_group), len(known_comparison)
        group_value = len(target_group) / len(known_group) * 100 if known_group else 0
        comparison_value = len(target_comparison) / len(known_comparison) * 100 if known_comparison else 0
        evidence_group, evidence_comparison = known_group, known_comparison
        support_group_n, support_comparison_n = group_n, comparison_n
    coverage = (len(known_group) + (len(known_comparison) if comparison_facts is not facts else 0)) / len(population) if population else 0
    if comparison_facts is facts:
        minimum_population = len(known_group)
        minimum_event = len(target_group)
        if (minimum_population < MIN_GROUP_N or minimum_event < MIN_GROUP_N
                or coverage < MIN_COVERAGE):
            return None
    elif group_n < MIN_GROUP_N or comparison_n < MIN_COMPARISON_N or coverage < MIN_COVERAGE:
        return None
    if group_media_count is not None and group_media_count < MIN_GROUP_N:
        return None
    if comparison_media_count is not None and comparison_media_count < MIN_COMPARISON_N:
        return None
    absolute = group_value - comparison_value
    if comparison_facts is not facts and abs(absolute) < RECEPTION_DIFFERENCE_PP:
        return None
    support, explanation = support_for(
        group_n=support_group_n, comparison_n=support_comparison_n, coverage=coverage,
        magnitude=absolute, threshold=RECEPTION_DIFFERENCE_PP,
    )
    if comparison_facts is facts:
        title = f"{title_suffix}: concentração observada"
        summary = (
            f"{group_label} reuniu {group_n} de {len(known_group)} comentários avaliados "
            f"({group_value:.1f}%). O resultado descreve o recorte e não atribui causalidade."
        )
        comparison_label = "demais comentários avaliados"
    else:
        direction = "maior" if absolute > 0 else "menor"
        title = f"{group_label}: {direction} proporção de {title_suffix.casefold()}"
        summary = (
            f"Entre os comentários avaliados, {group_label} apresentou {group_value:.1f}% de {title_suffix.casefold()}, "
            f"frente a {comparison_value:.1f}% em demais conteúdos classificados (diferença {absolute:+.1f} p.p.). "
            "O padrão é descritivo e não causal."
        )
        comparison_label = "demais conteúdos classificados"
    evidence = list(group_evidence or _comment_evidence(evidence_group, "group"))
    evidence.extend(comparison_evidence or _comment_evidence(evidence_comparison, "comparison"))
    return FindingDraft(
        rule_id=rule_id, rule_version=f"{rule_id}-v1", kind=kind, title=title, summary=summary,
        metric_key="comment_proportion", metric_version=None, group_label=group_label,
        comparison_label=comparison_label, group_value=group_value, comparison_value=comparison_value,
        absolute_difference=absolute, relative_difference=(absolute / abs(comparison_value)
                                                            if comparison_value else None),
        difference_unit="percentage_points", group_n=group_n, comparison_n=comparison_n,
        coverage=coverage, support_level=support, support_explanation=explanation,
        details={
            "evaluated_group_comments": len(known_group),
            "evaluated_comparison_comments": len(known_comparison),
            "target_group_comments": len(target_group),
            "target_comparison_comments": len(target_comparison),
            "group_publications": group_media_count,
            "comparison_publications": comparison_media_count,
        }, evidence=_unique_evidence(evidence),
    )


def _reception_findings(facts: Sequence[CommentFact]) -> list[FindingDraft]:
    definitions = (
        ("comment_sentiment_concentration", "Sentimento negativo", lambda a: a.sentiment == "negative", lambda a: a.sentiment is not None),
        ("comment_sentiment_concentration", "Sentimento positivo", lambda a: a.sentiment == "positive", lambda a: a.sentiment is not None),
        ("comment_question_concentration", "Perguntas", lambda a: a.intent == "question", lambda a: a.intent is not None),
        ("comment_response_need", "Necessidade de resposta", lambda a: a.needs_response is True, lambda a: a.needs_response is not None),
        ("comment_risk_concentration", "Risco médio ou alto", lambda a: a.risk in {"medium", "high"}, lambda a: a.risk is not None),
    )
    findings = []
    for rule_id, label, predicate, known in definitions:
        finding = _reception_finding(
            rule_id=rule_id, kind="reception", group_label=label, facts=facts,
            predicate=predicate, known=known, title_suffix=label,
        )
        if finding is not None:
            findings.append(finding)
    by_topic: dict[str, list[CommentFact]] = defaultdict(list)
    topic_known = [fact for fact in facts if fact.assessment and fact.assessment.topic_normalized]
    for fact in topic_known:
        by_topic[fact.assessment.topic_normalized].append(fact)
    for topic, topic_facts in sorted(by_topic.items(), key=lambda item: (item[0].casefold(), item[0])):
        remaining = [fact for fact in topic_known if fact not in topic_facts]
        if len(topic_facts) < MIN_RECURRING_TOPIC_N or len(remaining) < MIN_COMPARISON_N:
            continue
        coverage = len(topic_known) / len(facts) if facts else 0
        if coverage < MIN_COVERAGE:
            continue
        share = len(topic_facts) / len(topic_known) * 100
        support, explanation = support_for(
            group_n=len(topic_facts), comparison_n=len(remaining), coverage=coverage,
            magnitude=share, threshold=RECEPTION_DIFFERENCE_PP,
        )
        findings.append(FindingDraft(
            rule_id="comment_topic_recurrence", rule_version="comment_topic_recurrence-v1", kind="reception",
            title=f"Tema recorrente nos comentários: {topic}",
            summary=(f"O tema normalizado {topic} apareceu em {len(topic_facts)} de {len(topic_known)} comentários "
                     f"com tema informado ({share:.1f}%). É um padrão descritivo do recorte analisado."),
            metric_key="topic_recurrence", metric_version=None, group_label=topic,
            comparison_label="demais temas informados", group_value=share,
            comparison_value=len(remaining) / len(topic_known) * 100,
            absolute_difference=None, relative_difference=None, difference_unit="percentage_points",
            group_n=len(topic_facts), comparison_n=len(remaining), coverage=coverage,
            support_level=support, support_explanation=explanation,
            details={"evaluated_comments": len(topic_known)},
            evidence=_unique_evidence(_comment_evidence(topic_facts, "group") + _comment_evidence(remaining, "comparison")),
        ))
    return findings


def _content_reception_findings(rows: Sequence[MediaRow], facts: Sequence[CommentFact],
                                project: AnalyticsProject) -> list[FindingDraft]:
    classifications = current_classifications(project.id, media_ids=tuple(row.media.id for row in rows))
    by_dimension: dict[str, list[InstagramMediaClassification]] = defaultdict(list)
    for item in classifications:
        if item.dimension.selection_mode == "single" and item.dimension.key in EDITORIAL_DIMENSION_KEYS:
            by_dimension[item.instagram_taxonomy_dimension_id].append(item)
    facts_by_media: dict[str, list[CommentFact]] = defaultdict(list)
    for fact in facts:
        facts_by_media[fact.comment.instagram_media_id].append(fact)
    findings: list[FindingDraft] = []
    definitions = (
        ("editorial_reception_difference", "Sentimento negativo", lambda a: a.sentiment == "negative", lambda a: a.sentiment is not None),
        ("editorial_reception_difference", "Perguntas", lambda a: a.intent == "question", lambda a: a.intent is not None),
        ("editorial_reception_difference", "Necessidade de resposta", lambda a: a.needs_response is True, lambda a: a.needs_response is not None),
        ("editorial_reception_difference", "Risco médio ou alto", lambda a: a.risk in {"medium", "high"}, lambda a: a.risk is not None),
    )
    for dimension_id, assignments in sorted(by_dimension.items()):
        by_category: dict[str, list[InstagramMediaClassification]] = defaultdict(list)
        for item in assignments:
            by_category[item.instagram_taxonomy_category_id].append(item)
        all_classified_ids = {item.instagram_media_id for item in assignments}
        for _category_id, category_assignments in sorted(
            by_category.items(), key=lambda item: (item[1][0].category.name.casefold(), item[0])
        ):
            group_ids = {item.instagram_media_id for item in category_assignments}
            comparison_ids = all_classified_ids - group_ids
            group_facts = tuple(fact for media_id in group_ids for fact in facts_by_media.get(media_id, ()))
            comparison_facts = tuple(fact for media_id in comparison_ids for fact in facts_by_media.get(media_id, ()))
            category = category_assignments[0].category
            dimension = category_assignments[0].dimension
            label = f"{dimension.name}: {category.name}"
            for rule_id, suffix, predicate, known in definitions:
                finding = _reception_finding(
                    rule_id=rule_id, kind="content_reception", group_label=label,
                    facts=group_facts, comparison_facts=comparison_facts,
                    predicate=predicate, known=known, title_suffix=suffix,
                    group_media_count=len(group_ids), comparison_media_count=len(comparison_ids),
                )
                if finding is not None:
                    findings.append(finding)
    return findings


def _ordered(drafts: Iterable[FindingDraft]) -> tuple[FindingDraft, ...]:
    return tuple(sorted(drafts, key=lambda item: (
        -SUPPORT_RANK[item.support_level],
        -(abs(item.absolute_difference) if item.absolute_difference is not None else 0),
        -item.coverage, item.rule_id, item.group_label.casefold(), item.title.casefold(),
    ))[:MAX_FINDINGS])


def build_insight_drafts(project: AnalyticsProject, period: str,
                         timezone_name: str) -> tuple[datetime | None, tuple[MediaRow, ...],
                                                       tuple[CommentFact, ...], tuple[FindingDraft, ...]]:
    """Monta o recorte uma vez e produz achados determinísticos em memória."""
    if period not in PERIOD_DAYS:
        raise InstagramInsightsError("Período inválido.")
    reference_at, rows = selected_media_rows(project, period, timezone_name)
    facts = _comment_facts(project, tuple(row.media.id for row in rows))
    drafts = []
    if rows:
        drafts.extend(_performance_by_editorial(rows, project))
        drafts.extend(_performance_by_media_type(rows))
    if facts:
        drafts.extend(_reception_findings(facts))
        if rows:
            drafts.extend(_content_reception_findings(rows, facts, project))
    return reference_at, rows, facts, _ordered(drafts)


def _validate_draft(draft: FindingDraft) -> None:
    if draft.group_n < 0 or draft.comparison_n < 0 or not 0 <= draft.coverage <= 1:
        raise InstagramInsightsError("Um achado contém contagem ou cobertura inválida.")
    if draft.support_level not in SUPPORT_RANK:
        raise InstagramInsightsError("Um achado contém grau de suporte inválido.")
    for value in (draft.group_value, draft.comparison_value, draft.absolute_difference, draft.relative_difference):
        _finite(value)


def generate_insights(project: AnalyticsProject, period: str, timezone_name: str) -> InstagramInsightRun:
    """Gera uma execução somente por chamada explícita e persiste tudo em transação."""
    if project.status != "active" or project.archived_at is not None:
        raise InstagramInsightsError("Projetos arquivados não podem gerar novos insights.")
    reference_at, rows, facts, drafts = build_insight_drafts(project, period, timezone_name)
    period_start, period_end = dashboard_period_bounds(period, reference_at, timezone_name)
    parameters = {
        "period": period,
        "engine_version": INSTAGRAM_INSIGHTS_VERSION,
        "derived_metrics_version": DERIVED_METRICS_VERSION,
        "latest_observation_per_media": True,
        "reference_at": reference_at.isoformat() if reference_at else None,
        "thresholds": insight_thresholds(),
    }
    try:
        run = InstagramInsightRun(
            analytics_project_id=project.id, period_key=period, period_start=period_start,
            period_end=period_end, reference_at=reference_at,
            engine_version=INSTAGRAM_INSIGHTS_VERSION, parameters_json=parameters,
            publication_count=len(rows), comment_count=len(facts), finding_count=len(drafts),
        )
        db.session.add(run)
        db.session.flush()
        for draft in drafts:
            _validate_draft(draft)
            finding = InstagramInsightFinding(
                instagram_insight_run_id=run.id, rule_id=draft.rule_id,
                rule_version=draft.rule_version, engine_version=INSTAGRAM_INSIGHTS_VERSION,
                kind=draft.kind, title=draft.title, summary=draft.summary,
                metric_key=draft.metric_key, metric_version=draft.metric_version,
                group_label=draft.group_label, comparison_label=draft.comparison_label,
                group_value=_finite(draft.group_value), comparison_value=_finite(draft.comparison_value),
                absolute_difference=_finite(draft.absolute_difference),
                relative_difference=_finite(draft.relative_difference), difference_unit=draft.difference_unit,
                group_n=draft.group_n, comparison_n=draft.comparison_n, coverage=draft.coverage,
                support_level=draft.support_level, support_explanation=draft.support_explanation,
                details_json=dict(draft.details),
            )
            db.session.add(finding)
            db.session.flush()
            db.session.add_all(
                InstagramInsightEvidence(
                    instagram_insight_finding_id=finding.id, source_type=reference.source_type,
                    source_id=reference.source_id, role=reference.role,
                    details_json=dict(reference.details),
                )
                for reference in draft.evidence
            )
        db.session.commit()
        return run
    except InstagramInsightsError:
        db.session.rollback()
        raise
    except Exception as error:
        db.session.rollback()
        raise InstagramInsightsPersistenceError(
            "Não foi possível gerar os insights; nenhuma execução parcial foi gravada."
        ) from error


def latest_insight_run(project: AnalyticsProject, period: str) -> InstagramInsightRun | None:
    """Retorna somente o último run explícito do recorte solicitado."""
    if period not in PERIOD_DAYS:
        raise InstagramInsightsError("Período inválido.")
    return db.session.scalar(
        select(InstagramInsightRun)
        .where(InstagramInsightRun.analytics_project_id == project.id, InstagramInsightRun.period_key == period)
        .options(selectinload(InstagramInsightRun.findings))
        .order_by(InstagramInsightRun.generated_at.desc(), InstagramInsightRun.id.desc())
    )


def finding_for_project(project: AnalyticsProject, finding_id: str) -> InstagramInsightFinding | None:
    return db.session.scalar(
        select(InstagramInsightFinding)
        .join(InstagramInsightRun)
        .where(
            InstagramInsightFinding.id == finding_id,
            InstagramInsightRun.analytics_project_id == project.id,
        )
        .options(joinedload(InstagramInsightFinding.insight_run), selectinload(InstagramInsightFinding.evidence))
    )


def _safe_permalink(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def evidence_views(project: AnalyticsProject, finding: InstagramInsightFinding) -> tuple[EvidenceView, ...]:
    """Resolve as referências em lote para uma tela de detalhe sem FKs polimórficas falsas."""
    references = tuple(finding.evidence)
    source_ids: dict[str, set[str]] = defaultdict(set)
    for reference in references:
        source_ids[reference.source_type].add(reference.source_id)
    media = {
        item.id: item for item in db.session.scalars(
            select(InstagramMedia)
            .join(InstagramAccount)
            .where(
                InstagramMedia.id.in_(source_ids["media"]),
                InstagramAccount.analytics_project_id == project.id,
            )
        ).all()
    } if source_ids["media"] else {}
    observations = {
        item.id: item for item in db.session.scalars(
            select(InstagramMediaObservation)
            .join(InstagramMedia)
            .join(InstagramAccount)
            .where(
                InstagramMediaObservation.id.in_(source_ids["media_observation"]),
                InstagramAccount.analytics_project_id == project.id,
            )
            .options(joinedload(InstagramMediaObservation.instagram_media))
        ).all()
    } if source_ids["media_observation"] else {}
    classifications = {
        item.id: item for item in db.session.scalars(
            select(InstagramMediaClassification)
            .join(InstagramMedia)
            .join(InstagramAccount)
            .where(
                InstagramMediaClassification.id.in_(source_ids["media_classification"]),
                InstagramAccount.analytics_project_id == project.id,
            ).options(
                joinedload(InstagramMediaClassification.instagram_media),
                joinedload(InstagramMediaClassification.dimension),
                joinedload(InstagramMediaClassification.category),
            )
        ).all()
    } if source_ids["media_classification"] else {}
    comments = {
        item.id: item for item in db.session.scalars(
            select(InstagramComment)
            .join(InstagramMedia)
            .join(InstagramAccount)
            .where(
                InstagramComment.id.in_(source_ids["comment"]),
                InstagramAccount.analytics_project_id == project.id,
            )
            .options(joinedload(InstagramComment.instagram_media))
        ).all()
    } if source_ids["comment"] else {}
    assessments = {
        item.id: item for item in db.session.scalars(
            select(InstagramCommentAssessment)
            .join(InstagramComment)
            .join(InstagramMedia)
            .join(InstagramAccount)
            .where(
                InstagramCommentAssessment.id.in_(source_ids["comment_assessment"]),
                InstagramAccount.analytics_project_id == project.id,
            ).options(joinedload(InstagramCommentAssessment.instagram_comment).joinedload(InstagramComment.instagram_media))
        ).all()
    } if source_ids["comment_assessment"] else {}
    views = []
    for reference in references:
        item = None
        if reference.source_type == "media":
            item = media.get(reference.source_id)
            if item:
                caption = item.caption_original.strip() or item.external_id or "Publicação sem legenda"
                views.append(EvidenceView(reference.source_type, item.id, reference.role, "Publicação", caption,
                                          media_id=item.id, permalink=_safe_permalink(item.permalink)))
                continue
        elif reference.source_type == "media_observation":
            item = observations.get(reference.source_id)
            if item:
                media_item = item.instagram_media
                caption = media_item.caption_original.strip() or media_item.external_id or "Publicação sem legenda"
                views.append(EvidenceView(reference.source_type, item.id, reference.role, "Observação de métricas",
                                          f"{caption} · observada em {item.observed_at.isoformat()}",
                                          media_id=media_item.id, permalink=_safe_permalink(media_item.permalink)))
                continue
        elif reference.source_type == "media_classification":
            item = classifications.get(reference.source_id)
            if item:
                views.append(EvidenceView(reference.source_type, item.id, reference.role, "Classificação editorial",
                                          f"{item.dimension.name}: {item.category.name}", media_id=item.instagram_media_id,
                                          permalink=_safe_permalink(item.instagram_media.permalink)))
                continue
        elif reference.source_type == "comment":
            item = comments.get(reference.source_id)
            if item:
                text = item.original_text.strip()
                snippet = text if len(text) <= 140 else f"{text[:137]}…"
                views.append(EvidenceView(reference.source_type, item.id, reference.role, "Comentário", snippet,
                                          media_id=item.instagram_media_id, comment_id=item.id,
                                          permalink=_safe_permalink(item.instagram_media.permalink)))
                continue
        elif reference.source_type == "comment_assessment":
            item = assessments.get(reference.source_id)
            if item:
                labels = []
                if item.sentiment:
                    labels.append(SENTIMENT_LABELS[item.sentiment])
                if item.intent:
                    labels.append(INTENT_LABELS[item.intent])
                if item.risk:
                    labels.append(f"Risco {RISK_LABELS[item.risk]}")
                source = "revisão humana" if item.source in HUMAN_ASSESSMENT_SOURCES else "classificação automática"
                views.append(EvidenceView(reference.source_type, item.id, reference.role, "Avaliação de comentário",
                                          f"{source}: {', '.join(labels) or 'sem campos informados'}",
                                          media_id=item.instagram_comment.instagram_media_id,
                                          comment_id=item.instagram_comment_id,
                                          permalink=_safe_permalink(item.instagram_comment.instagram_media.permalink)))
                continue
        views.append(EvidenceView(reference.source_type, reference.source_id, reference.role,
                                  "Evidência histórica", "Registro de origem indisponível."))
    return tuple(views)


def grouped_findings(findings: Iterable[InstagramInsightFinding]) -> dict[str, tuple[InstagramInsightFinding, ...]]:
    labels = {
        "content_performance": "Conteúdo e performance",
        "media_type_performance": "Tipo de mídia",
        "reception": "Recepção",
        "content_reception": "Conteúdo × recepção",
    }
    grouped: dict[str, list[InstagramInsightFinding]] = defaultdict(list)
    for finding in findings:
        grouped[labels.get(finding.kind, "Outros achados")].append(finding)
    return {
        label: tuple(sorted(items, key=lambda item: (
            -SUPPORT_RANK[item.support_level],
            -(abs(item.absolute_difference) if item.absolute_difference is not None else 0),
            item.rule_id, item.group_label.casefold(), item.id,
        )))
        for label, items in sorted(grouped.items(), key=lambda item: item[0])
    }


__all__ = [
    "INSTAGRAM_INSIGHTS_VERSION", "MIN_COMPARISON_N", "MIN_COVERAGE", "MIN_GROUP_N",
    "EvidenceView", "FindingDraft", "InstagramInsightsError", "InstagramInsightsPersistenceError",
    "build_insight_drafts", "evidence_views", "finding_for_project", "generate_insights",
    "grouped_findings", "insight_thresholds", "latest_insight_run", "support_for",
]
