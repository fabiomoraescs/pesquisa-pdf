"""Cria o domínio privado do Diagramador editorial.

Revision ID: a4b5c6d7e8f
Revises: f4a5b6c7d8e9
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a4b5c6d7e8f"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editorial_projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_editorial_project_name"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_editorial_project_status"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_editorial_projects_owner_status", "editorial_projects", ["owner_user_id", "status"])
    op.create_index("ix_editorial_projects_owner_updated", "editorial_projects", ["owner_user_id", "updated_at"])

    op.create_table(
        "editorial_documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("editorial_project_id", sa.String(length=36), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("storage_name", sa.String(length=160), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="uploaded"),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(original_name)) > 0", name="ck_editorial_document_original_name"),
        sa.CheckConstraint("length(trim(storage_name)) > 0", name="ck_editorial_document_storage_name"),
        sa.CheckConstraint("length(trim(storage_key)) > 0", name="ck_editorial_document_storage_key"),
        sa.CheckConstraint("byte_size > 0", name="ck_editorial_document_byte_size"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_editorial_document_sha256"),
        sa.CheckConstraint("status IN ('uploaded', 'unavailable')", name="ck_editorial_document_status"),
        sa.ForeignKeyConstraint(["editorial_project_id"], ["editorial_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "editorial_project_id", name="uq_editorial_document_id_project"),
    )
    op.create_index("ix_editorial_documents_editorial_project_id", "editorial_documents", ["editorial_project_id"])
    op.create_index(
        "ix_editorial_documents_project_uploaded",
        "editorial_documents",
        ["editorial_project_id", "uploaded_at"],
    )

    op.create_table(
        "editorial_layout_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("editorial_project_id", sa.String(length=36), nullable=False),
        sa.Column("source_document_id", sa.String(length=36), nullable=False),
        sa.Column("configuration_json", sa.JSON(), nullable=False),
        sa.Column("configuration_version", sa.String(length=32), nullable=False, server_default="diagramador-v1"),
        sa.Column("engine_version", sa.String(length=64), nullable=False, server_default="diagramador-v5-core"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.String(length=300), nullable=True),
        sa.Column("integrity_json", sa.JSON(), nullable=False),
        sa.Column("result_storage_key", sa.String(length=500), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("result_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed')", name="ck_editorial_run_status"),
        sa.CheckConstraint("length(trim(configuration_version)) > 0", name="ck_editorial_run_configuration_version"),
        sa.CheckConstraint("result_byte_size IS NULL OR result_byte_size > 0", name="ck_editorial_run_result_byte_size"),
        sa.CheckConstraint("result_sha256 IS NULL OR length(result_sha256) = 64", name="ck_editorial_run_result_sha256"),
        sa.ForeignKeyConstraint(["editorial_project_id"], ["editorial_projects.id"], name="fk_editorial_run_project"),
        sa.ForeignKeyConstraint(
            ["source_document_id", "editorial_project_id"],
            ["editorial_documents.id", "editorial_documents.editorial_project_id"],
            name="fk_editorial_run_source_document_project",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_editorial_layout_runs_project_status",
        "editorial_layout_runs",
        ["editorial_project_id", "status"],
    )
    op.create_index(
        "ix_editorial_layout_runs_project_created",
        "editorial_layout_runs",
        ["editorial_project_id", "created_at"],
    )
    op.create_index(
        "ix_editorial_layout_runs_document_created",
        "editorial_layout_runs",
        ["source_document_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("editorial_layout_runs", "editorial_documents", "editorial_projects"):
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                "O downgrade do domínio editorial foi bloqueado porque existem dados persistidos."
            )
    op.drop_table("editorial_layout_runs")
    op.drop_table("editorial_documents")
    op.drop_table("editorial_projects")
