"""Aprendizados consolidados, humanos e rastreáveis do Instagram."""

from __future__ import annotations

from typing import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload

from .extensions import db
from .instagram_cohorts import cohort_label, normalize_cohort_snapshot
from .models import (
    AnalyticsProject,
    InstagramLearning,
    InstagramRecommendation,
    InstagramRecommendationAction,
    InstagramRecommendationActionEvaluation,
    InstagramRecommendationFinding,
)


INSTAGRAM_LEARNING_VERSION = "instagram-learning-v1"
LEARNING_ASSESSMENTS = frozenset({
    "consistent", "not_consistent", "mixed", "inconclusive", "not_evaluable",
})
LEARNING_ASSESSMENT_LABELS = {
    "consistent": "Consistente com a expectativa",
    "not_consistent": "Não consistente com a expectativa",
    "mixed": "Misto",
    "inconclusive": "Inconclusivo",
    "not_evaluable": "Não avaliável",
}


class InstagramLearningError(ValueError):
    """Entrada ou vínculo incompatível para um aprendizado consolidado."""


class InstagramLearningPersistenceError(RuntimeError):
    """Falha transacional sem aprendizado parcial."""


def _text(values: Mapping[str, object], key: str, label: str, maximum: int, *, required: bool = False) -> str:
    value = str(values.get(key) or "").strip()
    if required and not value:
        raise InstagramLearningError(f"Informe {label}.")
    if len(value) > maximum:
        raise InstagramLearningError(f"{label.capitalize()} excede o limite de {maximum:,} caracteres.")
    return value


def _assessment(values: Mapping[str, object]) -> str:
    value = str(values.get("assessment") or "").strip()
    if value not in LEARNING_ASSESSMENTS:
        raise InstagramLearningError("Selecione uma interpretação válida.")
    return value


def _evaluation_query(project: AnalyticsProject):
    return (
        select(InstagramRecommendationActionEvaluation)
        .join(InstagramRecommendationAction)
        .where(InstagramRecommendationAction.analytics_project_id == project.id)
        .options(
            joinedload(InstagramRecommendationActionEvaluation.analytics_run),
            joinedload(InstagramRecommendationActionEvaluation.action)
            .joinedload(InstagramRecommendationAction.recommendation)
            .selectinload(InstagramRecommendation.finding_links)
            .joinedload(InstagramRecommendationFinding.finding),
        )
    )


def comparable_evaluations(project: AnalyticsProject) -> tuple[InstagramRecommendationActionEvaluation, ...]:
    """Lista snapshots completos que podem compor uma síntese quantitativa."""
    evaluations = db.session.scalars(_evaluation_query(project)).unique().all()
    items = [
        item for item in evaluations
        if item.metric_key and item.cohort_signature and item.baseline_value is not None
        and item.followup_value is not None and normalize_cohort_snapshot(item.cohort_json)
    ]
    return tuple(sorted(items, key=lambda item: (item.created_at, item.id)))


def learnings_for_project(project: AnalyticsProject) -> tuple[InstagramLearning, ...]:
    items = db.session.scalars(
        select(InstagramLearning)
        .where(InstagramLearning.analytics_project_id == project.id)
        .options(selectinload(InstagramLearning.evaluations).joinedload(
            InstagramRecommendationActionEvaluation.analytics_run
        ))
        .order_by(InstagramLearning.created_at.desc(), InstagramLearning.id.desc())
    ).unique().all()
    return tuple(items)


def learning_for_project(project: AnalyticsProject, learning_id: str) -> InstagramLearning | None:
    return db.session.scalar(
        select(InstagramLearning)
        .where(
            InstagramLearning.id == learning_id,
            InstagramLearning.analytics_project_id == project.id,
        )
        .options(
            selectinload(InstagramLearning.evaluations)
            .joinedload(InstagramRecommendationActionEvaluation.analytics_run),
            selectinload(InstagramLearning.evaluations)
            .joinedload(InstagramRecommendationActionEvaluation.action)
            .joinedload(InstagramRecommendationAction.recommendation)
            .selectinload(InstagramRecommendation.finding_links)
            .joinedload(InstagramRecommendationFinding.finding),
        )
    )


