"""Permite comentários Meta identificados sem texto disponibilizado.

Revision ID: e3f4a5b6c7d8
Revises: d2e1f0a9b8c7
"""

from alembic import op
import sqlalchemy as sa


revision = "e3f4a5b6c7d8"
down_revision = "d2e1f0a9b8c7"
branch_labels = None
depends_on = None


def _sqlite_driver_transaction_active(bind) -> bool:
    """Consulta o estado transacional real do sqlite3, não só o autobegin ORM."""
    driver_connection = getattr(bind.connection, "driver_connection", None)
    state = getattr(driver_connection, "in_transaction", None)
    if not isinstance(state, bool):
        raise RuntimeError("Não foi possível verificar a transação SQLite com segurança.")
    return state


def _sqlite_foreign_keys_state(bind) -> int:
    state = bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    if state not in (0, 1):
        raise RuntimeError("Não foi possível verificar as chaves estrangeiras SQLite com segurança.")
    return state


def _assert_sqlite_integrity(bind, *, stage: str) -> None:
    if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError(f"A integridade referencial SQLite falhou {stage}.")


def _assert_sqlite_fk_preconditions(bind) -> None:
    # A mudança de PRAGMA é ignorada pelo SQLite dentro de uma transação real.
    # Falhar antes de iniciar o batch evita uma cópia parcial com FKs ativas.
    if _sqlite_driver_transaction_active(bind):
        raise RuntimeError("A migration não pode reconstruir comentários durante uma transação SQLite ativa.")
    if _sqlite_foreign_keys_state(bind) != 1:
        raise RuntimeError("A migration requer chaves estrangeiras SQLite ativadas antes da reconstrução.")
    _assert_sqlite_integrity(bind, stage="antes da reconstrução")


def _finish_sqlite_batch_transaction(bind, *, succeeded: bool) -> None:
    """Fecha apenas a transação criada pelo batch antes de restaurar a PRAGMA."""
    if _sqlite_driver_transaction_active(bind):
        if succeeded:
            bind.commit()
        else:
            bind.rollback()
    if _sqlite_driver_transaction_active(bind):
        raise RuntimeError("Não foi possível encerrar a transação SQLite antes de restaurar as chaves estrangeiras.")


def _alter_original_text_nullability(*, nullable: bool, constraint_sql: str) -> None:
    """Recria a tabela SQLite sem violar FKs das versões/avaliações/replies."""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        with op.batch_alter_table("instagram_comments") as batch_op:
            batch_op.drop_constraint("ck_instagram_comment_original_text", type_="check")
            batch_op.alter_column("original_text", existing_type=sa.Text(), nullable=nullable)
            batch_op.create_check_constraint("ck_instagram_comment_original_text", constraint_sql)
        return

    _assert_sqlite_fk_preconditions(bind)
    succeeded = False
    try:
        # batch_alter_table recria instagram_comments. A mudança só é feita
        # após comprovar que a conexão SQLite não possui transação real ativa.
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if _sqlite_driver_transaction_active(bind) or _sqlite_foreign_keys_state(bind) != 0:
            raise RuntimeError("Não foi possível desativar as chaves estrangeiras SQLite com segurança.")
        with op.batch_alter_table("instagram_comments") as batch_op:
            batch_op.drop_constraint("ck_instagram_comment_original_text", type_="check")
            batch_op.alter_column("original_text", existing_type=sa.Text(), nullable=nullable)
            batch_op.create_check_constraint("ck_instagram_comment_original_text", constraint_sql)
        succeeded = True
    finally:
        # O INSERT da cópia SQLite pode abrir uma transação. Ela deve ser
        # finalizada antes do PRAGMA, que seria ignorado se executado nela.
        _finish_sqlite_batch_transaction(bind, succeeded=succeeded)
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")
        if _sqlite_driver_transaction_active(bind) or _sqlite_foreign_keys_state(bind) != 1:
            raise RuntimeError("Não foi possível restaurar as chaves estrangeiras SQLite com segurança.")
        _assert_sqlite_integrity(bind, stage="após a reconstrução")


def upgrade():
    # Uma versão textual continua exigindo conteúdo. Apenas a identidade
    # canônica pode registrar que a API ainda não disponibilizou o texto.
    _alter_original_text_nullability(
        nullable=True,
        constraint_sql="original_text IS NULL OR length(trim(original_text)) > 0",
    )


def downgrade():
    # Não inventa nem descarta texto para tornar a coluna obrigatória outra vez.
    # Em bancos que já tenham registros Meta sem texto, o downgrade deve parar
    # explicitamente até que uma decisão de retenção de dados seja tomada.
    missing_text_count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM instagram_comments WHERE original_text IS NULL"
    )).scalar_one()
    if missing_text_count:
        raise RuntimeError(
            "Não é seguro reverter a migration enquanto houver comentários sem texto disponibilizado."
        )
    _alter_original_text_nullability(
        nullable=False,
        constraint_sql="length(trim(original_text)) > 0",
    )
