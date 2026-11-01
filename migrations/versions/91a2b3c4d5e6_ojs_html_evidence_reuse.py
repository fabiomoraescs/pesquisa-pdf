"""Registra reaproveitamento rastreável de evidências HTML entre referências.

Revision ID: 91a2b3c4d5e6
Revises: 8192a4b5c6d7

Nenhuma evidência HTML ou observação bibliográfica existente é modificada. A
tabela somente vincula uma nova referência OAI a uma análise HTML histórica
que foi validada como compatível pelo serviço de domínio.
"""

from alembic import op
import sqlalchemy as sa


revision = "91a2b3c4d5e6"
down_revision = "8192a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ojs_html_evidence_reuses",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("journal_id", sa.String(length=36), nullable=False),
        sa.Column("target_oai_audit_run_id", sa.String(length=36), nullable=False),
        sa.Column("source_oai_audit_run_id", sa.String(length=36), nullable=False),
        sa.Column("article_id", sa.String(length=36), nullable=False),
        sa.Column("target_observation_id", sa.String(length=36), nullable=False),
        sa.Column("source_observation_id", sa.String(length=36), nullable=False),
        sa.Column("source_html_audit_run_id", sa.String(length=36), nullable=False),
        sa.Column("source_html_analysis_id", sa.String(length=36), nullable=False),
        sa.Column("criteria_version", sa.String(length=64), nullable=False),
        sa.Column("reuse_reason", sa.String(length=80), nullable=False),
        sa.Column("reused_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["journal_id"], ["ojs_journals.id"]),
        sa.ForeignKeyConstraint(
            ["target_oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_html_reuse_target_oai_journal",
        ),
        sa.ForeignKeyConstraint(
            ["source_oai_audit_run_id", "journal_id"],
            ["ojs_seo_audit_runs.id", "ojs_seo_audit_runs.journal_id"],
            name="fk_ojs_html_reuse_source_oai_journal",
        ),
        sa.ForeignKeyConstraint(
            ["article_id", "journal_id"],
            ["ojs_articles.id", "ojs_articles.journal_id"],
            name="fk_ojs_html_reuse_article_journal",
        ),
        sa.ForeignKeyConstraint(["target_observation_id"], ["ojs_article_observations.id"]),
        sa.ForeignKeyConstraint(["source_observation_id"], ["ojs_article_observations.id"]),
        sa.ForeignKeyConstraint(
            ["source_html_audit_run_id", "journal_id"],
            ["ojs_html_audit_runs.id", "ojs_html_audit_runs.journal_id"],
            name="fk_ojs_html_reuse_source_html_run_journal",
        ),
        sa.ForeignKeyConstraint(["source_html_analysis_id"], ["ojs_article_html_analyses.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_oai_audit_run_id", "article_id", name="uq_ojs_html_reuse_target_article",
        ),
        sa.CheckConstraint(
            "length(trim(criteria_version)) > 0", name="ck_ojs_html_reuse_criteria_version",
        ),
        sa.CheckConstraint(
            "length(trim(reuse_reason)) > 0", name="ck_ojs_html_reuse_reason",
        ),
    )
    op.create_index(
        "ix_ojs_html_reuse_journal_target",
        "ojs_html_evidence_reuses",
        ["journal_id", "target_oai_audit_run_id"],
    )
    op.create_index(
        "ix_ojs_html_reuse_source_analysis",
        "ojs_html_evidence_reuses",
        ["source_html_analysis_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ojs_html_reuse_source_analysis", table_name="ojs_html_evidence_reuses")
    op.drop_index("ix_ojs_html_reuse_journal_target", table_name="ojs_html_evidence_reuses")
    op.drop_table("ojs_html_evidence_reuses")
