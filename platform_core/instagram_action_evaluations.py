"""Acompanhamento observacional, reproduzível e humano de ações Instagram.

Este módulo não cria Insights, Recommendations ou Actions. Ele seleciona um
AnalyticsRun posterior explícito, calcula um snapshot com as métricas canônicas
e registra a interpretação escolhida pela pessoa usuária.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from statistics import median
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload

from .extensions import db
from .instagram_cohorts import cohort_label, cohort_media_ids, cohort_signature, normalize_cohort_snapshot
from .instagram_metrics import DERIVED_METRICS_BY_ID, calculate_derived_metrics
from .models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramMediaObservation,
    InstagramRecommendationAction,
    InstagramRecommendationActionEvaluation,
)


INSTAGRAM_ACTION_EVALUATION_VERSION = "instagram-action-evaluation-v2"
ASSESSMENTS = frozenset({"consistent", "not_consistent", "inconclusive", "not_evaluable"})
ASSESSMENT_LABELS = {
    "consistent": "Consistente com a expectativa",
    "not_consistent": "Não consistente com a expectativa",
    "inconclusive": "Inconclusivo",
    "not_evaluable": "Não avaliável",
}
RAW_METRIC_LABELS = {
    "reach": "Alcance", "impressions": "Impressões", "plays": "Reproduções",
    "likes": "Curtidas", "comments_count": "Comentários", "shares": "Compartilhamentos",
    "saves": "Salvamentos", "interactions": "Interações", "follows_generated": "Seguidores gerados",
}
_SMALL_SAMPLE = 3
_LOW_COVERAGE = 0.70


class InstagramActionEvaluationError(ValueError):
    """Dados inválidos para criar ou alterar um acompanhamento."""


class InstagramActionEvaluationPersistenceError(RuntimeError):
    """Persistência falhou sem criar um acompanhamento parcial."""


class InstagramActionEvaluationDuplicateError(InstagramActionEvaluationError):
    def __init__(self, evaluation_id: str):
        super().__init__("Este período já foi acompanhado para esta ação/teste.")
        self.evaluation_id = evaluation_id


@dataclass(frozen=True)
class ActionEvaluationPreview:
    action: InstagramRecommendationAction
    analytics_run: AnalyticsRun
    metric_key: str | None
    cohort_json: Mapping[str, str] | None
    cohort_signature: str | None
    metric_label: str
    unit: str | None
    baseline_value: float | None
    followup_value: float | None
    absolute_change: float | None
    relative_change: float | None
    baseline_sample_size: int | None
    baseline_coverage: float | None
    followup_sample_size: int | None
    followup_population_size: int | None
    followup_coverage: float | None
    limitations: tuple[str, ...]
    evaluation_version: str = INSTAGRAM_ACTION_EVALUATION_VERSION

    @property
    def evaluable(self) -> bool:
        return (
            self.metric_key is not None
            and self.cohort_signature is not None
            and self.baseline_value is not None
            and self.followup_value is not None
        )

    @property
    def cohort_display(self) -> str:
        return cohort_label(self.cohort_json) or "Coorte não disponível"

    @property
    def baseline_display(self) -> str:
        return format_metric_value(self.baseline_value, self.unit)

    @property
    def followup_display(self) -> str:
        return format_metric_value(self.followup_value, self.unit)

    @property
    def absolute_display(self) -> str:
        return format_metric_value(self.absolute_change, self.unit, signed=True)

    @property
    def relative_display(self) -> str:
        return format_metric_value(
            self.relative_change,
            "%",
            signed=True,
            percentage_fraction=True,
            fixed_decimals=2,
        )

    @property
    def followup_context_display(self) -> str:
        if self.followup_sample_size is None or self.followup_population_size is None:
            return "Não disponível"
        coverage = "—" if self.followup_coverage is None else format_metric_value(
            self.followup_coverage * 100, "%"
        )
        return f"n = {self.followup_sample_size} de {self.followup_population_size} · cobertura {coverage}"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _metric_metadata(metric_key: str | None) -> tuple[str, str | None] | None:
    if not metric_key:
        return None
    if metric_key in RAW_METRIC_LABELS:
        return RAW_METRIC_LABELS[metric_key], "contagem"
    definition = DERIVED_METRICS_BY_ID.get(metric_key)
    if definition is None:
        return None
    return definition.name, definition.unit


def metric_label(metric_key: str | None) -> str | None:
    """Retorna o rótulo humano canônico de uma métrica para apresentação."""
    metadata = _metric_metadata(metric_key)
    return metadata[0] if metadata is not None else None


def format_metric_value(
    value: float | int | None,
    unit: str | None,
    *,
    signed: bool = False,
    percentage_fraction: bool = False,
    fixed_decimals: int | None = None,
) -> str:
    """Apresenta snapshots sem transformar NULL em zero nem arredondar cálculos."""
    if value is None:
        return "Não disponível"
    rendered_value = value * 100 if percentage_fraction else value
    sign = "+" if signed and rendered_value > 0 else ""
    if fixed_decimals is None and percentage_fraction:
        fixed_decimals = 2
    if fixed_decimals is not None:
        rendered = f"{rendered_value:,.{fixed_decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    elif float(rendered_value).is_integer():
        rendered = f"{int(rendered_value):,}".replace(",", ".")
    else:
        rendered = f"{rendered_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sign}{rendered}%" if unit == "%" else f"{sign}{rendered}"


def _run_duration_days(period_start: datetime | None, period_end: datetime | None) -> float | None:
    start, end = _as_utc(period_start), _as_utc(period_end)
    return (end - start).total_seconds() / 86_400 if start and end else None


def eligible_followup_runs(
    project: AnalyticsProject, action: InstagramRecommendationAction,
) -> tuple[AnalyticsRun, ...]:
    """Retorna somente runs concluídos inteiramente posteriores à conclusão.

    Períodos sem limites ou que se sobrepõem à intervenção são excluídos da V1
    para não apresentar uma comparação aparentemente limpa como se fosse tal.
    """
    completed_at = _as_utc(action.completed_at)
    if action.analytics_project_id != project.id or action.status != "completed" or completed_at is None:
        return ()
    runs = db.session.scalars(
        select(AnalyticsRun).where(
            AnalyticsRun.analytics_project_id == project.id,
            AnalyticsRun.status == "completed",
            AnalyticsRun.period_start.is_not(None),
            AnalyticsRun.period_end.is_not(None),
        ).order_by(AnalyticsRun.period_start.asc(), AnalyticsRun.completed_at.asc(), AnalyticsRun.id.asc())
    ).all()
    already_used = set(db.session.scalars(
        select(InstagramRecommendationActionEvaluation.analytics_run_id).where(
            InstagramRecommendationActionEvaluation.instagram_recommendation_action_id == action.id
        )
    ).all())
    return tuple(
        run for run in runs
        if run.id not in already_used and _as_utc(run.period_start) >= completed_at
    )


def evaluation_for_action_run(
    project: AnalyticsProject, action: InstagramRecommendationAction, run_id: str,
) -> InstagramRecommendationActionEvaluation | None:
    return db.session.scalar(
        select(InstagramRecommendationActionEvaluation)
        .join(AnalyticsRun)
        .where(
            InstagramRecommendationActionEvaluation.instagram_recommendation_action_id == action.id,
            InstagramRecommendationActionEvaluation.analytics_run_id == run_id,
            AnalyticsRun.analytics_project_id == project.id,
        )
        .options(joinedload(InstagramRecommendationActionEvaluation.analytics_run))
    )


def evaluations_for_action(
    project: AnalyticsProject, action: InstagramRecommendationAction,
) -> tuple[InstagramRecommendationActionEvaluation, ...]:
    items = db.session.scalars(
        select(InstagramRecommendationActionEvaluation)
        .join(AnalyticsRun)
        .where(
            InstagramRecommendationActionEvaluation.instagram_recommendation_action_id == action.id,
            AnalyticsRun.analytics_project_id == project.id,
        )
        .options(joinedload(InstagramRecommendationActionEvaluation.analytics_run))
    ).unique().all()
    return tuple(sorted(items, key=lambda item: (item.analytics_run.period_start or datetime.min.replace(tzinfo=timezone.utc), item.id)))


def _source_baseline_context(
    action: InstagramRecommendationAction, cohort: Mapping[str, str] | None,
) -> tuple[int | None, float | None, datetime | None, datetime | None]:
    links = action.recommendation.finding_links if action.recommendation is not None else ()
    findings = sorted((link.finding for link in links if link.finding is not None), key=lambda item: item.id)
    signature = cohort_signature(cohort)
    finding = next((item for item in findings if item.metric_key == action.target_metric
                    and cohort_signature(item.cohort_json) == signature), None)
    if finding is None:
        return None, None, None, None
    run = finding.insight_run
    population = (finding.details_json or {}).get("group_publications")
    coverage = finding.group_n / population if isinstance(population, int) and population > 0 else None
    return finding.group_n, coverage, run.period_start, run.period_end


def _followup_values(
    project: AnalyticsProject, run: AnalyticsRun, metric_key: str, cohort: Mapping[str, str] | None,
) -> tuple[float | None, int | None, int | None, float | None]:
    media_ids = cohort_media_ids(project, cohort)
    if media_ids is None:
        return None, None, None, None
    if not media_ids:
        return None, 0, 0, None
    observations = db.session.scalars(
        select(InstagramMediaObservation).where(
            InstagramMediaObservation.analytics_run_id == run.id,
            InstagramMediaObservation.instagram_media_id.in_(media_ids),
        )
    ).all()
    # O denominador é o universo de observações do run selecionado que pertence
    # à coorte. Mídia da mesma coorte sem observação neste run não entra na
    # cobertura; NULL nunca vira zero.
    population = len(observations)
    values: list[int | float] = []
    for observation in observations:
        if metric_key in RAW_METRIC_LABELS:
            value = getattr(observation, metric_key)
        else:
            raw_values = {field: getattr(observation, field) for field in RAW_METRIC_LABELS}
            value = calculate_derived_metrics(raw_values).get(metric_key)
        if value is not None:
            values.append(value)
    known = len(values)
    return (float(median(values)) if values else None, known, population, known / population if population else None)


def preview_action_evaluation(
    project: AnalyticsProject, action: InstagramRecommendationAction, run_id: str,
) -> ActionEvaluationPreview:
    """Calcula uma prévia para um run explicitamente escolhido, sem persistir."""
    if action.analytics_project_id != project.id:
        raise InstagramActionEvaluationError("A ação/teste não pertence a este projeto.")
    if action.status != "completed":
        raise InstagramActionEvaluationError("Acompanhamento formal só está disponível após a conclusão.")
    run = db.session.scalar(select(AnalyticsRun).where(
        AnalyticsRun.id == run_id, AnalyticsRun.analytics_project_id == project.id,
    ))
    if run is None:
        raise InstagramActionEvaluationError("O período selecionado não pertence a este projeto.")
    if run.id not in {candidate.id for candidate in eligible_followup_runs(project, action)}:
        raise InstagramActionEvaluationError("O período selecionado não é elegível para acompanhamento posterior.")

    metric_key = action.target_metric
    cohort = normalize_cohort_snapshot(action.cohort_json)
    cohort_sig = cohort_signature(cohort)
    metadata = _metric_metadata(metric_key)
    baseline_n, baseline_coverage, source_start, source_end = _source_baseline_context(action, cohort)
    limitations: list[str] = []
    if cohort is None:
        limitations.append(
            "A coorte original desta ação/teste não pôde ser reconstruída com segurança para o período selecionado."
        )
    if metadata is None:
        limitations.append("A métrica registrada na ação/teste não possui cálculo canônico de follow-up nesta versão.")
        metric_label, unit = "Métrica não avaliável", None
        followup_value, followup_n, followup_population, followup_coverage = None, 0, 0, None
    else:
        metric_label, unit = metadata
        followup_value, followup_n, followup_population, followup_coverage = _followup_values(
            project, run, metric_key, cohort,
        )
    baseline = action.baseline_value
    if action.target_metric is None:
        limitations.append("A ação/teste não registrou uma métrica-alvo.")
    if baseline is None:
        limitations.append("A baseline registrada na ação/teste não está disponível.")
    if cohort is not None and followup_value is None:
        limitations.append("Não há valor conhecido da métrica no período de follow-up selecionado.")
    if followup_n and followup_n < _SMALL_SAMPLE:
        limitations.append(f"O follow-up possui amostra pequena (n = {followup_n}).")
    if followup_coverage is not None and followup_coverage < _LOW_COVERAGE:
        limitations.append(f"A cobertura do follow-up é de {followup_coverage * 100:.0f}%, abaixo de 70%.")
    if baseline is not None and followup_value is not None:
        absolute = followup_value - baseline
        relative = absolute / baseline if baseline != 0 else None
        if baseline == 0:
            limitations.append("A variação relativa não é calculável com baseline zero.")
    else:
        absolute = relative = None
    baseline_duration = _run_duration_days(source_start, source_end)
    followup_duration = _run_duration_days(run.period_start, run.period_end)
    if baseline_duration and followup_duration and max(baseline_duration, followup_duration) / min(baseline_duration, followup_duration) >= 2:
        limitations.append("Os períodos de baseline e follow-up têm durações substancialmente diferentes.")
    return ActionEvaluationPreview(
        action=action, analytics_run=run, metric_key=metric_key, cohort_json=cohort,
        cohort_signature=cohort_sig, metric_label=metric_label, unit=unit,
        baseline_value=baseline, followup_value=followup_value, absolute_change=absolute,
        relative_change=relative, baseline_sample_size=baseline_n, baseline_coverage=baseline_coverage,
        followup_sample_size=followup_n, followup_population_size=followup_population,
        followup_coverage=followup_coverage, limitations=tuple(limitations),
    )


def _learning_summary(values: Mapping[str, object]) -> str:
    summary = str(values.get("learning_summary") or "").strip()
    if not summary:
        raise InstagramActionEvaluationError("Registre o aprendizado observado.")
    if len(summary) > 4_000:
        raise InstagramActionEvaluationError("O aprendizado excede o limite de 4.000 caracteres.")
    return summary


def _assessment(values: Mapping[str, object], preview: ActionEvaluationPreview) -> str:
    assessment = str(values.get("assessment") or "").strip()
    if assessment not in ASSESSMENTS:
        raise InstagramActionEvaluationError("Selecione uma avaliação válida.")
    if not preview.evaluable and assessment != "not_evaluable":
        raise InstagramActionEvaluationError("Use “Não avaliável” enquanto faltarem condições técnicas mínimas.")
    if preview.evaluable and assessment == "not_evaluable":
        raise InstagramActionEvaluationError("Há dados suficientes para registrar uma avaliação humana do período.")
    return assessment


def create_action_evaluation(
    project: AnalyticsProject, action: InstagramRecommendationAction, run_id: str,
    values: Mapping[str, object], *, actor_id: str,
) -> InstagramRecommendationActionEvaluation:
    """Persiste snapshot e interpretação humana em uma única transação."""
    if project.status != "active" or project.archived_at is not None:
        raise InstagramActionEvaluationError("Projetos arquivados não podem receber acompanhamentos.")
    existing = evaluation_for_action_run(project, action, run_id)
    if existing is not None:
        raise InstagramActionEvaluationDuplicateError(existing.id)
    preview = preview_action_evaluation(project, action, run_id)
    assessment = _assessment(values, preview)
    evaluation = InstagramRecommendationActionEvaluation(
        instagram_recommendation_action_id=action.id,
        analytics_run_id=preview.analytics_run.id,
        created_by_user_id=actor_id,
        metric_key=preview.metric_key,
        cohort_json=dict(preview.cohort_json) if preview.cohort_json else None,
        cohort_signature=preview.cohort_signature,
        baseline_value=preview.baseline_value,
        followup_value=preview.followup_value,
        absolute_change=preview.absolute_change,
        relative_change=preview.relative_change,
        baseline_sample_size=preview.baseline_sample_size,
        baseline_coverage=preview.baseline_coverage,
        followup_sample_size=preview.followup_sample_size,
        followup_population_size=preview.followup_population_size,
        followup_coverage=preview.followup_coverage,
        assessment=assessment,
        learning_summary=_learning_summary(values),
        limitations_json=list(preview.limitations),
        evaluation_version=preview.evaluation_version,
    )
    try:
        db.session.add(evaluation)
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        existing = evaluation_for_action_run(project, action, run_id)
        if existing is not None:
            raise InstagramActionEvaluationDuplicateError(existing.id) from error
        raise InstagramActionEvaluationPersistenceError("Não foi possível salvar o acompanhamento.") from error
    except (SQLAlchemyError, ValueError) as error:
        db.session.rollback()
        raise InstagramActionEvaluationPersistenceError(
            "Não foi possível salvar o acompanhamento. Nenhum registro parcial foi gravado."
        ) from error
    return evaluation


def update_action_evaluation(
    project: AnalyticsProject, evaluation: InstagramRecommendationActionEvaluation, values: Mapping[str, object],
) -> InstagramRecommendationActionEvaluation:
    """Atualiza apenas a interpretação humana; snapshots quantitativos são imutáveis."""
    if project.status != "active" or project.archived_at is not None:
        raise InstagramActionEvaluationError("Projetos arquivados não podem alterar acompanhamentos.")
    assessment = str(values.get("assessment") or "").strip()
    if assessment not in ASSESSMENTS:
        raise InstagramActionEvaluationError("Selecione uma avaliação válida.")
    evaluable = not (
        _metric_metadata(evaluation.metric_key) is None
        or evaluation.cohort_signature is None
        or evaluation.baseline_value is None
        or evaluation.followup_value is None
    )
    if not evaluable and assessment != "not_evaluable":
        raise InstagramActionEvaluationError("Este acompanhamento permanece não avaliável com os dados registrados.")
    if evaluable and assessment == "not_evaluable":
        raise InstagramActionEvaluationError("Há dados suficientes para registrar uma avaliação humana do período.")
    evaluation.assessment = assessment
    evaluation.learning_summary = _learning_summary(values)
    try:
        db.session.commit()
    except (SQLAlchemyError, ValueError) as error:
        db.session.rollback()
        raise InstagramActionEvaluationPersistenceError("Não foi possível atualizar o acompanhamento.") from error
    return evaluation


def evaluation_view(evaluation: InstagramRecommendationActionEvaluation) -> ActionEvaluationPreview:
    metadata = _metric_metadata(evaluation.metric_key)
    label, unit = metadata if metadata is not None else ("Métrica não avaliável", None)
    cohort = normalize_cohort_snapshot(evaluation.cohort_json)
    limitations = list(evaluation.limitations_json or ())
    if cohort is None and "A coorte original desta ação/teste não pôde ser reconstruída com segurança para o período selecionado." not in limitations:
        limitations.append("Acompanhamento legado: a coorte original não foi registrada e o snapshot histórico foi preservado sem recálculo.")
    return ActionEvaluationPreview(
        action=evaluation.action, analytics_run=evaluation.analytics_run, metric_key=evaluation.metric_key,
        cohort_json=cohort, cohort_signature=evaluation.cohort_signature,
        metric_label=label, unit=unit, baseline_value=evaluation.baseline_value,
        followup_value=evaluation.followup_value, absolute_change=evaluation.absolute_change,
        relative_change=evaluation.relative_change, baseline_sample_size=evaluation.baseline_sample_size,
        baseline_coverage=evaluation.baseline_coverage, followup_sample_size=evaluation.followup_sample_size,
        followup_population_size=evaluation.followup_population_size,
        followup_coverage=evaluation.followup_coverage,
        limitations=tuple(limitations), evaluation_version=evaluation.evaluation_version,
    )


__all__ = [
    "ASSESSMENT_LABELS", "ASSESSMENTS", "INSTAGRAM_ACTION_EVALUATION_VERSION",
    "ActionEvaluationPreview", "InstagramActionEvaluationDuplicateError", "InstagramActionEvaluationError",
    "InstagramActionEvaluationPersistenceError", "create_action_evaluation", "eligible_followup_runs",
    "evaluation_for_action_run", "evaluation_view", "evaluations_for_action", "format_metric_value",
    "metric_label",
    "preview_action_evaluation", "update_action_evaluation",
]
