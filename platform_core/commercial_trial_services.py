"""Períodos experimentais comerciais, sem persistir CPF em texto.

O domínio continua independente de fornecedores externos e não conta recursos
Meta/OJS. Ele cria apenas a concessão temporária já existente, depois de
registrar a utilização única por grupo comercial.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re

from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from .commercial_catalog import commercial_plan_definition, is_commercial_plan_assignable
from .extensions import db
from .models import CommercialTrial, Plan, User, Workspace, WorkspaceMembership, WorkspaceSubscription, utcnow
from .services import ACCOUNT_LIFECYCLE_LOCK, access_is_active, current_grant, record_audit, replace_grant


TRIAL_DURATION = timedelta(days=7)


class CommercialTrialError(ValueError):
    """Falha de regra comercial apresentada sem revelar CPF ou dados privados."""


def _trial_storage_available() -> bool:
    """Mantém o painel utilizável enquanto a migration ainda não foi aplicada."""
    try:
        with db.session.begin_nested():
            with db.session.no_autoflush:
                db.session.execute(select(CommercialTrial.id).limit(1))
        return True
    except OperationalError:
        return False


def trial_mask_storage_available() -> bool:
    """Indica se a coluna aditiva da máscara já existe neste banco.

    A verificação deixa a área administrativa legível durante uma implantação
    em que a aplicação já recebeu o código, mas a migration ainda não foi
    aplicada. Não tenta derivar a máscara a partir do HMAC.
    """
    try:
        with db.session.begin_nested():
            with db.session.no_autoflush:
                db.session.execute(select(CommercialTrial.cpf_masked).limit(1))
        return True
    except OperationalError:
        return False


def normalize_cpf(value: object) -> str:
    """Valida CPF pelo dígito verificador e retorna somente os 11 dígitos."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 11 or len(set(digits)) == 1:
        raise CommercialTrialError("Informe um CPF válido para ativar o período de teste.")
    first = sum(int(digit) * weight for digit, weight in zip(digits[:9], range(10, 1, -1)))
    first_digit = (first * 10 % 11) % 10
    second = sum(int(digit) * weight for digit, weight in zip(digits[:10], range(11, 1, -1)))
    second_digit = (second * 10 % 11) % 10
    if digits[-2:] != f"{first_digit}{second_digit}":
        raise CommercialTrialError("Informe um CPF válido para ativar o período de teste.")
    return digits


def mask_cpf(cpf: str) -> str:
    """Retorna somente a representação administrativa minimizada do CPF."""
    if len(cpf) != 11 or not cpf.isdigit():
        raise ValueError("CPF normalizado inválido para mascaramento.")
    return f"*** {cpf[3:6]}.{cpf[6:9]} **"


def trial_cpf_mask_display(trial: CommercialTrial) -> str:
    """Apresenta máscara administrativa ou indicação neutra para histórico."""
    if not trial_mask_storage_available():
        return "Não disponível"
    return trial.cpf_masked or "Não disponível"


def _fingerprint_cpf(cpf: str) -> tuple[str, str]:
    secret = current_app.config.get("PESQUISAPDF_CPF_HMAC_SECRET", "")
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise CommercialTrialError(
            "O período de teste exige a chave privada de proteção de CPF configurada no servidor."
        )
    version = str(current_app.config.get("PESQUISAPDF_CPF_HMAC_KEY_VERSION", "v1")).strip()[:32] or "v1"
    fingerprint = hmac.new(
        secret.encode("utf-8"),
        f"analisis:commercial-trial:{cpf}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return fingerprint, version


def trial_state(trial: CommercialTrial, *, at: datetime | None = None) -> str:
    """Estado derivado; a expiração é sempre calculada no servidor."""
    instant = at or utcnow()
    instant = instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant.astimezone(timezone.utc)
    expires = trial.expires_at.replace(tzinfo=timezone.utc) if trial.expires_at.tzinfo is None else trial.expires_at
    return "active" if instant < expires else "expired"


def user_commercial_trials(user_id: str) -> list[CommercialTrial]:
    if not _trial_storage_available():
        return []
    return list(db.session.scalars(select(CommercialTrial).where(
        CommercialTrial.user_id == user_id
    ).order_by(CommercialTrial.started_at.desc(), CommercialTrial.id.desc())))


def all_commercial_trials() -> list[CommercialTrial]:
    if not _trial_storage_available():
        return []
    return list(db.session.scalars(select(CommercialTrial).order_by(
        CommercialTrial.started_at.desc(), CommercialTrial.id.desc()
    )))


def _validate_trial_workspace(user: User, workspace_id: str | None) -> tuple[Workspace | None, WorkspaceSubscription | None]:
    if not workspace_id:
        return None, None
    workspace = db.session.get(Workspace, workspace_id)
    membership = db.session.scalar(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == user.id,
        WorkspaceMembership.role == "owner",
        WorkspaceMembership.status == "active",
    ))
    if workspace is None or workspace.status != "active" or membership is None:
        raise CommercialTrialError("A área de trabalho indicada não pode receber este período de teste.")
    subscription = db.session.scalar(select(WorkspaceSubscription).where(
        WorkspaceSubscription.workspace_id == workspace.id,
        WorkspaceSubscription.status == "active",
        WorkspaceSubscription.ended_at.is_(None),
    ).order_by(WorkspaceSubscription.started_at.desc(), WorkspaceSubscription.id.desc()))
    return workspace, subscription


