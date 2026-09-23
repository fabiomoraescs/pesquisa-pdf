"""Ciclo de vida de projetos e estados das bibliotecas oficiais.

Revision ID: b49c62e8a113
Revises: 7d3f8be319a2
"""

from alembic import op
import sqlalchemy as sa

revision = "b49c62e8a113"
down_revision = "7d3f8be319a2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE projects SET archived_at = updated_at WHERE status = 'archived'")

    op.add_column("vocabulary_libraries", sa.Column("status", sa.String(16), nullable=False, server_default="published"))
    op.add_column("vocabulary_libraries", sa.Column("version", sa.String(24), nullable=False, server_default="v1"))
    op.add_column("vocabulary_libraries", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE vocabulary_libraries SET status = 'inactive' WHERE active = 0")
    op.execute("UPDATE vocabulary_libraries SET updated_at = created_at")
    op.add_column("project_libraries", sa.Column("source_version", sa.String(24), nullable=False, server_default="v1"))


def downgrade():
    op.drop_column("project_libraries", "source_version")
    op.drop_column("vocabulary_libraries", "updated_at")
    op.drop_column("vocabulary_libraries", "version")
    op.drop_column("vocabulary_libraries", "status")
    op.drop_column("projects", "archived_at")
