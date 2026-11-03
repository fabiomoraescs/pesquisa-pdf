"""Adiciona apresentação minimizada de CPF aos períodos de teste.

Revision ID: 1b2c3d4e5f60
Revises: 0a1b2c3d4e5f

Mantém o HMAC como único identificador de elegibilidade. A coluna opcional
armazena apenas a máscara administrativa dos seis dígitos centrais e preserva
todos os períodos de teste históricos sem tentar recuperar seus CPFs.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "1b2c3d4e5f60"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "commercial_trials",
        sa.Column("cpf_masked", sa.String(length=14), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("commercial_trials", "cpf_masked")
