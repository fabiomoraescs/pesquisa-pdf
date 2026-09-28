"""Preferência de rejeição por codificação, sem reinterpretar o histórico.

Revision ID: c73a91e24f08
Revises: b62f8a10d947
"""
from alembic import op
import sqlalchemy as sa

revision = "c73a91e24f08"
down_revision = "b62f8a10d947"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("qualitative_codings", sa.Column(
        "contextual_rejection_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Antes desta revisão toda autocodificação excluída gerava rejeição.
    codings = sa.table("qualitative_codings", sa.column("origin", sa.String()),
                       sa.column("contextual_rejection_enabled", sa.Boolean()))
    op.execute(codings.update().where(codings.c.origin.in_([
        "automatic_literal", "automatic_regex", "automatic_lexical", "automatic_semantic"
    ])).values(contextual_rejection_enabled=True))


def downgrade():
    # O código antigo rejeitaria obrigatoriamente ao excluir. Não perder a opção OFF.
    codings = sa.table("qualitative_codings", sa.column("origin", sa.String()),
                       sa.column("contextual_rejection_enabled", sa.Boolean()))
    if op.get_bind().execute(sa.select(sa.func.count()).select_from(codings).where(
        codings.c.origin.in_(["automatic_literal", "automatic_regex", "automatic_lexical", "automatic_semantic"]),
        codings.c.contextual_rejection_enabled.is_(False)
    )).scalar():
        raise RuntimeError("Downgrade recusado: existem autocodificações com rejeição contextual desligada.")
    with op.batch_alter_table("qualitative_codings") as batch:
        batch.drop_column("contextual_rejection_enabled")
