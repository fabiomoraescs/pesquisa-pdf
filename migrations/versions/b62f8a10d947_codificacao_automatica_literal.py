"""Proveniência e rejeições contextuais, preservando codificações existentes.

Revision ID: b62f8a10d947
Revises: a81d6e3f902b
"""
from alembic import op
import sqlalchemy as sa

revision = "b62f8a10d947"
down_revision = "a81d6e3f902b"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("qualitative_codings") as batch:
        batch.alter_column("origin", existing_type=sa.String(16), type_=sa.String(32), existing_nullable=False)
        batch.add_column(sa.Column("source_query", sa.String(200)))
        batch.add_column(sa.Column("source_case_sensitive", sa.Boolean()))
        batch.add_column(sa.Column("source_start", sa.Integer()))
        batch.add_column(sa.Column("source_end", sa.Integer()))
        batch.add_column(sa.Column("source_page_hash", sa.String(64)))
        batch.drop_constraint("ck_qualitative_coding_origin", type_="check")
        batch.create_check_constraint("ck_qualitative_coding_origin",
            "origin IN ('manual', 'assisted', 'automatic_literal', 'automatic_regex', 'automatic_lexical', 'automatic_semantic')")
        batch.create_check_constraint("ck_qualitative_coding_source",
            "origin IN ('manual', 'assisted') OR (source_query IS NOT NULL AND length(source_query) > 0 "
            "AND source_case_sensitive IS NOT NULL AND source_start IS NOT NULL AND source_end IS NOT NULL "
            "AND source_start >= 0 AND source_end > source_start "
            "AND source_page_hash IS NOT NULL AND length(source_page_hash) = 64)")
    op.create_table("qualitative_rejections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("code_id", sa.String(36), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("page_text_hash", sa.String(64), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("query", sa.String(200), nullable=False),
        sa.Column("case_sensitive", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id", "analysis_id"], ["analysis_documents.id", "analysis_documents.analysis_id"],
                                name="fk_qualitative_rejection_document_base"),
        sa.ForeignKeyConstraint(["code_id", "analysis_id"], ["qualitative_codes.id", "qualitative_codes.analysis_id"],
                                name="fk_qualitative_rejection_code_base"),
        sa.UniqueConstraint("analysis_id", "document_id", "page_number", "start_offset", "end_offset",
                            "page_text_hash", "code_id", "origin", "query", "case_sensitive",
                            name="uq_qualitative_rejection_context"),
        sa.CheckConstraint("page_number >= 1 AND start_offset >= 0 AND end_offset > start_offset",
                           name="ck_qualitative_rejection_offsets"),
        sa.CheckConstraint("length(page_text_hash) = 64 AND length(query) > 0", name="ck_qualitative_rejection_source"),
        sa.CheckConstraint("origin IN ('automatic_literal', 'automatic_regex', 'automatic_lexical', 'automatic_semantic')",
                           name="ck_qualitative_rejection_origin"),
    )
    op.create_index("ix_qualitative_rejections_analysis_id", "qualitative_rejections", ["analysis_id"])


def downgrade():
    # Não apagar decisões/proveniência ou reinterpretar automáticas como manuais.
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT count(*) FROM qualitative_rejections")).scalar() or connection.execute(
        sa.text("SELECT count(*) FROM qualitative_codings WHERE origin NOT IN ('manual', 'assisted')")
    ).scalar():
        raise RuntimeError("Downgrade recusado: existem codificações automáticas ou rejeições a preservar.")
    op.drop_table("qualitative_rejections")
    with op.batch_alter_table("qualitative_codings") as batch:
        batch.drop_constraint("ck_qualitative_coding_source", type_="check")
        batch.drop_constraint("ck_qualitative_coding_origin", type_="check")
        for name in ("source_query", "source_case_sensitive", "source_start", "source_end", "source_page_hash"):
            batch.drop_column(name)
        batch.alter_column("origin", existing_type=sa.String(32), type_=sa.String(16), existing_nullable=False)
        batch.create_check_constraint("ck_qualitative_coding_origin", "origin IN ('manual', 'assisted')")
