"""Políticas comerciais de limite, desacopladas de conectores e autorização.

Este módulo calcula condições contratuais. Ele não conta conexões reais, não
altera ``AccessGrant`` e não habilita ferramentas. A aplicação de cotas aos
adaptadores Meta/OJS será uma etapa posterior e deverá usar estes contratos em
vez de constantes locais.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .commercial_catalog import (
    COMMERCIAL_LIMIT_RESOURCE_IDS,
    COMMERCIAL_LIMIT_RESOURCES,
    COMMERCIAL_PLANS,
    commercial_plan_definition,
    commercial_plan_initial_limits,
)
from .extensions import db
from .models import (
    Plan,
    PlanLimitPolicy,
    User,
    WorkspaceSubscription,
    WorkspaceSubscriptionLimitAdjustment,
    utcnow,
)


class CommercialLimitError(ValueError):
    """Erro seguro de domínio para formulários e serviços administrativos."""


def _policy_storage_available() -> bool:
    """Permite que o núcleo 2.2 continue em bancos ainda anteriores à migration."""
    # Não use ``Inspector.has_table`` aqui: em SQLite ``:memory:`` ele pode
    # abrir/fechar a mesma conexão compartilhada durante uma transação de
    # bootstrap e desfazer inserts ainda não confirmados. A consulta ocorre na
    # sessão corrente, depois de flush explícito quando há registros pendentes.
    try:
        # O savepoint também mantém uma eventual transação externa utilizável
        # quando a tabela ainda não existe (inclusive fora do SQLite).
        with db.session.begin_nested():
            with db.session.no_autoflush:
                db.session.execute(select(PlanLimitPolicy.id).limit(1))
        return True
    except OperationalError:
        # Mantenha o fallback compatível com a fase 2.2 sem desfazer alterações
        # em curso na transação chamadora.
        return False


def _active_administrator(actor: User) -> None:
    if not actor.is_active or actor.role != "admin":
        raise CommercialLimitError("Esta alteração exige administrador autorizado.")


def _as_non_negative_integer(value: object, resource_id: str) -> int:
    if isinstance(value, bool):
        raise CommercialLimitError("Os limites devem ser números inteiros não negativos.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise CommercialLimitError(
            f"Informe uma quantidade inteira não negativa para {resource_name(resource_id)}."
        )
    if parsed < 0:
        raise CommercialLimitError(
            f"Informe uma quantidade inteira não negativa para {resource_name(resource_id)}."
        )
    return parsed


def resource_name(resource_id: str) -> str:
    return next(
        (resource.name for resource in COMMERCIAL_LIMIT_RESOURCES if resource.resource_id == resource_id),
        resource_id,
    )


def validate_limit_values(values: Mapping[str, object], *, require_all: bool = True) -> dict[str, int]:
    """Normaliza exclusivamente os recursos comerciais aprovados."""
    if not isinstance(values, Mapping):
        raise CommercialLimitError("Os limites comerciais devem ser estruturados.")
    unknown = set(values) - COMMERCIAL_LIMIT_RESOURCE_IDS
    if unknown:
        raise CommercialLimitError("O recurso comercial informado não é reconhecido.")
    normalized: dict[str, int] = {}
    for resource in COMMERCIAL_LIMIT_RESOURCES:
        if resource.resource_id not in values:
            if require_all:
                raise CommercialLimitError(f"Informe o limite para {resource.name}.")
            continue
        normalized[resource.resource_id] = _as_non_negative_integer(
            values[resource.resource_id], resource.resource_id
        )
    return normalized


def _limits_from_plan(plan: Plan) -> dict[str, int]:
    """Lê valores vigentes sem confiar em JSON antigo ou malformado."""
    defaults = commercial_plan_initial_limits(plan.id)
    source = plan.limits_json if isinstance(plan.limits_json, Mapping) else {}
    resolved: dict[str, int] = {}
    for resource_id, default in defaults.items():
        try:
            resolved[resource_id] = _as_non_negative_integer(source.get(resource_id, default), resource_id)
        except CommercialLimitError:
            # Não reescrevemos um dado histórico inválido em uma leitura; a
            # primeira publicação administrativa corrige a política de modo auditável.
            resolved[resource_id] = default
    return resolved


def current_plan_limit_policy(plan: Plan) -> PlanLimitPolicy | None:
    if not _policy_storage_available():
        return None
    return db.session.scalar(select(PlanLimitPolicy).where(
        PlanLimitPolicy.plan_id == plan.id
    ).order_by(PlanLimitPolicy.version.desc(), PlanLimitPolicy.id.desc()))


def plan_limit_policy_history(plan: Plan, *, limit: int | None = None) -> list[PlanLimitPolicy]:
    if not _policy_storage_available():
        return []
    statement = select(PlanLimitPolicy).where(PlanLimitPolicy.plan_id == plan.id).order_by(
        PlanLimitPolicy.version.desc(), PlanLimitPolicy.id.desc()
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(db.session.scalars(statement))


def current_plan_limits(plan: Plan) -> dict[str, int]:
    if commercial_plan_definition(plan.id) is None:
        raise CommercialLimitError("Este plano não possui política comercial configurável.")
    policy = current_plan_limit_policy(plan)
    if policy is not None:
        return validate_limit_values(policy.limits_json)
    return _limits_from_plan(plan)


def initialize_commercial_limit_policies() -> tuple[PlanLimitPolicy, ...]:
    """Inicializa a primeira política apenas em bootstrap vazio e schema atualizado.

    A operação é idempotente: uma política existente é prova de que a
    configuração já foi publicada e jamais é sobrescrita por seed.
    """
    if not _policy_storage_available():
        return ()
    created: list[PlanLimitPolicy] = []
    for definition_plan_id in (definition.plan_id for definition in COMMERCIAL_PLANS):
        plan = db.session.get(Plan, definition_plan_id)
        if plan is None or current_plan_limit_policy(plan) is not None:
            continue
        limits = _limits_from_plan(plan)
        merged_limits = dict(plan.limits_json) if isinstance(plan.limits_json, Mapping) else {}
        merged_limits.update(limits)
        plan.limits_json = merged_limits
        created.append(PlanLimitPolicy(
            plan_id=plan.id,
            version=1,
            limits_json=deepcopy(limits),
            justification="Valores iniciais da política comercial.",
            published_by_name_snapshot="Inicialização da plataforma",
        ))
    if created:
        db.session.add_all(created)
        db.session.flush()
    return tuple(created)


def publish_plan_limit_policy(
    actor: User,
    plan_id: str,
    values: Mapping[str, object],
    *,
    justification: str = "",
) -> tuple[PlanLimitPolicy | None, bool]:
    """Publica uma nova versão somente quando a política realmente muda."""
    _active_administrator(actor)
    if not _policy_storage_available():
        raise CommercialLimitError("A migration de políticas comerciais ainda não foi aplicada.")
    plan = db.session.get(Plan, plan_id)
    if plan is None or commercial_plan_definition(plan_id) is None:
        raise CommercialLimitError("Plano comercial não encontrado.")
    normalized = validate_limit_values(values)
    explanation = (justification or "").strip()
    if len(explanation) > 500:
        raise CommercialLimitError("A justificativa deve ter no máximo 500 caracteres.")
    previous = current_plan_limit_policy(plan)
    previous_limits = current_plan_limits(plan)
    if normalized == previous_limits:
        return previous, False
    next_version = (previous.version if previous is not None else 0) + 1
    policy = PlanLimitPolicy(
        plan_id=plan.id,
        version=next_version,
        limits_json=deepcopy(normalized),
        justification=explanation,
        published_by_id=actor.id,
        published_by_name_snapshot=actor.name,
    )
    retained = dict(plan.limits_json) if isinstance(plan.limits_json, Mapping) else {}
    retained.update(normalized)
    plan.limits_json = retained
    db.session.add(policy)
    db.session.flush()
    # Importação tardia evita ciclo com services.seed_platform.
    from .services import record_audit
    record_audit(actor, "plan_limit_policy_published", "plan_limit_policy", policy.id, {
        "plan_id": plan.id,
        "version": previous.version if previous else None,
        "limits": previous_limits,
    }, {
        "plan_id": plan.id,
        "version": policy.version,
        "limits": normalized,
        "justification": explanation,
    })
    return policy, True


def snapshot_plan_limits(plan: Plan) -> dict[str, object]:
    """Captura a política atual para uma nova assinatura sem alterar contratos antigos."""
    if commercial_plan_definition(plan.id) is None:
        return {
            "policy_version": "plan-limits-v1",
            "plan_policy_version": None,
            "plan_policy_id": None,
            "plan_limits": deepcopy(plan.limits_json or {}),
        }
    policy = current_plan_limit_policy(plan)
    return {
        "policy_version": "plan-limits-v1",
        "plan_policy_version": policy.version if policy is not None else None,
        "plan_policy_id": policy.id if policy is not None else None,
        "plan_limits": deepcopy(current_plan_limits(plan)),
    }


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _active_subscription(subscription: WorkspaceSubscription, at: datetime) -> bool:
    return (
        subscription.status == "active"
        and (subscription.ended_at is None or _as_utc(subscription.ended_at) > at)
    )


def _subscription_snapshot_limits(subscription: WorkspaceSubscription) -> dict[str, int]:
    terms = subscription.terms_json if isinstance(subscription.terms_json, Mapping) else {}
    raw = terms.get("plan_limits", {})
    if not isinstance(raw, Mapping):
        raw = {}
    normalized: dict[str, int] = {}
    for resource in COMMERCIAL_LIMIT_RESOURCES:
        try:
            normalized[resource.resource_id] = _as_non_negative_integer(
                raw.get(resource.resource_id, 0), resource.resource_id
            )
        except CommercialLimitError:
            normalized[resource.resource_id] = 0
    return normalized


def effective_subscription_limits(
    subscription: WorkspaceSubscription,
    *,
    at: datetime | None = None,
) -> dict[str, object]:
    """Resolve limites contratuais; não consulta nem bloqueia recursos externos."""
    instant = _as_utc(at or utcnow())
    is_active = _active_subscription(subscription, instant)
    contractual = _subscription_snapshot_limits(subscription)
    additional = {resource.resource_id: 0 for resource in COMMERCIAL_LIMIT_RESOURCES}
    if is_active:
        for adjustment in subscription.limit_adjustments:
            if adjustment.status != "active":
                continue
            if _as_utc(adjustment.starts_at) > instant:
                continue
            if adjustment.ends_at is not None and _as_utc(adjustment.ends_at) <= instant:
                continue
            additional[adjustment.resource_key] += adjustment.additional_units
    return {
        "subscription_id": subscription.id,
        "status": subscription.status,
        "active": is_active,
        "resources": {
            resource.resource_id: {
                "contractual_limit": contractual[resource.resource_id],
                "additional_units": additional[resource.resource_id] if is_active else 0,
                "effective_limit": (
                    contractual[resource.resource_id] + additional[resource.resource_id]
                    if is_active else 0
                ),
                "commercially_available": bool(
                    is_active and (contractual[resource.resource_id] + additional[resource.resource_id])
                ),
            }
            for resource in COMMERCIAL_LIMIT_RESOURCES
        },
    }


def grant_subscription_limit_adjustment(
    actor: User,
    subscription_id: str,
    resource_key: str,
    additional_units: object,
    *,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    justification: str = "",
) -> WorkspaceSubscriptionLimitAdjustment:
    """Registra acréscimo contratual explícito; nunca altera o snapshot base."""
    _active_administrator(actor)
    if resource_key not in COMMERCIAL_LIMIT_RESOURCE_IDS:
        raise CommercialLimitError("O recurso comercial informado não é reconhecido.")
    units = _as_non_negative_integer(additional_units, resource_key)
    if units == 0:
        raise CommercialLimitError("O ajuste adicional deve ser maior que zero.")
    subscription = db.session.get(WorkspaceSubscription, subscription_id)
    if subscription is None or subscription.status != "active" or subscription.ended_at is not None:
        raise CommercialLimitError("A assinatura não está ativa para receber ajuste de limite.")
    starts = _as_utc(starts_at or utcnow())
    ends = _as_utc(ends_at) if ends_at is not None else None
    if ends is not None and ends <= starts:
        raise CommercialLimitError("A vigência final deve ser posterior ao início do ajuste.")
    explanation = (justification or "").strip()
    if len(explanation) > 500:
        raise CommercialLimitError("A justificativa deve ter no máximo 500 caracteres.")
    adjustment = WorkspaceSubscriptionLimitAdjustment(
        workspace_subscription_id=subscription.id,
        resource_key=resource_key,
        additional_units=units,
        status="active",
        starts_at=starts,
        ends_at=ends,
        justification=explanation,
        approved_by_id=actor.id,
        approved_by_name_snapshot=actor.name,
    )
    db.session.add(adjustment)
    db.session.flush()
    from .services import record_audit
    record_audit(actor, "workspace_subscription_limit_adjustment_granted",
                 "workspace_subscription_limit_adjustment", adjustment.id, {}, {
        "subscription_id": subscription.id,
        "resource_key": resource_key,
        "additional_units": units,
        "starts_at": starts.isoformat(),
        "ends_at": ends.isoformat() if ends else None,
        "justification": explanation,
    })
    return adjustment


def revoke_subscription_limit_adjustment(
    actor: User,
    adjustment_id: str,
    *,
    reason: str = "",
) -> tuple[WorkspaceSubscriptionLimitAdjustment, bool]:
    """Revoga somente um ajuste ativo e preserva seu histórico auditável."""
    _active_administrator(actor)
    adjustment = db.session.get(WorkspaceSubscriptionLimitAdjustment, adjustment_id)
    if adjustment is None:
        raise CommercialLimitError("Ajuste de limite não encontrado.")
    if adjustment.status == "revoked":
        return adjustment, False
    explanation = (reason or "").strip()
    if len(explanation) > 500:
        raise CommercialLimitError("A justificativa deve ter no máximo 500 caracteres.")
    adjustment.status = "revoked"
    adjustment.revoked_at = utcnow()
    adjustment.revoked_by_id = actor.id
    adjustment.revoked_by_name_snapshot = actor.name
    adjustment.revocation_reason = explanation
    from .services import record_audit
    record_audit(actor, "workspace_subscription_limit_adjustment_revoked",
                 "workspace_subscription_limit_adjustment", adjustment.id, {
        "status": "active",
        "additional_units": adjustment.additional_units,
    }, {
        "status": "revoked",
        "reason": explanation,
    })
    return adjustment, True
