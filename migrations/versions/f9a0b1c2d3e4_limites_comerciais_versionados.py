"""Versiona limites comerciais e ajustes por assinatura de workspace.

Revision ID: f9a0b1c2d3e4
Revises: e8f901a2b3c4

Não aplica cotas a integrações, não altera grants e não modifica snapshots de
assinaturas preexistentes. Os valores são apenas a política inicial para novos
contratos e para administração futura.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "f9a0b1c2d3e4"
down_revision = "e8f901a2b3c4"
branch_labels = None
depends_on = None


_INITIAL_LIMITS = {
    "research_quali": {"instagram_accounts": 0, "ojs_journals": 0},
    "research_quanti": {"instagram_accounts": 0, "ojs_journals": 0},
    "research_complete": {"instagram_accounts": 0, "ojs_journals": 0},
    "marketing_instagram": {"instagram_accounts": 1, "ojs_journals": 0},
    "marketing_sites": {"instagram_accounts": 0, "ojs_journals": 1},
    "marketing_complete": {"instagram_accounts": 3, "ojs_journals": 1},
    "integral": {"instagram_accounts": 5, "ojs_journals": 1},
}


def _valid_limit(value: object, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback


def upgrade() -> None:
    op.create_table(
        "plan_limit_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("limits_json", sa.JSON(), nullable=False),
        sa.Column("justification", sa.String(length=500), nullable=False),
        sa.Column("published_by_id", sa.String(length=36), nullable=True),
        sa.Column("published_by_name_snapshot", sa.String(length=160), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_plan_limit_policy_positive_version"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.ForeignKeyConstraint(["published_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "version", name="uq_plan_limit_policy_version"),
    )
    op.create_index("ix_plan_limit_policies_plan_id", "plan_limit_policies", ["plan_id"])
    op.create_index("ix_plan_limit_policies_published_by_id", "plan_limit_policies", ["published_by_id"])

    op.create_table(
        "workspace_subscription_limit_adjustments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_subscription_id", sa.String(length=36), nullable=False),
        sa.Column("resource_key", sa.String(length=64), nullable=False),
        sa.Column("additional_units", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("justification", sa.String(length=500), nullable=False),
        sa.Column("approved_by_id", sa.String(length=36), nullable=True),
        sa.Column("approved_by_name_snapshot", sa.String(length=160), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_id", sa.String(length=36), nullable=True),
        sa.Column("revoked_by_name_snapshot", sa.String(length=160), nullable=True),
        sa.Column("revocation_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resource_key IN ('instagram_accounts', 'ojs_journals')",
            name="ck_workspace_subscription_limit_adjustment_resource",
        ),
        sa.CheckConstraint(
            "additional_units > 0", name="ck_workspace_subscription_limit_adjustment_positive"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_workspace_subscription_limit_adjustment_status"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_subscription_id"], ["workspace_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revoked_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workspace_subscription_limit_adjustments_workspace_subscription_id",
        "workspace_subscription_limit_adjustments", ["workspace_subscription_id"],
    )
    op.create_index(
        "ix_workspace_subscription_limit_adjustments_approved_by_id",
        "workspace_subscription_limit_adjustments", ["approved_by_id"],
    )
    op.create_index(
        "ix_workspace_subscription_limit_adjustments_revoked_by_id",
        "workspace_subscription_limit_adjustments", ["revoked_by_id"],
    )
    op.create_index(
        "ix_workspace_subscription_limit_adjustment_active",
        "workspace_subscription_limit_adjustments",
        ["workspace_subscription_id", "resource_key", "status"],
    )

    bind = op.get_bind()
    plans = sa.table(
        "plans",
        sa.column("id", sa.String),
        sa.column("limits_json", sa.JSON),
    )
    policies = sa.table(
        "plan_limit_policies",
        sa.column("id", sa.String),
        sa.column("plan_id", sa.String),
        sa.column("version", sa.Integer),
        sa.column("limits_json", sa.JSON),
        sa.column("justification", sa.String),
        sa.column("published_by_id", sa.String),
        sa.column("published_by_name_snapshot", sa.String),
        sa.column("published_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    existing = {row.id: row.limits_json for row in bind.execute(sa.select(plans.c.id, plans.c.limits_json))}
    missing = set(_INITIAL_LIMITS) - set(existing)
    if missing:
        raise RuntimeError(
            "A política comercial exige os planos das migrations anteriores; ausentes: "
            + ", ".join(sorted(missing))
        )
    now = datetime.now(timezone.utc)
    for plan_id, defaults in _INITIAL_LIMITS.items():
        raw = existing[plan_id] if isinstance(existing[plan_id], dict) else {}
        merged = dict(raw)
        limits = {
            resource: _valid_limit(raw.get(resource), default)
            for resource, default in defaults.items()
        }
        merged.update(limits)
        bind.execute(plans.update().where(plans.c.id == plan_id).values(limits_json=merged))
        bind.execute(policies.insert().values(
            id=str(uuid4()),
            plan_id=plan_id,
            version=1,
            limits_json=limits,
            justification="Valores iniciais da política comercial.",
            published_by_id=None,
            published_by_name_snapshot="Inicialização da plataforma",
            published_at=now,
            created_at=now,
        ))


def downgrade() -> None:
    """Só descarta o schema quando não há decisão administrativa posterior."""
    bind = op.get_bind()
    adjustments = sa.table("workspace_subscription_limit_adjustments", sa.column("id", sa.String))
    policies = sa.table(
        "plan_limit_policies",
        sa.column("version", sa.Integer),
        sa.column("published_by_id", sa.String),
    )
    adjustment_count = bind.execute(sa.select(sa.func.count()).select_from(adjustments)).scalar_one()
    custom_policy_count = bind.execute(sa.select(sa.func.count()).select_from(policies).where(
        sa.or_(policies.c.version != 1, policies.c.published_by_id.is_not(None))
    )).scalar_one()
    if adjustment_count or custom_policy_count:
        raise RuntimeError(
            "O downgrade de limites foi interrompido: há ajustes ou políticas comerciais publicadas."
        )

    op.drop_index(
        "ix_workspace_subscription_limit_adjustment_active",
        table_name="workspace_subscription_limit_adjustments",
    )
    op.drop_index(
        "ix_workspace_subscription_limit_adjustments_revoked_by_id",
        table_name="workspace_subscription_limit_adjustments",
    )
    op.drop_index(
        "ix_workspace_subscription_limit_adjustments_approved_by_id",
        table_name="workspace_subscription_limit_adjustments",
    )
    op.drop_index(
        "ix_workspace_subscription_limit_adjustments_workspace_subscription_id",
        table_name="workspace_subscription_limit_adjustments",
    )
    op.drop_table("workspace_subscription_limit_adjustments")
    op.drop_index("ix_plan_limit_policies_published_by_id", table_name="plan_limit_policies")
    op.drop_index("ix_plan_limit_policies_plan_id", table_name="plan_limit_policies")
    op.drop_table("plan_limit_policies")
