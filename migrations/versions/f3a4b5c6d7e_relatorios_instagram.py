"""Add reproducible Instagram reports and private artifact metadata.

Revision ID: f3a4b5c6d7e
Revises: e2f3a4b5c6d
"""

from alembic import op
import sqlalchemy as sa


revision = "f3a4b5c6d7e"
down_revision = "e2f3a4b5c6d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_reports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analytics_project_id", sa.String(length=36), sa.ForeignKey("analytics_projects.id"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(length=320), nullable=False),
        sa.Column("report_type", sa.String(length=24), nullable=False),
        sa.Column("analysis_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("baseline_run_id", sa.String(length=36), sa.ForeignKey("analytics_runs.id"), nullable=False),
        sa.Column("final_run_id", sa.String(length=36), sa.ForeignKey("analytics_runs.id"), nullable=False),
        sa.Column("selected_sections_json", sa.JSON(), nullable=False),
        sa.Column("formats_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("generator_version", sa.String(length=80), nullable=False),
        sa.Column("pdf_filename", sa.String(length=255)),
        sa.Column("xlsx_filename", sa.String(length=255)),
        sa.Column("error_message", sa.String(length=500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_instagram_report_title"),
        sa.CheckConstraint("report_type IN ('executive', 'analytical')", name="ck_instagram_report_type"),
        sa.CheckConstraint("analysis_mode IN ('single_period', 'comparative_evolution')", name="ck_instagram_report_analysis_mode"),
        sa.CheckConstraint("status IN ('generating', 'completed', 'failed')", name="ck_instagram_report_status"),
        sa.CheckConstraint("length(trim(generator_version)) > 0", name="ck_instagram_report_generator_version"),
    )
    op.create_index("ix_instagram_reports_project_created", "instagram_reports", ["analytics_project_id", "created_at"])
    op.create_index("ix_instagram_reports_project_status", "instagram_reports", ["analytics_project_id", "status"])


def downgrade():
    op.drop_index("ix_instagram_reports_project_status", table_name="instagram_reports")
    op.drop_index("ix_instagram_reports_project_created", table_name="instagram_reports")
    op.drop_table("instagram_reports")
