"""Perfil do usuário separado da autenticação.

Revision ID: 7d3f8be319a2
Revises: 5fcbca0145d1
"""

from alembic import op
import sqlalchemy as sa

revision = "7d3f8be319a2"
down_revision = "5fcbca0145d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("education_level", sa.String(length=40), nullable=True),
        sa.Column("formation_area", sa.String(length=160), nullable=True),
        sa.Column("occupation", sa.String(length=160), nullable=True),
        sa.Column("institutional_affiliation", sa.String(length=200), nullable=True),
        sa.Column("gender", sa.String(length=24), nullable=True),
        sa.Column("race_color", sa.String(length=24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("user_profiles")
