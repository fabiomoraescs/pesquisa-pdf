"""Serializa campanhas HTML abertas para cobertura progressiva.

Revision ID: 6f708192a4b5
Revises: 5e6f708192a4

Não altera dados históricos nem os contratos OAI-PMH. O índice parcial cobre
somente execuções pendentes ou em processamento do mesmo periódico.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "6f708192a4b5"
down_revision = "5e6f708192a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_ojs_html_audit_one_open_journal",
        "ojs_html_audit_runs",
        ["journal_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'running')"),
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_ojs_html_audit_one_open_journal", table_name="ojs_html_audit_runs")
