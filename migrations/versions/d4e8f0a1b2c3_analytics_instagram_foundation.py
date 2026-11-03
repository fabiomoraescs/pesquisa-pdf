"""Create the Analytics Instagram persistence foundation.

Revision ID: d4e8f0a1b2c3
Revises: c3d9e8a7f2b4
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e8f0a1b2c3"
down_revision = "c3d9e8a7f2b4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analytics_projects",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("module_key", sa.String(length=40), nullable=False, server_default="instagram"),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_analytics_project_name"),
    )
    op.create_index(
        "ix_analytics_projects_owner_module_status", "analytics_projects",
        ["owner_user_id", "module_key", "status"],
    )
    op.create_index(
        "ix_analytics_projects_owner_updated", "analytics_projects", ["owner_user_id", "updated_at"],
    )

    op.create_table(
        "analytics_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analytics_project_id", sa.String(length=36), sa.ForeignKey("analytics_projects.id"), nullable=False),
        sa.Column("source_kind", sa.String(length=24), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("processor_version", sa.String(length=64), nullable=False, server_default="v1"),
        sa.Column("source_hash", sa.String(length=64)),
        sa.Column("parameters_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("record_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.String(length=300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("record_count >= 0", name="ck_analytics_run_record_count"),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="ck_analytics_run_period",
        ),
    )
    op.create_index("ix_analytics_runs_project_started", "analytics_runs", ["analytics_project_id", "started_at"])
    op.create_index("ix_analytics_runs_project_status", "analytics_runs", ["analytics_project_id", "status"])
    op.create_index(
        "ix_analytics_runs_project_period", "analytics_runs",
        ["analytics_project_id", "period_start", "period_end"],
    )

    op.create_table(
        "instagram_accounts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analytics_project_id", sa.String(length=36), sa.ForeignKey("analytics_projects.id"), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("username_normalized", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.String(length=128)),
        sa.Column("display_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("account_type", sa.String(length=40)),
        sa.Column("connection_status", sa.String(length=24), nullable=False, server_default="not_connected"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analytics_project_id", name="uq_instagram_account_project"),
        sa.UniqueConstraint("external_id", name="uq_instagram_account_external_id"),
        sa.CheckConstraint("length(trim(username)) > 0", name="ck_instagram_account_username"),
        sa.CheckConstraint("length(trim(username_normalized)) > 0", name="ck_instagram_account_username_normalized"),
    )

    op.create_table(
        "instagram_media",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_account_id", sa.String(length=36), sa.ForeignKey("instagram_accounts.id"), nullable=False),
        sa.Column("external_id", sa.String(length=128)),
        sa.Column("source_key", sa.String(length=200)),
        sa.Column("permalink", sa.String(length=512)),
        sa.Column("caption_original", sa.Text(), nullable=False, server_default=""),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("media_type_raw", sa.String(length=40)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instagram_account_id", "external_id", name="uq_instagram_media_account_external_id"),
        sa.UniqueConstraint("instagram_account_id", "source_key", name="uq_instagram_media_account_source_key"),
        sa.CheckConstraint(
            "external_id IS NOT NULL OR source_key IS NOT NULL",
            name="ck_instagram_media_identity",
        ),
    )
    op.create_index(
        "ix_instagram_media_account_published", "instagram_media", ["instagram_account_id", "published_at"],
    )

    op.create_table(
        "instagram_media_observations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_media_id", sa.String(length=36), sa.ForeignKey("instagram_media.id"), nullable=False),
        sa.Column("analytics_run_id", sa.String(length=36), sa.ForeignKey("analytics_runs.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("reach", sa.BigInteger()),
        sa.Column("impressions", sa.BigInteger()),
        sa.Column("plays", sa.BigInteger()),
        sa.Column("likes", sa.BigInteger()),
        sa.Column("comments_count", sa.BigInteger()),
        sa.Column("shares", sa.BigInteger()),
        sa.Column("saves", sa.BigInteger()),
        sa.Column("interactions", sa.BigInteger()),
        sa.Column("follows_generated", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analytics_run_id", "instagram_media_id", name="uq_instagram_media_observation_run_media"),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="ck_instagram_media_observation_period",
        ),
        sa.CheckConstraint(
            "(reach IS NULL OR reach >= 0) AND "
            "(impressions IS NULL OR impressions >= 0) AND "
            "(plays IS NULL OR plays >= 0) AND "
            "(likes IS NULL OR likes >= 0) AND "
            "(comments_count IS NULL OR comments_count >= 0) AND "
            "(shares IS NULL OR shares >= 0) AND "
            "(saves IS NULL OR saves >= 0) AND "
            "(interactions IS NULL OR interactions >= 0) AND "
            "(follows_generated IS NULL OR follows_generated >= 0)",
            name="ck_instagram_media_observation_nonnegative",
        ),
    )
    op.create_index(
        "ix_instagram_media_observations_media_observed", "instagram_media_observations",
        ["instagram_media_id", "observed_at"],
    )

    op.create_table(
        "instagram_account_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_account_id", sa.String(length=36), sa.ForeignKey("instagram_accounts.id"), nullable=False),
        sa.Column("analytics_run_id", sa.String(length=36), sa.ForeignKey("analytics_runs.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("followers_count", sa.BigInteger()),
        sa.Column("new_followers", sa.BigInteger()),
        sa.Column("account_reach", sa.BigInteger()),
        sa.Column("profile_views", sa.BigInteger()),
        sa.Column("website_clicks", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analytics_run_id", "instagram_account_id", name="uq_instagram_account_snapshot_run_account"),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="ck_instagram_account_snapshot_period",
        ),
        sa.CheckConstraint(
            "(followers_count IS NULL OR followers_count >= 0) AND "
            "(new_followers IS NULL OR new_followers >= 0) AND "
            "(account_reach IS NULL OR account_reach >= 0) AND "
            "(profile_views IS NULL OR profile_views >= 0) AND "
            "(website_clicks IS NULL OR website_clicks >= 0)",
            name="ck_instagram_account_snapshot_nonnegative",
        ),
    )
    op.create_index(
        "ix_instagram_account_snapshots_account_observed", "instagram_account_snapshots",
        ["instagram_account_id", "observed_at"],
    )


def downgrade():
    op.drop_table("instagram_account_snapshots")
    op.drop_table("instagram_media_observations")
    op.drop_table("instagram_media")
    op.drop_table("instagram_accounts")
    op.drop_table("analytics_runs")
    op.drop_table("analytics_projects")
