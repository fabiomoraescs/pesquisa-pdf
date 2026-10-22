"""Catálogo comercial declarativo, separado da navegação e da autorização.

Os grupos deste módulo organizam a oferta de planos.  Eles não são grupos da
sidebar e não substituem ``PlanTool``: este continua sendo a fonte persistida
de autorização para ferramentas operacionais.

Os limites iniciais deste catálogo servem apenas para inicializar políticas
persistidas.  Eles não são cotas operacionais nem uma fonte rígida de decisão:
depois da publicação da política, a fonte de verdade é ``Plan.limits_json`` e
o histórico versionado correspondente.
"""

from __future__ import annotations

from dataclasses import dataclass


COMMERCIAL_AVAILABILITY = frozenset({"available", "partially_available", "development"})
COMMERCIAL_LIMIT_RESOURCE_IDS = frozenset({"instagram_accounts", "ojs_journals"})


@dataclass(frozen=True, slots=True)
class CommercialLimitResourceDefinition:
    """Unidade comercial estável, independente de qualquer fornecedor externo."""

    resource_id: str
    name: str
    description: str


COMMERCIAL_LIMIT_RESOURCES: tuple[CommercialLimitResourceDefinition, ...] = (
    CommercialLimitResourceDefinition(
        "instagram_accounts", "Contas Instagram",
        "Quantidade de contas profissionais distintas conectadas.",
    ),
    CommercialLimitResourceDefinition(
        "ojs_journals", "Revistas OJS",
        "Quantidade de revistas OJS distintas conectadas.",
    ),
)


@dataclass(frozen=True, slots=True)
class CommercialGroupDefinition:
    """Metadados estáveis de uma família comercial de planos."""

    group_id: str
    name: str
    description: str
    display_order: int


@dataclass(frozen=True, slots=True)
class CommercialPlanDefinition:
    """Oferta comercial e suas ferramentas presentes e futuras.

    ``available_tool_ids`` só contém ferramentas já implementadas.  Os IDs em
    ``planned_tool_ids`` não autorizam nada por si mesmos e podem referir
    capacidades ainda não persistidas em ``tools``.
    """

    plan_id: str
    name: str
    description: str
    group_id: str
    display_order: int
    planned_tool_ids: tuple[str, ...]
    available_tool_ids: tuple[str, ...]
    commercial_availability: str
    eligible_for_new_assignment: bool
    initial_limits: tuple[tuple[str, int], ...]
    configurable_limit_resource_ids: tuple[str, ...]


COMMERCIAL_GROUPS: tuple[CommercialGroupDefinition, ...] = (
    CommercialGroupDefinition(
        "research", "Pesquisador",
        "Planos para pesquisa qualitativa e quantitativa.", 0,
    ),
    CommercialGroupDefinition(
        "management", "Gestão",
        "Comunicação, divulgação, SEO e análise de periódicos científicos e outros sites.", 1,
    ),
    CommercialGroupDefinition(
        "research_management", "Pesquisador + Gestão",
        "Plano integrado para pesquisa e gestão.", 2,
    ),
)


COMMERCIAL_PLANS: tuple[CommercialPlanDefinition, ...] = (
    CommercialPlanDefinition(
        "research_quali", "Quali",
        "Pesquisa Quali: raspagem por termos, raspagem estruturada e análise documental.",
        "research", 0,
        ("pdf_scraper", "document_analysis", "qualitative_analysis"),
        ("pdf_scraper", "document_analysis", "qualitative_analysis"),
        "available", True, (("instagram_accounts", 0), ("ojs_journals", 0)), (),
    ),
    CommercialPlanDefinition(
        "research_quanti", "Quanti",
        "Pesquisa Quanti. As ferramentas quantitativas ainda estão em desenvolvimento.",
        "research", 1,
        ("quantitative_analysis",), (),
        "development", True, (("instagram_accounts", 0), ("ojs_journals", 0)), (),
    ),
    CommercialPlanDefinition(
        "research_complete", "Quali + Quanti",
        "Pesquisa Quali disponível e Pesquisa Quanti prevista para fase futura.",
        "research", 2,
        ("pdf_scraper", "document_analysis", "qualitative_analysis", "quantitative_analysis"),
        ("pdf_scraper", "document_analysis", "qualitative_analysis"),
        "partially_available", True, (("instagram_accounts", 0), ("ojs_journals", 0)), (),
    ),
    CommercialPlanDefinition(
        "marketing_instagram", "Instagram",
        "Analytics Instagram para comunicação e divulgação científica.",
        "management", 0,
        ("analytics_instagram",), ("analytics_instagram",),
        "available", True, (("instagram_accounts", 1), ("ojs_journals", 0)), ("instagram_accounts",),
    ),
    CommercialPlanDefinition(
        "marketing_sites", "OJS",
        "OJS SEO e OJS Analytics para revistas científicas. Em desenvolvimento.",
        "management", 1,
        ("ojs_seo", "ojs_analytics"), (),
        "development", True, (("instagram_accounts", 0), ("ojs_journals", 1)), ("ojs_journals",),
    ),
    CommercialPlanDefinition(
        "marketing_complete", "Insta + OJS",
        "Analytics Instagram disponível; OJS SEO e OJS Analytics previstos para fase futura.",
        "management", 2,
        ("analytics_instagram", "ojs_seo", "ojs_analytics"),
        ("analytics_instagram",),
        "partially_available", True, (("instagram_accounts", 3), ("ojs_journals", 1)),
        ("instagram_accounts", "ojs_journals"),
    ),
    CommercialPlanDefinition(
        "integral", "Integral",
        "Pesquisa Quali e Analytics Instagram disponíveis; Pesquisa Quanti, OJS SEO e OJS Analytics previstos.",
        "research_management", 0,
        (
            "pdf_scraper", "document_analysis", "qualitative_analysis", "quantitative_analysis",
            "analytics_instagram", "ojs_seo", "ojs_analytics",
        ),
        ("pdf_scraper", "document_analysis", "qualitative_analysis", "analytics_instagram"),
        "partially_available", True, (("instagram_accounts", 5), ("ojs_journals", 1)),
        ("instagram_accounts", "ojs_journals"),
    ),
)


