"""Ações e testes operacionais, sempre derivados de recomendações Instagram.

Esta camada não consulta métricas brutas nem executa intervenções externas. Ela
somente transforma uma recomendação persistida em um registro revisável pelo
usuário, preservando a cadeia Action -> Recommendation -> Finding -> Evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload

from .extensions import db
from .models import (
    AnalyticsProject,
    InstagramInsightFinding,
    InstagramRecommendation,
    InstagramRecommendationAction,
    InstagramRecommendationActionEvaluation,
    InstagramRecommendationFinding,
    InstagramRecommendationRun,
)


ACTION_KINDS = frozenset({"action", "test"})
ACTION_STATUSES = frozenset({"planned", "in_progress", "completed", "cancelled"})
ACTION_OUTCOMES = frozenset({"supported", "not_supported", "inconclusive", "not_applicable"})
ACTION_STATUS_ORDER = {"in_progress": 0, "planned": 1, "completed": 2, "cancelled": 3}
MAX_DESCRIPTION_LENGTH = 4_000
MAX_OBJECTIVE_LENGTH = 1_500
MAX_HYPOTHESIS_LENGTH = 1_500
MAX_SUCCESS_CRITERION_LENGTH = 1_500
MAX_RESULT_SUMMARY_LENGTH = 4_000

ACTION_KIND_LABELS = {"action": "Ação", "test": "Teste"}
ACTION_STATUS_LABELS = {
    "planned": "Planejada",
    "in_progress": "Em andamento",
    "completed": "Concluída",
    "cancelled": "Cancelada",
}
ACTION_OUTCOME_LABELS = {
    "supported": "Resultado consistente",
    "not_supported": "Resultado não consistente",
    "inconclusive": "Inconclusivo",
    "not_applicable": "Não aplicável",
}
_TRANSITIONS = {
    "planned": frozenset({"in_progress", "cancelled"}),
    "in_progress": frozenset({"completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


class InstagramActionError(ValueError):
    """Dados operacionais inválidos ou transição não permitida."""


class InstagramActionPersistenceError(RuntimeError):
    """A persistência falhou sem deixar uma ação/teste parcial."""


@dataclass(frozen=True)
class ActionPrefill:
    action_kind: str
    title: str
    description: str
    objective: str
    hypothesis: str
    target_metric: str | None
    baseline_value: float | None


def _findings(recommendation: InstagramRecommendation) -> tuple[InstagramInsightFinding, ...]:
    return tuple(sorted(
        (link.finding for link in recommendation.finding_links if link.finding is not None),
        key=lambda item: (item.id, item.rule_id),
    ))


def _metric_label(finding: InstagramInsightFinding | None) -> str:
    if finding is None:
        return "a métrica registrada"
    label = (finding.details_json or {}).get("metric_label")
    if isinstance(label, str) and label.strip():
        return label.strip().casefold()
    return finding.metric_key or "a métrica registrada"


def _primary_finding(recommendation: InstagramRecommendation) -> InstagramInsightFinding | None:
    findings = _findings(recommendation)
    return findings[0] if findings else None


def action_prefill(
    recommendation: InstagramRecommendation, action_kind: str,
) -> ActionPrefill:
    """Cria um prefill reproduzível apenas com Recommendation -> Finding persistidos."""
    if action_kind not in ACTION_KINDS:
        raise InstagramActionError("Tipo de ação inválido.")

    finding = _primary_finding(recommendation)
    metric = finding.metric_key if finding and finding.metric_key else None
    baseline = finding.group_value if finding and finding.group_value is not None else None
    group = finding.group_label if finding else "o padrão registrado"
    metric_label = _metric_label(finding)
    recommendation_type = recommendation.recommendation_type
    title = recommendation.title if action_kind == "action" else f"Teste: {recommendation.title}"
    description = recommendation.summary

    if recommendation_type in {"content_mix", "editorial_strategy", "media_format"}:
        objective = (
            f"Acompanhar em novo ciclo se o padrão descrito para {group} se mantém em {metric_label}."
        )
        hypothesis = (
            f"A intervenção planejada para {group} pode permanecer associada a {metric_label}; "
            "a observação não demonstra causalidade."
        )
    elif recommendation_type == "information_clarity":
        objective = "Revisar a clareza das informações e registrar a intervenção antes de acompanhar a recepção."
        hypothesis = (
            "Uma instrução ou informação mais explícita pode coincidir com mudança na métrica acompanhada; "
            "isso não demonstra causalidade."
        )
    elif recommendation_type == "community_response":
        objective = "Organizar acompanhamento humano da conversa e registrar o que foi revisado."
        hypothesis = (
            "O acompanhamento estruturado pode ajudar a observar se o padrão de recepção se mantém; "
            "outros fatores também podem influenciar o resultado."
        )
    elif recommendation_type == "risk_review":
        objective = "Realizar revisão humana prioritária do contexto identificado, sem executar moderação automática."
        hypothesis = ""
    elif recommendation_type == "measurement_quality":
        objective = "Ampliar a cobertura ou a qualidade do registro antes de orientar mudança editorial."
        hypothesis = ""
    else:
        objective = "Registrar uma intervenção operacional vinculada à recomendação e acompanhar o novo ciclo."
        hypothesis = "A observação posterior deve ser interpretada sem assumir causalidade."

    if action_kind == "action":
        hypothesis = ""
    return ActionPrefill(
        action_kind=action_kind,
        title=title[:320],
        description=description,
        objective=objective,
        hypothesis=hypothesis,
        target_metric=metric,
        baseline_value=baseline,
    )


def parse_planned_datetime(value: str | None, timezone_name: str) -> datetime | None:
    """Interpreta ``datetime-local`` na timezone de apresentação e persiste UTC."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise InstagramActionError("Data planejada inválida.") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc)


