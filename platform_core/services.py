"""Regras de acesso e seeds administrativos, independentes dos analisadores."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from flask import abort
from sqlalchemy import select

from historico_racial.vocabulary import _contagens, _seed, hash_vocabulario

from .extensions import db
from .models import (
    AccessGrant, AuditLog, Plan, PlanTool, Project, Tool, User,
    UserToolOverride, VocabularyLibrary, utcnow,
)

PLANOS = (
    ("student", "Estudante", "Acesso gratuito inicial"),
    ("researcher", "Pesquisador", "Plano de pesquisa"),
    ("pro", "Pro", "Plano avançado"),
    ("institutional", "Institucional", "Plano institucional"),
)
FERRAMENTAS = (
    ("pdf_scraper", "Raspagem padrão", "/"),
    ("document_analysis", "Raspagem de Dados — Análise documental em Ciências Sociais", "/analise-documental"),
)
ACCOUNT_LIFECYCLE_LOCK = RLock()


def account_accepts_new_work(user_id: str) -> bool:
    """Consulta o banco, não o usuário em cache da requisição concorrente."""
    return db.session.scalar(select(User.id).where(
        User.id == user_id, User.status == "active", User.deleted_at.is_(None)
    )) is not None


def seed_platform() -> None:
    """Idempotente; nunca reescreve uma biblioteca ou permissão existente."""
    for code, name, description in PLANOS:
        if db.session.get(Plan, code) is None:
            db.session.add(Plan(id=code, name=name, description=description, active=True))
    for code, name, route in FERRAMENTAS:
        if db.session.get(Tool, code) is None:
            db.session.add(Tool(id=code, name=name, route=route, active=True))
    db.session.flush()
    # Ambas as ferramentas iniciais ficam no plano gratuito; nenhum limite
    # arbitrário de PDFs ou projetos é introduzido nesta etapa.
    for plan_id, tool_id in (
        ("student", "pdf_scraper"), ("student", "document_analysis"),
        ("researcher", "pdf_scraper"), ("researcher", "document_analysis"),
        ("pro", "pdf_scraper"), ("pro", "document_analysis"),
        ("institutional", "pdf_scraper"), ("institutional", "document_analysis"),
    ):
        if db.session.get(PlanTool, (plan_id, tool_id)) is None:
            db.session.add(PlanTool(plan_id=plan_id, tool_id=tool_id))
    snapshot = _seed()
    counts = _contagens(snapshot)
    if counts != {"grupos": 8, "entidades": 75, "variantes": 117}:
        raise RuntimeError(f"O seed Relações raciais divergiu: {counts}")
    existing_library = db.session.get(VocabularyLibrary, "relacoes_raciais")
    if existing_library is None:
        db.session.add(VocabularyLibrary(
            id="relacoes_raciais", name="Relações raciais",
            description="Biblioteca oficial inicial derivada de entities.yml.",
            snapshot_json=snapshot, content_hash=hash_vocabulario(snapshot), counts_json=counts,
            status="published", version="v1", active=True,
        ))
    elif (existing_library.snapshot_json != snapshot or existing_library.content_hash != hash_vocabulario(snapshot)
          or existing_library.counts_json != counts):
        raise RuntimeError("A biblioteca oficial Relações raciais divergiu do seed; intervenção manual necessária.")
    db.session.commit()


def current_grant(user: User) -> AccessGrant | None:
    return db.session.scalar(
        select(AccessGrant).where(AccessGrant.user_id == user.id, AccessGrant.ended_at.is_(None))
        .order_by(AccessGrant.created_at.desc(), AccessGrant.id.desc())
    )


def access_is_active(user: User) -> bool:
    if not user.is_active:
        return False
    if user.role == "admin":
        return True
    grant = current_grant(user)
    if grant is None or grant.status not in {"active", "trial"}:
        return False
    if grant.expires_at is not None:
        expiration = grant.expires_at.replace(tzinfo=timezone.utc) if grant.expires_at.tzinfo is None else grant.expires_at
        if expiration <= datetime.now(timezone.utc):
            return False
    plan = db.session.get(Plan, grant.plan_id)
    return bool(plan and plan.active)


def can_use_tool(user: User, tool_id: str) -> bool:
    if not access_is_active(user):
        return False
    tool = db.session.get(Tool, tool_id)
    if tool is None or not tool.active:
        return False
    if user.role == "admin":
        return True
    override = db.session.get(UserToolOverride, (user.id, tool_id))
    if override is not None:
        return override.decision == "allow"
    grant = current_grant(user)
    return db.session.get(PlanTool, (grant.plan_id, tool_id)) is not None


def get_project_for_user(project_id: str, user: User, *, include_inactive: bool = False) -> Project:
    project = db.session.get(Project, project_id)
    if project is None or (project.owner_user_id != user.id and user.role != "admin"):
        abort(404)
    if not include_inactive and (project.deleted_at is not None or project.status != "active"):
        abort(404)
    return project


def record_audit(admin: User | None, action: str, target_type: str, target_id: str, before: dict | None = None, after: dict | None = None) -> None:
    db.session.add(AuditLog(
        admin_user_id=admin.id if admin else None, action=action, target_type=target_type,
        admin_name_snapshot=admin.name if admin else None,
        target_id=target_id, before_json=before or {}, after_json=after or {},
    ))


def replace_grant(user: User, plan_id: str, access_mode: str, status: str, admin: User | None = None, expires_at: datetime | None = None) -> AccessGrant:
    plan = db.session.get(Plan, plan_id)
    if plan is None or not plan.active:
        raise ValueError("Este plano está inativo e não pode receber novas concessões.")
    previous = current_grant(user)
    if previous:
        previous.ended_at = utcnow()
    grant = AccessGrant(
        user_id=user.id, plan_id=plan_id, access_mode=access_mode,
        status=status, granted_by_id=admin.id if admin else None,
        granted_by_name_snapshot=admin.name if admin else None,
        expires_at=expires_at,
    )
    db.session.add(grant)
    if admin:
        record_audit(admin, "access_grant_changed", "user", user.id,
                     {"plan": previous.plan_id, "mode": previous.access_mode, "status": previous.status} if previous else {},
                     {"plan": plan_id, "mode": access_mode, "status": status, "expires_at": expires_at.isoformat() if expires_at else None})
    return grant
