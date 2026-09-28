"""Proprietário opcional de bibliotecas; registros anteriores permanecem globais."""
from contextlib import nullcontext

from alembic import op
import sqlalchemy as sa

revision = "d84b02f35a19"
down_revision = "c73a91e24f08"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        # ADD COLUMN aceita REFERENCES inline quando nullable/default NULL, mesmo
        # com foreign_keys=ON. add_column(ForeignKey) gera uma segunda operação
        # ADD CONSTRAINT no Alembic; batch recriaria uma tabela já referenciada.
        # O savepoint torna coluna + índice atômicos também no sqlite3 em modo
        # transacional legado. Não apagar possíveis resíduos de batches antigos.
        transaction = nullcontext() if op.get_context().as_sql else op.get_bind().begin_nested()
        with transaction:
            op.execute(
                "ALTER TABLE vocabulary_libraries ADD COLUMN owner_user_id VARCHAR(36) "
                "CONSTRAINT fk_vocabulary_libraries_owner REFERENCES users (id)"
            )
            op.create_index("ix_vocabulary_libraries_owner_user_id", "vocabulary_libraries", ["owner_user_id"])
    else:
        op.add_column("vocabulary_libraries", sa.Column(
            "owner_user_id", sa.String(36),
            sa.ForeignKey("users.id", name="fk_vocabulary_libraries_owner"), nullable=True))
        op.create_index("ix_vocabulary_libraries_owner_user_id", "vocabulary_libraries", ["owner_user_id"])


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM vocabulary_libraries WHERE owner_user_id IS NOT NULL"
    )).scalar():
        raise RuntimeError("Downgrade recusado: tornaria bibliotecas privadas globais.")
    if op.get_bind().dialect.name == "sqlite":
        # DROP COLUMN nativo existe desde 3.35; versões antigas devem recusar
        # antes de mudar o schema, nunca reconstruir a tabela com FKs ativas.
        if op.get_bind().dialect.server_version_info < (3, 35, 0):
            raise RuntimeError("Downgrade requer SQLite >= 3.35 para remover a coluna sem reconstruir a tabela.")
        with op.get_bind().begin_nested():
            op.drop_index("ix_vocabulary_libraries_owner_user_id", table_name="vocabulary_libraries")
            # A FK inline pertence à própria coluna e é removida junto dela.
            op.execute("ALTER TABLE vocabulary_libraries DROP COLUMN owner_user_id")
    else:
        op.drop_index("ix_vocabulary_libraries_owner_user_id", table_name="vocabulary_libraries")
        op.drop_constraint("fk_vocabulary_libraries_owner", "vocabulary_libraries", type_="foreignkey")
        op.drop_column("vocabulary_libraries", "owner_user_id")
