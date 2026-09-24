"""Metadados persistentes; PDFs, corpus integral e embeddings ficam fora do banco."""

from __future__ import annotations

from datetime import datetime, timezone
import unicodedata
from uuid import uuid4

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint, event
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
    must_change_password: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    role: Mapped[str] = mapped_column(db.String(16), nullable=False, default="user")
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="active")
    student_verification_status: Mapped[str] = mapped_column(db.String(24), default="not_required", nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    status_before_deletion: Mapped[str | None] = mapped_column(db.String(16))
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


class PasswordRecoveryToken(db.Model):
    """Credencial efêmera: somente o SHA-256 do token é persistido."""

    __tablename__ = "password_recovery_tokens"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), index=True, nullable=False)
    token_digest: Mapped[str] = mapped_column(db.String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))


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
    granted_by_name_snapshot: Mapped[str | None] = mapped_column(db.String(160))
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
    scrape_type: Mapped[str] = mapped_column(db.String(16), default="systematic", nullable=False, index=True)
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


class Analysis(db.Model):
    """Execução persistente, independente do cache de progresso do Gunicorn."""

    __tablename__ = "analyses"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(db.ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(db.String(200), nullable=False)
    name_confirmed: Mapped[bool] = mapped_column(db.Boolean, default=False, nullable=False)
    source_type: Mapped[str] = mapped_column(db.String(16), nullable=False)
    tool_id: Mapped[str] = mapped_column(db.ForeignKey("tools.id"), nullable=False)
    tool_version: Mapped[str] = mapped_column(db.String(32), nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="processando")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    document_count: Mapped[int] = mapped_column(db.Integer, default=0, nullable=False)
    result_count: Mapped[int] = mapped_column(db.Integer, default=0, nullable=False)
    parameters_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    excel_files_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    error_message: Mapped[str | None] = mapped_column(db.String(300))

    @property
    def display_name(self) -> str:
        return self.name if self.name_confirmed else "Base de análise sem nome"


class AnalysisDocument(db.Model):
    """PDF próprio da execução; nunca aponta para um PDF compartilhado."""

    __tablename__ = "analysis_documents"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analysis_id: Mapped[str] = mapped_column(db.ForeignKey("analyses.id"), index=True, nullable=False)
    original_name: Mapped[str] = mapped_column(db.String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(db.String(255), nullable=False)
    __table_args__ = (
        UniqueConstraint("analysis_id", "stored_name"),
        # Permite FKs compostas que provam que o documento pertence à Base.
        Index("ux_analysis_documents_id_analysis_id", "id", "analysis_id", unique=True),
    )


def normalized_qualitative_code_name(name: str) -> str:
    """Chave estável para unicidade de códigos dentro de uma Base."""
    return unicodedata.normalize("NFKC", " ".join(name.split())).casefold()


class QualitativeCode(db.Model):
    __tablename__ = "qualitative_codes"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analysis_id: Mapped[str] = mapped_column(db.ForeignKey("analyses.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(db.String(160), nullable=False)
    normalized_name: Mapped[str] = mapped_column(db.String(160), nullable=False)
    description: Mapped[str] = mapped_column(db.Text, default="", nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("analysis_id", "normalized_name", name="uq_qualitative_code_name_per_base"),
        UniqueConstraint("id", "analysis_id", name="uq_qualitative_code_id_base"),
        CheckConstraint("length(trim(name)) > 0", name="ck_qualitative_code_name"),
        CheckConstraint("length(normalized_name) > 0", name="ck_qualitative_code_normalized_name"),
    )


@event.listens_for(QualitativeCode, "before_insert")
@event.listens_for(QualitativeCode, "before_update")
def _prepare_qualitative_code(_mapper, _connection, code: QualitativeCode) -> None:
    name = " ".join(code.name.split())
    normalized = normalized_qualitative_code_name(name)
    if not name or len(name) > 160 or len(normalized) > 160:
        raise ValueError("Informe um nome de código válido (até 160 caracteres).")
    code.name = name
    code.normalized_name = normalized


class QualitativeExcerpt(db.Model):
    __tablename__ = "qualitative_excerpts"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analysis_id: Mapped[str] = mapped_column(db.ForeignKey("analyses.id"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    page_number: Mapped[int] = mapped_column(db.Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(db.Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(db.Integer, nullable=False)
    quoted_text: Mapped[str] = mapped_column(db.Text, nullable=False)
    page_text_hash: Mapped[str] = mapped_column(db.String(64), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "analysis_id"],
            ["analysis_documents.id", "analysis_documents.analysis_id"],
            name="fk_qualitative_excerpt_document_base",
        ),
        UniqueConstraint("id", "analysis_id", name="uq_qualitative_excerpt_id_base"),
        CheckConstraint("page_number >= 1", name="ck_qualitative_excerpt_page"),
        CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="ck_qualitative_excerpt_offsets"),
        CheckConstraint("length(quoted_text) > 0", name="ck_qualitative_excerpt_text"),
        CheckConstraint("length(page_text_hash) = 64", name="ck_qualitative_excerpt_hash"),
    )


class QualitativeCoding(db.Model):
    __tablename__ = "qualitative_codings"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    # A Base repetida aqui permite impor no banco que código e trecho coincidem.
    analysis_id: Mapped[str] = mapped_column(db.ForeignKey("analyses.id"), nullable=False, index=True)
    excerpt_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    code_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    origin: Mapped[str] = mapped_column(db.String(16), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["excerpt_id", "analysis_id"],
                             ["qualitative_excerpts.id", "qualitative_excerpts.analysis_id"],
                             name="fk_qualitative_coding_excerpt_base"),
        ForeignKeyConstraint(["code_id", "analysis_id"],
                             ["qualitative_codes.id", "qualitative_codes.analysis_id"],
                             name="fk_qualitative_coding_code_base"),
        UniqueConstraint("excerpt_id", "code_id", name="uq_qualitative_coding_excerpt_code"),
        CheckConstraint("origin IN ('manual', 'assisted')", name="ck_qualitative_coding_origin"),
    )


class QualitativeMemo(db.Model):
    __tablename__ = "qualitative_memos"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analysis_id: Mapped[str] = mapped_column(db.ForeignKey("analyses.id"), nullable=False, index=True)
    document_id: Mapped[str | None] = mapped_column(db.String(36))
    code_id: Mapped[str | None] = mapped_column(db.String(36))
    excerpt_id: Mapped[str | None] = mapped_column(db.String(36))
    text: Mapped[str] = mapped_column(db.Text, nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["document_id", "analysis_id"],
                             ["analysis_documents.id", "analysis_documents.analysis_id"],
                             name="fk_qualitative_memo_document_base"),
        ForeignKeyConstraint(["code_id", "analysis_id"],
                             ["qualitative_codes.id", "qualitative_codes.analysis_id"],
                             name="fk_qualitative_memo_code_base"),
        ForeignKeyConstraint(["excerpt_id", "analysis_id"],
                             ["qualitative_excerpts.id", "qualitative_excerpts.analysis_id"],
                             name="fk_qualitative_memo_excerpt_base"),
        CheckConstraint(
            "(CASE WHEN document_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN code_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN excerpt_id IS NOT NULL THEN 1 ELSE 0 END) <= 1",
            name="ck_qualitative_memo_single_target",
        ),
        CheckConstraint("length(trim(text)) > 0", name="ck_qualitative_memo_text"),
    )


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    admin_user_id: Mapped[str | None] = mapped_column(db.ForeignKey("users.id"))
    admin_name_snapshot: Mapped[str | None] = mapped_column(db.String(160))
    action: Mapped[str] = mapped_column(db.String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(db.String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(db.String(80), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    before_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    after_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
