"""Metadados persistentes; PDFs, corpus integral e embeddings ficam fora do banco."""

from __future__ import annotations

from datetime import datetime, timezone
import unicodedata
from uuid import uuid4

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint, event, inspect, select, text
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
    analytics_projects: Mapped[list[AnalyticsProject]] = relationship(back_populates="owner")
    profile: Mapped[UserProfile | None] = relationship(back_populates="user", uselist=False)
    owned_workspaces: Mapped[list[Workspace]] = relationship(
        back_populates="owner", foreign_keys="Workspace.owner_user_id"
    )
    workspace_memberships: Mapped[list[WorkspaceMembership]] = relationship(
        back_populates="user", foreign_keys="WorkspaceMembership.user_id"
    )
    commercial_trials: Mapped[list[CommercialTrial]] = relationship(
        back_populates="beneficiary", foreign_keys="CommercialTrial.user_id"
    )

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
    workspace_subscriptions: Mapped[list[WorkspaceSubscription]] = relationship(back_populates="plan")
    limit_policies: Mapped[list[PlanLimitPolicy]] = relationship(
        back_populates="plan", order_by="PlanLimitPolicy.version"
    )
    commercial_trials: Mapped[list[CommercialTrial]] = relationship(
        back_populates="plan", foreign_keys="CommercialTrial.plan_id"
    )


