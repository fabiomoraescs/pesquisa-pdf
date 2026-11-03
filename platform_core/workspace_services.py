"""Serviços internos do núcleo SaaS, sem dependência de ferramentas ou APIs.

Nesta etapa workspaces e assinaturas são registros estruturais: a autorização
operacional continua exclusivamente em AccessGrant/PlanTool/UserToolOverride.
"""

from __future__ import annotations

from copy import deepcopy
from threading import RLock

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .commercial_limit_services import snapshot_plan_limits
from .extensions import db
from .models import Plan, User, Workspace, WorkspaceMembership, WorkspaceSubscription, utcnow
from .services import record_audit


WORKSPACE_LOCK = RLock()


class WorkspaceServiceError(ValueError):
    """Erro de domínio seguro para chamadas internas ou futuras rotas protegidas."""


def _require_active_user(user: User) -> None:
    if not user.is_active:
        raise WorkspaceServiceError("A conta não está ativa para administrar áreas de trabalho.")


def _require_administrator(user: User) -> None:
    _require_active_user(user)
    if user.role != "admin":
        raise WorkspaceServiceError("A atribuição de assinatura exige administrador autorizado.")


def _workspace_name(name: str) -> str:
    normalized = (name or "").strip()
    if not normalized or len(normalized) > 200:
        raise WorkspaceServiceError("Informe o nome da área de trabalho com até 200 caracteres.")
    return normalized


def _active_membership(workspace_id: str, user_id: str) -> WorkspaceMembership | None:
    return db.session.scalar(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == user_id,
        WorkspaceMembership.status == "active",
    ).order_by(WorkspaceMembership.joined_at.desc(), WorkspaceMembership.id.desc()))


def _insert_with_unique_guard(record: WorkspaceMembership | WorkspaceSubscription, message: str) -> None:
    """Converte corrida entre processos em erro de domínio sem abortar a transação externa."""
    try:
        with db.session.begin_nested():
            db.session.add(record)
            db.session.flush()
    except IntegrityError as exc:
        raise WorkspaceServiceError(message) from exc


def _workspace_for_actor(actor: User, workspace_id: str, *, require_owner: bool) -> Workspace:
    _require_active_user(actor)
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None or workspace.status != "active":
        raise WorkspaceServiceError("Área de trabalho indisponível.")
    if actor.role == "admin":
        return workspace
    membership = _active_membership(workspace.id, actor.id)
    if membership is None or (require_owner and membership.role != "owner"):
        raise WorkspaceServiceError("Você não possui autorização para esta área de trabalho.")
    return workspace


def create_workspace(actor: User, name: str, workspace_type: str) -> Workspace:
    """Cria área individual ou de equipe e a associação de titular, sem plano."""
    if workspace_type not in {"individual", "team"}:
        raise WorkspaceServiceError("Tipo de área de trabalho inválido.")
    _require_active_user(actor)
    workspace = Workspace(name=_workspace_name(name), workspace_type=workspace_type, owner_user_id=actor.id)
    membership = WorkspaceMembership(workspace=workspace, user=actor, role="owner", status="active")
    db.session.add_all((workspace, membership))
    db.session.flush()
    record_audit(actor, "workspace_created", "workspace", workspace.id, {}, {
        "workspace_type": workspace.workspace_type,
        "owner_user_id": actor.id,
    })
    return workspace


def create_individual_workspace(actor: User, name: str) -> Workspace:
    return create_workspace(actor, name, "individual")


def create_team_workspace(actor: User, name: str) -> Workspace:
    return create_workspace(actor, name, "team")


def get_workspace_for_user(actor: User, workspace_id: str) -> Workspace:
    """Recupera somente área ativa da qual a pessoa participa; admin mantém manutenção."""
    return _workspace_for_actor(actor, workspace_id, require_owner=False)


def list_workspaces_for_user(user: User) -> list[Workspace]:
    _require_active_user(user)
    statement = select(Workspace).join(WorkspaceMembership).where(
        WorkspaceMembership.user_id == user.id,
        WorkspaceMembership.status == "active",
        Workspace.status == "active",
    ).order_by(Workspace.created_at, Workspace.id)
    return list(db.session.scalars(statement).unique())


def add_workspace_member(actor: User, workspace_id: str, member: User) -> WorkspaceMembership:
    """Inclui membro sem conceder plano, ferramenta ou acesso operacional."""
    with WORKSPACE_LOCK:
        workspace = _workspace_for_actor(actor, workspace_id, require_owner=True)
        _require_active_user(member)
        if _active_membership(workspace.id, member.id) is not None:
            raise WorkspaceServiceError("A pessoa já participa desta área de trabalho.")
        membership = WorkspaceMembership(
            workspace_id=workspace.id, user_id=member.id, role="member", status="active"
        )
        _insert_with_unique_guard(membership, "A pessoa já participa desta área de trabalho.")
        record_audit(actor, "workspace_member_added", "workspace_membership", membership.id, {}, {
            "workspace_id": workspace.id,
            "user_id": member.id,
            "role": membership.role,
        })
        return membership


