"""Fundação aditiva da codificação qualitativa manual.

Revision ID: a81d6e3f902b
Revises: c94a67b2d501
"""

from alembic import op
import sqlalchemy as sa


revision = "a81d6e3f902b"
down_revision = "c94a67b2d501"
branch_labels = None
depends_on = None


def upgrade():
    # A chave composta permite provar que um trecho/memo usa documento da
    # própria Base sem reconstruir a tabela legada no SQLite.
    op.create_index("ux_analysis_documents_id_analysis_id", "analysis_documents",
                    ["id", "analysis_id"], unique=True)

    op.create_table(
        "qualitative_codes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("normalized_name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analysis_id", "normalized_name", name="uq_qualitative_code_name_per_base"),
        sa.UniqueConstraint("id", "analysis_id", name="uq_qualitative_code_id_base"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_qualitative_code_name"),
        sa.CheckConstraint("length(normalized_name) > 0", name="ck_qualitative_code_normalized_name"),
    )
    op.create_index("ix_qualitative_codes_analysis_id", "qualitative_codes", ["analysis_id"])

    op.create_table(
        "qualitative_excerpts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("quoted_text", sa.Text(), nullable=False),
        sa.Column("page_text_hash", sa.String(64), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id", "analysis_id"],
                                ["analysis_documents.id", "analysis_documents.analysis_id"],
                                name="fk_qualitative_excerpt_document_base"),
        sa.UniqueConstraint("id", "analysis_id", name="uq_qualitative_excerpt_id_base"),
        sa.CheckConstraint("page_number >= 1", name="ck_qualitative_excerpt_page"),
        sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset",
                           name="ck_qualitative_excerpt_offsets"),
        sa.CheckConstraint("length(quoted_text) > 0", name="ck_qualitative_excerpt_text"),
        sa.CheckConstraint("length(page_text_hash) = 64", name="ck_qualitative_excerpt_hash"),
    )
    op.create_index("ix_qualitative_excerpts_analysis_id", "qualitative_excerpts", ["analysis_id"])
    op.create_index("ix_qualitative_excerpts_document_page", "qualitative_excerpts",
                    ["document_id", "page_number"])

    op.create_table(
        "qualitative_codings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False),
        sa.Column("excerpt_id", sa.String(36), nullable=False),
        sa.Column("code_id", sa.String(36), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["excerpt_id", "analysis_id"],
                                ["qualitative_excerpts.id", "qualitative_excerpts.analysis_id"],
                                name="fk_qualitative_coding_excerpt_base"),
        sa.ForeignKeyConstraint(["code_id", "analysis_id"],
                                ["qualitative_codes.id", "qualitative_codes.analysis_id"],
                                name="fk_qualitative_coding_code_base"),
        sa.UniqueConstraint("excerpt_id", "code_id", name="uq_qualitative_coding_excerpt_code"),
        sa.CheckConstraint("origin IN ('manual', 'assisted')", name="ck_qualitative_coding_origin"),
    )
    op.create_index("ix_qualitative_codings_analysis_id", "qualitative_codings", ["analysis_id"])
    op.create_index("ix_qualitative_codings_code_id", "qualitative_codings", ["code_id"])

    op.create_table(
        "qualitative_memos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("analyses.id"), nullable=False),
        sa.Column("document_id", sa.String(36)),
        sa.Column("code_id", sa.String(36)),
        sa.Column("excerpt_id", sa.String(36)),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id", "analysis_id"],
                                ["analysis_documents.id", "analysis_documents.analysis_id"],
                                name="fk_qualitative_memo_document_base"),
        sa.ForeignKeyConstraint(["code_id", "analysis_id"],
                                ["qualitative_codes.id", "qualitative_codes.analysis_id"],
                                name="fk_qualitative_memo_code_base"),
        sa.ForeignKeyConstraint(["excerpt_id", "analysis_id"],
                                ["qualitative_excerpts.id", "qualitative_excerpts.analysis_id"],
                                name="fk_qualitative_memo_excerpt_base"),
        sa.CheckConstraint(
            "(CASE WHEN document_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN code_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN excerpt_id IS NOT NULL THEN 1 ELSE 0 END) <= 1",
            name="ck_qualitative_memo_single_target",
        ),
        sa.CheckConstraint("length(trim(text)) > 0", name="ck_qualitative_memo_text"),
    )
    op.create_index("ix_qualitative_memos_analysis_id", "qualitative_memos", ["analysis_id"])
    op.create_index("ix_qualitative_memos_document_id", "qualitative_memos", ["document_id"])
    op.create_index("ix_qualitative_memos_code_id", "qualitative_memos", ["code_id"])
    op.create_index("ix_qualitative_memos_excerpt_id", "qualitative_memos", ["excerpt_id"])


def downgrade():
    op.drop_table("qualitative_memos")
    op.drop_table("qualitative_codings")
    op.drop_table("qualitative_excerpts")
    op.drop_table("qualitative_codes")
    op.drop_index("ux_analysis_documents_id_analysis_id", table_name="analysis_documents")