def _selected_evaluations(
    project: AnalyticsProject, evaluation_ids: Iterable[object],
) -> tuple[InstagramRecommendationActionEvaluation, ...]:
    requested = tuple(dict.fromkeys(str(item).strip() for item in evaluation_ids if str(item).strip()))
    if not requested:
        raise InstagramLearningError("Selecione ao menos um acompanhamento comparável.")
    evaluations = tuple(
        item for item in comparable_evaluations(project) if item.id in set(requested)
    )
    if len(evaluations) != len(requested):
        raise InstagramLearningError("Um ou mais acompanhamentos selecionados não pertencem ao projeto ou não são comparáveis.")
    first = evaluations[0]
    if any(
        item.metric_key != first.metric_key or item.cohort_signature != first.cohort_signature
        for item in evaluations[1:]
    ):
        raise InstagramLearningError("Selecione apenas acompanhamentos da mesma métrica e da mesma coorte.")
    return evaluations


def create_learning(
    project: AnalyticsProject, values: Mapping[str, object], evaluation_ids: Iterable[object], *, actor_id: str,
) -> InstagramLearning:
    if project.status != "active" or project.archived_at is not None:
        raise InstagramLearningError("Projetos arquivados não podem receber aprendizados consolidados.")
    evaluations = _selected_evaluations(project, evaluation_ids)
    first = evaluations[0]
    cohort = normalize_cohort_snapshot(first.cohort_json)
    learning = InstagramLearning(
        analytics_project_id=project.id,
        created_by_user_id=actor_id,
        title=_text(values, "title", "um título", 320, required=True),
        learning_summary=_text(values, "learning_summary", "a síntese humana", 4000, required=True),
        assessment=_assessment(values),
        metric_key=first.metric_key,
        cohort_json=cohort,
        cohort_signature=first.cohort_signature,
        learning_version=INSTAGRAM_LEARNING_VERSION,
        evaluations=list(evaluations),
    )
    try:
        db.session.add(learning)
        db.session.commit()
    except (SQLAlchemyError, ValueError) as error:
        db.session.rollback()
        raise InstagramLearningPersistenceError(
            "Não foi possível salvar o aprendizado consolidado. Nenhum registro parcial foi gravado."
        ) from error
    return learning


def update_learning(
    project: AnalyticsProject, learning: InstagramLearning, values: Mapping[str, object],
) -> InstagramLearning:
    if project.status != "active" or project.archived_at is not None:
        raise InstagramLearningError("Projetos arquivados não podem alterar aprendizados consolidados.")
    if learning.analytics_project_id != project.id:
        raise InstagramLearningError("O aprendizado não pertence a este projeto.")
    learning.title = _text(values, "title", "um título", 320, required=True)
    learning.learning_summary = _text(values, "learning_summary", "a síntese humana", 4000, required=True)
    learning.assessment = _assessment(values)
    try:
        db.session.commit()
    except (SQLAlchemyError, ValueError) as error:
        db.session.rollback()
        raise InstagramLearningPersistenceError("Não foi possível atualizar o aprendizado consolidado.") from error
    return learning


def delete_learning(project: AnalyticsProject, learning: InstagramLearning, confirmation: str) -> None:
    if project.status != "active" or project.archived_at is not None:
        raise InstagramLearningError("Projetos arquivados não podem excluir aprendizados consolidados.")
    if learning.analytics_project_id != project.id:
        raise InstagramLearningError("O aprendizado não pertence a este projeto.")
    if confirmation.strip() != "deletar":
        raise InstagramLearningError('Digite exatamente "deletar" para confirmar a exclusão.')
    try:
        db.session.delete(learning)
        db.session.commit()
    except (SQLAlchemyError, ValueError) as error:
        db.session.rollback()
        raise InstagramLearningPersistenceError("Não foi possível excluir o aprendizado consolidado.") from error


def learning_cohort_label(learning: InstagramLearning) -> str:
    return cohort_label(learning.cohort_json) or "Coorte não disponível"


__all__ = [
    "INSTAGRAM_LEARNING_VERSION",
    "LEARNING_ASSESSMENT_LABELS",
    "LEARNING_ASSESSMENTS",
    "InstagramLearningError",
    "InstagramLearningPersistenceError",
    "comparable_evaluations",
    "create_learning",
    "delete_learning",
    "learning_cohort_label",
    "learning_for_project",
    "learnings_for_project",
    "update_learning",
]
