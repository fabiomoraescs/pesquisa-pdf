"""Create operational actions and tests derived from Instagram recommendations.

Revision ID: c0d1e2f3a4b5
Revises: b9cad1e2f3a4
"""

from alembic import op
import sqlalchemy as sa


revision = "c0d1e2f3a4b5"
down_revision = "b9cad1e2f3a4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_recommendation_actions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analytics_project_id", sa.String(length=36), sa.ForeignKey("analytics_projects.id"), nullable=False),
        sa.Column("instagram_recommendation_id", sa.String(length=36), sa.ForeignKey("instagram_recommendations.id"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action_kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("hypothesis", sa.Text()),
        sa.Column("target_metric", sa.String(length=80)),
        sa.Column("baseline_value", sa.Float()),
        sa.Column("target_value", sa.Float()),
        sa.Column("success_criterion", sa.Text()),
        sa.Column("planned_start_at", sa.DateTime(timezone=True)),
        sa.Column("planned_end_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_summary", sa.Text()),
        sa.Column("observed_result", sa.Float()),
        sa.Column("outcome", sa.String(length=20)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("action_kind IN ('action', 'test')", name="ck_instagram_action_kind"),
        sa.CheckConstraint(
            "status IN ('planned', 'in_progress', 'completed', 'cancelled')",
            name="ck_instagram_action_status",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('supported', 'not_supported', 'inconclusive', 'not_applicable')",
            name="ck_instagram_action_outcome",
        ),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_instagram_action_title"),
        sa.CheckConstraint("length(trim(objective)) > 0", name="ck_instagram_action_objective"),
        sa.CheckConstraint(
            "planned_start_at IS NULL OR planned_end_at IS NULL OR planned_start_at <= planned_end_at",
            name="ck_instagram_action_planned_range",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR status = 'completed'",
            name="ck_instagram_action_completed_at",
        ),
        sa.CheckConstraint(
            "status != 'completed' OR (completed_at IS NOT NULL AND outcome IS NOT NULL "
            "AND result_summary IS NOT NULL AND length(trim(result_summary)) > 0)",
            name="ck_instagram_action_completion_payload",
        ),
        sa.CheckConstraint(
            "result_summary IS NULL OR length(result_summary) <= 4000",
            name="ck_instagram_action_result_summary_length",
        ),
    )
    op.create_index(
        "ix_instagram_actions_project_status_created",
        "instagram_recommendation_actions", ["analytics_project_id", "status", "created_at"],
    )
    op.create_index(
        "ix_instagram_actions_recommendation_created",
        "instagram_recommendation_actions", ["instagram_recommendation_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_instagram_actions_recommendation_created", table_name="instagram_recommendation_actions")
    op.drop_index("ix_instagram_actions_project_status_created", table_name="instagram_recommendation_actions")
    op.drop_table("instagram_recommendation_actions")
