"""Create deterministic insights, findings and evidence for Analytics Instagram.

Revision ID: a8b9c0d1e2f3
Revises: f6a7b8c9d0e1
"""

from alembic import op
import sqlalchemy as sa


revision = "a8b9c0d1e2f3"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_insight_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analytics_project_id", sa.String(length=36), sa.ForeignKey("analytics_projects.id"), nullable=False),
        sa.Column("period_key", sa.String(length=12), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("reference_at", sa.DateTime(timezone=True)),
        sa.Column("engine_version", sa.String(length=80), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("publication_count", sa.BigInteger(), nullable=False),
        sa.Column("comment_count", sa.BigInteger(), nullable=False),
        sa.Column("finding_count", sa.BigInteger(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("period_key IN ('7d', '30d', '90d', 'all')", name="ck_instagram_insight_run_period"),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="ck_instagram_insight_run_period_range",
        ),
        sa.CheckConstraint(
            "publication_count >= 0 AND comment_count >= 0 AND finding_count >= 0",
            name="ck_instagram_insight_run_counts",
        ),
    )
    op.create_index(
        "ix_instagram_insight_runs_project_generated", "instagram_insight_runs",
        ["analytics_project_id", "generated_at"],
    )
    op.create_index(
        "ix_instagram_insight_runs_project_period", "instagram_insight_runs",
        ["analytics_project_id", "period_key", "generated_at"],
    )

    op.create_table(
        "instagram_insight_findings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_insight_run_id", sa.String(length=36), sa.ForeignKey("instagram_insight_runs.id"), nullable=False),
        sa.Column("rule_id", sa.String(length=80), nullable=False),
        sa.Column("rule_version", sa.String(length=80), nullable=False),
        sa.Column("engine_version", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("metric_key", sa.String(length=80)),
        sa.Column("metric_version", sa.String(length=80)),
        sa.Column("group_label", sa.String(length=240), nullable=False),
        sa.Column("comparison_label", sa.String(length=240)),
        sa.Column("group_value", sa.Float()),
        sa.Column("comparison_value", sa.Float()),
        sa.Column("absolute_difference", sa.Float()),
        sa.Column("relative_difference", sa.Float()),
        sa.Column("difference_unit", sa.String(length=32)),
        sa.Column("group_n", sa.Integer(), nullable=False),
        sa.Column("comparison_n", sa.Integer(), nullable=False),
        sa.Column("coverage", sa.Float(), nullable=False),
        sa.Column("support_level", sa.String(length=16), nullable=False),
        sa.Column("support_explanation", sa.String(length=500), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(rule_id)) > 0", name="ck_instagram_insight_finding_rule_id"),
        sa.CheckConstraint("length(trim(rule_version)) > 0", name="ck_instagram_insight_finding_rule_version"),
        sa.CheckConstraint("length(trim(engine_version)) > 0", name="ck_instagram_insight_finding_engine_version"),
        sa.CheckConstraint("length(trim(kind)) > 0", name="ck_instagram_insight_finding_kind"),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_instagram_insight_finding_title"),
        sa.CheckConstraint("length(trim(summary)) > 0", name="ck_instagram_insight_finding_summary"),
        sa.CheckConstraint("group_n >= 0 AND comparison_n >= 0", name="ck_instagram_insight_finding_counts"),
        sa.CheckConstraint("coverage >= 0 AND coverage <= 1", name="ck_instagram_insight_finding_coverage"),
        sa.CheckConstraint(
            "support_level IN ('exploratory', 'moderate', 'consistent')",
            name="ck_instagram_insight_finding_support",
        ),
    )
    op.create_index(
        "ix_instagram_insight_findings_run_kind", "instagram_insight_findings",
        ["instagram_insight_run_id", "kind"],
    )
    op.create_index(
        "ix_instagram_insight_findings_run_rule", "instagram_insight_findings",
        ["instagram_insight_run_id", "rule_id"],
    )

    op.create_table(
        "instagram_insight_evidence",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_insight_finding_id", sa.String(length=36), sa.ForeignKey("instagram_insight_findings.id"), nullable=False),
        sa.Column("source_type", sa.String(length=48), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('media', 'media_observation', 'media_classification', "
            "'comment', 'comment_assessment', 'comment_text_version', 'account_snapshot')",
            name="ck_instagram_insight_evidence_source_type",
        ),
        sa.CheckConstraint("length(trim(source_id)) > 0", name="ck_instagram_insight_evidence_source_id"),
        sa.CheckConstraint("length(trim(role)) > 0", name="ck_instagram_insight_evidence_role"),
        sa.UniqueConstraint(
            "instagram_insight_finding_id", "source_type", "source_id", "role",
            name="uq_instagram_insight_evidence_finding_source_role",
        ),
    )
    op.create_index(
        "ix_instagram_insight_evidence_finding_role", "instagram_insight_evidence",
        ["instagram_insight_finding_id", "role"],
    )
    op.create_index(
        "ix_instagram_insight_evidence_source", "instagram_insight_evidence", ["source_type", "source_id"],
    )


def downgrade():
    op.drop_table("instagram_insight_evidence")
    op.drop_table("instagram_insight_findings")
    op.drop_table("instagram_insight_runs")
