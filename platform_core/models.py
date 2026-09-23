"""Metadados persistentes. PDFs, trechos e embeddings não são salvos aqui."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from flask_login import UserMixin
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(db.String(160), nullable=False)
    email: Mapped[str] = mapped_column(db.String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(db.String(512), nullable=False)
    role: Mapped[str] = mapped_column(db.String(16), nullable=False, default="user")
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="active")
    student_verification_status: Mapped[str] = mapped_column(db.String(24), default="not_required", nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    grants: Mapped[list[AccessGrant]] = relationship(back_populates="user", foreign_keys="AccessGrant.user_id", order_by="AccessGrant.created_at")
    projects: Mapped[list[Project]] = relationship(back_populates="owner")
    profile: Mapped[UserProfile | None] = relationship(back_populates="user", uselist=False)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="scrypt")

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.deleted_at is None


class UserProfile(db.Model):
    """Dados opcionais de perfil, separados das credenciais e permissões."""

    __tablename__ = "user_profiles"
    user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), primary_key=True)
    education_level: Mapped[str | None] = mapped_column(db.String(40))
    formation_area: Mapped[str | None] = mapped_column(db.String(160))
    occupation: Mapped[str | None] = mapped_column(db.String(160))
    institutional_affiliation: Mapped[str | None] = mapped_column(db.String(200))
    gender: Mapped[str | None] = mapped_column(db.String(24))
    race_color: Mapped[str | None] = mapped_column(db.String(24))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    user: Mapped[User] = relationship(back_populates="profile")


class Plan(db.Model):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(db.String(32), primary_key=True)
    name: Mapped[str] = mapped_column(db.String(100), nullable=False)
    description: Mapped[str] = mapped_column(db.Text, default="", nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    limits_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)


class Tool(db.Model):
    __tablename__ = "tools"
    id: Mapped[str] = mapped_column(db.String(40), primary_key=True)
    name: Mapped[str] = mapped_column(db.String(160), nullable=False)
    description: Mapped[str] = mapped_column(db.Text, default="", nullable=False)
    route: Mapped[str] = mapped_column(db.String(160), nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)


class PlanTool(db.Model):
    __tablename__ = "plan_tools"
    plan_id: Mapped[str] = mapped_column(db.ForeignKey("plans.id"), primary_key=True)
    tool_id: Mapped[str] = mapped_column(db.ForeignKey("tools.id"), primary_key=True)


class AccessGrant(db.Model):
    __tablename__ = "access_grants"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), index=True, nullable=False)
    plan_id: Mapped[str] = mapped_column(db.ForeignKey("plans.id"), nullable=False)
    access_mode: Mapped[str] = mapped_column(db.String(24), nullable=False)
    status: Mapped[str] = mapped_column(db.String(24), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    granted_by_id: Mapped[str | None] = mapped_column(db.ForeignKey("users.id"))
    ended_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    payment_provider: Mapped[str | None] = mapped_column(db.String(80))
    external_subscription_id: Mapped[str | None] = mapped_column(db.String(160))
    user: Mapped[User] = relationship(back_populates="grants", foreign_keys=[user_id])
    plan: Mapped[Plan] = relationship()


class UserToolOverride(db.Model):
    __tablename__ = "user_tool_overrides"
    user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), primary_key=True)
    tool_id: Mapped[str] = mapped_column(db.ForeignKey("tools.id"), primary_key=True)
    decision: Mapped[str] = mapped_column(db.String(8), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Project(db.Model):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(db.String(200), nullable=False)
    description: Mapped[str] = mapped_column(db.Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    owner: Mapped[User] = relationship(back_populates="projects")


class VocabularyLibrary(db.Model):
    __tablename__ = "vocabulary_libraries"
    id: Mapped[str] = mapped_column(db.String(60), primary_key=True)
    name: Mapped[str] = mapped_column(db.String(160), nullable=False)
    description: Mapped[str] = mapped_column(db.Text, default="", nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), default="published", nullable=False)
    version: Mapped[str] = mapped_column(db.String(24), default="v1", nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(db.JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(db.String(64), nullable=False)
    counts_json: Mapped[dict] = mapped_column(db.JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProjectLibrary(db.Model):
    __tablename__ = "project_libraries"
    project_id: Mapped[str] = mapped_column(db.ForeignKey("projects.id"), primary_key=True)
    library_id: Mapped[str] = mapped_column(db.ForeignKey("vocabulary_libraries.id"), primary_key=True)
    source_hash: Mapped[str] = mapped_column(db.String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(db.String(24), default="v1", nullable=False)


class ProjectVocabularyVersion(db.Model):
    __tablename__ = "project_vocabulary_versions"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(db.ForeignKey("projects.id"), index=True, nullable=False)
    version: Mapped[str] = mapped_column(db.String(24), nullable=False)
    parent_version: Mapped[str | None] = mapped_column(db.String(24))
    content_hash: Mapped[str] = mapped_column(db.String(64), nullable=False)
    counts_json: Mapped[dict] = mapped_column(db.JSON, nullable=False)
    snapshot_path: Mapped[str] = mapped_column(db.String(400), nullable=False)
    note: Mapped[str] = mapped_column(db.String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    __table_args__ = (UniqueConstraint("project_id", "version"),)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    admin_user_id: Mapped[str | None] = mapped_column(db.ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(db.String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(db.String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(db.String(80), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    before_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    after_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
