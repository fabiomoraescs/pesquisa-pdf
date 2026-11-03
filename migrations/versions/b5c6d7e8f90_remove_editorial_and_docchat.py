"""Remove definitivamente o domínio Editorial e a preparação DocChat.

Revision ID: b5c6d7e8f90
Revises: a4b5c6d7e8f
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b5c6d7e8f90"
down_revision = "a4b5c6d7e8f"
branch_labels = None
depends_on = None


_EDITORIAL_AUDIT_ACTIONS = (
    "docchat_ai_settings_changed",
    "editorial_project_created",
    "editorial_project_renamed",
    "editorial_project_archived",
    "editorial_project_restored",
    "editorial_project_permanently_deleted",
    "editorial_document_uploaded",
    "editorial_document_permanently_deleted",
    "editorial_layout_run_created",
    "editorial_layout_run_completed",
    "editorial_layout_run_failed",
)


def _assert_no_unrelated_tool_data(bind) -> None:
    count = bind.execute(sa.text(
        "SELECT COUNT(*) FROM analyses WHERE tool_id = 'diagramador'"
    )).scalar_one()
    if count:
        raise RuntimeError(
            "A remoção editorial foi interrompida: existem análises não editoriais "
            "referenciando a ferramenta Diagramador."
        )


def upgrade() -> None:
    """Exclui somente dados e estruturas exclusivos do Editorial autorizado."""

    bind = op.get_bind()
    _assert_no_unrelated_tool_data(bind)

    # Referências das tabelas administrativas devem sair antes do catálogo Tool.
    bind.execute(sa.text(
        "DELETE FROM tool_visibility_settings "
        "WHERE (scope_type = 'group' AND scope_id = 'editorial') "
        "OR (scope_type = 'tool' AND scope_id IN ('chatdoc', 'diagramador'))"
    ))
    bind.execute(sa.text(
        "DELETE FROM user_tool_overrides WHERE tool_id IN ('chatdoc', 'diagramador')"
    ))
    bind.execute(sa.text(
        "DELETE FROM plan_tools WHERE tool_id IN ('chatdoc', 'diagramador')"
    ))
    bind.execute(sa.text(
        "DELETE FROM audit_logs "
        "WHERE action IN (" + ", ".join(f"'{action}'" for action in _EDITORIAL_AUDIT_ACTIONS) + ") "
        "OR target_type IN ('editorial_project', 'editorial_document', 'editorial_layout_run') "
        "OR target_id IN ('group:editorial', 'tool:chatdoc', 'tool:diagramador')"
    ))

    # A ordem filho → pai preserva a integridade mesmo com foreign_keys ativo.
    op.drop_table("editorial_layout_runs")
    op.drop_table("editorial_documents")
    op.drop_table("editorial_projects")

    bind.execute(sa.text("DELETE FROM tools WHERE id IN ('chatdoc', 'diagramador')"))

    # DocChat nunca foi uma ferramenta operacional: estes dois campos eram
    # apenas a preparação administrativa exclusiva do recurso removido.
    with op.batch_alter_table("assistant_ai_settings") as batch_op:
        batch_op.drop_column("docchat_provider")
        batch_op.drop_column("docchat_model")


def downgrade() -> None:
    raise RuntimeError(
        "A remoção do Editorial é irreversível: recriar tabelas não restaura "
        "os projetos, documentos e execuções eliminados autorizadamente."
    )
