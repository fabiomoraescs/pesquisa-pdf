"""Create follow-up evaluations and learning snapshots for Instagram actions.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
"""

from alembic import op
import sqlalchemy as sa


revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_recommendation_action_evaluations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_recommendation_action_id", sa.String(length=36), sa.ForeignKey("instagram_recommendation_actions.id"), nullable=False),
        sa.Column("analytics_run_id", sa.String(length=36), sa.ForeignKey("analytics_runs.id"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("metric_key", sa.String(length=80)),
        sa.Column("baseline_value", sa.Float()),
        sa.Column("followup_value", sa.Float()),
        sa.Column("absolute_change", sa.Float()),
        sa.Column("relative_change", sa.Float()),
        sa.Column("baseline_sample_size", sa.Integer()),
        sa.Column("baseline_coverage", sa.Float()),
        sa.Column("followup_sample_size", sa.Integer()),
        sa.Column("followup_population_size", sa.Integer()),
        sa.Column("followup_coverage", sa.Float()),
        sa.Column("assessment", sa.String(length=24), nullable=False),
        sa.Column("learning_summary", sa.Text(), nullable=False),
        sa.Column("limitations_json", sa.JSON(), nullable=False),
        sa.Column("evaluation_version", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "assessment IN ('consistent', 'not_consistent', 'inconclusive', 'not_evaluable')",
            name="ck_instagram_action_evaluation_assessment",
        ),
        sa.CheckConstraint("baseline_sample_size IS NULL OR baseline_sample_size >= 0", name="ck_instagram_action_evaluation_baseline_n"),
        sa.CheckConstraint("followup_sample_size IS NULL OR followup_sample_size >= 0", name="ck_instagram_action_evaluation_followup_n"),
        sa.CheckConstraint("followup_population_size IS NULL OR followup_population_size >= 0", name="ck_instagram_action_evaluation_followup_population"),
        sa.CheckConstraint("baseline_coverage IS NULL OR (baseline_coverage >= 0 AND baseline_coverage <= 1)", name="ck_instagram_action_evaluation_baseline_coverage"),
        sa.CheckConstraint("followup_coverage IS NULL OR (followup_coverage >= 0 AND followup_coverage <= 1)", name="ck_instagram_action_evaluation_followup_coverage"),
        sa.CheckConstraint("length(trim(learning_summary)) > 0 AND length(learning_summary) <= 4000", name="ck_instagram_action_evaluation_learning_summary"),
        sa.CheckConstraint("length(trim(evaluation_version)) > 0", name="ck_instagram_action_evaluation_version"),
        sa.UniqueConstraint("instagram_recommendation_action_id", "analytics_run_id", name="uq_instagram_action_evaluation_action_run"),
    )
    op.create_index("ix_instagram_action_evaluations_action_created", "instagram_recommendation_action_evaluations", ["instagram_recommendation_action_id", "created_at"])
    op.create_index("ix_instagram_action_evaluations_run", "instagram_recommendation_action_evaluations", ["analytics_run_id"])


def downgrade():
    op.drop_index("ix_instagram_action_evaluations_run", table_name="instagram_recommendation_action_evaluations")
    op.drop_index("ix_instagram_action_evaluations_action_created", table_name="instagram_recommendation_action_evaluations")
    op.drop_table("instagram_recommendation_action_evaluations")
