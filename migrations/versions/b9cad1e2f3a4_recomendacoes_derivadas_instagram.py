"""Create deterministic recommendations derived from Instagram findings.

Revision ID: b9cad1e2f3a4
Revises: a8b9c0d1e2f3
"""

from alembic import op
import sqlalchemy as sa


revision = "b9cad1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_recommendation_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analytics_project_id", sa.String(length=36), sa.ForeignKey("analytics_projects.id"), nullable=False),
        sa.Column("instagram_insight_run_id", sa.String(length=36), sa.ForeignKey("instagram_insight_runs.id"), nullable=False),
        sa.Column("engine_version", sa.String(length=80), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("number_of_findings_considered", sa.BigInteger(), nullable=False),
        sa.Column("number_of_recommendations_generated", sa.BigInteger(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "number_of_findings_considered >= 0 AND number_of_recommendations_generated >= 0",
            name="ck_instagram_recommendation_run_counts",
        ),
        sa.UniqueConstraint(
            "instagram_insight_run_id", "engine_version",
            name="uq_instagram_recommendation_run_insight_engine",
        ),
    )
    op.create_index(
        "ix_instagram_recommendation_runs_project_generated",
        "instagram_recommendation_runs", ["analytics_project_id", "generated_at"],
    )
    op.create_index(
        "ix_instagram_recommendation_runs_insight_generated",
        "instagram_recommendation_runs", ["instagram_insight_run_id", "generated_at"],
    )

    op.create_table(
        "instagram_recommendations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_recommendation_run_id", sa.String(length=36), sa.ForeignKey("instagram_recommendation_runs.id"), nullable=False),
        sa.Column("canonical_key", sa.String(length=320), nullable=False),
        sa.Column("rule_id", sa.String(length=80), nullable=False),
        sa.Column("rule_version", sa.String(length=80), nullable=False),
        sa.Column("engine_version", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("recommendation_type", sa.String(length=48), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("support_level", sa.String(length=16), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("limitations_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(canonical_key)) > 0", name="ck_instagram_recommendation_key"),
        sa.CheckConstraint("length(trim(rule_id)) > 0", name="ck_instagram_recommendation_rule_id"),
        sa.CheckConstraint("length(trim(rule_version)) > 0", name="ck_instagram_recommendation_rule_version"),
        sa.CheckConstraint("length(trim(engine_version)) > 0", name="ck_instagram_recommendation_engine_version"),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_instagram_recommendation_title"),
        sa.CheckConstraint("length(trim(summary)) > 0", name="ck_instagram_recommendation_summary"),
        sa.CheckConstraint("priority IN ('low', 'medium', 'high')", name="ck_instagram_recommendation_priority"),
        sa.CheckConstraint(
            "support_level IN ('exploratory', 'moderate', 'consistent')",
            name="ck_instagram_recommendation_support",
        ),
        sa.UniqueConstraint(
            "instagram_recommendation_run_id", "canonical_key",
            name="uq_instagram_recommendation_run_key",
        ),
    )
    op.create_index(
        "ix_instagram_recommendations_run_priority",
        "instagram_recommendations", ["instagram_recommendation_run_id", "priority", "support_level"],
    )
    op.create_index(
        "ix_instagram_recommendations_run_type",
        "instagram_recommendations", ["instagram_recommendation_run_id", "recommendation_type"],
    )

    op.create_table(
        "instagram_recommendation_findings",
        sa.Column("instagram_recommendation_id", sa.String(length=36), sa.ForeignKey("instagram_recommendations.id"), primary_key=True),
        sa.Column("instagram_insight_finding_id", sa.String(length=36), sa.ForeignKey("instagram_insight_findings.id"), primary_key=True),
    )
    op.create_index(
        "ix_instagram_recommendation_findings_finding",
        "instagram_recommendation_findings", ["instagram_insight_finding_id"],
    )


def downgrade():
    op.drop_table("instagram_recommendation_findings")
    op.drop_table("instagram_recommendations")
    op.drop_table("instagram_recommendation_runs")
