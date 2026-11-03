"""Use Facebook Login as the Instagram API connection provider.

Revision ID: b0c9d8e7f6a5
Revises: a9b8c7d6e5f4
"""

from alembic import op


revision = "b0c9d8e7f6a5"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def _replace_provider_constraint(*, old_value: str, new_value: str) -> None:
    """Funciona também em SQLite, onde CHECK requer recriação da tabela."""
    with op.batch_alter_table("instagram_api_connections", recreate="always") as batch:
        batch.drop_constraint("ck_instagram_api_connection_provider", type_="check")
    op.execute(
        "UPDATE instagram_api_connections "
        f"SET provider = '{new_value}' WHERE provider = '{old_value}'"
    )
    with op.batch_alter_table("instagram_api_connections", recreate="always") as batch:
        batch.create_check_constraint(
            "ck_instagram_api_connection_provider", f"provider = '{new_value}'"
        )


def upgrade():
    _replace_provider_constraint(old_value="instagram_login", new_value="facebook_login")


def downgrade():
    _replace_provider_constraint(old_value="facebook_login", new_value="instagram_login")