def _text(values: Mapping[str, object], key: str, label: str, maximum: int, *, required: bool = False) -> str:
    value = str(values.get(key) or "").strip()
    if required and not value:
        raise InstagramActionError(f"Informe {label}.")
    if len(value) > maximum:
        raise InstagramActionError(f"{label.capitalize()} excede o limite de {maximum:,} caracteres.")
    return value


def _number(values: Mapping[str, object], key: str, label: str) -> float | None:
    value = values.get(key)
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError) as error:
        raise InstagramActionError(f"{label.capitalize()} deve ser um número válido.") from error
    if not isfinite(number):
        raise InstagramActionError(f"{label.capitalize()} deve ser um número finito.")
    return number


def _validate_project_recommendation(
    project: AnalyticsProject, recommendation: InstagramRecommendation,
) -> None:
    if recommendation.recommendation_run.analytics_project_id != project.id:
        raise InstagramActionError("A recomendação não pertence a este projeto.")


def create_recommendation_action(
    project: AnalyticsProject,
    recommendation: InstagramRecommendation,
    action_kind: str,
    values: Mapping[str, object],
    *,
    actor_id: str,
) -> InstagramRecommendationAction:
    """Valida e persiste uma ação/teste sem alterar Recommendation ou Findings."""
    if project.status != "active" or project.archived_at is not None:
        raise InstagramActionError("Projetos arquivados não podem receber ações ou testes.")
    if action_kind not in ACTION_KINDS:
        raise InstagramActionError("Tipo de ação inválido.")
    _validate_project_recommendation(project, recommendation)
    title = _text(values, "title", "um título", 320, required=True)
    description = _text(values, "description", "a descrição", MAX_DESCRIPTION_LENGTH)
    objective = _text(values, "objective", "o objetivo", MAX_OBJECTIVE_LENGTH, required=True)
    hypothesis = _text(values, "hypothesis", "a hipótese", MAX_HYPOTHESIS_LENGTH)
    if action_kind == "test" and not hypothesis:
        raise InstagramActionError("Informe a hipótese do teste.")
    target_metric = _text(values, "target_metric", "a métrica", 80) or None
    success_criterion = _text(
        values, "success_criterion", "o critério de sucesso", MAX_SUCCESS_CRITERION_LENGTH,
    ) or None
    planned_start_at = values.get("planned_start_at")
    planned_end_at = values.get("planned_end_at")
    if planned_start_at is not None and not isinstance(planned_start_at, datetime):
        raise InstagramActionError("Data inicial planejada inválida.")
    if planned_end_at is not None and not isinstance(planned_end_at, datetime):
        raise InstagramActionError("Data final planejada inválida.")
    if planned_start_at and planned_end_at and planned_end_at < planned_start_at:
        raise InstagramActionError("A data final planejada não pode ser anterior à data inicial.")

    action = InstagramRecommendationAction(
        analytics_project_id=project.id,
        instagram_recommendation_id=recommendation.id,
        created_by_user_id=actor_id,
        action_kind=action_kind,
        title=title,
        description=description,
        objective=objective,
        hypothesis=hypothesis or None,
        target_metric=target_metric,
        baseline_value=_number(values, "baseline_value", "a baseline"),
        target_value=_number(values, "target_value", "o valor-alvo"),
        success_criterion=success_criterion,
        planned_start_at=planned_start_at,
        planned_end_at=planned_end_at,
        status="planned",
    )
    try:
        db.session.add(action)
        db.session.commit()
    except (SQLAlchemyError, ValueError) as error:
        db.session.rollback()
        raise InstagramActionPersistenceError(
            "Não foi possível criar a ação ou teste. Nenhum registro parcial foi gravado."
        ) from error
    return action


def recommendation_action_for_project(
    project: AnalyticsProject, action_id: str,
) -> InstagramRecommendationAction | None:
    return db.session.scalar(
        select(InstagramRecommendationAction)
        .join(InstagramRecommendation)
        .join(InstagramRecommendationRun)
        .where(
            InstagramRecommendationAction.id == action_id,
            InstagramRecommendationAction.analytics_project_id == project.id,
            InstagramRecommendationRun.analytics_project_id == project.id,
        )
        .options(
            joinedload(InstagramRecommendationAction.recommendation)
            .joinedload(InstagramRecommendation.recommendation_run)
            .joinedload(InstagramRecommendationRun.insight_run),
            joinedload(InstagramRecommendationAction.recommendation)
            .selectinload(InstagramRecommendation.finding_links)
            .joinedload(InstagramRecommendationFinding.finding),
        )
    )


