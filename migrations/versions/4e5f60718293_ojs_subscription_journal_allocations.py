"""Cria reservas duráveis de periódicos OJS por assinatura de workspace.

Revision ID: 4e5f60718293
Revises: 3d4e5f607182

As reservas são deliberadamente independentes dos projetos Analytics e das
regras de AccessGrant. Elas apenas vinculam uma revista já confirmada à
assinatura que consome sua cota comercial. Não há backfill: periódicos OJS
anteriores não recebem uma assinatura presumida.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "4e5f60718293"
down_revision = "3d4e5f607182"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_subscription_ojs_journals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_subscription_id", sa.String(length=36), nullable=False),
        sa.Column("ojs_journal_id", sa.String(length=36), nullable=False),
        sa.Column("canonical_identity_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_endpoint_url", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'released')",
            name="ck_workspace_subscription_ojs_journal_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND released_at IS NULL) OR "
            "(status = 'released' AND released_at IS NOT NULL)",
            name="ck_workspace_subscription_ojs_journal_release_state",
        ),
        sa.CheckConstraint(
            "length(trim(canonical_identity_hash)) = 64",
            name="ck_workspace_subscription_ojs_journal_identity_hash",
        ),
        sa.CheckConstraint(
            "length(trim(canonical_endpoint_url)) > 0",
            name="ck_workspace_subscription_ojs_journal_endpoint",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_subscription_id"], ["workspace_subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["ojs_journal_id"], ["ojs_journals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workspace_subscription_ojs_journal_subscription_status",
        "workspace_subscription_ojs_journals",
        ["workspace_subscription_id", "status"],
    )
    op.create_index(
        "ix_workspace_subscription_ojs_journals_workspace_subscription_id",
        "workspace_subscription_ojs_journals",
        ["workspace_subscription_id"],
    )
    op.create_index(
        "ix_workspace_subscription_ojs_journals_ojs_journal_id",
        "workspace_subscription_ojs_journals",
        ["ojs_journal_id"],
    )
    op.create_index(
        "uq_workspace_subscription_ojs_journal_active_journal",
        "workspace_subscription_ojs_journals",
        ["ojs_journal_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_workspace_subscription_ojs_journal_active_identity",
        "workspace_subscription_ojs_journals",
        ["workspace_subscription_id", "canonical_identity_hash"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_workspace_subscription_ojs_journal_active_identity",
        table_name="workspace_subscription_ojs_journals",
    )
    op.drop_index(
        "uq_workspace_subscription_ojs_journal_active_journal",
        table_name="workspace_subscription_ojs_journals",
    )
    op.drop_index(
        "ix_workspace_subscription_ojs_journals_ojs_journal_id",
        table_name="workspace_subscription_ojs_journals",
    )
    op.drop_index(
        "ix_workspace_subscription_ojs_journals_workspace_subscription_id",
        table_name="workspace_subscription_ojs_journals",
    )
    op.drop_index(
        "ix_workspace_subscription_ojs_journal_subscription_status",
        table_name="workspace_subscription_ojs_journals",
    )
    op.drop_table("workspace_subscription_ojs_journals")