def commercial_groups_in_display_order() -> tuple[CommercialGroupDefinition, ...]:
    return tuple(sorted(COMMERCIAL_GROUPS, key=lambda item: (item.display_order, item.group_id)))


def commercial_plans_in_display_order() -> tuple[CommercialPlanDefinition, ...]:
    group_order = {group.group_id: group.display_order for group in COMMERCIAL_GROUPS}
    return tuple(sorted(
        COMMERCIAL_PLANS,
        key=lambda item: (group_order[item.group_id], item.display_order, item.plan_id),
    ))


def commercial_plan_definition(plan_id: str) -> CommercialPlanDefinition | None:
    return next((item for item in COMMERCIAL_PLANS if item.plan_id == plan_id), None)


def commercial_plan_display_name(plan_id: object, fallback: object | None = None) -> str:
    """Resolve o rótulo público atual sem alterar IDs ou registros históricos.

    O banco pode conter nomes persistidos de versões anteriores do catálogo.
    As superfícies de apresentação devem sempre usar esta função para os sete
    planos comerciais, mantendo o fallback para registros não comerciais.
    """
    definition = commercial_plan_definition(str(plan_id))
    if definition is not None:
        return definition.name
    return str(fallback) if fallback not in (None, "") else str(plan_id)


def commercial_limit_resource_definition(resource_id: str) -> CommercialLimitResourceDefinition | None:
    return next((item for item in COMMERCIAL_LIMIT_RESOURCES if item.resource_id == resource_id), None)


def commercial_plan_initial_limits(plan_id: str) -> dict[str, int]:
    """Valores de inicialização; mudanças publicadas não voltam a este catálogo."""
    definition = commercial_plan_definition(plan_id)
    if definition is None:
        raise ValueError(f"Plano comercial desconhecido: {plan_id}")
    return dict(definition.initial_limits)


def configurable_limit_resources(plan_id: str) -> tuple[CommercialLimitResourceDefinition, ...]:
    """Recursos que podem ser administrados no Painel para este plano.

    Os limites zero dos planos de pesquisa continuam parte do snapshot
    comercial interno, mas não representam cotas de pesquisa e não são
    expostos como configuração quantitativa.
    """
    definition = commercial_plan_definition(plan_id)
    if definition is None:
        return ()
    enabled = set(definition.configurable_limit_resource_ids)
    return tuple(item for item in COMMERCIAL_LIMIT_RESOURCES if item.resource_id in enabled)


def is_commercial_plan_assignable(plan_id: str) -> bool:
    definition = commercial_plan_definition(plan_id)
    return bool(definition and definition.eligible_for_new_assignment)


def validate_commercial_catalog() -> None:
    """Valida invariantes estáveis do catálogo sem consultar o banco."""
    group_ids = [item.group_id for item in COMMERCIAL_GROUPS]
    plan_ids = [item.plan_id for item in COMMERCIAL_PLANS]
    if len(COMMERCIAL_GROUPS) != 3 or len(COMMERCIAL_PLANS) != 7:
        raise RuntimeError("O catálogo comercial deve conter três grupos e sete planos.")
    if len(group_ids) != len(set(group_ids)) or len(plan_ids) != len(set(plan_ids)):
        raise RuntimeError("O catálogo comercial contém identificadores duplicados.")
    valid_groups = set(group_ids)
    for plan in COMMERCIAL_PLANS:
        if plan.group_id not in valid_groups:
            raise RuntimeError(f"Plano comercial em grupo inexistente: {plan.plan_id}")
        if plan.commercial_availability not in COMMERCIAL_AVAILABILITY:
            raise RuntimeError(f"Disponibilidade comercial inválida: {plan.plan_id}")
        if len(plan.planned_tool_ids) != len(set(plan.planned_tool_ids)):
            raise RuntimeError(f"Ferramentas previstas duplicadas: {plan.plan_id}")
        if not set(plan.available_tool_ids).issubset(plan.planned_tool_ids):
            raise RuntimeError(f"Ferramentas disponíveis fora da oferta: {plan.plan_id}")
        if not plan.available_tool_ids and plan.commercial_availability != "development":
            raise RuntimeError(f"Plano sem ferramenta disponível deve estar em desenvolvimento: {plan.plan_id}")
        if not plan.eligible_for_new_assignment and plan.commercial_availability != "development":
            raise RuntimeError(f"Plano indisponível para atribuição deve estar em desenvolvimento: {plan.plan_id}")
        initial_limits = dict(plan.initial_limits)
        if set(initial_limits) != COMMERCIAL_LIMIT_RESOURCE_IDS:
            raise RuntimeError(f"Limites iniciais incompletos: {plan.plan_id}")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in initial_limits.values()):
            raise RuntimeError(f"Limites iniciais inválidos: {plan.plan_id}")
        configured = plan.configurable_limit_resource_ids
        if len(configured) != len(set(configured)) or not set(configured).issubset(COMMERCIAL_LIMIT_RESOURCE_IDS):
            raise RuntimeError(f"Recursos configuráveis inválidos: {plan.plan_id}")


validate_commercial_catalog()
