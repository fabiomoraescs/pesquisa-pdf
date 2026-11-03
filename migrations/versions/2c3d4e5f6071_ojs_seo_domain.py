"""Cria o domínio persistente e independente do OJS SEO.

Revision ID: 2c3d4e5f6071
Revises: 1b2c3d4e5f60

As tabelas não alteram os projetos, execuções ou integrações existentes. A
relação de um periódico OJS com AnalyticsProject permanece 1:1, enquanto o
histórico de auditorias e observações pertence exclusivamente a este domínio.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "2c3d4e5f6071"
down_revision = "1b2c3d4e5f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ojs_journals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analytics_project_id", sa.String(length=36), sa.ForeignKey("analytics_projects.id"), nullable=False),
        sa.Column("public_url", sa.String(length=2048), nullable=False),
        sa.Column("oai_endpoint_url", sa.String(length=2048)),
        sa.Column("oai_endpoint_validated_at", sa.DateTime(timezone=True)),
        sa.Column("repository_name", sa.String(length=500)),
        sa.Column("observed_ojs_version", sa.String(length=64)),
        sa.Column("version_evidence", sa.String(length=1000)),
        sa.Column("last_identified_at", sa.DateTime(timezone=True)),
        sa.Column("discovery_status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("last_discovery_error", sa.String(length=300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analytics_project_id", name="uq_ojs_journal_project"),
        sa.CheckConstraint("length(trim(public_url)) > 0", name="ck_ojs_journal_public_url"),
        sa.CheckConstraint(
            "discovery_status IN ('pending', 'identified', 'failed', 'unavailable')",
            name="ck_ojs_journal_discovery_status",
        ),
    )
    op.create_index("ix_ojs_journals_discovery_status", "ojs_journals", ["discovery_status"])

    op.create_table(
        "ojs_seo_audit_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("journal_id", sa.String(length=36), sa.ForeignKey("ojs_journals.id"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("processor_version", sa.String(length=64), nullable=False, server_default="ojs-oai-v1"),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("errors_json", sa.JSON(), nullable=False),
        sa.Column("completion_reason", sa.String(length=80)),
        sa.Column("trace_hash", sa.String(length=64)),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("record_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("deleted_record_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("duplicate_identifier_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("doi_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("response_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("id", "journal_id", name="uq_ojs_audit_run_id_journal"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed', 'canceled')",
            name="ck_ojs_audit_run_status",
        ),
        sa.CheckConstraint(
            "page_count >= 0 AND record_count >= 0 AND deleted_record_count >= 0 AND "
            "duplicate_identifier_count >= 0 AND doi_count >= 0 AND response_bytes >= 0",
            name="ck_ojs_audit_run_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR started_at <= completed_at",
            name="ck_ojs_audit_run_time_order",
        ),
    )
    op.create_index("ix_ojs_audit_runs_journal_created", "ojs_seo_audit_runs", ["journal_id", "created_at"])
    op.create_index("ix_ojs_audit_runs_journal_status", "ojs_seo_audit_runs", ["journal_id", "status"])

    op.create_table(
        "ojs_articles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("journal_id", sa.String(length=36), sa.ForeignKey("ojs_journals.id"), nullable=False),
        sa.Column("oai_identifier", sa.String(length=1024), nullable=False),
        sa.Column("record_status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("title", sa.Text()),
        sa.Column("titles_json", sa.JSON(), nullable=False),
        sa.Column("creators_json", sa.JSON(), nullable=False),
        sa.Column("subjects_json", sa.JSON(), nullable=False),
        sa.Column("descriptions_json", sa.JSON(), nullable=False),
        sa.Column("identifiers_json", sa.JSON(), nullable=False),
        sa.Column("dates_json", sa.JSON(), nullable=False),
        sa.Column("languages_json", sa.JSON(), nullable=False),
        sa.Column("types_json", sa.JSON(), nullable=False),
        sa.Column("doi", sa.String(length=500)),
        sa.Column("dois_json", sa.JSON(), nullable=False),
        sa.Column("oai_datestamp", sa.String(length=64)),
        sa.Column("first_observed_at", sa.DateTime(timezone=True)),
        sa.Column("last_observed_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_hash", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("journal_id", "oai_identifier", name="uq_ojs_article_journal_oai_identifier"),
        sa.UniqueConstraint("id", "journal_id", name="uq_ojs_article_id_journal"),
        sa.CheckConstraint("length(trim(oai_identifier)) > 0", name="ck_ojs_article_oai_identifier"),
        sa.CheckConstraint("record_status IN ('active', 'deleted')", name="ck_ojs_article_status"),
    )
    op.create_index("ix_ojs_articles_journal_status", "ojs_articles", ["journal_id", "record_status"])
    op.create_index("ix_ojs_articles_journal_doi", "ojs_articles", ["journal_id", "doi"])

    op.create_table(
        "ojs_article_observations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("journal_id", sa.String(length=36), sa.ForeignKey("ojs_journals.id"), nullable=False),
        sa.Column("article_id", sa.String(length=36), nullable=False),
        sa.Column("audit_run_id", sa.String(length=36), nullable=False),
        sa.Column("record_status", sa.String(length=16), nullable=False),
        sa.Column("dublin_core_json", sa.JSON(), nullable=False),
        sa.Column("doi_observed", sa.String(length=500)),
        sa.Column("dois_observed_json", sa.JSON(), nullable=False),
        sa.Column("oai_datestamp", sa.String(length=64)),
        sa.Column("metadata_hash", sa.String(length=64)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id", "journal_id"],
            ["ojs_articles.id", "ojs_articles.journal_id"],
            name="fk_ojs_observation_article_journal",
        ),
        sa.ForeignKeyConstraint(
            ["audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_observation_audit_run_journal",
        ),
        sa.UniqueConstraint("article_id", "audit_run_id", name="uq_ojs_observation_article_run"),
        sa.CheckConstraint("record_status IN ('active', 'deleted')", name="ck_ojs_observation_status"),
    )
    op.create_index("ix_ojs_article_observations_run", "ojs_article_observations", ["audit_run_id"])
    op.create_index(
        "ix_ojs_article_observations_article_observed",
        "ojs_article_observations",
        ["article_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_table("ojs_article_observations")
    op.drop_table("ojs_articles")
    op.drop_table("ojs_seo_audit_runs")
    op.drop_table("ojs_journals")
