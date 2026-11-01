"""Registra operações integradas OAI-PMH e HTML do OJS SEO.

Revision ID: 8192a4b5c6d7
Revises: 708192a4b5c6
"""

from alembic import op
import sqlalchemy as sa


revision = "8192a4b5c6d7"
down_revision = "708192a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ojs_seo_analysis_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("journal_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("oai_audit_run_id", sa.String(length=36), nullable=True),
        sa.Column("html_campaign_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("errors_json", sa.JSON(), nullable=False),
        sa.Column("completion_reason", sa.String(length=80), nullable=True),
        sa.ForeignKeyConstraint(["journal_id"], ["ojs_journals.id"]),
        sa.ForeignKeyConstraint(
            ["oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_operation_oai_journal",
        ),
        sa.ForeignKeyConstraint(
            ["html_campaign_id", "journal_id"],
            ["ojs_html_audit_campaigns.id", "ojs_html_audit_campaigns.journal_id"],
            name="fk_ojs_operation_html_campaign_journal",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "journal_id", name="uq_ojs_operation_id_journal"),
        sa.UniqueConstraint("oai_audit_run_id", name="uq_ojs_operation_oai_run"),
        sa.UniqueConstraint("html_campaign_id", name="uq_ojs_operation_html_campaign"),
        sa.CheckConstraint("mode IN ('initial', 'update')", name="ck_ojs_operation_mode"),
        sa.CheckConstraint(
            "status IN ('oai_pending', 'oai_running', 'html_pending', 'html_running', "
            "'completed', 'completed_with_pending', 'interrupted', 'failed', 'canceled')",
            name="ck_ojs_operation_status",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR started_at <= completed_at",
            name="ck_ojs_operation_time_order",
        ),
    )
    op.create_index("ix_ojs_operation_journal_created", "ojs_seo_analysis_operations", ["journal_id", "created_at"])
    op.create_index("ix_ojs_operation_journal_status", "ojs_seo_analysis_operations", ["journal_id", "status"])
    op.create_index(
        "uq_ojs_operation_one_open_journal", "ojs_seo_analysis_operations", ["journal_id"], unique=True,
        sqlite_where=sa.text("status IN ('oai_pending', 'oai_running', 'html_pending', 'html_running', 'interrupted')"),
        postgresql_where=sa.text("status IN ('oai_pending', 'oai_running', 'html_pending', 'html_running', 'interrupted')"),
    )


def downgrade() -> None:
    op.drop_index("uq_ojs_operation_one_open_journal", table_name="ojs_seo_analysis_operations")
    op.drop_index("ix_ojs_operation_journal_status", table_name="ojs_seo_analysis_operations")
    op.drop_index("ix_ojs_operation_journal_created", table_name="ojs_seo_analysis_operations")
    op.drop_table("ojs_seo_analysis_operations")
