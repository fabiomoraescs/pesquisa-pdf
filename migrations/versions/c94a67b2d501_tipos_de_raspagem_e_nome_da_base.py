"""Tipo do projeto e confirmação do nome das bases existentes.

Revision ID: c94a67b2d501
Revises: d83f4b6a19c2
"""

import re

from alembic import op
import sqlalchemy as sa


revision = "c94a67b2d501"
down_revision = "d83f4b6a19c2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("scrape_type", sa.String(16), nullable=False,
                                        server_default="systematic"))
    op.create_index("ix_projects_scrape_type", "projects", ["scrape_type"])
    op.add_column("analyses", sa.Column("name_confirmed", sa.Boolean(), nullable=False,
                                        server_default=sa.false()))
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, name FROM analyses")).all()
    for identifier, name in rows:
        # A geração anterior usava exatamente este formato. Nomes personalizados
        # permanecem confirmados; os automáticos passam a pedir nome ao reabrir.
        if name and not re.fullmatch(r"Análise \d{2}/\d{2}/\d{4} \d{2}:\d{2}", name):
            connection.execute(sa.text("UPDATE analyses SET name_confirmed = 1 WHERE id = :id"),
                               {"id": identifier})


def downgrade():
    op.drop_column("analyses", "name_confirmed")
    op.drop_index("ix_projects_scrape_type", table_name="projects")
    op.drop_column("projects", "scrape_type")
