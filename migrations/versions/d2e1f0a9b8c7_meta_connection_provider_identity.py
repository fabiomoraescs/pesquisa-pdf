"""Store the official Meta identity separately from manual project identity.

Revision ID: d2e1f0a9b8c7
Revises: c1d0e9f8a7b6
"""

from alembic import op
import sqlalchemy as sa


revision = "d2e1f0a9b8c7"
down_revision = "c1d0e9f8a7b6"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable preserves unusual historical connection rows that were created
    # before a provider identity was recorded. New validated connections always
    # receive an official provider id in the application service.
    with op.batch_alter_table("instagram_api_connections", recreate="always") as batch:
        batch.add_column(sa.Column("provider_account_external_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("provider_username", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column(
            "identity_binding", sa.String(length=24), nullable=False, server_default="exact"
        ))

    op.execute(
        "UPDATE instagram_api_connections "
        "SET provider_account_external_id = ("
        "SELECT external_id FROM instagram_accounts "
        "WHERE instagram_accounts.id = instagram_api_connections.instagram_account_id"
        ") WHERE provider_account_external_id IS NULL"
    )
    op.execute(
        "UPDATE instagram_api_connections "
        "SET provider_username = ("
        "SELECT username FROM instagram_accounts "
        "WHERE instagram_accounts.id = instagram_api_connections.instagram_account_id"
        ") WHERE provider_username IS NULL"
    )
    with op.batch_alter_table("instagram_api_connections", recreate="always") as batch:
        batch.create_unique_constraint(
            "uq_instagram_api_connection_provider_account_external_id",
            ["provider_account_external_id"],
        )
        batch.create_check_constraint(
            "ck_instagram_api_connection_identity_binding",
            "identity_binding IN ('exact', 'reconciled')",
        )


def downgrade():
    with op.batch_alter_table("instagram_api_connections", recreate="always") as batch:
        batch.drop_constraint(
            "uq_instagram_api_connection_provider_account_external_id", type_="unique"
        )
        batch.drop_constraint("ck_instagram_api_connection_identity_binding", type_="check")
        batch.drop_column("identity_binding")
        batch.drop_column("provider_username")
        batch.drop_column("provider_account_external_id")
