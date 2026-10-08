"""Motor determinístico de recomendações derivadas de achados Instagram.

Esta camada nunca consulta métricas brutas para propor uma orientação. Ela usa
somente ``InstagramInsightFinding`` já persistido, preservando a cadeia
Recommendation -> Finding -> Evidence e o histórico de cada execução.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from .extensions import db
from .models import (
    AnalyticsProject,
    InstagramInsightFinding,
    InstagramInsightRun,
    InstagramRecommendation,
    InstagramRecommendationFinding,
    InstagramRecommendationRun,
)


INSTAGRAM_RECOMMENDATIONS_VERSION = "instagram-recommendations-v1"
MAX_RECOMMENDATIONS_PER_RUN = 12
MIN_ACTIONABLE_SUPPORT = "moderate"
MIN_ACTIONABLE_N = 3
MIN_ACTIONABLE_COVERAGE = 0.70

SUPPORT_RANK = {"exploratory": 1, "moderate": 2, "consistent": 3}
PRIORITY_RANK = {"low": 1, "medium": 2, "high": 3}
RECOMMENDATION_TYPES = frozenset({
    "content_mix",
    "editorial_strategy",
    "media_format",
    "information_clarity",
    "community_response",
    "risk_review",
    "measurement_quality",
})

PERFORMANCE_RULES = frozenset({
    "editorial_metric_difference",
    "media_type_metric_difference",
})
RECEPTION_RULES = frozenset({
    "comment_sentiment_concentration",
    "comment_question_concentration",
    "comment_response_need",
    "comment_risk_concentration",
    "comment_topic_recurrence",
    "editorial_reception_difference",
})
RECOMMENDATION_RULES = {
    "performance_superior": {"version": "performance_superior-v1"},
    "performance_inferior": {"version": "performance_inferior-v1"},
    "performance_mixed_signal": {"version": "performance_mixed_signal-v1"},
    "recurring_questions": {"version": "recurring_questions-v1"},
    "response_need": {"version": "response_need-v1"},
    "risk_review": {"version": "risk_review-v1"},
    "negative_sentiment": {"version": "negative_sentiment-v1"},
    "recurring_topic": {"version": "recurring_topic-v1"},
    "measurement_quality_exploratory_support": {
        "version": "measurement_quality_exploratory_support-v1",
    },
}


class InstagramRecommendationsError(ValueError):
    """O run de Insights ou o projeto não é elegível para a geração."""


class InstagramRecommendationsPersistenceError(RuntimeError):
    """Falha transacional sem recomendações parciais persistidas."""


@dataclass(frozen=True)
class RecommendationDraft:
    canonical_key: str
    rule_id: str
    rule_version: str
    title: str
    summary: str
    recommendation_type: str
    priority: str
    support_level: str
    rationale: str
    limitations: tuple[str, ...]
    findings: tuple[InstagramInsightFinding, ...]


def recommendation_thresholds() -> dict[str, int | float | str]:
    """Snapshot explícito da política V1, registrado em cada run."""
    return {
        "max_recommendations_per_run": MAX_RECOMMENDATIONS_PER_RUN,
        "minimum_actionable_support": MIN_ACTIONABLE_SUPPORT,
        "minimum_group_or_comparison_n": MIN_ACTIONABLE_N,
        "minimum_coverage": MIN_ACTIONABLE_COVERAGE,
        "support_aggregation": "minimum_source_support",
        "priority_policy": "risk_high; otherwise support, convergence and coverage",
    }


def _canonical_part(value: str) -> str:
    return "-".join(value.casefold().split())


def _metric_label(finding: InstagramInsightFinding) -> str:
    value = (finding.details_json or {}).get("metric_label")
    if isinstance(value, str) and value.strip():
        return value.strip()
    labels = {
        "reach": "alcance",
        "interactions": "interações",
        "saves": "salvamentos",
        "shares": "compartilhamentos",
        "comments_count": "comentários",
        "interaction_rate_by_reach": "taxa de interação por alcance",
        "saves_rate_by_reach": "salvamentos por alcance",
        "shares_rate_by_reach": "compartilhamentos por alcance",
    }
    return labels.get(finding.metric_key or "", finding.metric_key or "métrica registrada")


def _support_for(findings: Sequence[InstagramInsightFinding]) -> str:
    return min((item.support_level for item in findings), key=lambda value: SUPPORT_RANK[value])


def _eligible_actionable(finding: InstagramInsightFinding) -> bool:
    """Exige o limiar V1 mesmo que um finding antigo seja carregado manualmente."""
    return (
        SUPPORT_RANK.get(finding.support_level, 0) >= SUPPORT_RANK[MIN_ACTIONABLE_SUPPORT]
        and min(finding.group_n, finding.comparison_n) >= MIN_ACTIONABLE_N
        and finding.coverage >= MIN_ACTIONABLE_COVERAGE
    )


def _limitations(findings: Sequence[InstagramInsightFinding]) -> tuple[str, ...]:
    limitations = [
        "Os dados descrevem associações observadas no período e não demonstram causalidade.",
    ]
    if any(item.coverage < 1 for item in findings):
        limitations.append("A cobertura disponível não inclui todos os registros possíveis do recorte.")
    if any(min(item.group_n, item.comparison_n) < 5 for item in findings):
        limitations.append("Pelo menos um grupo possui amostra reduzida; acompanhe o padrão em novo ciclo.")
    if any(item.support_level == "moderate" for item in findings):
        limitations.append("O suporte é moderado; a orientação deve ser testada e acompanhada, não tratada como certeza.")
    if any(item.rule_id in RECEPTION_RULES for item in findings):
        limitations.append("Comentários avaliados não representam automaticamente toda a audiência.")
    return tuple(limitations)


def _priority_for(recommendation_type: str, support_level: str,
                  findings: Sequence[InstagramInsightFinding]) -> str:
    """Heurística operacional explícita; não mede urgência objetiva nem certeza."""
    if recommendation_type == "risk_review":
        return "high"
    if (
        support_level == "consistent"
        and len(findings) >= 2
        and min(item.coverage for item in findings) >= .90
    ):
        return "high"
    if support_level in {"moderate", "consistent"}:
        return "medium"
    return "low"


def _draft(*, canonical_key: str, rule_id: str, title: str, summary: str,
           recommendation_type: str, rationale: str,
           findings: Sequence[InstagramInsightFinding]) -> RecommendationDraft:
    if recommendation_type not in RECOMMENDATION_TYPES:
        raise InstagramRecommendationsError("Tipo de recomendação inválido.")
    rule = RECOMMENDATION_RULES.get(rule_id)
    if rule is None:
        raise InstagramRecommendationsError("Regra de recomendação inválida.")
    ordered_findings = tuple(sorted(findings, key=lambda item: (item.id, item.rule_id)))
    support_level = _support_for(ordered_findings)
    return RecommendationDraft(
        canonical_key=canonical_key,
        rule_id=rule_id,
        rule_version=rule["version"],
        title=title,
        summary=summary,
        recommendation_type=recommendation_type,
        priority=_priority_for(recommendation_type, support_level, ordered_findings),
        support_level=support_level,
        rationale=rationale,
        limitations=_limitations(ordered_findings),
        findings=ordered_findings,
    )


def _performance_drafts(findings: Iterable[InstagramInsightFinding]) -> list[RecommendationDraft]:
    """Converge métricas compatíveis e trata sinais opostos como hipótese de teste."""
    groups: dict[tuple[str, str], list[InstagramInsightFinding]] = defaultdict(list)
    for finding in findings:
        if finding.rule_id not in PERFORMANCE_RULES or not _eligible_actionable(finding):
            continue
        if finding.absolute_difference is None or finding.absolute_difference == 0:
            continue
        source = "editorial" if finding.rule_id == "editorial_metric_difference" else "media"
        groups[(source, finding.group_label)].append(finding)

    drafts: list[RecommendationDraft] = []
    for (source, group_label), group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1].casefold())):
        positive = [item for item in group if (item.absolute_difference or 0) > 0]
        negative = [item for item in group if (item.absolute_difference or 0) < 0]
        key_prefix = "editorial" if source == "editorial" else "media"
        if positive and negative:
            metrics = ", ".join(sorted({_metric_label(item) for item in group}, key=str.casefold))
            drafts.append(_draft(
                canonical_key=f"performance-mixed:{key_prefix}:{_canonical_part(group_label)}",
                rule_id="performance_mixed_signal",
                title=f"Testar {group_label} com acompanhamento por métrica",
                summary=(
                    f"No período analisado, {group_label} apresentou sinais distintos entre {metrics}. "
                    "Vale avaliar um teste delimitado e acompanhar cada métrica, sem assumir uma direção única."
                ),
                recommendation_type="editorial_strategy" if source == "editorial" else "media_format",
                rationale=(
                    "Gerada porque achados persistidos para o mesmo grupo apontam direções diferentes; "
                    "a regra evita transformar esse conflito em orientações incompatíveis."
                ),
                findings=group,
            ))
            continue

        direction = "positive" if positive else "negative"
        selected = positive or negative
        metrics = ", ".join(sorted({_metric_label(item) for item in selected}, key=str.casefold))
        if direction == "positive":
            if any(item.metric_key in {"saves", "saves_rate_by_reach"} for item in selected):
                action = "testar conteúdos de referência ou consultáveis com características semelhantes"
            elif any(item.metric_key in {"shares", "shares_rate_by_reach"} for item in selected):
                action = "testar conteúdos com características semelhantes em novo ciclo"
            else:
                action = "testar uma presença maior desse tipo de conteúdo em um próximo ciclo"
            title = f"Testar maior presença de {group_label}"
            summary = (
                f"No período analisado, {group_label} apresentou mediana superior em {metrics}. "
                f"Considere {action} e acompanhar se o padrão se repete."
            )
            recommendation_type = "content_mix" if source == "editorial" else "media_format"
            rule_id = "performance_superior"
            rationale = (
                "Gerada porque os achados de desempenho persistidos indicam diferença positiva "
                "com suporte e cobertura suficientes para uma hipótese de teste."
            )
        else:
            title = f"Revisar a abordagem de {group_label}"
            summary = (
                f"No período analisado, {group_label} apresentou mediana inferior em {metrics}. "
                "Vale avaliar a abordagem, o objetivo editorial e ajustes possíveis antes de tirar conclusões."
            )
            recommendation_type = "editorial_strategy" if source == "editorial" else "media_format"
            rule_id = "performance_inferior"
            rationale = (
                "Gerada porque os achados de desempenho persistidos indicam diferença negativa; "
                "a regra propõe revisão cautelosa, não interrupção automática do conteúdo."
            )
        drafts.append(_draft(
            canonical_key=f"performance-{direction}:{key_prefix}:{_canonical_part(group_label)}",
            rule_id=rule_id,
            title=title,
            summary=summary,
            recommendation_type=recommendation_type,
            rationale=rationale,
            findings=selected,
        ))
    return drafts


def _reception_issue(finding: InstagramInsightFinding) -> str | None:
    title = finding.title.casefold()
    label = finding.group_label.casefold()
    if finding.rule_id == "comment_topic_recurrence":
        return "topic"
    if finding.rule_id == "comment_question_concentration" or "pergunta" in title:
        return "questions"
    if finding.rule_id == "comment_response_need" or "necessidade de resposta" in title:
        return "response"
    if finding.rule_id == "comment_risk_concentration" or "risco" in title:
        return "risk"
    if (finding.rule_id == "comment_sentiment_concentration" and "negativo" in label) or "sentimento negativo" in title:
        return "negative_sentiment"
    return None


def _reception_drafts(findings: Iterable[InstagramInsightFinding]) -> list[RecommendationDraft]:
    groups: dict[tuple[str, str], list[InstagramInsightFinding]] = defaultdict(list)
    for finding in findings:
        issue = _reception_issue(finding)
        if not issue or not _eligible_actionable(finding):
            continue
        if finding.rule_id == "editorial_reception_difference" and (finding.absolute_difference or 0) <= 0:
            continue
        groups[(issue, finding.group_label)].append(finding)

    drafts: list[RecommendationDraft] = []
    for (issue, label), group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1].casefold())):
        suffix = f" em {label}" if label else ""
        if issue == "questions":
            title = f"Revisar a clareza informacional{suffix}"
            summary = (
                f"Os achados do período indicam concentração de perguntas{suffix}. "
                "Considere tornar informações, legenda ou instruções mais explícitas e acompanhar a recepção."
            )
            recommendation_type, rule_id = "information_clarity", "recurring_questions"
        elif issue == "response":
            title = f"Acompanhar a necessidade de resposta{suffix}"
            summary = (
                f"Os achados do período indicam comentários que podem demandar resposta{suffix}. "
                "Vale priorizar o acompanhamento da conversa e revisar se informações podem ser reforçadas."
            )
            recommendation_type, rule_id = "community_response", "response_need"
        elif issue == "risk":
            title = f"Priorizar revisão humana de risco{suffix}"
            summary = (
                f"Os achados do período indicam comentários com risco médio ou alto{suffix}. "
                "Considere revisar prioritariamente esse conjunto antes de qualquer decisão de moderação."
            )
            recommendation_type, rule_id = "risk_review", "risk_review"
        elif issue == "negative_sentiment":
            title = f"Revisar o contexto de comentários negativos{suffix}"
            summary = (
                f"Os achados do período indicam concentração de sentimento negativo{suffix}. "
                "Vale ler o contexto, identificar temas recorrentes e avaliar se algum esclarecimento é pertinente."
            )
            recommendation_type, rule_id = "community_response", "negative_sentiment"
        else:
            title = f"Considerar esclarecimento sobre o tema: {label}"
            summary = (
                f"Os achados do período registram recorrência do tema {label} nos comentários. "
                "Considere tratá-lo em conteúdo futuro ou incluir esclarecimentos, acompanhando se a recorrência se mantém."
            )
            recommendation_type, rule_id = "community_response", "recurring_topic"
        drafts.append(_draft(
            canonical_key=f"reception-{issue}:{_canonical_part(label)}",
            rule_id=rule_id,
            title=title,
            summary=summary,
            recommendation_type=recommendation_type,
            rationale=(
                "Gerada porque achados persistidos de recepção atingiram os critérios de suporte, "
                "amostra e cobertura desta versão."
            ),
            findings=group,
        ))
    return drafts


def _measurement_quality_drafts(findings: Iterable[InstagramInsightFinding]) -> list[RecommendationDraft]:
    """Transforma somente achados exploratórios existentes em orientação de medição."""
    drafts = []
    for finding in sorted(findings, key=lambda item: (item.group_label.casefold(), item.id)):
        if (
            finding.support_level != "exploratory"
            or min(finding.group_n, finding.comparison_n) < MIN_ACTIONABLE_N
            or finding.coverage < MIN_ACTIONABLE_COVERAGE
        ):
            continue
        drafts.append(_draft(
            canonical_key=f"measurement-quality:{finding.id}",
            rule_id="measurement_quality_exploratory_support",
            title=f"Ampliar a base de observação para {finding.group_label}",
            summary=(
                f"O achado sobre {finding.group_label} foi registrado com suporte exploratório. "
                "Antes de orientar mudanças editoriais, considere ampliar o período, a cobertura ou a classificação disponível."
            ),
            recommendation_type="measurement_quality",
            rationale=(
                "Gerada a partir de um achado persistido com suporte exploratório; "
                "não cria uma recomendação diretamente a partir de dados brutos."
            ),
            findings=(finding,),
        ))
    return drafts


def _ordered(drafts: Iterable[RecommendationDraft]) -> tuple[tuple[RecommendationDraft, ...], int]:
    by_key: dict[str, RecommendationDraft] = {}
    for draft in drafts:
        current = by_key.get(draft.canonical_key)
        if current is None:
            by_key[draft.canonical_key] = draft
            continue
        combined = tuple({item.id: item for item in (*current.findings, *draft.findings)}.values())
        by_key[draft.canonical_key] = _draft(
            canonical_key=current.canonical_key,
            rule_id=current.rule_id,
            title=current.title,
            summary=current.summary,
            recommendation_type=current.recommendation_type,
            rationale=current.rationale,
            findings=combined,
        )
    ordered = sorted(
        by_key.values(),
        key=lambda item: (
            -PRIORITY_RANK[item.priority],
            -SUPPORT_RANK[item.support_level],
            -len(item.findings),
            item.recommendation_type,
            item.title.casefold(),
            item.canonical_key,
        ),
    )
    return tuple(ordered[:MAX_RECOMMENDATIONS_PER_RUN]), len(ordered)


def build_recommendation_drafts(
    findings: Iterable[InstagramInsightFinding],
) -> tuple[tuple[RecommendationDraft, ...], int]:
    """Avalia somente findings persistidos e retorna drafts ordenados/reproduzíveis."""
    ordered_findings = tuple(sorted(findings, key=lambda item: (item.id, item.rule_id)))
    candidates = []
    candidates.extend(_performance_drafts(ordered_findings))
    candidates.extend(_reception_drafts(ordered_findings))
    candidates.extend(_measurement_quality_drafts(ordered_findings))
    return _ordered(candidates)


def recommendation_run_for_insight(
    project: AnalyticsProject, insight_run_id: str,
) -> InstagramRecommendationRun | None:
    return db.session.scalar(
        select(InstagramRecommendationRun)
        .where(
            InstagramRecommendationRun.analytics_project_id == project.id,
            InstagramRecommendationRun.instagram_insight_run_id == insight_run_id,
            InstagramRecommendationRun.engine_version == INSTAGRAM_RECOMMENDATIONS_VERSION,
        )
        .options(
            selectinload(InstagramRecommendationRun.recommendations)
            .selectinload(InstagramRecommendation.finding_links)
            .joinedload(InstagramRecommendationFinding.finding),
            selectinload(InstagramRecommendationRun.recommendations)
            .selectinload(InstagramRecommendation.actions),
            joinedload(InstagramRecommendationRun.insight_run),
        )
    )


def _finding_sort_key(finding: InstagramInsightFinding) -> tuple:
    return (finding.title.casefold(), finding.rule_id, finding.id)


def ordered_recommendations(
    recommendation_run: InstagramRecommendationRun | None,
) -> tuple[InstagramRecommendation, ...]:
    if recommendation_run is None:
        return ()
    return tuple(sorted(
        recommendation_run.recommendations,
        key=lambda item: (
            -PRIORITY_RANK[item.priority],
            -SUPPORT_RANK[item.support_level],
            -len(item.finding_links),
            item.recommendation_type,
            item.title.casefold(),
            item.id,
        ),
    ))


def recommendation_for_project(
    project: AnalyticsProject, recommendation_id: str,
) -> InstagramRecommendation | None:
    return db.session.scalar(
        select(InstagramRecommendation)
        .join(InstagramRecommendationRun)
        .where(
            InstagramRecommendation.id == recommendation_id,
            InstagramRecommendationRun.analytics_project_id == project.id,
        )
        .options(
            joinedload(InstagramRecommendation.recommendation_run)
            .joinedload(InstagramRecommendationRun.insight_run),
            selectinload(InstagramRecommendation.finding_links)
            .joinedload(InstagramRecommendationFinding.finding)
            .joinedload(InstagramInsightFinding.insight_run),
            selectinload(InstagramRecommendation.actions),
        )
    )


def generate_recommendations(
    project: AnalyticsProject, insight_run: InstagramInsightRun,
) -> InstagramRecommendationRun:
    """Persiste uma execução atômica ou retorna a mesma versão já gerada.

    A constraint ``insight_run + engine_version`` garante que uma repetição não
    crie duplicatas. Alterar a versão do motor abre deliberadamente novo
    histórico sem modificar recomendações existentes.
    """
    if project.status != "active" or project.archived_at is not None:
        raise InstagramRecommendationsError("Projetos arquivados não podem gerar novas recomendações.")
    if insight_run.analytics_project_id != project.id:
        raise InstagramRecommendationsError("A execução de Insights não pertence a este projeto.")
    existing = recommendation_run_for_insight(project, insight_run.id)
    if existing is not None:
        return existing

    findings = tuple(db.session.scalars(
        select(InstagramInsightFinding)
        .where(InstagramInsightFinding.instagram_insight_run_id == insight_run.id)
        .order_by(InstagramInsightFinding.id)
    ).all())
    drafts, candidate_count = build_recommendation_drafts(findings)
    linked_finding_ids = {finding.id for draft in drafts for finding in draft.findings}
    parameters = {
        "engine_version": INSTAGRAM_RECOMMENDATIONS_VERSION,
        "insight_engine_version": insight_run.engine_version,
        "insight_run_id": insight_run.id,
        "rules": recommendation_thresholds(),
        "candidate_count_before_limit": candidate_count,
        "findings_linked_to_recommendations": len(linked_finding_ids),
        "findings_ignored_by_rules": len(findings) - len(linked_finding_ids),
        "converged_source_findings": max(0, len(linked_finding_ids) - len(drafts)),
        "deduplicated_or_limited": candidate_count - len(drafts),
        "truncated_by_limit": candidate_count > MAX_RECOMMENDATIONS_PER_RUN,
    }
    try:
        run = InstagramRecommendationRun(
            analytics_project_id=project.id,
            instagram_insight_run_id=insight_run.id,
            engine_version=INSTAGRAM_RECOMMENDATIONS_VERSION,
            parameters_json=parameters,
            number_of_findings_considered=len(findings),
            number_of_recommendations_generated=len(drafts),
        )
        db.session.add(run)
        db.session.flush()
        for draft in drafts:
            recommendation = InstagramRecommendation(
                instagram_recommendation_run_id=run.id,
                canonical_key=draft.canonical_key,
                rule_id=draft.rule_id,
                rule_version=draft.rule_version,
                engine_version=INSTAGRAM_RECOMMENDATIONS_VERSION,
                title=draft.title,
                summary=draft.summary,
                recommendation_type=draft.recommendation_type,
                priority=draft.priority,
                support_level=draft.support_level,
                rationale=draft.rationale,
                limitations_json=list(draft.limitations),
            )
            db.session.add(recommendation)
            db.session.flush()
            db.session.add_all(
                InstagramRecommendationFinding(
                    instagram_recommendation_id=recommendation.id,
                    instagram_insight_finding_id=finding.id,
                )
                for finding in draft.findings
            )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = recommendation_run_for_insight(project, insight_run.id)
        if existing is not None:
            return existing
        raise InstagramRecommendationsPersistenceError(
            "Não foi possível gerar as recomendações; nenhuma execução parcial foi gravada."
        )
    except Exception as error:
        db.session.rollback()
        raise InstagramRecommendationsPersistenceError(
            "Não foi possível gerar as recomendações; nenhuma execução parcial foi gravada."
        ) from error
    return recommendation_run_for_insight(project, insight_run.id) or run


def linked_findings(recommendation: InstagramRecommendation) -> tuple[InstagramInsightFinding, ...]:
    """Exibe fontes em ordem estável e sem reconsultar evidências."""
    return tuple(sorted((item.finding for item in recommendation.finding_links), key=_finding_sort_key))
