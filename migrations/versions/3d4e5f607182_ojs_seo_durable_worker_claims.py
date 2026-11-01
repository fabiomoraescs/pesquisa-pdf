"""Adiciona claims duráveis e exclusividade por periódico ao OJS SEO.

Revision ID: 3d4e5f607182
Revises: 2c3d4e5f6071

Uma auditoria ``running`` anterior a esta revisão não possui token ou lease e
portanto não pode ser recuperada com segurança. Ela é marcada como ``failed``
de forma conservadora, sem tocar artigos, observações ou qualquer outro
domínio. A próxima auditoria pendente poderá então ser executada normalmente.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "3d4e5f607182"
down_revision = "2c3d4e5f6071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Primeiro adicionamos colunas sem o check de claim: bases que já tenham
    # uma execução ``running`` legada ainda não possuem token/lease.
    columns = (
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("ojs_seo_audit_runs", recreate="always") as batch:
            for column in columns:
                batch.add_column(column)
    else:
        for column in columns:
            op.add_column("ojs_seo_audit_runs", column)

    # Não é seguro presumir que o processo que criou um running legado ainda
    # esteja ativo. Falhar a execução preserva seu histórico e evita duas
    # coletas concorrentes ao habilitar o worker novo.
    op.execute(
        "UPDATE ojs_seo_audit_runs "
        "SET status = 'failed', "
        "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
        "completion_reason = COALESCE(completion_reason, 'worker_claim_schema_upgrade') "
        "WHERE status = 'running'"
    )

    claim_state = (
        "(status = 'running' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL AND "
        "lease_expires_at IS NOT NULL) OR "
        "(status != 'running' AND claim_token IS NULL AND lease_expires_at IS NULL)"
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("ojs_seo_audit_runs", recreate="always") as batch:
            batch.create_check_constraint("ck_ojs_audit_run_attempt_count", "attempt_count >= 0")
            batch.create_check_constraint("ck_ojs_audit_run_claim_state", claim_state)
    else:
        op.create_check_constraint("ck_ojs_audit_run_attempt_count", "ojs_seo_audit_runs", "attempt_count >= 0")
        op.create_check_constraint("ck_ojs_audit_run_claim_state", "ojs_seo_audit_runs", claim_state)

    op.create_index(
        "ix_ojs_audit_runs_lease",
        "ojs_seo_audit_runs",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "uq_ojs_audit_runs_one_running_journal",
        "ojs_seo_audit_runs",
        ["journal_id"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_ojs_audit_runs_one_running_journal", table_name="ojs_seo_audit_runs")
    op.drop_index("ix_ojs_audit_runs_lease", table_name="ojs_seo_audit_runs")
    columns = ("attempt_count", "lease_expires_at", "claimed_at", "claim_token")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("ojs_seo_audit_runs", recreate="always") as batch:
            batch.drop_constraint("ck_ojs_audit_run_claim_state", type_="check")
            batch.drop_constraint("ck_ojs_audit_run_attempt_count", type_="check")
            for column in columns:
                batch.drop_column(column)
    else:
        op.drop_constraint("ck_ojs_audit_run_claim_state", "ojs_seo_audit_runs", type_="check")
        op.drop_constraint("ck_ojs_audit_run_attempt_count", "ojs_seo_audit_runs", type_="check")
        for column in columns:
            op.drop_column("ojs_seo_audit_runs", column)
