"""Persist configuration for the Assistant Análysis providers.

Revision ID: b8c4d3e2f1a0
Revises: a6f2c9e41b07
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "b8c4d3e2f1a0"
down_revision = "a6f2c9e41b07"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "assistant_ai_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("strategy", sa.String(length=24), nullable=False),
        sa.Column("primary_provider", sa.String(length=32), nullable=False),
        sa.Column("enabled_providers", sa.JSON(), nullable=False),
        sa.Column("fallback_order", sa.JSON(), nullable=False),
        sa.Column("gemini_model", sa.String(length=160), nullable=False),
        sa.Column("openai_model", sa.String(length=160), nullable=False),
        sa.Column("anthropic_model", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_assistant_ai_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    settings = sa.table(
        "assistant_ai_settings",
        sa.column("id", sa.Integer()),
        sa.column("strategy", sa.String()),
        sa.column("primary_provider", sa.String()),
        sa.column("enabled_providers", sa.JSON()),
        sa.column("fallback_order", sa.JSON()),
        sa.column("gemini_model", sa.String()),
        sa.column("openai_model", sa.String()),
        sa.column("anthropic_model", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(settings, [{
        "id": 1,
        "strategy": "single",
        "primary_provider": "gemini",
        "enabled_providers": ["gemini"],
        "fallback_order": [],
        "gemini_model": "gemini-3.8-flash",
        "openai_model": "gpt-4.1",
        "anthropic_model": "",
        "created_at": now,
        "updated_at": now,
    }])


def downgrade():
    op.drop_table("assistant_ai_settings")