class Workspace(db.Model):
    """Área de trabalho SaaS, independente de qualquer ferramenta ou conector."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(db.String(200), nullable=False)
    workspace_type: Mapped[str] = mapped_column(db.String(16), nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="active")
    # O titular é uma conveniência histórica. A associação ``owner`` em
    # WorkspaceMembership é a fonte estrutural do papel no workspace.
    owner_user_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    owner: Mapped[User | None] = relationship(back_populates="owned_workspaces", foreign_keys=[owner_user_id])
    memberships: Mapped[list[WorkspaceMembership]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    subscriptions: Mapped[list[WorkspaceSubscription]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    commercial_trials: Mapped[list[CommercialTrial]] = relationship(
        back_populates="workspace", foreign_keys="CommercialTrial.workspace_id"
    )

    __table_args__ = (
        CheckConstraint("workspace_type IN ('individual', 'team')", name="ck_workspace_type"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_workspace_status"),
    )


class WorkspaceMembership(db.Model):
    """Participação histórica; somente uma associação ativa por pessoa e área."""

    __tablename__ = "workspace_memberships"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        db.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(db.String(16), nullable=False, default="member")
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="active")
    joined_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    workspace: Mapped[Workspace] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="workspace_memberships", foreign_keys=[user_id])

    __table_args__ = (
        CheckConstraint("role IN ('owner', 'member')", name="ck_workspace_membership_role"),
        CheckConstraint("status IN ('active', 'ended')", name="ck_workspace_membership_status"),
        Index(
            "uq_workspace_membership_active_user",
            "workspace_id", "user_id", unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )


class WorkspaceSubscription(db.Model):
    """Registro histórico de plano contratado por área, ainda sem cobrar ou autorizar."""

    __tablename__ = "workspace_subscriptions"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        db.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(db.ForeignKey("plans.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(db.String(24), nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    assigned_by_id: Mapped[str | None] = mapped_column(db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    assigned_by_name_snapshot: Mapped[str | None] = mapped_column(db.String(160))
    # Snapshot contratual imutável. A reserva OJS consulta-o pelo resolvedor
    # central de limites; outras cotas continuam sem aplicação operacional.
    terms_json: Mapped[dict] = mapped_column(db.JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="subscriptions")
    plan: Mapped[Plan] = relationship(back_populates="workspace_subscriptions")
    limit_adjustments: Mapped[list[WorkspaceSubscriptionLimitAdjustment]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan",
        order_by="WorkspaceSubscriptionLimitAdjustment.created_at",
    )
    commercial_trials: Mapped[list[CommercialTrial]] = relationship(
        back_populates="workspace_subscription", foreign_keys="CommercialTrial.workspace_subscription_id"
    )
    ojs_journal_allocations: Mapped[list[WorkspaceSubscriptionOjsJournal]] = relationship(
        back_populates="subscription"
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'canceled', 'expired')", name="ck_workspace_subscription_status"),
        Index(
            "uq_workspace_subscription_open",
            "workspace_id", unique=True,
            sqlite_where=text("status = 'active' AND ended_at IS NULL"),
            postgresql_where=text("status = 'active' AND ended_at IS NULL"),
        ),
    )


class PlanLimitPolicy(db.Model):
    """Versão imutável da política comercial publicada para um plano.

    ``Plan.limits_json`` guarda a política vigente para criação de contratos.
    Esta tabela mantém as versões já publicadas, sem reescrever snapshots em
    ``WorkspaceSubscription.terms_json``.
    """

    __tablename__ = "plan_limit_policies"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    plan_id: Mapped[str] = mapped_column(db.ForeignKey("plans.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(db.Integer, nullable=False)
    limits_json: Mapped[dict] = mapped_column(db.JSON, nullable=False, default=dict)
    justification: Mapped[str] = mapped_column(db.String(500), nullable=False, default="")
    published_by_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    published_by_name_snapshot: Mapped[str | None] = mapped_column(db.String(160))
    published_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="limit_policies")

    __table_args__ = (
        UniqueConstraint("plan_id", "version", name="uq_plan_limit_policy_version"),
        CheckConstraint("version > 0", name="ck_plan_limit_policy_positive_version"),
    )


class WorkspaceSubscriptionLimitAdjustment(db.Model):
    """Acréscimo comercial explícito, sem relação com permissões de ferramenta."""

    __tablename__ = "workspace_subscription_limit_adjustments"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    workspace_subscription_id: Mapped[str] = mapped_column(
        db.ForeignKey("workspace_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_key: Mapped[str] = mapped_column(db.String(64), nullable=False)
    additional_units: Mapped[int] = mapped_column(db.Integer, nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="active")
    starts_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    justification: Mapped[str] = mapped_column(db.String(500), nullable=False, default="")
    approved_by_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    approved_by_name_snapshot: Mapped[str | None] = mapped_column(db.String(160))
    revoked_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    revoked_by_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    revoked_by_name_snapshot: Mapped[str | None] = mapped_column(db.String(160))
    revocation_reason: Mapped[str | None] = mapped_column(db.String(500))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    subscription: Mapped[WorkspaceSubscription] = relationship(back_populates="limit_adjustments")

    __table_args__ = (
        CheckConstraint(
            "resource_key IN ('instagram_accounts', 'ojs_journals')",
            name="ck_workspace_subscription_limit_adjustment_resource",
        ),
        CheckConstraint("additional_units > 0", name="ck_workspace_subscription_limit_adjustment_positive"),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_workspace_subscription_limit_adjustment_status",
        ),
        Index(
            "ix_workspace_subscription_limit_adjustment_active",
            "workspace_subscription_id", "resource_key", "status",
        ),
    )


class CommercialTrial(db.Model):
    """Registro imutável de elegibilidade experimental por CPF e grupo.

    O CPF em texto jamais é persistido. ``cpf_fingerprint`` é HMAC-SHA-256
    calculado com segredo de servidor estável e serve exclusivamente para
    impedir a repetição do benefício por novas contas ou workspaces.
    """

    __tablename__ = "commercial_trials"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    cpf_fingerprint: Mapped[str] = mapped_column(db.String(64), nullable=False)
    cpf_key_version: Mapped[str] = mapped_column(db.String(32), nullable=False, default="v1")
    # Dado administrativo minimizado; o CPF em texto nunca é armazenado.
    # ``deferred`` mantém a leitura dos registros anteriores compatível até a
    # migration aditiva estar presente no banco de cada ambiente.
    cpf_masked: Mapped[str | None] = mapped_column(db.String(14), nullable=True, deferred=True)
    commercial_group_id: Mapped[str] = mapped_column(db.String(48), nullable=False)
    user_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    plan_id: Mapped[str] = mapped_column(db.ForeignKey("plans.id"), nullable=False, index=True)
    workspace_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("workspaces.id", ondelete="SET NULL"), index=True
    )
    workspace_subscription_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("workspace_subscriptions.id", ondelete="SET NULL"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    activated_by_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    activated_by_name_snapshot: Mapped[str | None] = mapped_column(db.String(160))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    beneficiary: Mapped[User | None] = relationship(
        back_populates="commercial_trials", foreign_keys=[user_id]
    )
    plan: Mapped[Plan] = relationship(back_populates="commercial_trials", foreign_keys=[plan_id])
    workspace: Mapped[Workspace | None] = relationship(
        back_populates="commercial_trials", foreign_keys=[workspace_id]
    )
    workspace_subscription: Mapped[WorkspaceSubscription | None] = relationship(
        back_populates="commercial_trials", foreign_keys=[workspace_subscription_id]
    )

    __table_args__ = (
        UniqueConstraint(
            "cpf_fingerprint", "commercial_group_id",
            name="uq_commercial_trial_cpf_group",
        ),
        CheckConstraint(
            "commercial_group_id IN ('research', 'management', 'research_management')",
            name="ck_commercial_trial_group",
        ),
        CheckConstraint("expires_at > started_at", name="ck_commercial_trial_period"),
    )


@event.listens_for(PlanLimitPolicy, "before_update")
def _prevent_plan_limit_policy_mutation(_mapper, _connection, _target) -> None:
    """Versões publicadas são somente anexadas; publique uma nova versão."""
    raise ValueError("Políticas de limite publicadas são imutáveis.")


@event.listens_for(PlanLimitPolicy, "before_delete")
def _prevent_plan_limit_policy_deletion(_mapper, _connection, _target) -> None:
    """O histórico comercial não pode ser removido pelo ORM."""
    raise ValueError("Políticas de limite publicadas não podem ser removidas.")


class Tool(db.Model):
    __tablename__ = "tools"
    id: Mapped[str] = mapped_column(db.String(40), primary_key=True)
    name: Mapped[str] = mapped_column(db.String(160), nullable=False)
    description: Mapped[str] = mapped_column(db.Text, default="", nullable=False)
    route: Mapped[str] = mapped_column(db.String(160), nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, default=True, nullable=False)


class ToolVisibilitySetting(db.Model):
    """Preferência administrativa de apresentação, independente de acesso."""

    __tablename__ = "tool_visibility_settings"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    scope_type: Mapped[str] = mapped_column(db.String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(db.String(80), nullable=False)
    visible: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=True)
    changed_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    # O snapshot preserva autoria de uma decisão mesmo se a conta
    # administrativa for removida de acordo com o fluxo existente.
    changed_by_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    changed_by_name_snapshot: Mapped[str | None] = mapped_column(db.String(160))

    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", name="uq_tool_visibility_setting_scope"),
        CheckConstraint("scope_type IN ('group', 'tool')", name="ck_tool_visibility_setting_scope_type"),
    )


class AssistantAISettings(db.Model):
    """Configuração operacional global dos providers; nunca contém credenciais."""

    __tablename__ = "assistant_ai_settings"

    # Uma chave fixa faz do registro um singleton também no banco, sem depender
    # de um cache de processo ou de uma convenção apenas da aplicação.
    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, default=1)
    strategy: Mapped[str] = mapped_column(db.String(24), nullable=False, default="single")
    primary_provider: Mapped[str] = mapped_column(db.String(32), nullable=False, default="gemini")
    enabled_providers: Mapped[list] = mapped_column(db.JSON, nullable=False, default=lambda: ["gemini"])
    fallback_order: Mapped[list] = mapped_column(db.JSON, nullable=False, default=list)
    analysis_native_model: Mapped[str] = mapped_column(db.String(160), nullable=False, default="analysis-native-v1")
    gemini_model: Mapped[str] = mapped_column(db.String(160), nullable=False, default="gemini-3.8-flash")
    openai_model: Mapped[str] = mapped_column(db.String(160), nullable=False, default="gpt-4.1")
    anthropic_model: Mapped[str] = mapped_column(db.String(160), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_assistant_ai_settings_singleton"),
    )


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
    # NULL preserva bibliotecas administrativas globais; proprietário define a privada.
    owner_user_id: Mapped[str | None] = mapped_column(db.ForeignKey("users.id"), index=True)
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


class AnalyticsProject(db.Model):
    """Acompanhamento Analytics independente dos projetos e Bases de PDF."""

    __tablename__ = "analytics_projects"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    # A V1 cria somente projetos Instagram; a coluna mantém o núcleo utilizável
    # por um módulo futuro sem acoplar este domínio a Project.scrape_type.
    module_key: Mapped[str] = mapped_column(db.String(40), nullable=False, default="instagram")
    name: Mapped[str] = mapped_column(db.String(200), nullable=False)
    description: Mapped[str] = mapped_column(db.Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    owner: Mapped[User] = relationship(back_populates="analytics_projects")
    runs: Mapped[list[AnalyticsRun]] = relationship(back_populates="analytics_project")
    instagram_account: Mapped[InstagramAccount | None] = relationship(
        back_populates="analytics_project", uselist=False
    )
    taxonomy_dimensions: Mapped[list[InstagramTaxonomyDimension]] = relationship(
        back_populates="analytics_project"
    )
    insight_runs: Mapped[list[InstagramInsightRun]] = relationship(
        back_populates="analytics_project"
    )
    recommendation_runs: Mapped[list[InstagramRecommendationRun]] = relationship(
        back_populates="analytics_project"
    )
    recommendation_actions: Mapped[list[InstagramRecommendationAction]] = relationship(
        back_populates="analytics_project"
    )
    learnings: Mapped[list[InstagramLearning]] = relationship(back_populates="analytics_project")
    reports: Mapped[list[InstagramReport]] = relationship(back_populates="analytics_project")
    ojs_journal: Mapped[OjsJournal | None] = relationship(
        back_populates="analytics_project", uselist=False
    )

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_analytics_project_name"),
        Index("ix_analytics_projects_owner_module_status", "owner_user_id", "module_key", "status"),
        Index("ix_analytics_projects_owner_updated", "owner_user_id", "updated_at"),
    )


class OjsJournal(db.Model):
    """Periódico OJS próprio de um projeto Analytics com módulo ``ojs_seo``.

    ``public_url`` registra a URL informada. O endpoint, nome e versão só são
    preenchidos quando uma descoberta futura os confirmar; não há inferência de
    versão apenas pela URL ou por um anúncio externo.
    """

    __tablename__ = "ojs_journals"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analytics_project_id: Mapped[str] = mapped_column(
        db.ForeignKey("analytics_projects.id"), nullable=False, unique=True
    )
    public_url: Mapped[str] = mapped_column(db.String(2048), nullable=False)
    oai_endpoint_url: Mapped[str | None] = mapped_column(db.String(2048))
    oai_endpoint_validated_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    repository_name: Mapped[str | None] = mapped_column(db.String(500))
    observed_ojs_version: Mapped[str | None] = mapped_column(db.String(64))
    version_evidence: Mapped[str | None] = mapped_column(db.String(1_000))
    last_identified_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    discovery_status: Mapped[str] = mapped_column(db.String(24), nullable=False, default="pending")
    last_discovery_error: Mapped[str | None] = mapped_column(db.String(300))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    analytics_project: Mapped[AnalyticsProject] = relationship(back_populates="ojs_journal")
    audit_runs: Mapped[list[OjsSeoAuditRun]] = relationship(back_populates="journal")
    articles: Mapped[list[OjsArticle]] = relationship(back_populates="journal")
    subscription_allocations: Mapped[list[WorkspaceSubscriptionOjsJournal]] = relationship(
        back_populates="journal"
    )
    html_audit_runs: Mapped[list[OjsHtmlAuditRun]] = relationship(back_populates="journal")
    html_audit_campaigns: Mapped[list[OjsHtmlAuditCampaign]] = relationship(back_populates="journal")
    analysis_operations: Mapped[list[OjsSeoAnalysisOperation]] = relationship(back_populates="journal")

    __table_args__ = (
        CheckConstraint("length(trim(public_url)) > 0", name="ck_ojs_journal_public_url"),
        CheckConstraint(
            "discovery_status IN ('pending', 'identified', 'failed', 'unavailable')",
            name="ck_ojs_journal_discovery_status",
        ),
        Index("ix_ojs_journals_discovery_status", "discovery_status"),
    )


class WorkspaceSubscriptionOjsJournal(db.Model):
    """Reserva histórica de uma revista OJS na assinatura que a custeia.

    A identidade é o hash do endpoint OAI-PMH canônico efetivamente validado,
    e não o domínio, o título da revista ou uma URL informada pelo usuário.
    Assim, duas revistas na mesma instalação continuam distintas quando seus
    endpoints OAI são distintos. A reserva não é global: clientes diferentes
    podem cadastrar a mesma revista em assinaturas diferentes.
    """

    __tablename__ = "workspace_subscription_ojs_journals"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    workspace_subscription_id: Mapped[str] = mapped_column(
        db.ForeignKey("workspace_subscriptions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ojs_journal_id: Mapped[str] = mapped_column(
        db.ForeignKey("ojs_journals.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Snapshot da identidade verificada no momento da reserva. Não é possível
    # substituí-la por edição posterior do endpoint do periódico ativo.
    canonical_identity_hash: Mapped[str] = mapped_column(db.String(64), nullable=False)
    canonical_endpoint_url: Mapped[str] = mapped_column(db.String(2048), nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    subscription: Mapped[WorkspaceSubscription] = relationship(back_populates="ojs_journal_allocations")
    journal: Mapped[OjsJournal] = relationship(back_populates="subscription_allocations")

    __table_args__ = (
        CheckConstraint("status IN ('active', 'released')", name="ck_workspace_subscription_ojs_journal_status"),
        CheckConstraint(
            "(status = 'active' AND released_at IS NULL) OR "
            "(status = 'released' AND released_at IS NOT NULL)",
            name="ck_workspace_subscription_ojs_journal_release_state",
        ),
        CheckConstraint(
            "length(trim(canonical_identity_hash)) = 64",
            name="ck_workspace_subscription_ojs_journal_identity_hash",
        ),
        CheckConstraint(
            "length(trim(canonical_endpoint_url)) > 0",
            name="ck_workspace_subscription_ojs_journal_endpoint",
        ),
        Index(
            "uq_workspace_subscription_ojs_journal_active_journal",
            "ojs_journal_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_workspace_subscription_ojs_journal_active_identity",
            "workspace_subscription_id", "canonical_identity_hash",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_workspace_subscription_ojs_journal_subscription_status",
            "workspace_subscription_id", "status",
        ),
    )


@event.listens_for(OjsJournal, "before_update")
def _prevent_active_ojs_allocation_endpoint_mutation(_mapper, connection, target: OjsJournal) -> None:
    """Impede que uma edição contorne a identidade usada pela reserva ativa."""
    if not inspect(target).attrs.oai_endpoint_url.history.has_changes():
        return
    allocation_id = connection.execute(
        select(WorkspaceSubscriptionOjsJournal.id)
        .where(
            WorkspaceSubscriptionOjsJournal.ojs_journal_id == target.id,
            WorkspaceSubscriptionOjsJournal.status == "active",
        )
        .limit(1)
    ).scalar_one_or_none()
    if allocation_id is not None:
        raise ValueError(
            "Não é permitido alterar o endpoint OAI-PMH de um periódico com alocação comercial ativa."
        )


class OjsSeoAuditRun(db.Model):
    """Histórico de uma auditoria OAI-PMH, independente de ``AnalyticsRun``.

    O lease persistido é a posse durável de um worker. Ele não é um lock em
    memória: o token, a expiração e o índice parcial por periódico impedem que
    processos distintos executem simultaneamente auditorias do mesmo periódico.
    """

    __tablename__ = "ojs_seo_audit_runs"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    journal_id: Mapped[str] = mapped_column(db.ForeignKey("ojs_journals.id"), nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    processor_version: Mapped[str] = mapped_column(db.String(64), nullable=False, default="ojs-oai-v1")
    parameters_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    metrics_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    warnings_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    errors_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    completion_reason: Mapped[str | None] = mapped_column(db.String(80))
    trace_hash: Mapped[str | None] = mapped_column(db.String(64))
    page_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    record_count: Mapped[int] = mapped_column(db.BigInteger, nullable=False, default=0)
    deleted_record_count: Mapped[int] = mapped_column(db.BigInteger, nullable=False, default=0)
    duplicate_identifier_count: Mapped[int] = mapped_column(db.BigInteger, nullable=False, default=0)
    doi_count: Mapped[int] = mapped_column(db.BigInteger, nullable=False, default=0)
    response_bytes: Mapped[int] = mapped_column(db.BigInteger, nullable=False, default=0)
    claim_token: Mapped[str | None] = mapped_column(db.String(36))
    claimed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)

    journal: Mapped[OjsJournal] = relationship(back_populates="audit_runs")
    article_observations: Mapped[list[OjsArticleObservation]] = relationship(
        back_populates="audit_run", viewonly=True
    )

    __table_args__ = (
        UniqueConstraint("id", "journal_id", name="uq_ojs_audit_run_id_journal"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed', 'canceled')",
            name="ck_ojs_audit_run_status",
        ),
        CheckConstraint(
            "page_count >= 0 AND record_count >= 0 AND deleted_record_count >= 0 AND "
            "duplicate_identifier_count >= 0 AND doi_count >= 0 AND response_bytes >= 0",
            name="ck_ojs_audit_run_nonnegative_counts",
        ),
        CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR started_at <= completed_at",
            name="ck_ojs_audit_run_time_order",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_ojs_audit_run_attempt_count"),
        CheckConstraint(
            "(status = 'running' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL AND "
            "lease_expires_at IS NOT NULL) OR "
            "(status != 'running' AND claim_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_ojs_audit_run_claim_state",
        ),
        Index("ix_ojs_audit_runs_journal_created", "journal_id", "created_at"),
        Index("ix_ojs_audit_runs_journal_status", "journal_id", "status"),
        Index("ix_ojs_audit_runs_lease", "status", "lease_expires_at"),
        Index(
            "uq_ojs_audit_runs_one_running_journal",
            "journal_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
    )


class OjsArticle(db.Model):
    """Estado bibliográfico atual de uma identidade OAI de um periódico."""

    __tablename__ = "ojs_articles"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    journal_id: Mapped[str] = mapped_column(db.ForeignKey("ojs_journals.id"), nullable=False)
    oai_identifier: Mapped[str] = mapped_column(db.String(1024), nullable=False)
    record_status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="active")
    title: Mapped[str | None] = mapped_column(db.Text)
    titles_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    creators_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    subjects_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    descriptions_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    identifiers_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    dates_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    languages_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    types_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    doi: Mapped[str | None] = mapped_column(db.String(500))
    dois_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    oai_datestamp: Mapped[str | None] = mapped_column(db.String(64))
    first_observed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    last_observed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    metadata_hash: Mapped[str | None] = mapped_column(db.String(64))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    journal: Mapped[OjsJournal] = relationship(back_populates="articles")
    observations: Mapped[list[OjsArticleObservation]] = relationship(back_populates="article", viewonly=True)
    html_analyses: Mapped[list[OjsArticleHtmlAnalysis]] = relationship(back_populates="article", viewonly=True)

    __table_args__ = (
        UniqueConstraint("journal_id", "oai_identifier", name="uq_ojs_article_journal_oai_identifier"),
        UniqueConstraint("id", "journal_id", name="uq_ojs_article_id_journal"),
        CheckConstraint("length(trim(oai_identifier)) > 0", name="ck_ojs_article_oai_identifier"),
        CheckConstraint("record_status IN ('active', 'deleted')", name="ck_ojs_article_status"),
        Index("ix_ojs_articles_journal_status", "journal_id", "record_status"),
        Index("ix_ojs_articles_journal_doi", "journal_id", "doi"),
    )


class OjsArticleObservation(db.Model):
    """Evidência OAI de um artigo em uma auditoria, sem XML bruto."""

    __tablename__ = "ojs_article_observations"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    journal_id: Mapped[str] = mapped_column(db.ForeignKey("ojs_journals.id"), nullable=False)
    article_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    audit_run_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    record_status: Mapped[str] = mapped_column(db.String(16), nullable=False)
    dublin_core_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    doi_observed: Mapped[str | None] = mapped_column(db.String(500))
    dois_observed_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    oai_datestamp: Mapped[str | None] = mapped_column(db.String(64))
    metadata_hash: Mapped[str | None] = mapped_column(db.String(64))
    observed_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    article: Mapped[OjsArticle] = relationship(back_populates="observations", viewonly=True)
    audit_run: Mapped[OjsSeoAuditRun] = relationship(back_populates="article_observations", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["article_id", "journal_id"],
            ["ojs_articles.id", "ojs_articles.journal_id"],
            name="fk_ojs_observation_article_journal",
        ),
        ForeignKeyConstraint(
            ["audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_observation_audit_run_journal",
        ),
        UniqueConstraint("article_id", "audit_run_id", name="uq_ojs_observation_article_run"),
        CheckConstraint("record_status IN ('active', 'deleted')", name="ck_ojs_observation_status"),
        Index("ix_ojs_article_observations_run", "audit_run_id"),
        Index("ix_ojs_article_observations_article_observed", "article_id", "observed_at"),
    )


class OjsSeoAnalysisOperation(db.Model):
    """Coordena, sem rede, uma atualização OAI-PMH e sua campanha HTML.

    A operação mantém os workers independentes: ela apenas registra a ordem e
    os vínculos imutáveis das execuções que cada worker deve processar.
    """

    __tablename__ = "ojs_seo_analysis_operations"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    journal_id: Mapped[str] = mapped_column(db.ForeignKey("ojs_journals.id"), nullable=False)
    mode: Mapped[str] = mapped_column(db.String(16), nullable=False)
    status: Mapped[str] = mapped_column(db.String(32), nullable=False, default="oai_pending")
    oai_audit_run_id: Mapped[str | None] = mapped_column(db.String(36))
    html_campaign_id: Mapped[str | None] = mapped_column(db.String(36))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    parameters_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    warnings_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    errors_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    completion_reason: Mapped[str | None] = mapped_column(db.String(80))

    journal: Mapped[OjsJournal] = relationship(back_populates="analysis_operations")

    __table_args__ = (
        ForeignKeyConstraint(
            ["oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_operation_oai_journal",
        ),
        ForeignKeyConstraint(
            ["html_campaign_id", "journal_id"],
            ["ojs_html_audit_campaigns.id", "ojs_html_audit_campaigns.journal_id"],
            name="fk_ojs_operation_html_campaign_journal",
        ),
        UniqueConstraint("id", "journal_id", name="uq_ojs_operation_id_journal"),
        UniqueConstraint("oai_audit_run_id", name="uq_ojs_operation_oai_run"),
        UniqueConstraint("html_campaign_id", name="uq_ojs_operation_html_campaign"),
        CheckConstraint("mode IN ('initial', 'update')", name="ck_ojs_operation_mode"),
        CheckConstraint(
            "status IN ('oai_pending', 'oai_running', 'html_pending', 'html_running', "
            "'completed', 'completed_with_pending', 'interrupted', 'failed', 'canceled')",
            name="ck_ojs_operation_status",
        ),
        CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR started_at <= completed_at",
            name="ck_ojs_operation_time_order",
        ),
        Index("ix_ojs_operation_journal_created", "journal_id", "created_at"),
        Index("ix_ojs_operation_journal_status", "journal_id", "status"),
        Index(
            "uq_ojs_operation_one_open_journal", "journal_id", unique=True,
            sqlite_where=text("status IN ('oai_pending', 'oai_running', 'html_pending', 'html_running', 'interrupted')"),
            postgresql_where=text("status IN ('oai_pending', 'oai_running', 'html_pending', 'html_running', 'interrupted')"),
        ),
    )


class OjsHtmlAuditCampaign(db.Model):
    """Campanha durável que encadeia lotes HTML de uma única referência OAI.

    A campanha registra a intenção editorial de cobrir a referência inteira;
    cada ``OjsHtmlAuditRun`` continua sendo um lote limitado e histórico. Isso
    evita que uma solicitação Flask mantenha conexões externas abertas ou que
    uma reinicialização perca o ponto de retomada.
    """

    __tablename__ = "ojs_html_audit_campaigns"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    journal_id: Mapped[str] = mapped_column(db.ForeignKey("ojs_journals.id"), nullable=False)
    source_oai_audit_run_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    status: Mapped[str] = mapped_column(db.String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    last_progress_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    batch_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    no_progress_batch_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    parameters_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    warnings_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    errors_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    completion_reason: Mapped[str | None] = mapped_column(db.String(80))

    journal: Mapped[OjsJournal] = relationship(back_populates="html_audit_campaigns")
    audit_runs: Mapped[list[OjsHtmlAuditRun]] = relationship(back_populates="campaign", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_html_campaign_source_oai_journal",
        ),
        UniqueConstraint("id", "journal_id", name="uq_ojs_html_campaign_id_journal"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'completed_with_pending', 'interrupted', 'failed', 'canceled')",
            name="ck_ojs_html_campaign_status",
        ),
        CheckConstraint(
            "batch_count >= 0 AND no_progress_batch_count >= 0",
            name="ck_ojs_html_campaign_nonnegative_counts",
        ),
        CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR started_at <= completed_at",
            name="ck_ojs_html_campaign_time_order",
        ),
        Index("ix_ojs_html_campaign_journal_created", "journal_id", "created_at"),
        Index("ix_ojs_html_campaign_journal_status", "journal_id", "status"),
        Index(
            "uq_ojs_html_campaign_active_reference", "journal_id", "source_oai_audit_run_id", unique=True,
            sqlite_where=text("status IN ('pending', 'running', 'interrupted')"),
            postgresql_where=text("status IN ('pending', 'running', 'interrupted')"),
        ),
    )


class OjsHtmlAuditRun(db.Model):
    """Execução histórica e durável da análise técnica HTML de um periódico.

    A fonte bibliográfica é fixada em ``source_oai_audit_run_id``. Assim, uma
    análise técnica posterior não altera observações Dublin Core nem mistura
    páginas de coletas OAI diferentes. Claims são persistidos pelo mesmo
    motivo do worker OAI: a fase de rede não mantém uma transação aberta.
    """

    __tablename__ = "ojs_html_audit_runs"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    journal_id: Mapped[str] = mapped_column(db.ForeignKey("ojs_journals.id"), nullable=False)
    source_oai_audit_run_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(db.String(36))
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    processor_version: Mapped[str] = mapped_column(db.String(64), nullable=False, default="ojs-html-v1")
    parameters_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    metrics_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    warnings_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    errors_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    completion_reason: Mapped[str | None] = mapped_column(db.String(80))
    eligible_article_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    analyzed_page_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    confirmed_problem_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    potential_problem_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    unverified_page_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    claim_token: Mapped[str | None] = mapped_column(db.String(36))
    claimed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)

    journal: Mapped[OjsJournal] = relationship(back_populates="html_audit_runs")
    # ``campaign_id`` é atribuído explicitamente pelo serviço de campanhas.
    # Como a FK composta também contém ``journal_id`` (já mantido pela relação
    # principal do periódico), esta relação é apenas de leitura para não
    # disputar a coluna com ``journal`` no ORM.
    campaign: Mapped[OjsHtmlAuditCampaign | None] = relationship(
        back_populates="audit_runs", viewonly=True
    )
    analyses: Mapped[list[OjsArticleHtmlAnalysis]] = relationship(back_populates="html_audit_run", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_html_audit_source_oai_journal",
        ),
        ForeignKeyConstraint(
            ["campaign_id", "journal_id"],
            ["ojs_html_audit_campaigns.id", "ojs_html_audit_campaigns.journal_id"],
            name="fk_ojs_html_audit_campaign_journal",
        ),
        UniqueConstraint("id", "journal_id", name="uq_ojs_html_audit_id_journal"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed', 'canceled')",
            name="ck_ojs_html_audit_status",
        ),
        CheckConstraint(
            "eligible_article_count >= 0 AND analyzed_page_count >= 0 AND "
            "confirmed_problem_count >= 0 AND potential_problem_count >= 0 AND unverified_page_count >= 0",
            name="ck_ojs_html_audit_nonnegative_counts",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_ojs_html_audit_attempt_count"),
        CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR started_at <= completed_at",
            name="ck_ojs_html_audit_time_order",
        ),
        CheckConstraint(
            "(status = 'running' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL AND "
            "lease_expires_at IS NOT NULL) OR "
            "(status != 'running' AND claim_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_ojs_html_audit_claim_state",
        ),
        Index("ix_ojs_html_audit_journal_created", "journal_id", "created_at"),
        Index("ix_ojs_html_audit_journal_status", "journal_id", "status"),
        Index("ix_ojs_html_audit_campaign_status", "campaign_id", "status"),
        Index("ix_ojs_html_audit_lease", "status", "lease_expires_at"),
        Index(
            "uq_ojs_html_audit_one_running_journal", "journal_id", unique=True,
            sqlite_where=text("status = 'running'"), postgresql_where=text("status = 'running'"),
        ),
        # A fila HTML admite no máximo uma execução aberta por periódico. A
        # restrição cobre a janela entre a solicitação e o claim do worker,
        # evitando que duas requisições concorrentes iniciem campanhas iguais.
        Index(
            "uq_ojs_html_audit_one_open_journal", "journal_id", unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )


class OjsArticleHtmlAnalysis(db.Model):
    """Evidência técnica de uma página, sem armazenar o HTML bruto."""

    __tablename__ = "ojs_article_html_analyses"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    journal_id: Mapped[str] = mapped_column(db.ForeignKey("ojs_journals.id"), nullable=False)
    article_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    source_observation_id: Mapped[str] = mapped_column(
        db.ForeignKey("ojs_article_observations.id"), nullable=False
    )
    html_audit_run_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), nullable=False)
    source_url: Mapped[str] = mapped_column(db.String(2048), nullable=False)
    final_url: Mapped[str | None] = mapped_column(db.String(2048))
    http_status: Mapped[int | None] = mapped_column(db.Integer)
    content_type: Mapped[str | None] = mapped_column(db.String(200))
    response_time_ms: Mapped[int | None] = mapped_column(db.Integer)
    redirect_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    facts_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    findings_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    evidence_hash: Mapped[str | None] = mapped_column(db.String(64))
    confirmed_problem_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    potential_problem_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    unverified_count: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    checked_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    article: Mapped[OjsArticle] = relationship(back_populates="html_analyses", viewonly=True)
    html_audit_run: Mapped[OjsHtmlAuditRun] = relationship(back_populates="analyses", viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["article_id", "journal_id"],
            ["ojs_articles.id", "ojs_articles.journal_id"],
            name="fk_ojs_html_analysis_article_journal",
        ),
        ForeignKeyConstraint(
            ["html_audit_run_id", "journal_id"],
            ["ojs_html_audit_runs.id", "ojs_html_audit_runs.journal_id"],
            name="fk_ojs_html_analysis_audit_journal",
        ),
        UniqueConstraint("html_audit_run_id", "article_id", name="uq_ojs_html_analysis_run_article"),
        CheckConstraint("status IN ('analyzed', 'not_verified', 'skipped')", name="ck_ojs_html_analysis_status"),
        CheckConstraint(
            "redirect_count >= 0 AND confirmed_problem_count >= 0 AND potential_problem_count >= 0 AND "
            "unverified_count >= 0",
            name="ck_ojs_html_analysis_nonnegative_counts",
        ),
        Index("ix_ojs_html_analysis_run_status", "html_audit_run_id", "status"),
        Index("ix_ojs_html_analysis_journal_article", "journal_id", "article_id"),
    )


class OjsHtmlEvidenceReuse(db.Model):
    """Vínculo explícito entre uma nova referência OAI e evidência HTML antiga.

    A linha não duplica a resposta nem finge uma nova requisição HTTP. Ela
    registra que a evidência de ``source_html_analysis_id`` foi considerada
    compatível com uma observação de outra referência bibliográfica, pelos
    critérios e na data informados. A análise-fonte continua imutável.
    """

    __tablename__ = "ojs_html_evidence_reuses"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    journal_id: Mapped[str] = mapped_column(db.ForeignKey("ojs_journals.id"), nullable=False)
    target_oai_audit_run_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    source_oai_audit_run_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    article_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    target_observation_id: Mapped[str] = mapped_column(
        db.ForeignKey("ojs_article_observations.id"), nullable=False
    )
    source_observation_id: Mapped[str] = mapped_column(
        db.ForeignKey("ojs_article_observations.id"), nullable=False
    )
    source_html_audit_run_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    source_html_analysis_id: Mapped[str] = mapped_column(
        db.ForeignKey("ojs_article_html_analyses.id"), nullable=False
    )
    criteria_version: Mapped[str] = mapped_column(db.String(64), nullable=False)
    reuse_reason: Mapped[str] = mapped_column(db.String(80), nullable=False)
    reused_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["target_oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_html_reuse_target_oai_journal",
        ),
        ForeignKeyConstraint(
            ["source_oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_html_reuse_source_oai_journal",
        ),
        ForeignKeyConstraint(
            ["article_id", "journal_id"],
            ["ojs_articles.id", "ojs_articles.journal_id"],
            name="fk_ojs_html_reuse_article_journal",
        ),
        ForeignKeyConstraint(
            ["source_html_audit_run_id", "journal_id"],
            ["ojs_html_audit_runs.id", "ojs_html_audit_runs.journal_id"],
            name="fk_ojs_html_reuse_source_html_run_journal",
        ),
        UniqueConstraint(
            "target_oai_audit_run_id", "article_id", name="uq_ojs_html_reuse_target_article",
        ),
        CheckConstraint(
            "length(trim(criteria_version)) > 0", name="ck_ojs_html_reuse_criteria_version",
        ),
        CheckConstraint(
            "length(trim(reuse_reason)) > 0", name="ck_ojs_html_reuse_reason",
        ),
        Index("ix_ojs_html_reuse_journal_target", "journal_id", "target_oai_audit_run_id"),
        Index("ix_ojs_html_reuse_source_analysis", "source_html_analysis_id"),
    )


class AnalyticsRun(db.Model):
    """Execução histórica de coleta ou importação, sem identidade por hash."""

    __tablename__ = "analytics_runs"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analytics_project_id: Mapped[str] = mapped_column(db.ForeignKey("analytics_projects.id"), nullable=False)
    source_kind: Mapped[str] = mapped_column(db.String(24), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="pending")
    period_start: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    processor_version: Mapped[str] = mapped_column(db.String(64), nullable=False, default="v1")
    # Evidência para alertar reimportações; não é uma chave única de execução.
    source_hash: Mapped[str | None] = mapped_column(db.String(64))
    parameters_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    record_count: Mapped[int] = mapped_column(db.BigInteger, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(db.String(300))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    analytics_project: Mapped[AnalyticsProject] = relationship(back_populates="runs")
    media_observations: Mapped[list[InstagramMediaObservation]] = relationship(back_populates="analytics_run")
    account_snapshots: Mapped[list[InstagramAccountSnapshot]] = relationship(back_populates="analytics_run")
    comment_observations: Mapped[list[InstagramCommentObservation]] = relationship(back_populates="analytics_run")
    action_evaluations: Mapped[list[InstagramRecommendationActionEvaluation]] = relationship(
        back_populates="analytics_run"
    )

    __table_args__ = (
        CheckConstraint("record_count >= 0", name="ck_analytics_run_record_count"),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="ck_analytics_run_period",
        ),
        Index("ix_analytics_runs_project_started", "analytics_project_id", "started_at"),
        Index("ix_analytics_runs_project_status", "analytics_project_id", "status"),
        Index("ix_analytics_runs_project_period", "analytics_project_id", "period_start", "period_end"),
    )


class InstagramAccount(db.Model):
    """Conta acompanhada por um projeto Instagram; não armazena credenciais."""

    __tablename__ = "instagram_accounts"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    # Um projeto Instagram representa uma conta na V1, de forma explícita.
    analytics_project_id: Mapped[str] = mapped_column(db.ForeignKey("analytics_projects.id"), nullable=False)
    username: Mapped[str] = mapped_column(db.String(100), nullable=False)
    username_normalized: Mapped[str] = mapped_column(db.String(100), nullable=False)
    external_id: Mapped[str | None] = mapped_column(db.String(128))
    display_name: Mapped[str] = mapped_column(db.String(200), default="", nullable=False)
    account_type: Mapped[str | None] = mapped_column(db.String(40))
    connection_status: Mapped[str] = mapped_column(db.String(24), nullable=False, default="not_connected")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    analytics_project: Mapped[AnalyticsProject] = relationship(back_populates="instagram_account")
    media: Mapped[list[InstagramMedia]] = relationship(back_populates="instagram_account")
    snapshots: Mapped[list[InstagramAccountSnapshot]] = relationship(back_populates="instagram_account")
    api_connection: Mapped[InstagramApiConnection | None] = relationship(
        back_populates="instagram_account", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("analytics_project_id", name="uq_instagram_account_project"),
        UniqueConstraint("external_id", name="uq_instagram_account_external_id"),
        CheckConstraint("length(trim(username)) > 0", name="ck_instagram_account_username"),
        CheckConstraint("length(trim(username_normalized)) > 0", name="ck_instagram_account_username_normalized"),
    )


class InstagramApiConnection(db.Model):
    """Credencial separada da conta, limitada à conexão e diagnóstico da API."""

    __tablename__ = "instagram_api_connections"

    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_account_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_accounts.id"), nullable=False, unique=True
    )
    provider: Mapped[str] = mapped_column(db.String(32), nullable=False, default="facebook_login")
    # A identidade da API é separada da identidade que pode ter sido informada
    # em uma importação manual anterior. Isso permite reconciliar uma conta
    # preexistente sem reescrever dados históricos do projeto.
    provider_account_external_id: Mapped[str | None] = mapped_column(db.String(128))
    provider_username: Mapped[str | None] = mapped_column(db.String(100))
    identity_binding: Mapped[str] = mapped_column(db.String(24), nullable=False, default="exact")
    status: Mapped[str] = mapped_column(db.String(24), nullable=False, default="error")
    encrypted_access_token: Mapped[str | None] = mapped_column(db.Text)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(db.Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    scopes_json: Mapped[list] = mapped_column(db.JSON, nullable=False, default=list)
    connected_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    last_capability_test_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    api_version: Mapped[str] = mapped_column(db.String(24), nullable=False)
    capability_result_json: Mapped[dict] = mapped_column(db.JSON, nullable=False, default=dict)
    last_error_category: Mapped[str | None] = mapped_column(db.String(48))
    last_error_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    instagram_account: Mapped[InstagramAccount] = relationship(back_populates="api_connection")

    __table_args__ = (
        CheckConstraint(
            "provider IN ('instagram_login', 'facebook_login')",
            name="ck_instagram_api_connection_provider",
        ),
        CheckConstraint(
            "identity_binding IN ('exact', 'reconciled')",
            name="ck_instagram_api_connection_identity_binding",
        ),
        CheckConstraint(
            "status IN ('connected', 'needs_reauth', 'revoked', 'error')",
            name="ck_instagram_api_connection_status",
        ),
        UniqueConstraint(
            "provider_account_external_id",
            name="uq_instagram_api_connection_provider_account_external_id",
        ),
        Index("ix_instagram_api_connections_status", "status"),
    )


class InstagramMedia(db.Model):
    """Publicação estável; métricas mutáveis ficam somente nas observações."""

    __tablename__ = "instagram_media"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_account_id: Mapped[str] = mapped_column(db.ForeignKey("instagram_accounts.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(db.String(128))
    # Importadores futuros devem preencher uma chave estável quando a fonte não
    # fornecer external_id; linhas sem qualquer identidade serão rejeitadas.
    source_key: Mapped[str | None] = mapped_column(db.String(200))
    permalink: Mapped[str | None] = mapped_column(db.String(512))
    caption_original: Mapped[str] = mapped_column(db.Text, default="", nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    media_type_raw: Mapped[str | None] = mapped_column(db.String(40))
    first_seen_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    instagram_account: Mapped[InstagramAccount] = relationship(back_populates="media")
    observations: Mapped[list[InstagramMediaObservation]] = relationship(back_populates="instagram_media")
    classifications: Mapped[list[InstagramMediaClassification]] = relationship(
        back_populates="instagram_media"
    )
    comments: Mapped[list[InstagramComment]] = relationship(back_populates="instagram_media")

    __table_args__ = (
        UniqueConstraint("instagram_account_id", "external_id", name="uq_instagram_media_account_external_id"),
        UniqueConstraint("instagram_account_id", "source_key", name="uq_instagram_media_account_source_key"),
        CheckConstraint(
            "external_id IS NOT NULL OR source_key IS NOT NULL",
            name="ck_instagram_media_identity",
        ),
        Index("ix_instagram_media_account_published", "instagram_account_id", "published_at"),
    )


class InstagramMediaObservation(db.Model):
    """Métricas brutas observadas para uma publicação em uma execução."""

    __tablename__ = "instagram_media_observations"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_media_id: Mapped[str] = mapped_column(db.ForeignKey("instagram_media.id"), nullable=False)
    analytics_run_id: Mapped[str] = mapped_column(db.ForeignKey("analytics_runs.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    reach: Mapped[int | None] = mapped_column(db.BigInteger)
    impressions: Mapped[int | None] = mapped_column(db.BigInteger)
    plays: Mapped[int | None] = mapped_column(db.BigInteger)
    likes: Mapped[int | None] = mapped_column(db.BigInteger)
    comments_count: Mapped[int | None] = mapped_column(db.BigInteger)
    shares: Mapped[int | None] = mapped_column(db.BigInteger)
    saves: Mapped[int | None] = mapped_column(db.BigInteger)
    interactions: Mapped[int | None] = mapped_column(db.BigInteger)
    follows_generated: Mapped[int | None] = mapped_column(db.BigInteger)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    instagram_media: Mapped[InstagramMedia] = relationship(back_populates="observations")
    analytics_run: Mapped[AnalyticsRun] = relationship(back_populates="media_observations")

    __table_args__ = (
        UniqueConstraint("analytics_run_id", "instagram_media_id", name="uq_instagram_media_observation_run_media"),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="ck_instagram_media_observation_period",
        ),
        CheckConstraint(
            "(reach IS NULL OR reach >= 0) AND "
            "(impressions IS NULL OR impressions >= 0) AND "
            "(plays IS NULL OR plays >= 0) AND "
            "(likes IS NULL OR likes >= 0) AND "
            "(comments_count IS NULL OR comments_count >= 0) AND "
            "(shares IS NULL OR shares >= 0) AND "
            "(saves IS NULL OR saves >= 0) AND "
            "(interactions IS NULL OR interactions >= 0) AND "
            "(follows_generated IS NULL OR follows_generated >= 0)",
            name="ck_instagram_media_observation_nonnegative",
        ),
        Index("ix_instagram_media_observations_media_observed", "instagram_media_id", "observed_at"),
    )


class InstagramAccountSnapshot(db.Model):
    """Métricas históricas da conta, independentes das observações de mídia."""

    __tablename__ = "instagram_account_snapshots"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_account_id: Mapped[str] = mapped_column(db.ForeignKey("instagram_accounts.id"), nullable=False)
    analytics_run_id: Mapped[str] = mapped_column(db.ForeignKey("analytics_runs.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    followers_count: Mapped[int | None] = mapped_column(db.BigInteger)
    new_followers: Mapped[int | None] = mapped_column(db.BigInteger)
    account_reach: Mapped[int | None] = mapped_column(db.BigInteger)
    profile_views: Mapped[int | None] = mapped_column(db.BigInteger)
    website_clicks: Mapped[int | None] = mapped_column(db.BigInteger)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    instagram_account: Mapped[InstagramAccount] = relationship(back_populates="snapshots")
    analytics_run: Mapped[AnalyticsRun] = relationship(back_populates="account_snapshots")

    __table_args__ = (
        UniqueConstraint("analytics_run_id", "instagram_account_id", name="uq_instagram_account_snapshot_run_account"),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="ck_instagram_account_snapshot_period",
        ),
        CheckConstraint(
            "(followers_count IS NULL OR followers_count >= 0) AND "
            "(new_followers IS NULL OR new_followers >= 0) AND "
            "(account_reach IS NULL OR account_reach >= 0) AND "
            "(profile_views IS NULL OR profile_views >= 0) AND "
            "(website_clicks IS NULL OR website_clicks >= 0)",
            name="ck_instagram_account_snapshot_nonnegative",
        ),
        Index("ix_instagram_account_snapshots_account_observed", "instagram_account_id", "observed_at"),
    )


class InstagramComment(db.Model):
    """Identidade estável de um comentário; nunca contém métricas mutáveis."""

    __tablename__ = "instagram_comments"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_media_id: Mapped[str] = mapped_column(db.ForeignKey("instagram_media.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(db.String(128), nullable=False)
    parent_comment_id: Mapped[str | None] = mapped_column(db.ForeignKey("instagram_comments.id"))
    author_username: Mapped[str | None] = mapped_column(db.String(100))
    author_external_id: Mapped[str | None] = mapped_column(db.String(128))
    published_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    # A primeira evidência textual permanece aqui; versões posteriores vivem
    # em instagram_comment_text_versions e não a substituem. A Meta pode
    # devolver uma identidade válida sem disponibilizar o corpo do comentário;
    # nesse caso, ``None`` registra essa indisponibilidade sem fabricar texto.
    # ``active_history`` forces SQLAlchemy to load the persisted value before
    # replacing an expired/deferred attribute.  The ORM guard below must prove
    # that this is the one permitted transition (None -> first real text), not
    # merely infer it from a possibly incomplete history.
    original_text: Mapped[str | None] = mapped_column(db.Text, active_history=True)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    instagram_media: Mapped[InstagramMedia] = relationship(back_populates="comments")
    parent: Mapped[InstagramComment | None] = relationship(
        "InstagramComment", remote_side="InstagramComment.id", foreign_keys=[parent_comment_id],
        back_populates="replies",
    )
    replies: Mapped[list[InstagramComment]] = relationship(
        "InstagramComment", foreign_keys=[parent_comment_id], back_populates="parent"
    )
    text_versions: Mapped[list[InstagramCommentTextVersion]] = relationship(back_populates="instagram_comment")
    observations: Mapped[list[InstagramCommentObservation]] = relationship(back_populates="instagram_comment")
    assessments: Mapped[list[InstagramCommentAssessment]] = relationship(back_populates="instagram_comment")

    __table_args__ = (
        UniqueConstraint("instagram_media_id", "external_id", name="uq_instagram_comment_media_external_id"),
        CheckConstraint("length(trim(external_id)) > 0", name="ck_instagram_comment_external_id"),
        CheckConstraint(
            "original_text IS NULL OR length(trim(original_text)) > 0",
            name="ck_instagram_comment_original_text",
        ),
        CheckConstraint(
            "parent_comment_id IS NULL OR parent_comment_id <> id",
            name="ck_instagram_comment_not_self_parent",
        ),
        Index("ix_instagram_comments_media_published", "instagram_media_id", "published_at"),
        Index("ix_instagram_comments_parent", "parent_comment_id"),
    )


@event.listens_for(InstagramComment, "before_update")
def _preserve_instagram_comment_original_text(_mapper, _connection, comment: InstagramComment) -> None:
    """Só permite preencher uma lacuna textual que a Meta informou como indisponível."""
    history = inspect(comment).attrs.original_text.history
    if not history.has_changes():
        return

    # Nunca aceite uma alteração cujo valor anterior não tenha sido carregado:
    # após expire()/refresh() a história só é confiável porque active_history
    # acima força esse carregamento.  Assim, a única escrita permitida é a
    # transição comprovada de texto indisponível para o primeiro texto real.
    if (
        tuple(history.deleted) != (None,)
        or not isinstance(comment.original_text, str)
        or not comment.original_text.strip()
    ):
        raise ValueError("O texto original do comentário não pode ser alterado.")


class InstagramCommentTextVersion(db.Model):
    """Versão textual observada, inclusive em caso de edição externa."""

    __tablename__ = "instagram_comment_text_versions"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_comment_id: Mapped[str] = mapped_column(db.ForeignKey("instagram_comments.id"), nullable=False)
    text: Mapped[str] = mapped_column(db.Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(db.String(64), nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    is_original: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    instagram_comment: Mapped[InstagramComment] = relationship(back_populates="text_versions")
    assessments: Mapped[list[InstagramCommentAssessment]] = relationship(back_populates="text_version")

    __table_args__ = (
        UniqueConstraint("instagram_comment_id", "content_hash", name="uq_instagram_comment_text_version_hash"),
        CheckConstraint("length(trim(text)) > 0", name="ck_instagram_comment_text_version_text"),
        CheckConstraint("length(content_hash) = 64", name="ck_instagram_comment_text_version_hash"),
        Index("ix_instagram_comment_text_versions_comment_observed", "instagram_comment_id", "first_observed_at"),
    )


class InstagramCommentObservation(db.Model):
    """Métricas de comentário observadas em uma execução histórica."""

    __tablename__ = "instagram_comment_observations"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_comment_id: Mapped[str] = mapped_column(db.ForeignKey("instagram_comments.id"), nullable=False)
    analytics_run_id: Mapped[str] = mapped_column(db.ForeignKey("analytics_runs.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    like_count: Mapped[int | None] = mapped_column(db.BigInteger)
    replies_count: Mapped[int | None] = mapped_column(db.BigInteger)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    instagram_comment: Mapped[InstagramComment] = relationship(back_populates="observations")
    analytics_run: Mapped[AnalyticsRun] = relationship(back_populates="comment_observations")

    __table_args__ = (
        UniqueConstraint("analytics_run_id", "instagram_comment_id", name="uq_instagram_comment_observation_run_comment"),
        CheckConstraint(
            "(like_count IS NULL OR like_count >= 0) AND (replies_count IS NULL OR replies_count >= 0)",
            name="ck_instagram_comment_observation_nonnegative",
        ),
        Index("ix_instagram_comment_observations_comment_observed", "instagram_comment_id", "observed_at"),
    )


class InstagramCommentAssessment(db.Model):
    """Classificação histórica de uma versão específica do comentário."""

    __tablename__ = "instagram_comment_assessments"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_comment_id: Mapped[str] = mapped_column(db.ForeignKey("instagram_comments.id"), nullable=False)
    instagram_comment_text_version_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_comment_text_versions.id"), nullable=False
    )
    supersedes_assessment_id: Mapped[str | None] = mapped_column(
        db.ForeignKey("instagram_comment_assessments.id")
    )
    source: Mapped[str] = mapped_column(db.String(24), nullable=False)
    classifier_key: Mapped[str | None] = mapped_column(db.String(80))
    classifier_version: Mapped[str | None] = mapped_column(db.String(80))
    reviewer_user_id: Mapped[str | None] = mapped_column(db.ForeignKey("users.id"))
    sentiment: Mapped[str | None] = mapped_column(db.String(16))
    intensity: Mapped[str | None] = mapped_column(db.String(16))
    intent: Mapped[str | None] = mapped_column(db.String(24))
    topic: Mapped[str | None] = mapped_column(db.String(200))
    topic_normalized: Mapped[str | None] = mapped_column(db.String(200))
    needs_response: Mapped[bool | None] = mapped_column(db.Boolean)
    risk: Mapped[str | None] = mapped_column(db.String(16))
    confidence: Mapped[float | None] = mapped_column(db.Float)
    rationale: Mapped[str | None] = mapped_column(db.Text)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    instagram_comment: Mapped[InstagramComment] = relationship(back_populates="assessments")
    text_version: Mapped[InstagramCommentTextVersion] = relationship(back_populates="assessments")
    supersedes: Mapped[InstagramCommentAssessment | None] = relationship(
        "InstagramCommentAssessment", remote_side="InstagramCommentAssessment.id",
        foreign_keys=[supersedes_assessment_id],
    )
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewer_user_id])

    __table_args__ = (
        CheckConstraint(
            "source IN ('manual', 'automatic', 'human_review', 'imported')",
            name="ck_instagram_comment_assessment_source",
        ),
        CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('positive', 'neutral', 'negative', 'mixed')",
            name="ck_instagram_comment_assessment_sentiment",
        ),
        CheckConstraint(
            "intensity IS NULL OR intensity IN ('low', 'medium', 'high')",
            name="ck_instagram_comment_assessment_intensity",
        ),
        CheckConstraint(
            "intent IS NULL OR intent IN ('praise', 'question', 'request', 'complaint', 'criticism', 'information', 'spam', 'other')",
            name="ck_instagram_comment_assessment_intent",
        ),
        CheckConstraint(
            "risk IS NULL OR risk IN ('none', 'low', 'medium', 'high')",
            name="ck_instagram_comment_assessment_risk",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_instagram_comment_assessment_confidence",
        ),
        CheckConstraint(
            "supersedes_assessment_id IS NULL OR supersedes_assessment_id <> id",
            name="ck_instagram_comment_assessment_not_self_supersedes",
        ),
        Index("ix_instagram_comment_assessments_comment_current", "instagram_comment_id", "superseded_at", "created_at"),
        Index("ix_instagram_comment_assessments_filters", "sentiment", "intent", "risk", "needs_response", "superseded_at"),
    )


class InstagramTaxonomyDimension(db.Model):
    """Dimensão editorial manual pertencente exclusivamente a um projeto Instagram."""

    __tablename__ = "instagram_taxonomy_dimensions"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analytics_project_id: Mapped[str] = mapped_column(
        db.ForeignKey("analytics_projects.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(db.String(80), nullable=False)
    name: Mapped[str] = mapped_column(db.String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(db.Text)
    selection_mode: Mapped[str] = mapped_column(db.String(16), nullable=False, default="single")
    position: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    analytics_project: Mapped[AnalyticsProject] = relationship(back_populates="taxonomy_dimensions")
    categories: Mapped[list[InstagramTaxonomyCategory]] = relationship(
        back_populates="dimension", order_by="InstagramTaxonomyCategory.position, InstagramTaxonomyCategory.name"
    )
    classifications: Mapped[list[InstagramMediaClassification]] = relationship(back_populates="dimension")

    __table_args__ = (
        UniqueConstraint("analytics_project_id", "key", name="uq_instagram_taxonomy_dimension_project_key"),
        CheckConstraint("length(trim(key)) > 0", name="ck_instagram_taxonomy_dimension_key"),
        CheckConstraint("length(trim(name)) > 0", name="ck_instagram_taxonomy_dimension_name"),
        CheckConstraint("selection_mode IN ('single', 'multi')", name="ck_instagram_taxonomy_dimension_mode"),
        CheckConstraint("position >= 0", name="ck_instagram_taxonomy_dimension_position"),
        Index(
            "ix_instagram_taxonomy_dimensions_project_active_position",
            "analytics_project_id", "active", "position",
        ),
    )


class InstagramTaxonomyCategory(db.Model):
    """Categoria editorial configurável dentro de uma dimensão do projeto."""

    __tablename__ = "instagram_taxonomy_categories"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_taxonomy_dimension_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_taxonomy_dimensions.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(db.String(80), nullable=False)
    name: Mapped[str] = mapped_column(db.String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(db.Text)
    position: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    dimension: Mapped[InstagramTaxonomyDimension] = relationship(back_populates="categories")
    classifications: Mapped[list[InstagramMediaClassification]] = relationship(back_populates="category")

    __table_args__ = (
        UniqueConstraint(
            "instagram_taxonomy_dimension_id", "key",
            name="uq_instagram_taxonomy_category_dimension_key",
        ),
        CheckConstraint("length(trim(key)) > 0", name="ck_instagram_taxonomy_category_key"),
        CheckConstraint("length(trim(name)) > 0", name="ck_instagram_taxonomy_category_name"),
        CheckConstraint("position >= 0", name="ck_instagram_taxonomy_category_position"),
        Index(
            "ix_instagram_taxonomy_categories_dimension_active_position",
            "instagram_taxonomy_dimension_id", "active", "position",
        ),
    )


class InstagramMediaClassification(db.Model):
    """Atribuição editorial histórica: a classificação anterior nunca é apagada."""

    __tablename__ = "instagram_media_classifications"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_media_id: Mapped[str] = mapped_column(db.ForeignKey("instagram_media.id"), nullable=False)
    instagram_taxonomy_dimension_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_taxonomy_dimensions.id"), nullable=False
    )
    instagram_taxonomy_category_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_taxonomy_categories.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(db.String(24), nullable=False, default="manual")
    assigned_by_user_id: Mapped[str | None] = mapped_column(db.ForeignKey("users.id"))
    assigned_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    confidence: Mapped[float | None] = mapped_column(db.Float)
    classifier_version: Mapped[str | None] = mapped_column(db.String(64))

    instagram_media: Mapped[InstagramMedia] = relationship(back_populates="classifications")
    dimension: Mapped[InstagramTaxonomyDimension] = relationship(back_populates="classifications")
    category: Mapped[InstagramTaxonomyCategory] = relationship(back_populates="classifications")
    assigned_by: Mapped[User | None] = relationship(foreign_keys=[assigned_by_user_id])

    __table_args__ = (
        CheckConstraint("source = 'manual'", name="ck_instagram_media_classification_source"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_instagram_media_classification_confidence",
        ),
        Index(
            "ix_instagram_media_classifications_media_dimension_current",
            "instagram_media_id", "instagram_taxonomy_dimension_id", "superseded_at",
        ),
        Index(
            "ix_instagram_media_classifications_dimension_category_current",
            "instagram_taxonomy_dimension_id", "instagram_taxonomy_category_id", "superseded_at",
        ),
    )


class InstagramInsightRun(db.Model):
    """Execução histórica e reproduzível do motor de achados Instagram."""

    __tablename__ = "instagram_insight_runs"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analytics_project_id: Mapped[str] = mapped_column(
        db.ForeignKey("analytics_projects.id"), nullable=False
    )
    period_key: Mapped[str] = mapped_column(db.String(12), nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    reference_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    engine_version: Mapped[str] = mapped_column(db.String(80), nullable=False)
    parameters_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    publication_count: Mapped[int] = mapped_column(db.BigInteger, nullable=False, default=0)
    comment_count: Mapped[int] = mapped_column(db.BigInteger, nullable=False, default=0)
    finding_count: Mapped[int] = mapped_column(db.BigInteger, nullable=False, default=0)
    generated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, nullable=False
    )

    analytics_project: Mapped[AnalyticsProject] = relationship(back_populates="insight_runs")
    findings: Mapped[list[InstagramInsightFinding]] = relationship(back_populates="insight_run")
    recommendation_runs: Mapped[list[InstagramRecommendationRun]] = relationship(
        back_populates="insight_run"
    )

    __table_args__ = (
        CheckConstraint("period_key IN ('7d', '30d', '90d', 'all')", name="ck_instagram_insight_run_period"),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="ck_instagram_insight_run_period_range",
        ),
        CheckConstraint(
            "publication_count >= 0 AND comment_count >= 0 AND finding_count >= 0",
            name="ck_instagram_insight_run_counts",
        ),
        Index("ix_instagram_insight_runs_project_generated", "analytics_project_id", "generated_at"),
        Index("ix_instagram_insight_runs_project_period", "analytics_project_id", "period_key", "generated_at"),
    )


class InstagramInsightFinding(db.Model):
    """Achado descritivo versionado, produzido por uma execução de insights."""

    __tablename__ = "instagram_insight_findings"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_insight_run_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_insight_runs.id"), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(db.String(80), nullable=False)
    rule_version: Mapped[str] = mapped_column(db.String(80), nullable=False)
    engine_version: Mapped[str] = mapped_column(db.String(80), nullable=False)
    kind: Mapped[str] = mapped_column(db.String(48), nullable=False)
    title: Mapped[str] = mapped_column(db.String(320), nullable=False)
    summary: Mapped[str] = mapped_column(db.Text, nullable=False)
    metric_key: Mapped[str | None] = mapped_column(db.String(80))
    metric_version: Mapped[str | None] = mapped_column(db.String(80))
    group_label: Mapped[str] = mapped_column(db.String(240), nullable=False)
    comparison_label: Mapped[str | None] = mapped_column(db.String(240))
    group_value: Mapped[float | None] = mapped_column(db.Float)
    comparison_value: Mapped[float | None] = mapped_column(db.Float)
    absolute_difference: Mapped[float | None] = mapped_column(db.Float)
    relative_difference: Mapped[float | None] = mapped_column(db.Float)
    difference_unit: Mapped[str | None] = mapped_column(db.String(32))
    group_n: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    comparison_n: Mapped[int] = mapped_column(db.Integer, nullable=False, default=0)
    coverage: Mapped[float] = mapped_column(db.Float, nullable=False, default=0)
    support_level: Mapped[str] = mapped_column(db.String(16), nullable=False)
    support_explanation: Mapped[str] = mapped_column(db.String(500), nullable=False)
    details_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    # A coorte é um snapshot estruturado, não uma interpretação do título livre.
    cohort_json: Mapped[dict | None] = mapped_column(db.JSON)
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, nullable=False
    )

    insight_run: Mapped[InstagramInsightRun] = relationship(back_populates="findings")
    evidence: Mapped[list[InstagramInsightEvidence]] = relationship(back_populates="finding")

    __table_args__ = (
        CheckConstraint("length(trim(rule_id)) > 0", name="ck_instagram_insight_finding_rule_id"),
        CheckConstraint("length(trim(rule_version)) > 0", name="ck_instagram_insight_finding_rule_version"),
        CheckConstraint("length(trim(engine_version)) > 0", name="ck_instagram_insight_finding_engine_version"),
        CheckConstraint("length(trim(kind)) > 0", name="ck_instagram_insight_finding_kind"),
        CheckConstraint("length(trim(title)) > 0", name="ck_instagram_insight_finding_title"),
        CheckConstraint("length(trim(summary)) > 0", name="ck_instagram_insight_finding_summary"),
        CheckConstraint("group_n >= 0 AND comparison_n >= 0", name="ck_instagram_insight_finding_counts"),
        CheckConstraint("coverage >= 0 AND coverage <= 1", name="ck_instagram_insight_finding_coverage"),
        CheckConstraint(
            "support_level IN ('exploratory', 'moderate', 'consistent')",
            name="ck_instagram_insight_finding_support",
        ),
        Index("ix_instagram_insight_findings_run_kind", "instagram_insight_run_id", "kind"),
        Index("ix_instagram_insight_findings_run_rule", "instagram_insight_run_id", "rule_id"),
    )


class InstagramInsightEvidence(db.Model):
    """Referência polimórfica declarada; a origem é validada pelo motor."""

    __tablename__ = "instagram_insight_evidence"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_insight_finding_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_insight_findings.id"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(db.String(48), nullable=False)
    source_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    role: Mapped[str] = mapped_column(db.String(32), nullable=False)
    details_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, nullable=False
    )

    finding: Mapped[InstagramInsightFinding] = relationship(back_populates="evidence")

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('media', 'media_observation', 'media_classification', "
            "'comment', 'comment_assessment', 'comment_text_version', 'account_snapshot')",
            name="ck_instagram_insight_evidence_source_type",
        ),
        CheckConstraint("length(trim(source_id)) > 0", name="ck_instagram_insight_evidence_source_id"),
        CheckConstraint("length(trim(role)) > 0", name="ck_instagram_insight_evidence_role"),
        UniqueConstraint(
            "instagram_insight_finding_id", "source_type", "source_id", "role",
            name="uq_instagram_insight_evidence_finding_source_role",
        ),
        Index("ix_instagram_insight_evidence_finding_role", "instagram_insight_finding_id", "role"),
        Index("ix_instagram_insight_evidence_source", "source_type", "source_id"),
    )


class InstagramRecommendationRun(db.Model):
    """Execução histórica do motor de recomendações derivada de um run de Insights."""

    __tablename__ = "instagram_recommendation_runs"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analytics_project_id: Mapped[str] = mapped_column(
        db.ForeignKey("analytics_projects.id"), nullable=False
    )
    instagram_insight_run_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_insight_runs.id"), nullable=False
    )
    engine_version: Mapped[str] = mapped_column(db.String(80), nullable=False)
    parameters_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    number_of_findings_considered: Mapped[int] = mapped_column(
        db.BigInteger, nullable=False, default=0
    )
    number_of_recommendations_generated: Mapped[int] = mapped_column(
        db.BigInteger, nullable=False, default=0
    )
    generated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, nullable=False
    )

    analytics_project: Mapped[AnalyticsProject] = relationship(back_populates="recommendation_runs")
    insight_run: Mapped[InstagramInsightRun] = relationship(back_populates="recommendation_runs")
    recommendations: Mapped[list[InstagramRecommendation]] = relationship(
        back_populates="recommendation_run"
    )

    __table_args__ = (
        CheckConstraint(
            "number_of_findings_considered >= 0 AND number_of_recommendations_generated >= 0",
            name="ck_instagram_recommendation_run_counts",
        ),
        UniqueConstraint(
            "instagram_insight_run_id", "engine_version",
            name="uq_instagram_recommendation_run_insight_engine",
        ),
        Index(
            "ix_instagram_recommendation_runs_project_generated",
            "analytics_project_id", "generated_at",
        ),
        Index(
            "ix_instagram_recommendation_runs_insight_generated",
            "instagram_insight_run_id", "generated_at",
        ),
    )


class InstagramRecommendation(db.Model):
    """Orientação analítica versionada e rastreável, sem plano de ação embutido."""

    __tablename__ = "instagram_recommendations"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_recommendation_run_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_recommendation_runs.id"), nullable=False
    )
    canonical_key: Mapped[str] = mapped_column(db.String(320), nullable=False)
    rule_id: Mapped[str] = mapped_column(db.String(80), nullable=False)
    rule_version: Mapped[str] = mapped_column(db.String(80), nullable=False)
    engine_version: Mapped[str] = mapped_column(db.String(80), nullable=False)
    title: Mapped[str] = mapped_column(db.String(320), nullable=False)
    summary: Mapped[str] = mapped_column(db.Text, nullable=False)
    recommendation_type: Mapped[str] = mapped_column(db.String(48), nullable=False)
    priority: Mapped[str] = mapped_column(db.String(16), nullable=False)
    support_level: Mapped[str] = mapped_column(db.String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(db.Text, nullable=False)
    limitations_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, nullable=False
    )

    recommendation_run: Mapped[InstagramRecommendationRun] = relationship(
        back_populates="recommendations"
    )
    finding_links: Mapped[list[InstagramRecommendationFinding]] = relationship(
        back_populates="recommendation"
    )
    actions: Mapped[list[InstagramRecommendationAction]] = relationship(
        back_populates="recommendation"
    )

    __table_args__ = (
        CheckConstraint("length(trim(canonical_key)) > 0", name="ck_instagram_recommendation_key"),
        CheckConstraint("length(trim(rule_id)) > 0", name="ck_instagram_recommendation_rule_id"),
        CheckConstraint("length(trim(rule_version)) > 0", name="ck_instagram_recommendation_rule_version"),
        CheckConstraint("length(trim(engine_version)) > 0", name="ck_instagram_recommendation_engine_version"),
        CheckConstraint("length(trim(title)) > 0", name="ck_instagram_recommendation_title"),
        CheckConstraint("length(trim(summary)) > 0", name="ck_instagram_recommendation_summary"),
        CheckConstraint("priority IN ('low', 'medium', 'high')", name="ck_instagram_recommendation_priority"),
        CheckConstraint(
            "support_level IN ('exploratory', 'moderate', 'consistent')",
            name="ck_instagram_recommendation_support",
        ),
        UniqueConstraint(
            "instagram_recommendation_run_id", "canonical_key",
            name="uq_instagram_recommendation_run_key",
        ),
        Index(
            "ix_instagram_recommendations_run_priority",
            "instagram_recommendation_run_id", "priority", "support_level",
        ),
        Index("ix_instagram_recommendations_run_type", "instagram_recommendation_run_id", "recommendation_type"),
    )


class InstagramRecommendationFinding(db.Model):
    """Ligação explícita que mantém Recommendation → Finding auditável."""

    __tablename__ = "instagram_recommendation_findings"
    instagram_recommendation_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_recommendations.id"), primary_key=True
    )
    instagram_insight_finding_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_insight_findings.id"), primary_key=True
    )

    recommendation: Mapped[InstagramRecommendation] = relationship(back_populates="finding_links")
    finding: Mapped[InstagramInsightFinding] = relationship()

    __table_args__ = (
        Index(
            "ix_instagram_recommendation_findings_finding",
            "instagram_insight_finding_id",
        ),
    )


instagram_learning_evaluations = db.Table(
    "instagram_learning_evaluations",
    db.Column(
        "instagram_learning_id",
        db.String(36),
        db.ForeignKey("instagram_learnings.id"),
        primary_key=True,
    ),
    db.Column(
        "instagram_recommendation_action_evaluation_id",
        db.String(36),
        db.ForeignKey("instagram_recommendation_action_evaluations.id"),
        primary_key=True,
    ),
    Index(
        "ix_instagram_learning_evaluations_evaluation",
        "instagram_recommendation_action_evaluation_id",
    ),
)


class InstagramRecommendationAction(db.Model):
    """Ação ou teste operacional, sempre derivado de uma recomendação persistida."""

    __tablename__ = "instagram_recommendation_actions"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analytics_project_id: Mapped[str] = mapped_column(
        db.ForeignKey("analytics_projects.id"), nullable=False
    )
    instagram_recommendation_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_recommendations.id"), nullable=False
    )
    created_by_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    action_kind: Mapped[str] = mapped_column(db.String(16), nullable=False)
    title: Mapped[str] = mapped_column(db.String(320), nullable=False)
    description: Mapped[str] = mapped_column(db.Text, default="", nullable=False)
    objective: Mapped[str] = mapped_column(db.Text, nullable=False)
    hypothesis: Mapped[str | None] = mapped_column(db.Text)
    target_metric: Mapped[str | None] = mapped_column(db.String(80))
    cohort_json: Mapped[dict | None] = mapped_column(db.JSON)
    cohort_signature: Mapped[str | None] = mapped_column(db.String(64))
    baseline_value: Mapped[float | None] = mapped_column(db.Float)
    target_value: Mapped[float | None] = mapped_column(db.Float)
    success_criterion: Mapped[str | None] = mapped_column(db.Text)
    planned_start_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    planned_end_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="planned")
    result_summary: Mapped[str | None] = mapped_column(db.Text)
    observed_result: Mapped[float | None] = mapped_column(db.Float)
    outcome: Mapped[str | None] = mapped_column(db.String(20))
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    analytics_project: Mapped[AnalyticsProject] = relationship(
        back_populates="recommendation_actions"
    )
    recommendation: Mapped[InstagramRecommendation] = relationship(back_populates="actions")
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    evaluations: Mapped[list[InstagramRecommendationActionEvaluation]] = relationship(
        back_populates="action"
    )

    __table_args__ = (
        CheckConstraint("action_kind IN ('action', 'test')", name="ck_instagram_action_kind"),
        CheckConstraint(
            "status IN ('planned', 'in_progress', 'completed', 'cancelled')",
            name="ck_instagram_action_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('supported', 'not_supported', 'inconclusive', 'not_applicable')",
            name="ck_instagram_action_outcome",
        ),
        CheckConstraint("length(trim(title)) > 0", name="ck_instagram_action_title"),
        CheckConstraint("length(trim(objective)) > 0", name="ck_instagram_action_objective"),
        CheckConstraint(
            "planned_start_at IS NULL OR planned_end_at IS NULL OR planned_start_at <= planned_end_at",
            name="ck_instagram_action_planned_range",
        ),
        CheckConstraint(
            "completed_at IS NULL OR status = 'completed'",
            name="ck_instagram_action_completed_at",
        ),
        CheckConstraint(
            "status != 'completed' OR (completed_at IS NOT NULL AND outcome IS NOT NULL "
            "AND result_summary IS NOT NULL AND length(trim(result_summary)) > 0)",
            name="ck_instagram_action_completion_payload",
        ),
        CheckConstraint(
            "result_summary IS NULL OR length(result_summary) <= 4000",
            name="ck_instagram_action_result_summary_length",
        ),
        Index(
            "ix_instagram_actions_project_status_created",
            "analytics_project_id", "status", "created_at",
        ),
        Index(
            "ix_instagram_actions_recommendation_created",
            "instagram_recommendation_id", "created_at",
        ),
    )


class InstagramRecommendationActionEvaluation(db.Model):
    """Snapshot observacional de um período posterior para uma ação/teste concluída.

    Os valores quantitativos são congelados quando o acompanhamento é criado.
    Apenas a leitura humana (assessment e learning_summary) pode ser ajustada
    depois, sem reprocessar a cadeia de Insights/Recomendações.
    """

    __tablename__ = "instagram_recommendation_action_evaluations"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    instagram_recommendation_action_id: Mapped[str] = mapped_column(
        db.ForeignKey("instagram_recommendation_actions.id"), nullable=False
    )
    analytics_run_id: Mapped[str] = mapped_column(db.ForeignKey("analytics_runs.id"), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    metric_key: Mapped[str | None] = mapped_column(db.String(80))
    cohort_json: Mapped[dict | None] = mapped_column(db.JSON)
    cohort_signature: Mapped[str | None] = mapped_column(db.String(64))
    baseline_value: Mapped[float | None] = mapped_column(db.Float)
    followup_value: Mapped[float | None] = mapped_column(db.Float)
    absolute_change: Mapped[float | None] = mapped_column(db.Float)
    relative_change: Mapped[float | None] = mapped_column(db.Float)
    baseline_sample_size: Mapped[int | None] = mapped_column(db.Integer)
    baseline_coverage: Mapped[float | None] = mapped_column(db.Float)
    followup_sample_size: Mapped[int | None] = mapped_column(db.Integer)
    followup_population_size: Mapped[int | None] = mapped_column(db.Integer)
    followup_coverage: Mapped[float | None] = mapped_column(db.Float)
    assessment: Mapped[str] = mapped_column(db.String(24), nullable=False)
    learning_summary: Mapped[str] = mapped_column(db.Text, nullable=False)
    limitations_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    evaluation_version: Mapped[str] = mapped_column(db.String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    action: Mapped[InstagramRecommendationAction] = relationship(back_populates="evaluations")
    analytics_run: Mapped[AnalyticsRun] = relationship(back_populates="action_evaluations")
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    learnings: Mapped[list[InstagramLearning]] = relationship(
        secondary=instagram_learning_evaluations, back_populates="evaluations"
    )

    __table_args__ = (
        CheckConstraint(
            "assessment IN ('consistent', 'not_consistent', 'inconclusive', 'not_evaluable')",
            name="ck_instagram_action_evaluation_assessment",
        ),
        CheckConstraint(
            "baseline_sample_size IS NULL OR baseline_sample_size >= 0",
            name="ck_instagram_action_evaluation_baseline_n",
        ),
        CheckConstraint(
            "followup_sample_size IS NULL OR followup_sample_size >= 0",
            name="ck_instagram_action_evaluation_followup_n",
        ),
        CheckConstraint(
            "followup_population_size IS NULL OR followup_population_size >= 0",
            name="ck_instagram_action_evaluation_followup_population",
        ),
        CheckConstraint(
            "baseline_coverage IS NULL OR (baseline_coverage >= 0 AND baseline_coverage <= 1)",
            name="ck_instagram_action_evaluation_baseline_coverage",
        ),
        CheckConstraint(
            "followup_coverage IS NULL OR (followup_coverage >= 0 AND followup_coverage <= 1)",
            name="ck_instagram_action_evaluation_followup_coverage",
        ),
        CheckConstraint(
            "length(trim(learning_summary)) > 0 AND length(learning_summary) <= 4000",
            name="ck_instagram_action_evaluation_learning_summary",
        ),
        CheckConstraint("length(trim(evaluation_version)) > 0", name="ck_instagram_action_evaluation_version"),
        UniqueConstraint(
            "instagram_recommendation_action_id", "analytics_run_id",
            name="uq_instagram_action_evaluation_action_run",
        ),
        Index(
            "ix_instagram_action_evaluations_action_created",
            "instagram_recommendation_action_id", "created_at",
        ),
        Index("ix_instagram_action_evaluations_run", "analytics_run_id"),
    )


@event.listens_for(InstagramRecommendationActionEvaluation, "before_update")
def _preserve_instagram_action_evaluation_snapshot(
    _mapper, _connection, evaluation: InstagramRecommendationActionEvaluation,
) -> None:
    """Impede que uma edição interpretativa altere o snapshot quantitativo."""
    immutable_fields = (
        "instagram_recommendation_action_id", "analytics_run_id", "created_by_user_id", "metric_key",
        "cohort_json", "cohort_signature",
        "baseline_value", "followup_value", "absolute_change", "relative_change",
        "baseline_sample_size", "baseline_coverage", "followup_sample_size",
        "followup_population_size", "followup_coverage", "limitations_json", "evaluation_version",
    )
    if any(inspect(evaluation).attrs[field].history.has_changes() for field in immutable_fields):
        raise ValueError("Os snapshots quantitativos do acompanhamento não podem ser alterados.")


class InstagramLearning(db.Model):
    """Síntese humana e rastreável baseada em avaliações comparáveis."""

    __tablename__ = "instagram_learnings"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analytics_project_id: Mapped[str] = mapped_column(
        db.ForeignKey("analytics_projects.id"), nullable=False
    )
    created_by_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(db.String(320), nullable=False)
    learning_summary: Mapped[str] = mapped_column(db.Text, nullable=False)
    assessment: Mapped[str] = mapped_column(db.String(24), nullable=False)
    metric_key: Mapped[str | None] = mapped_column(db.String(80))
    cohort_json: Mapped[dict | None] = mapped_column(db.JSON)
    cohort_signature: Mapped[str | None] = mapped_column(db.String(64))
    learning_version: Mapped[str] = mapped_column(db.String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    analytics_project: Mapped[AnalyticsProject] = relationship(back_populates="learnings")
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    evaluations: Mapped[list[InstagramRecommendationActionEvaluation]] = relationship(
        secondary=instagram_learning_evaluations, back_populates="learnings"
    )

    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="ck_instagram_learning_title"),
        CheckConstraint(
            "length(trim(learning_summary)) > 0 AND length(learning_summary) <= 4000",
            name="ck_instagram_learning_summary",
        ),
        CheckConstraint(
            "assessment IN ('consistent', 'not_consistent', 'mixed', 'inconclusive', 'not_evaluable')",
            name="ck_instagram_learning_assessment",
        ),
        CheckConstraint("length(trim(learning_version)) > 0", name="ck_instagram_learning_version"),
        Index("ix_instagram_learnings_project_created", "analytics_project_id", "created_at"),
        Index("ix_instagram_learnings_project_cohort", "analytics_project_id", "cohort_signature"),
    )


class InstagramReport(db.Model):
    """Relatório Instagram reproduzível, cujo conteúdo é congelado em snapshot."""

    __tablename__ = "instagram_reports"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analytics_project_id: Mapped[str] = mapped_column(
        db.ForeignKey("analytics_projects.id"), nullable=False
    )
    created_by_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(db.String(320), nullable=False)
    report_type: Mapped[str] = mapped_column(db.String(24), nullable=False)
    analysis_mode: Mapped[str] = mapped_column(db.String(32), nullable=False)
    status: Mapped[str] = mapped_column(db.String(16), nullable=False, default="generating")
    baseline_run_id: Mapped[str] = mapped_column(db.ForeignKey("analytics_runs.id"), nullable=False)
    final_run_id: Mapped[str] = mapped_column(db.ForeignKey("analytics_runs.id"), nullable=False)
    selected_sections_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    formats_json: Mapped[list] = mapped_column(db.JSON, default=list, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(db.JSON, default=dict, nullable=False)
    generator_version: Mapped[str] = mapped_column(db.String(80), nullable=False)
    pdf_filename: Mapped[str | None] = mapped_column(db.String(255))
    xlsx_filename: Mapped[str | None] = mapped_column(db.String(255))
    error_message: Mapped[str | None] = mapped_column(db.String(500))
    created_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, nullable=False
    )
    generated_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    analytics_project: Mapped[AnalyticsProject] = relationship(back_populates="reports")
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    baseline_run: Mapped[AnalyticsRun] = relationship(foreign_keys=[baseline_run_id])
    final_run: Mapped[AnalyticsRun] = relationship(foreign_keys=[final_run_id])

    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="ck_instagram_report_title"),
        CheckConstraint(
            "report_type IN ('executive', 'analytical')", name="ck_instagram_report_type"
        ),
        CheckConstraint(
            "analysis_mode IN ('single_period', 'comparative_evolution')",
            name="ck_instagram_report_analysis_mode",
        ),
        CheckConstraint(
            "status IN ('generating', 'completed', 'failed')", name="ck_instagram_report_status"
        ),
        CheckConstraint(
            "length(trim(generator_version)) > 0", name="ck_instagram_report_generator_version"
        ),
        Index("ix_instagram_reports_project_created", "analytics_project_id", "created_at"),
        Index("ix_instagram_reports_project_status", "analytics_project_id", "status"),
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
    # NULL nos registros legados usa o azul padrão; a escolha explícita é global ao código.
    color: Mapped[str | None] = mapped_column(db.String(16))
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
    # O nome automático preserva a expressão pesquisada; a chave de unicidade
    # continua normalizando espaços. Formulários manuais normalizam na entrada.
    name = code.name.strip()
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
    origin: Mapped[str] = mapped_column(db.String(32), default="manual", nullable=False)
    # Âncora da busca original: não muda quando o pesquisador ajusta o trecho.
    source_query: Mapped[str | None] = mapped_column(db.String(200))
    source_case_sensitive: Mapped[bool | None] = mapped_column(db.Boolean)
    source_start: Mapped[int | None] = mapped_column(db.Integer)
    source_end: Mapped[int | None] = mapped_column(db.Integer)
    source_page_hash: Mapped[str | None] = mapped_column(db.String(64))
    # Preferência da operação que criou o vínculo; não depende do checkbox futuro.
    contextual_rejection_enabled: Mapped[bool] = mapped_column(db.Boolean, default=False, server_default=db.false(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["excerpt_id", "analysis_id"],
                             ["qualitative_excerpts.id", "qualitative_excerpts.analysis_id"],
                             name="fk_qualitative_coding_excerpt_base"),
        ForeignKeyConstraint(["code_id", "analysis_id"],
                             ["qualitative_codes.id", "qualitative_codes.analysis_id"],
                             name="fk_qualitative_coding_code_base"),
        UniqueConstraint("excerpt_id", "code_id", name="uq_qualitative_coding_excerpt_code"),
        CheckConstraint("origin IN ('manual', 'assisted', 'automatic_literal', 'automatic_regex', "
                        "'automatic_lexical', 'automatic_semantic')", name="ck_qualitative_coding_origin"),
        CheckConstraint("origin IN ('manual', 'assisted') OR (source_query IS NOT NULL "
                        "AND length(source_query) > 0 AND source_case_sensitive IS NOT NULL "
                        "AND source_start IS NOT NULL AND source_end IS NOT NULL "
                        "AND source_start >= 0 AND source_end > source_start "
                        "AND source_page_hash IS NOT NULL AND length(source_page_hash) = 64)",
                        name="ck_qualitative_coding_source"),
    )


class QualitativeRejection(db.Model):
    """Decisão local sobre uma ocorrência/consulta; nunca exclusão global de texto."""

    __tablename__ = "qualitative_rejections"
    id: Mapped[str] = mapped_column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    analysis_id: Mapped[str] = mapped_column(db.ForeignKey("analyses.id"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    code_id: Mapped[str] = mapped_column(db.String(36), nullable=False)
    page_number: Mapped[int] = mapped_column(db.Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(db.Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(db.Integer, nullable=False)
    page_text_hash: Mapped[str] = mapped_column(db.String(64), nullable=False)
    origin: Mapped[str] = mapped_column(db.String(32), nullable=False)
    query: Mapped[str] = mapped_column(db.String(200), nullable=False)
    case_sensitive: Mapped[bool] = mapped_column(db.Boolean, nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(db.ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["document_id", "analysis_id"],
                             ["analysis_documents.id", "analysis_documents.analysis_id"],
                             name="fk_qualitative_rejection_document_base"),
        ForeignKeyConstraint(["code_id", "analysis_id"],
                             ["qualitative_codes.id", "qualitative_codes.analysis_id"],
                             name="fk_qualitative_rejection_code_base"),
        UniqueConstraint("analysis_id", "document_id", "page_number", "start_offset", "end_offset",
                         "page_text_hash", "code_id", "origin", "query", "case_sensitive",
                         name="uq_qualitative_rejection_context"),
        CheckConstraint("page_number >= 1 AND start_offset >= 0 AND end_offset > start_offset",
                        name="ck_qualitative_rejection_offsets"),
        CheckConstraint("length(page_text_hash) = 64 AND length(query) > 0",
                        name="ck_qualitative_rejection_source"),
        CheckConstraint("origin IN ('automatic_literal', 'automatic_regex', 'automatic_lexical', "
                        "'automatic_semantic')", name="ck_qualitative_rejection_origin"),
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
