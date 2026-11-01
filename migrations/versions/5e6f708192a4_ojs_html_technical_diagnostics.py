"""Adiciona histórico técnico HTML ao domínio OJS SEO.

Revision ID: 5e6f708192a4
Revises: 4e5f60718293

As tabelas preservam as auditorias OAI-PMH e seus snapshots Dublin Core. Cada
análise HTML aponta para a coleta OAI que forneceu sua amostra; HTML bruto e
credenciais nunca são persistidos.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "5e6f708192a4"
down_revision = "4e5f60718293"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ojs_html_audit_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("journal_id", sa.String(length=36), nullable=False),
        sa.Column("source_oai_audit_run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processor_version", sa.String(length=64), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("errors_json", sa.JSON(), nullable=False),
        sa.Column("completion_reason", sa.String(length=80), nullable=True),
        sa.Column("eligible_article_count", sa.Integer(), nullable=False),
        sa.Column("analyzed_page_count", sa.Integer(), nullable=False),
        sa.Column("confirmed_problem_count", sa.Integer(), nullable=False),
        sa.Column("potential_problem_count", sa.Integer(), nullable=False),
        sa.Column("unverified_page_count", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed', 'canceled')",
            name="ck_ojs_html_audit_status",
        ),
        sa.CheckConstraint(
            "eligible_article_count >= 0 AND analyzed_page_count >= 0 AND "
            "confirmed_problem_count >= 0 AND potential_problem_count >= 0 AND unverified_page_count >= 0",
            name="ck_ojs_html_audit_nonnegative_counts",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_ojs_html_audit_attempt_count"),
        sa.CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR started_at <= completed_at",
            name="ck_ojs_html_audit_time_order",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL AND "
            "lease_expires_at IS NOT NULL) OR "
            "(status != 'running' AND claim_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_ojs_html_audit_claim_state",
        ),
        sa.ForeignKeyConstraint(["journal_id"], ["ojs_journals.id"]),
        sa.ForeignKeyConstraint(
            ["source_oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_html_audit_source_oai_journal",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "journal_id", name="uq_ojs_html_audit_id_journal"),
    )
    op.create_index("ix_ojs_html_audit_journal_created", "ojs_html_audit_runs", ["journal_id", "created_at"])
    op.create_index("ix_ojs_html_audit_journal_status", "ojs_html_audit_runs", ["journal_id", "status"])
    op.create_index("ix_ojs_html_audit_lease", "ojs_html_audit_runs", ["status", "lease_expires_at"])
    op.create_index(
        "uq_ojs_html_audit_one_running_journal", "ojs_html_audit_runs", ["journal_id"], unique=True,
        sqlite_where=sa.text("status = 'running'"), postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "ojs_article_html_analyses",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("journal_id", sa.String(length=36), nullable=False),
        sa.Column("article_id", sa.String(length=36), nullable=False),
        sa.Column("source_observation_id", sa.String(length=36), nullable=False),
        sa.Column("html_audit_run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("final_url", sa.String(length=2048), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=200), nullable=True),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("redirect_count", sa.Integer(), nullable=False),
        sa.Column("facts_json", sa.JSON(), nullable=False),
        sa.Column("findings_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("confirmed_problem_count", sa.Integer(), nullable=False),
        sa.Column("potential_problem_count", sa.Integer(), nullable=False),
        sa.Column("unverified_count", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('analyzed', 'not_verified', 'skipped')", name="ck_ojs_html_analysis_status"),
        sa.CheckConstraint(
            "redirect_count >= 0 AND confirmed_problem_count >= 0 AND potential_problem_count >= 0 AND "
            "unverified_count >= 0",
            name="ck_ojs_html_analysis_nonnegative_counts",
        ),
        sa.ForeignKeyConstraint(["journal_id"], ["ojs_journals.id"]),
        sa.ForeignKeyConstraint(["source_observation_id"], ["ojs_article_observations.id"]),
        sa.ForeignKeyConstraint(
            ["article_id", "journal_id"], ["ojs_articles.id", "ojs_articles.journal_id"],
            name="fk_ojs_html_analysis_article_journal",
        ),
        sa.ForeignKeyConstraint(
            ["html_audit_run_id", "journal_id"], ["ojs_html_audit_runs.id", "ojs_html_audit_runs.journal_id"],
            name="fk_ojs_html_analysis_audit_journal",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("html_audit_run_id", "article_id", name="uq_ojs_html_analysis_run_article"),
    )
    op.create_index("ix_ojs_html_analysis_run_status", "ojs_article_html_analyses", ["html_audit_run_id", "status"])
    op.create_index("ix_ojs_html_analysis_journal_article", "ojs_article_html_analyses", ["journal_id", "article_id"])


def downgrade() -> None:
    op.drop_index("ix_ojs_html_analysis_journal_article", table_name="ojs_article_html_analyses")
    op.drop_index("ix_ojs_html_analysis_run_status", table_name="ojs_article_html_analyses")
    op.drop_table("ojs_article_html_analyses")
    op.drop_index("uq_ojs_html_audit_one_running_journal", table_name="ojs_html_audit_runs")
    op.drop_index("ix_ojs_html_audit_lease", table_name="ojs_html_audit_runs")
    op.drop_index("ix_ojs_html_audit_journal_status", table_name="ojs_html_audit_runs")
    op.drop_index("ix_ojs_html_audit_journal_created", table_name="ojs_html_audit_runs")
    op.drop_table("ojs_html_audit_runs")