def list_workspace_members(actor: User, workspace_id: str, *, include_ended: bool = False) -> list[WorkspaceMembership]:
    workspace = _workspace_for_actor(actor, workspace_id, require_owner=False)
    statement = select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace.id)
    if not include_ended:
        statement = statement.where(WorkspaceMembership.status == "active")
    return list(db.session.scalars(statement.order_by(
        WorkspaceMembership.joined_at, WorkspaceMembership.id
    )))


def deactivate_workspace_membership(actor: User, workspace_id: str, member_user_id: str) -> WorkspaceMembership:
    """Encerra a associação, preservando histórico e sem revogar grants legados."""
    with WORKSPACE_LOCK:
        workspace = _workspace_for_actor(actor, workspace_id, require_owner=True)
        membership = _active_membership(workspace.id, member_user_id)
        if membership is None:
            raise WorkspaceServiceError("A associação ativa não foi encontrada.")
        if membership.role == "owner":
            raise WorkspaceServiceError("A titularidade não pode ser encerrada por esta operação.")
        membership.status = "ended"
        membership.ended_at = utcnow()
        record_audit(actor, "workspace_member_ended", "workspace_membership", membership.id, {
            "status": "active",
        }, {"status": membership.status, "workspace_id": workspace.id, "user_id": member_user_id})
        return membership


def _subscription_terms(plan: Plan, terms: dict | None) -> dict:
    if terms is not None and not isinstance(terms, dict):
        raise WorkspaceServiceError("As condições da assinatura devem ser estruturadas.")
    snapshot = snapshot_plan_limits(plan)
    snapshot["additional_limits"] = deepcopy(terms or {})
    return snapshot


def current_workspace_subscription(workspace_id: str) -> WorkspaceSubscription | None:
    return db.session.scalar(select(WorkspaceSubscription).where(
        WorkspaceSubscription.workspace_id == workspace_id,
        WorkspaceSubscription.status == "active",
        WorkspaceSubscription.ended_at.is_(None),
    ).order_by(WorkspaceSubscription.started_at.desc(), WorkspaceSubscription.id.desc()))


def create_workspace_subscription(actor: User, workspace_id: str, plan_id: str,
                                  *, terms: dict | None = None) -> WorkspaceSubscription:
    """Registra plano por administrador; não habilita rotas nem substitui grants."""
    with WORKSPACE_LOCK:
        _require_administrator(actor)
        workspace = _workspace_for_actor(actor, workspace_id, require_owner=False)
        plan = db.session.get(Plan, plan_id)
        if plan is None or not plan.active:
            raise WorkspaceServiceError("Plano indisponível para assinatura.")
        if current_workspace_subscription(workspace.id) is not None:
            raise WorkspaceServiceError("A área de trabalho já possui uma assinatura ativa.")
        subscription = WorkspaceSubscription(
            workspace_id=workspace.id,
            plan_id=plan.id,
            status="active",
            assigned_by_id=actor.id,
            assigned_by_name_snapshot=actor.name,
            terms_json=_subscription_terms(plan, terms),
        )
        _insert_with_unique_guard(
            subscription,
            "A área de trabalho já possui uma assinatura ativa.",
        )
        record_audit(actor, "workspace_subscription_created", "workspace_subscription", subscription.id, {}, {
            "workspace_id": workspace.id,
            "plan_id": plan.id,
            "status": subscription.status,
        })
        return subscription


def end_workspace_subscription(actor: User, workspace_id: str, *, status: str = "canceled") -> WorkspaceSubscription:
    if status not in {"canceled", "expired"}:
        raise WorkspaceServiceError("Estado final de assinatura inválido.")
    with WORKSPACE_LOCK:
        _require_administrator(actor)
        workspace = _workspace_for_actor(actor, workspace_id, require_owner=False)
        subscription = current_workspace_subscription(workspace.id)
        if subscription is None:
            raise WorkspaceServiceError("A área de trabalho não possui assinatura ativa.")
        subscription.status = status
        subscription.ended_at = utcnow()
        record_audit(actor, "workspace_subscription_ended", "workspace_subscription", subscription.id, {
            "status": "active", "plan_id": subscription.plan_id,
        }, {"status": subscription.status, "workspace_id": workspace.id})
        return subscription


def list_workspace_subscriptions(actor: User, workspace_id: str) -> list[WorkspaceSubscription]:
    workspace = _workspace_for_actor(actor, workspace_id, require_owner=False)
    return list(db.session.scalars(select(WorkspaceSubscription).where(
        WorkspaceSubscription.workspace_id == workspace.id
    ).order_by(WorkspaceSubscription.started_at.desc(), WorkspaceSubscription.id.desc())))
