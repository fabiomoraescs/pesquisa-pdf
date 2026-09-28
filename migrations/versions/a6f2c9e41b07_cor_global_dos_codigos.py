"""Cor global opcional dos códigos qualitativos, sem reconstruir a tabela."""

from alembic import op
import sqlalchemy as sa


revision = "a6f2c9e41b07"
down_revision = "d84b02f35a19"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite gera ALTER TABLE ADD COLUMN simples. Sem batch: codings e rejeições
    # possuem FKs para qualitative_codes e não devem passar por DROP TABLE.
    op.add_column("qualitative_codes", sa.Column("color", sa.String(16), nullable=True))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM qualitative_codes WHERE color IS NOT NULL")).scalar():
        raise RuntimeError("Downgrade recusado: existem cores de códigos que seriam perdidas.")
    if bind.dialect.name == "sqlite":
        if bind.dialect.server_version_info < (3, 35, 0):
            raise RuntimeError("Downgrade requer SQLite >= 3.35 para remover a coluna sem reconstruir a tabela.")
        op.execute("ALTER TABLE qualitative_codes DROP COLUMN color")
    else:
        op.drop_column("qualitative_codes", "color")