def activate_commercial_trial(
    actor: User,
    user: User,
    plan_id: str,
    cpf_value: object,
    *,
    workspace_id: str | None = None,
    now: datetime | None = None,
) -> CommercialTrial:
    """Consome uma única elegibilidade CPF+grupo e cria grant temporário.

    A operação não cria assinatura, não altera workspaces e não contorna a
    autorização vigente: a concessão é um ``AccessGrant`` normal, com expiração
    calculada pelo servidor e ferramentas limitadas ao plano persistido.
    """
    if not actor.is_active or actor.role != "admin":
        raise CommercialTrialError("A ativação do período de teste exige administrador autorizado.")
    if not _trial_storage_available():
        raise CommercialTrialError("A migration de períodos de teste ainda não foi aplicada.")
    if not trial_mask_storage_available():
        raise CommercialTrialError(
            "A atualização de apresentação mascarada do CPF ainda não foi aplicada ao banco."
        )
    if not user.is_active or user.deleted_at is not None or user.role == "admin":
        raise CommercialTrialError("Esta conta não pode receber período de teste.")
    plan_definition = commercial_plan_definition(plan_id)
    if plan_definition is None or not is_commercial_plan_assignable(plan_id):
        raise CommercialTrialError("Este plano não possui ferramentas operacionais elegíveis para período de teste.")
    # A consulta explícita evita que um ID do catálogo sem registro persistido
    # crie um benefício que jamais concederia acesso.
    plan = db.session.get(Plan, plan_id)
    if plan is None or not plan.active:
        raise CommercialTrialError("Este plano está inativo e não pode receber período de teste.")
    if access_is_active(user):
        raise CommercialTrialError("A conta já possui acesso ativo; encerre ou regularize a concessão antes de ativar um teste.")

    cpf = normalize_cpf(cpf_value)
    cpf_masked = mask_cpf(cpf)
    fingerprint, key_version = _fingerprint_cpf(cpf)
    starts = now or utcnow()
    starts = starts.replace(tzinfo=timezone.utc) if starts.tzinfo is None else starts.astimezone(timezone.utc)
    expires = starts + TRIAL_DURATION
    workspace, subscription = _validate_trial_workspace(user, workspace_id)

    with ACCOUNT_LIFECYCLE_LOCK:
        exists = db.session.scalar(select(CommercialTrial.id).where(
            CommercialTrial.cpf_fingerprint == fingerprint,
            CommercialTrial.commercial_group_id == plan_definition.group_id,
        ))
        if exists is not None:
            raise CommercialTrialError("Este CPF já utilizou o período de teste deste grupo comercial.")
        trial = CommercialTrial(
            cpf_fingerprint=fingerprint,
            cpf_key_version=key_version,
            cpf_masked=cpf_masked,
            commercial_group_id=plan_definition.group_id,
            user_id=user.id,
            plan_id=plan.id,
            workspace_id=workspace.id if workspace is not None else None,
            workspace_subscription_id=(
                subscription.id if subscription is not None and subscription.plan_id == plan.id else None
            ),
            started_at=starts,
            expires_at=expires,
            activated_by_id=actor.id,
            activated_by_name_snapshot=actor.name,
        )
        try:
            with db.session.begin_nested():
                db.session.add(trial)
                db.session.flush()
        except IntegrityError as exc:
            raise CommercialTrialError(
                "Este CPF já utilizou o período de teste deste grupo comercial."
            ) from exc
        replace_grant(user, plan.id, "trial", "trial", actor, expires)
        record_audit(actor, "commercial_trial_activated", "commercial_trial", trial.id, {}, {
            "user_id": user.id,
            "plan_id": plan.id,
            "commercial_group_id": plan_definition.group_id,
            "starts_at": starts.isoformat(),
            "expires_at": expires.isoformat(),
            "workspace_id": workspace.id if workspace is not None else None,
        })
        return trial
