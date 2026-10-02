"""Add native provider and future DocChat configuration.

Revision ID: c3d9e8a7f2b4
Revises: b8c4d3e2f1a0
"""

from alembic import op
import sqlalchemy as sa


revision = "c3d9e8a7f2b4"
down_revision = "b8c4d3e2f1a0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "assistant_ai_settings",
        sa.Column("analysis_native_model", sa.String(length=160), nullable=False, server_default="analysis-native-v1"),
    )
    op.add_column("assistant_ai_settings", sa.Column("docchat_provider", sa.String(length=32), nullable=True))
    op.add_column("assistant_ai_settings", sa.Column("docchat_model", sa.String(length=160), nullable=True))


def downgrade():
    op.drop_column("assistant_ai_settings", "docchat_model")
    op.drop_column("assistant_ai_settings", "docchat_provider")
    op.drop_column("assistant_ai_settings", "analysis_native_model")
