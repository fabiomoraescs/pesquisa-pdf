"""Tokens de recuperação de senha de uso único.

Revision ID: f7e2b9c4d601
Revises: 2a6d1c9e4f70
"""

from alembic import op
import sqlalchemy as sa


revision = "f7e2b9c4d601"
down_revision = "2a6d1c9e4f70"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "password_recovery_tokens",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index("ix_password_recovery_tokens_user_id", "password_recovery_tokens", ["user_id"])


def downgrade():
    op.drop_index("ix_password_recovery_tokens_user_id", table_name="password_recovery_tokens")
    op.drop_table("password_recovery_tokens")
