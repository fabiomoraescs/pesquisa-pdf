"""Adiciona campanhas duráveis para a cobertura completa de páginas HTML.

Revision ID: 708192a4b5c6
Revises: 6f708192a4b5

Cada campanha fixa uma referência OAI-PMH e encadeia lotes HTML já limitados.
Auditorias e evidências existentes permanecem inalteradas; somente lotes
criados por uma campanha nova recebem ``campaign_id``.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "708192a4b5c6"
down_revision = "6f708192a4b5"
branch_labels = None
depends_on = None


def _sqlite_driver_transaction_active(bind) -> bool:
    """Consulta a transação real do sqlite3 antes de trocar a PRAGMA."""
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


def _finish_sqlite_batch_transaction(bind, *, succeeded: bool) -> None:
    """Fecha somente a transação iniciada pela cópia de tabela do SQLite."""
    if _sqlite_driver_transaction_active(bind):
        if succeeded:
            bind.commit()
        else:
            bind.rollback()
    if _sqlite_driver_transaction_active(bind):
        raise RuntimeError("Não foi possível encerrar a transação SQLite do batch.")


def _alter_sqlite_html_audit_runs(*, add_campaign_column: bool) -> None:
    """Recria a tabela sem violar a FK de análises HTML históricas.

    ``ojs_article_html_analyses`` referencia ``ojs_html_audit_runs``. O
    SQLite não permite apagar temporariamente a tabela-pai enquanto a
    fiscalização de FKs está ativa, que é exatamente como o Alembic executa
    ``batch_alter_table``. A troca é limitada à cópia de esquema; a
    integridade é comprovada antes e depois.
    """
    bind = op.get_bind()
    if _sqlite_driver_transaction_active(bind):
        raise RuntimeError("A migration não pode reconstruir auditorias HTML durante uma transação SQLite ativa.")
    if _sqlite_foreign_keys_state(bind) != 1:
        raise RuntimeError("A migration requer chaves estrangeiras SQLite ativadas antes da reconstrução.")
    _assert_sqlite_integrity(bind, stage="antes da reconstrução")

    succeeded = False
    try:
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if _sqlite_driver_transaction_active(bind) or _sqlite_foreign_keys_state(bind) != 0:
            raise RuntimeError("Não foi possível desativar as chaves estrangeiras SQLite com segurança.")
        with op.batch_alter_table("ojs_html_audit_runs", recreate="always") as batch:
            if add_campaign_column:
                batch.add_column(sa.Column("campaign_id", sa.String(length=36), nullable=True))
                batch.create_foreign_key(
                    "fk_ojs_html_audit_campaign_journal",
                    "ojs_html_audit_campaigns",
                    ["campaign_id", "journal_id"],
                    ["id", "journal_id"],
                )
            else:
                batch.drop_constraint("fk_ojs_html_audit_campaign_journal", type_="foreignkey")
                batch.drop_column("campaign_id")
        succeeded = True
    finally:
        _finish_sqlite_batch_transaction(bind, succeeded=succeeded)
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")
        if _sqlite_driver_transaction_active(bind) or _sqlite_foreign_keys_state(bind) != 1:
            raise RuntimeError("Não foi possível restaurar as chaves estrangeiras SQLite com segurança.")
        _assert_sqlite_integrity(bind, stage="após a reconstrução")


def upgrade() -> None:
    op.create_table(
        "ojs_html_audit_campaigns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("journal_id", sa.String(length=36), nullable=False),
        sa.Column("source_oai_audit_run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("no_progress_batch_count", sa.Integer(), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("errors_json", sa.JSON(), nullable=False),
        sa.Column("completion_reason", sa.String(length=80), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'completed_with_pending', 'interrupted', 'failed', 'canceled')",
            name="ck_ojs_html_campaign_status",
        ),
        sa.CheckConstraint(
            "batch_count >= 0 AND no_progress_batch_count >= 0",
            name="ck_ojs_html_campaign_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR started_at <= completed_at",
            name="ck_ojs_html_campaign_time_order",
        ),
        sa.ForeignKeyConstraint(["journal_id"], ["ojs_journals.id"]),
        sa.ForeignKeyConstraint(
            ["source_oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_html_campaign_source_oai_journal",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "journal_id", name="uq_ojs_html_campaign_id_journal"),
    )
    op.create_index(
        "ix_ojs_html_campaign_journal_created", "ojs_html_audit_campaigns", ["journal_id", "created_at"]
    )
    op.create_index(
        "ix_ojs_html_campaign_journal_status", "ojs_html_audit_campaigns", ["journal_id", "status"]
    )
    op.create_index(
        "uq_ojs_html_campaign_active_reference",
        "ojs_html_audit_campaigns",
        ["journal_id", "source_oai_audit_run_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'running', 'interrupted')"),
        postgresql_where=sa.text("status IN ('pending', 'running', 'interrupted')"),
    )

    if op.get_bind().dialect.name == "sqlite":
        _alter_sqlite_html_audit_runs(add_campaign_column=True)
    else:
        op.add_column("ojs_html_audit_runs", sa.Column("campaign_id", sa.String(length=36), nullable=True))
        op.create_foreign_key(
            "fk_ojs_html_audit_campaign_journal",
            "ojs_html_audit_runs",
            "ojs_html_audit_campaigns",
            ["campaign_id", "journal_id"],
            ["id", "journal_id"],
        )
    op.create_index("ix_ojs_html_audit_campaign_status", "ojs_html_audit_runs", ["campaign_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_ojs_html_audit_campaign_status", table_name="ojs_html_audit_runs")
    if op.get_bind().dialect.name == "sqlite":
        _alter_sqlite_html_audit_runs(add_campaign_column=False)
    else:
        op.drop_constraint("fk_ojs_html_audit_campaign_journal", "ojs_html_audit_runs", type_="foreignkey")
        op.drop_column("ojs_html_audit_runs", "campaign_id")
    op.drop_index("uq_ojs_html_campaign_active_reference", table_name="ojs_html_audit_campaigns")
    op.drop_index("ix_ojs_html_campaign_journal_status", table_name="ojs_html_audit_campaigns")
    op.drop_index("ix_ojs_html_campaign_journal_created", table_name="ojs_html_audit_campaigns")
    op.drop_table("ojs_html_audit_campaigns")
