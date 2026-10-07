"""Create the historical comments and reception foundation for Analytics Instagram.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""

from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_comments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_media_id", sa.String(length=36), sa.ForeignKey("instagram_media.id"), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("parent_comment_id", sa.String(length=36), sa.ForeignKey("instagram_comments.id")),
        sa.Column("author_username", sa.String(length=100)),
        sa.Column("author_external_id", sa.String(length=128)),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instagram_media_id", "external_id", name="uq_instagram_comment_media_external_id"),
        sa.CheckConstraint("length(trim(external_id)) > 0", name="ck_instagram_comment_external_id"),
        sa.CheckConstraint("length(trim(original_text)) > 0", name="ck_instagram_comment_original_text"),
        sa.CheckConstraint(
            "parent_comment_id IS NULL OR parent_comment_id <> id",
            name="ck_instagram_comment_not_self_parent",
        ),
    )
    op.create_index("ix_instagram_comments_media_published", "instagram_comments", ["instagram_media_id", "published_at"])
    op.create_index("ix_instagram_comments_parent", "instagram_comments", ["parent_comment_id"])

    op.create_table(
        "instagram_comment_text_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_comment_id", sa.String(length=36), sa.ForeignKey("instagram_comments.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_original", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instagram_comment_id", "content_hash", name="uq_instagram_comment_text_version_hash"),
        sa.CheckConstraint("length(trim(text)) > 0", name="ck_instagram_comment_text_version_text"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_instagram_comment_text_version_hash"),
    )
    op.create_index(
        "ix_instagram_comment_text_versions_comment_observed",
        "instagram_comment_text_versions", ["instagram_comment_id", "first_observed_at"],
    )

    op.create_table(
        "instagram_comment_observations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_comment_id", sa.String(length=36), sa.ForeignKey("instagram_comments.id"), nullable=False),
        sa.Column("analytics_run_id", sa.String(length=36), sa.ForeignKey("analytics_runs.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("like_count", sa.BigInteger()),
        sa.Column("replies_count", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analytics_run_id", "instagram_comment_id", name="uq_instagram_comment_observation_run_comment"),
        sa.CheckConstraint(
            "(like_count IS NULL OR like_count >= 0) AND (replies_count IS NULL OR replies_count >= 0)",
            name="ck_instagram_comment_observation_nonnegative",
        ),
    )
    op.create_index(
        "ix_instagram_comment_observations_comment_observed",
        "instagram_comment_observations", ["instagram_comment_id", "observed_at"],
    )

    op.create_table(
        "instagram_comment_assessments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_comment_id", sa.String(length=36), sa.ForeignKey("instagram_comments.id"), nullable=False),
        sa.Column(
            "instagram_comment_text_version_id", sa.String(length=36),
            sa.ForeignKey("instagram_comment_text_versions.id"), nullable=False,
        ),
        sa.Column("supersedes_assessment_id", sa.String(length=36), sa.ForeignKey("instagram_comment_assessments.id")),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("classifier_key", sa.String(length=80)),
        sa.Column("classifier_version", sa.String(length=80)),
        sa.Column("reviewer_user_id", sa.String(length=36), sa.ForeignKey("users.id")),
        sa.Column("sentiment", sa.String(length=16)),
        sa.Column("intensity", sa.String(length=16)),
        sa.Column("intent", sa.String(length=24)),
        sa.Column("topic", sa.String(length=200)),
        sa.Column("topic_normalized", sa.String(length=200)),
        sa.Column("needs_response", sa.Boolean()),
        sa.Column("risk", sa.String(length=16)),
        sa.Column("confidence", sa.Float()),
        sa.Column("rationale", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "source IN ('manual', 'automatic', 'human_review', 'imported')",
            name="ck_instagram_comment_assessment_source",
        ),
        sa.CheckConstraint(
            "sentiment IS NULL OR sentiment IN ('positive', 'neutral', 'negative', 'mixed')",
            name="ck_instagram_comment_assessment_sentiment",
        ),
        sa.CheckConstraint(
            "intensity IS NULL OR intensity IN ('low', 'medium', 'high')",
            name="ck_instagram_comment_assessment_intensity",
        ),
        sa.CheckConstraint(
            "intent IS NULL OR intent IN ('praise', 'question', 'request', 'complaint', 'criticism', 'information', 'spam', 'other')",
            name="ck_instagram_comment_assessment_intent",
        ),
        sa.CheckConstraint(
            "risk IS NULL OR risk IN ('none', 'low', 'medium', 'high')",
            name="ck_instagram_comment_assessment_risk",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_instagram_comment_assessment_confidence",
        ),
        sa.CheckConstraint(
            "supersedes_assessment_id IS NULL OR supersedes_assessment_id <> id",
            name="ck_instagram_comment_assessment_not_self_supersedes",
        ),
    )
    op.create_index(
        "ix_instagram_comment_assessments_comment_current",
        "instagram_comment_assessments", ["instagram_comment_id", "superseded_at", "created_at"],
    )
    op.create_index(
        "ix_instagram_comment_assessments_filters",
        "instagram_comment_assessments", ["sentiment", "intent", "risk", "needs_response", "superseded_at"],
    )


def downgrade():
    op.drop_table("instagram_comment_assessments")
    op.drop_table("instagram_comment_observations")
    op.drop_table("instagram_comment_text_versions")
    op.drop_table("instagram_comments")