def delete_recommendation_action(
    project: AnalyticsProject, action: InstagramRecommendationAction, confirmation: str,
) -> None:
    """Remove uma ação/teste criada por engano, sem apagar acompanhamento histórico."""
    if project.status != "active" or project.archived_at is not None:
        raise InstagramActionError("Projetos arquivados não podem alterar ações ou testes.")
    if action.analytics_project_id != project.id:
        raise InstagramActionError("A ação/teste não pertence a este projeto.")
    if confirmation.strip() != "deletar":
        raise InstagramActionError('Digite exatamente "deletar" para confirmar a exclusão.')
    if recommendation_action_has_evaluation(action):
        raise InstagramActionError(
            "Esta ação/teste possui acompanhamento registrado e não pode ser excluída."
        )
    try:
        db.session.delete(action)
        db.session.commit()
    except (SQLAlchemyError, ValueError) as error:
        db.session.rollback()
        raise InstagramActionPersistenceError(
            "Não foi possível excluir a ação ou teste. Nenhum registro relacionado foi removido."
        ) from error


def recommendation_action_has_evaluation(action: InstagramRecommendationAction) -> bool:
    """Informa se o histórico da ação/teste impede sua exclusão definitiva."""
    return db.session.scalar(
        select(InstagramRecommendationActionEvaluation.id)
        .where(InstagramRecommendationActionEvaluation.instagram_recommendation_action_id == action.id)
        .limit(1)
    ) is not None


def actions_for_project(
    project: AnalyticsProject, *, action_kind: str = "all", status: str = "all",
) -> tuple[InstagramRecommendationAction, ...]:
    if action_kind not in {*ACTION_KINDS, "all"} or status not in {*ACTION_STATUSES, "all"}:
        raise InstagramActionError("Filtro de ações inválido.")
    query = (
        select(InstagramRecommendationAction)
        .where(InstagramRecommendationAction.analytics_project_id == project.id)
        .options(
            joinedload(InstagramRecommendationAction.recommendation)
            .joinedload(InstagramRecommendation.recommendation_run),
            selectinload(InstagramRecommendationAction.evaluations),
        )
    )
    if action_kind != "all":
        query = query.where(InstagramRecommendationAction.action_kind == action_kind)
    if status != "all":
        query = query.where(InstagramRecommendationAction.status == status)
    items = tuple(db.session.scalars(query).unique().all())
    return tuple(sorted(
        items,
        key=lambda item: (
            ACTION_STATUS_ORDER[item.status],
            {"high": 0, "medium": 1, "low": 2}.get(item.recommendation.priority, 3),
            item.created_at or datetime.min.replace(tzinfo=timezone.utc),
            item.id,
        ),
    ))


def action_counts(actions: tuple[InstagramRecommendationAction, ...]) -> dict[str, int]:
    return {status: sum(item.status == status for item in actions) for status in ACTION_STATUSES}


def transition_recommendation_action(
    action: InstagramRecommendationAction,
    target_status: str,
    values: Mapping[str, object] | None = None,
) -> InstagramRecommendationAction:
    """Aplica apenas transições explícitas; status não depende de datas planejadas."""
    if target_status not in ACTION_STATUSES or target_status not in _TRANSITIONS[action.status]:
        raise InstagramActionError("Transição de status não permitida.")
    values = values or {}
    if target_status == "completed":
        result_summary = _text(
            values, "result_summary", "o resumo do resultado", MAX_RESULT_SUMMARY_LENGTH, required=True,
        )
        outcome = _text(values, "outcome", "a conclusão", 20, required=True)
        if outcome not in ACTION_OUTCOMES:
            raise InstagramActionError("Conclusão do resultado inválida.")
        action.result_summary = result_summary
        action.observed_result = _number(values, "observed_result", "o resultado observado")
        action.outcome = outcome
        action.completed_at = datetime.now(timezone.utc)
    action.status = target_status
    try:
        db.session.commit()
    except (SQLAlchemyError, ValueError) as error:
        db.session.rollback()
        raise InstagramActionPersistenceError(
            "Não foi possível atualizar a ação ou teste. Nenhuma alteração parcial foi gravada."
        ) from error
    return action


__all__ = [
    "ACTION_KINDS", "ACTION_KIND_LABELS", "ACTION_OUTCOMES", "ACTION_OUTCOME_LABELS",
    "ACTION_STATUSES", "ACTION_STATUS_LABELS", "ActionPrefill", "InstagramActionError",
    "InstagramActionPersistenceError", "action_counts", "action_prefill", "actions_for_project",
    "create_recommendation_action", "delete_recommendation_action", "parse_planned_datetime",
    "recommendation_action_for_project", "recommendation_action_has_evaluation",
    "transition_recommendation_action",
]
