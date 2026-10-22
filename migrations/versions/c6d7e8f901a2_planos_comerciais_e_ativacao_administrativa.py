"""Adiciona o catálogo comercial sem migrar concessões existentes.

Revision ID: c6d7e8f901a2
Revises: b5c6d7e8f90
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c6d7e8f901a2"
down_revision = "b5c6d7e8f90"
branch_labels = None
depends_on = None


_PLANS = (
    ("research_quali", "Plano Quali", "Pesquisa Quali: raspagem por termos, raspagem estruturada e análise documental."),
    ("research_quanti", "Plano Quanti", "Pesquisa Quanti. As ferramentas quantitativas ainda estão em desenvolvimento."),
    ("research_complete", "Plano Pesquisa Completo", "Pesquisa Quali disponível e Pesquisa Quanti prevista para fase futura."),
    ("marketing_instagram", "Plano Instagram", "Analytics Instagram para comunicação e divulgação científica."),
    ("marketing_sites", "Plano Sites", "SEO e Site Analytics para periódicos científicos, OJS e outros sites. Em desenvolvimento."),
    ("marketing_complete", "Plano Marketing Completo", "Analytics Instagram disponível; SEO e Site Analytics previstos para fase futura."),
    ("integral", "Plano Integral", "Pesquisa Quali e Analytics Instagram disponíveis; Pesquisa Quanti, SEO e Site Analytics previstos."),
)

_PLAN_TOOL_IDS = {
    "research_quali": ("pdf_scraper", "document_analysis", "qualitative_analysis"),
    "research_complete": ("pdf_scraper", "document_analysis", "qualitative_analysis"),
    "marketing_instagram": ("analytics_instagram",),
    "marketing_complete": ("analytics_instagram",),
    "integral": ("pdf_scraper", "document_analysis", "qualitative_analysis", "analytics_instagram"),
}


def upgrade() -> None:
    """Insere planos e vínculos existentes sem tocar em contas ou concessões."""
    bind = op.get_bind()
    plans = sa.table(
        "plans",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("active", sa.Boolean),
        sa.column("limits_json", sa.JSON),
    )
    tools = sa.table("tools", sa.column("id", sa.String))
    plan_tools = sa.table(
        "plan_tools",
        sa.column("plan_id", sa.String),
        sa.column("tool_id", sa.String),
    )

    existing_plan_ids = set(bind.execute(sa.select(plans.c.id)).scalars())
    for plan_id, name, description in _PLANS:
        if plan_id not in existing_plan_ids:
            bind.execute(plans.insert().values(
                id=plan_id,
                name=name,
                description=description,
                active=True,
                limits_json={},
            ))

    requested_tool_ids = {
        tool_id for tool_ids in _PLAN_TOOL_IDS.values() for tool_id in tool_ids
    }
    existing_tool_ids = set(bind.execute(
        sa.select(tools.c.id).where(tools.c.id.in_(requested_tool_ids))
    ).scalars())
    existing_links = set(bind.execute(sa.select(plan_tools.c.plan_id, plan_tools.c.tool_id)).all())
    for plan_id, tool_ids in _PLAN_TOOL_IDS.items():
        for tool_id in tool_ids:
            if tool_id in existing_tool_ids and (plan_id, tool_id) not in existing_links:
                bind.execute(plan_tools.insert().values(plan_id=plan_id, tool_id=tool_id))


def downgrade() -> None:
    """Remove apenas o catálogo novo, recusando apagar concessões comerciais."""
    bind = op.get_bind()
    plan_ids = tuple(plan_id for plan_id, _name, _description in _PLANS)
    grants = sa.table("access_grants", sa.column("plan_id", sa.String))
    plan_tools = sa.table(
        "plan_tools",
        sa.column("plan_id", sa.String),
        sa.column("tool_id", sa.String),
    )
    plans = sa.table("plans", sa.column("id", sa.String))

    grant_count = bind.execute(
        sa.select(sa.func.count()).select_from(grants).where(grants.c.plan_id.in_(plan_ids))
    ).scalar_one()
    if grant_count:
        raise RuntimeError(
            "O downgrade comercial foi interrompido: há concessões vinculadas aos novos planos."
        )

    bind.execute(plan_tools.delete().where(plan_tools.c.plan_id.in_(plan_ids)))
    bind.execute(plans.delete().where(plans.c.id.in_(plan_ids)))
