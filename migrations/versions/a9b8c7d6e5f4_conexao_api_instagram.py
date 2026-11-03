"""Add isolated encrypted Instagram API connection metadata.

Revision ID: a9b8c7d6e5f4
Revises: f3a4b5c6d7e
"""

from alembic import op
import sqlalchemy as sa


revision = "a9b8c7d6e5f4"
down_revision = "f3a4b5c6d7e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_api_connections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_account_id", sa.String(length=36), sa.ForeignKey("instagram_accounts.id"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("encrypted_access_token", sa.Text()),
        sa.Column("encrypted_refresh_token", sa.Text()),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("last_validated_at", sa.DateTime(timezone=True)),
        sa.Column("last_capability_test_at", sa.DateTime(timezone=True)),
        sa.Column("api_version", sa.String(length=24), nullable=False),
        sa.Column("capability_result_json", sa.JSON(), nullable=False),
        sa.Column("last_error_category", sa.String(length=48)),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instagram_account_id", name="uq_instagram_api_connection_account"),
        sa.CheckConstraint("provider = 'instagram_login'", name="ck_instagram_api_connection_provider"),
        sa.CheckConstraint(
            "status IN ('connected', 'needs_reauth', 'revoked', 'error')",
            name="ck_instagram_api_connection_status",
        ),
    )
    op.create_index("ix_instagram_api_connections_status", "instagram_api_connections", ["status"])


def downgrade():
    op.drop_index("ix_instagram_api_connections_status", table_name="instagram_api_connections")
    op.drop_table("instagram_api_connections")
