"""Create the manual editorial taxonomy for Analytics Instagram.

Revision ID: e5f6a7b8c9d0
Revises: d4e8f0a1b2c3
"""

from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e8f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_taxonomy_dimensions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("analytics_project_id", sa.String(length=36), sa.ForeignKey("analytics_projects.id"), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("selection_mode", sa.String(length=16), nullable=False, server_default="single"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analytics_project_id", "key", name="uq_instagram_taxonomy_dimension_project_key"),
        sa.CheckConstraint("length(trim(key)) > 0", name="ck_instagram_taxonomy_dimension_key"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_instagram_taxonomy_dimension_name"),
        sa.CheckConstraint("selection_mode IN ('single', 'multi')", name="ck_instagram_taxonomy_dimension_mode"),
        sa.CheckConstraint("position >= 0", name="ck_instagram_taxonomy_dimension_position"),
    )
    op.create_index(
        "ix_instagram_taxonomy_dimensions_project_active_position",
        "instagram_taxonomy_dimensions", ["analytics_project_id", "active", "position"],
    )

    op.create_table(
        "instagram_taxonomy_categories",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "instagram_taxonomy_dimension_id", sa.String(length=36),
            sa.ForeignKey("instagram_taxonomy_dimensions.id"), nullable=False,
        ),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "instagram_taxonomy_dimension_id", "key",
            name="uq_instagram_taxonomy_category_dimension_key",
        ),
        sa.CheckConstraint("length(trim(key)) > 0", name="ck_instagram_taxonomy_category_key"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_instagram_taxonomy_category_name"),
        sa.CheckConstraint("position >= 0", name="ck_instagram_taxonomy_category_position"),
    )
    op.create_index(
        "ix_instagram_taxonomy_categories_dimension_active_position",
        "instagram_taxonomy_categories", ["instagram_taxonomy_dimension_id", "active", "position"],
    )

    op.create_table(
        "instagram_media_classifications",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instagram_media_id", sa.String(length=36), sa.ForeignKey("instagram_media.id"), nullable=False),
        sa.Column(
            "instagram_taxonomy_dimension_id", sa.String(length=36),
            sa.ForeignKey("instagram_taxonomy_dimensions.id"), nullable=False,
        ),
        sa.Column(
            "instagram_taxonomy_category_id", sa.String(length=36),
            sa.ForeignKey("instagram_taxonomy_categories.id"), nullable=False,
        ),
        sa.Column("source", sa.String(length=24), nullable=False, server_default="manual"),
        sa.Column("assigned_by_user_id", sa.String(length=36), sa.ForeignKey("users.id")),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("confidence", sa.Float()),
        sa.Column("classifier_version", sa.String(length=64)),
        sa.CheckConstraint("source = 'manual'", name="ck_instagram_media_classification_source"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_instagram_media_classification_confidence",
        ),
    )
    op.create_index(
        "ix_instagram_media_classifications_media_dimension_current",
        "instagram_media_classifications",
        ["instagram_media_id", "instagram_taxonomy_dimension_id", "superseded_at"],
    )
    op.create_index(
        "ix_instagram_media_classifications_dimension_category_current",
        "instagram_media_classifications",
        ["instagram_taxonomy_dimension_id", "instagram_taxonomy_category_id", "superseded_at"],
    )


def downgrade():
    op.drop_table("instagram_media_classifications")
    op.drop_table("instagram_taxonomy_categories")
    op.drop_table("instagram_taxonomy_dimensions")
