"""Allow direct Instagram Login and Facebook Login connections.

Revision ID: c1d0e9f8a7b6
Revises: b0c9d8e7f6a5
"""

from alembic import op


revision = "c1d0e9f8a7b6"
down_revision = "b0c9d8e7f6a5"
branch_labels = None
depends_on = None


def _drop_provider_constraint() -> None:
    with op.batch_alter_table("instagram_api_connections", recreate="always") as batch:
        batch.drop_constraint("ck_instagram_api_connection_provider", type_="check")


def upgrade():
    _drop_provider_constraint()
    with op.batch_alter_table("instagram_api_connections", recreate="always") as batch:
        batch.create_check_constraint(
            "ck_instagram_api_connection_provider",
            "provider IN ('instagram_login', 'facebook_login')",
        )


def downgrade():
    _drop_provider_constraint()
    # A revisão anterior só conhece Facebook Login. Registros são preservados,
    # mas Instagram Login precisa ser representado pelo único provider aceito nela.
    op.execute(
        "UPDATE instagram_api_connections SET provider = 'facebook_login' "
        "WHERE provider = 'instagram_login'"
    )
    with op.batch_alter_table("instagram_api_connections", recreate="always") as batch:
        batch.create_check_constraint(
            "ck_instagram_api_connection_provider", "provider = 'facebook_login'"
        )
