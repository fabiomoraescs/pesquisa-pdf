"""Armazena a visibilidade administrativa do catálogo de ferramentas.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
"""

from alembic import op
import sqlalchemy as sa


revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tool_visibility_settings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=80), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("changed_by_id", sa.String(length=36), nullable=True),
        sa.Column("changed_by_name_snapshot", sa.String(length=160), nullable=True),
        sa.CheckConstraint(
            "scope_type IN ('group', 'tool')",
            name="ck_tool_visibility_setting_scope_type",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_id"], ["users.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_type", "scope_id", name="uq_tool_visibility_setting_scope"),
    )
    op.create_index(
        "ix_tool_visibility_settings_changed_by_id",
        "tool_visibility_settings",
        ["changed_by_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_tool_visibility_settings_changed_by_id", table_name="tool_visibility_settings")
    op.drop_table("tool_visibility_settings")
