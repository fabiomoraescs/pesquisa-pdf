"""Cria o núcleo estrutural SaaS sem migrar usuários, projetos ou grants.

Revision ID: e8f901a2b3c4
Revises: d7e8f901a2b3
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e8f901a2b3c4"
down_revision = "d7e8f901a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("workspace_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("workspace_type IN ('individual', 'team')", name="ck_workspace_type"),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_workspace_status"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workspaces_owner_user_id", "workspaces", ["owner_user_id"])

    op.create_table(
        "workspace_memberships",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_workspace_membership_role"),
        sa.CheckConstraint("status IN ('active', 'ended')", name="ck_workspace_membership_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workspace_memberships_workspace_id", "workspace_memberships", ["workspace_id"])
    op.create_index("ix_workspace_memberships_user_id", "workspace_memberships", ["user_id"])
    op.create_index(
        "uq_workspace_membership_active_user", "workspace_memberships", ["workspace_id", "user_id"],
        unique=True, sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "workspace_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_by_id", sa.String(length=36), nullable=True),
        sa.Column("assigned_by_name_snapshot", sa.String(length=160), nullable=True),
        sa.Column("terms_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'canceled', 'expired')", name="ck_workspace_subscription_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.ForeignKeyConstraint(["assigned_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workspace_subscriptions_workspace_id", "workspace_subscriptions", ["workspace_id"])
    op.create_index("ix_workspace_subscriptions_plan_id", "workspace_subscriptions", ["plan_id"])
    op.create_index("ix_workspace_subscriptions_assigned_by_id", "workspace_subscriptions", ["assigned_by_id"])
    op.create_index(
        "uq_workspace_subscription_open", "workspace_subscriptions", ["workspace_id"], unique=True,
        sqlite_where=sa.text("status = 'active' AND ended_at IS NULL"),
        postgresql_where=sa.text("status = 'active' AND ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_workspace_subscription_open", table_name="workspace_subscriptions")
    op.drop_index("ix_workspace_subscriptions_assigned_by_id", table_name="workspace_subscriptions")
    op.drop_index("ix_workspace_subscriptions_plan_id", table_name="workspace_subscriptions")
    op.drop_index("ix_workspace_subscriptions_workspace_id", table_name="workspace_subscriptions")
    op.drop_table("workspace_subscriptions")
    op.drop_index("uq_workspace_membership_active_user", table_name="workspace_memberships")
    op.drop_index("ix_workspace_memberships_user_id", table_name="workspace_memberships")
    op.drop_index("ix_workspace_memberships_workspace_id", table_name="workspace_memberships")
    op.drop_table("workspace_memberships")
    op.drop_index("ix_workspaces_owner_user_id", table_name="workspaces")
    op.drop_table("workspaces")
