"""Atualiza apenas a nomenclatura comercial de planos persistidos.

Revision ID: d7e8f901a2b3
Revises: c6d7e8f901a2

Os IDs ``marketing_*`` são contratos técnicos já introduzidos pela migration
anterior. Esta revisão altera exclusivamente nome e descrição exibidos; não
modifica concessões, vínculos de ferramentas, usuários ou planos legados.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d7e8f901a2b3"
down_revision = "c6d7e8f901a2"
branch_labels = None
depends_on = None


_CURRENT_METADATA = {
    "marketing_instagram": (
        "Plano Instagram",
        "Analytics Instagram para comunicação e divulgação científica.",
    ),
    "marketing_sites": (
        "Plano OJS",
        "OJS SEO e OJS Analytics para revistas científicas. Em desenvolvimento.",
    ),
    "marketing_complete": (
        "Plano Gestão Completa",
        "Analytics Instagram disponível; OJS SEO e OJS Analytics previstos para fase futura.",
    ),
}

_PREVIOUS_METADATA = {
    "marketing_instagram": (
        "Plano Instagram",
        "Analytics Instagram para comunicação e divulgação científica.",
    ),
    "marketing_sites": (
        "Plano Sites",
        "SEO e Site Analytics para periódicos científicos, OJS e outros sites. Em desenvolvimento.",
    ),
    "marketing_complete": (
        "Plano Marketing Completo",
        "Analytics Instagram disponível; SEO e Site Analytics previstos para fase futura.",
    ),
}


def _replace_metadata(metadata: dict[str, tuple[str, str]]) -> None:
    bind = op.get_bind()
    plans = sa.table(
        "plans",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
    )
    expected_ids = set(metadata)
    existing_ids = set(bind.execute(
        sa.select(plans.c.id).where(plans.c.id.in_(expected_ids))
    ).scalars())
    missing_ids = expected_ids - existing_ids
    if missing_ids:
        raise RuntimeError(
            "A atualização de nomenclatura comercial exige os planos da revisão "
            f"anterior; ausentes: {', '.join(sorted(missing_ids))}."
        )
    for plan_id, (name, description) in metadata.items():
        bind.execute(
            plans.update().where(plans.c.id == plan_id).values(
                name=name,
                description=description,
            )
        )


def upgrade() -> None:
    _replace_metadata(_CURRENT_METADATA)


def downgrade() -> None:
    _replace_metadata(_PREVIOUS_METADATA)
