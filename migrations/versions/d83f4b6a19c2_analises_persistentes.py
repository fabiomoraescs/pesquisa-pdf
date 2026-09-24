"""Análises e PDFs próprios de cada execução.

Revision ID: d83f4b6a19c2
Revises: f7e2b9c4d601
"""

from alembic import op
import sqlalchemy as sa


revision = "d83f4b6a19c2"
down_revision = "f7e2b9c4d601"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("tool_id", sa.String(40), sa.ForeignKey("tools.id"), nullable=False),
        sa.Column("tool_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("excel_files_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.String(300)),
    )
    op.create_index("ix_analyses_user_id", "analyses", ["user_id"])
    op.create_index("ix_analyses_project_id", "analyses", ["project_id"])
    op.create_table(
        "analysis_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("stored_name", sa.String(255), nullable=False),
        sa.UniqueConstraint("analysis_id", "stored_name"),
    )
    op.create_index("ix_analysis_documents_analysis_id", "analysis_documents", ["analysis_id"])


def downgrade():
    op.drop_index("ix_analysis_documents_analysis_id", table_name="analysis_documents")
    op.drop_table("analysis_documents")
    op.drop_index("ix_analyses_project_id", table_name="analyses")
    op.drop_index("ix_analyses_user_id", table_name="analyses")
    op.drop_table("analyses")
