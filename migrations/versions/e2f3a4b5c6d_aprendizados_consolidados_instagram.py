"""Persist structured cohorts and consolidated Instagram learnings.

Revision ID: e2f3a4b5c6d
Revises: d1e2f3a4b5c6
"""

from alembic import op
import sqlalchemy as sa


revision = "e2f3a4b5c6d"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("instagram_insight_findings", sa.Column("cohort_json", sa.JSON()))
    op.add_column("instagram_recommendation_actions", sa.Column("cohort_json", sa.JSON()))
    op.add_column("instagram_recommendation_actions", sa.Column("cohort_signature", sa.String(length=64)))
    op.add_column("instagram_recommendation_action_evaluations", sa.Column("cohort_json", sa.JSON()))
    op.add_column("instagram_recommendation_action_evaluations", sa.Column("cohort_signature", sa.String(length=64)))
    op.create_table(
        "instagram_learnings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analytics_project_id", sa.String(length=36), sa.ForeignKey("analytics_projects.id"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("learning_summary", sa.Text(), nullable=False),
        sa.Column("assessment", sa.String(length=24), nullable=False),
        sa.Column("metric_key", sa.String(length=80)),
        sa.Column("cohort_json", sa.JSON()),
        sa.Column("cohort_signature", sa.String(length=64)),
        sa.Column("learning_version", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_instagram_learning_title"),
        sa.CheckConstraint("length(trim(learning_summary)) > 0 AND length(learning_summary) <= 4000", name="ck_instagram_learning_summary"),
        sa.CheckConstraint("assessment IN ('consistent', 'not_consistent', 'mixed', 'inconclusive', 'not_evaluable')", name="ck_instagram_learning_assessment"),
        sa.CheckConstraint("length(trim(learning_version)) > 0", name="ck_instagram_learning_version"),
    )
    op.create_index("ix_instagram_learnings_project_created", "instagram_learnings", ["analytics_project_id", "created_at"])
    op.create_index("ix_instagram_learnings_project_cohort", "instagram_learnings", ["analytics_project_id", "cohort_signature"])
    op.create_table(
        "instagram_learning_evaluations",
        sa.Column("instagram_learning_id", sa.String(length=36), sa.ForeignKey("instagram_learnings.id"), primary_key=True),
        sa.Column("instagram_recommendation_action_evaluation_id", sa.String(length=36), sa.ForeignKey("instagram_recommendation_action_evaluations.id"), primary_key=True),
    )
    op.create_index("ix_instagram_learning_evaluations_evaluation", "instagram_learning_evaluations", ["instagram_recommendation_action_evaluation_id"])


def downgrade():
    op.drop_index("ix_instagram_learning_evaluations_evaluation", table_name="instagram_learning_evaluations")
    op.drop_table("instagram_learning_evaluations")
    op.drop_index("ix_instagram_learnings_project_cohort", table_name="instagram_learnings")
    op.drop_index("ix_instagram_learnings_project_created", table_name="instagram_learnings")
    op.drop_table("instagram_learnings")
    op.drop_column("instagram_recommendation_action_evaluations", "cohort_signature")
    op.drop_column("instagram_recommendation_action_evaluations", "cohort_json")
    op.drop_column("instagram_recommendation_actions", "cohort_signature")
    op.drop_column("instagram_recommendation_actions", "cohort_json")
    op.drop_column("instagram_insight_findings", "cohort_json")
