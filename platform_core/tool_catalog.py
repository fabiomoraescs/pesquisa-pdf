"""Catálogo declarativo de ferramentas e política pura de visibilidade.

O módulo não consulta o banco e não decide autorização.  Ele descreve a
organização estável da plataforma para que a navegação, a administração e,
futuramente, as regras comerciais possam consumir a mesma fonte de verdade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


IMPLEMENTATION_STATUSES = frozenset({"available", "development", "future"})
ANALYTICS_INSTAGRAM_TOOL = "analytics_instagram"
VISIBILITY_SCOPE_GROUP = "group"
VISIBILITY_SCOPE_TOOL = "tool"
VISIBILITY_SCOPE_TYPES = frozenset({VISIBILITY_SCOPE_GROUP, VISIBILITY_SCOPE_TOOL})


@dataclass(frozen=True, slots=True)
class ToolGroupDefinition:
    """Metadados estáveis de um agrupamento de ferramentas na navegação."""

    group_id: str
    name: str
    group_order: int
    icon: str


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Descrição estática de uma ferramenta, sem estado operacional."""

    group_id: str
    tool_id: str
    name: str
    tool_order: int
    implementation_status: str
    tool_code: str | None = None
    route: str | None = None
    endpoint: str | None = None
    authorization_points: tuple[str, ...] = ()
    icon: str | None = None


@dataclass(frozen=True, slots=True)
class VisibleToolGroup:
    """Resultado imutável e pronto para uma futura renderização da sidebar."""

    group: ToolGroupDefinition
    tools: tuple[ToolDefinition, ...]


TOOL_GROUPS: tuple[ToolGroupDefinition, ...] = (
    ToolGroupDefinition("research_quali", "Pesquisa Quali", 0, "qualitativa"),
    ToolGroupDefinition("research_quanti", "Pesquisa Quanti", 1, "quantitativa"),
    ToolGroupDefinition("analytics", "Analytics", 2, "analytics"),
)

TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        "research_quali", "pdf_scraper", "Raspagem por termos", 0, "available",
        tool_code="pdf_scraper", route="/", endpoint="projects.list_free_projects", icon="pdf",
        authorization_points=("app._enforce_platform_access", "projects._require_tool", "analyses._authorized"),
    ),
    ToolDefinition(
        "research_quali", "document_analysis", "Raspagem estruturada", 1, "available",
        tool_code="document_analysis", route="/analise-documental", endpoint="projects.list_projects", icon="lista",
        authorization_points=("app._enforce_platform_access", "projects._require_tool", "analyses._authorized"),
    ),
    ToolDefinition(
        "research_quali", "qualitative_analysis", "Análise Documental", 2, "available",
        tool_code="qualitative_analysis", route="/analise-qualitativa", endpoint="projects.list_qualitative_projects", icon="ficha",
        authorization_points=("app._enforce_platform_access", "projects._require_tool", "qualitative._require_tool"),
    ),
    ToolDefinition(
        "research_quali", "comments_analysis", "Análise de Comentários", 3, "future",
        icon="comentarios",
    ),
    ToolDefinition(
        "analytics", ANALYTICS_INSTAGRAM_TOOL, "Instagram", 0, "available",
        tool_code=ANALYTICS_INSTAGRAM_TOOL, route="/analytics/instagram", endpoint="instagram.home", icon="analytics",
        authorization_points=("app._enforce_platform_access", "instagram._require_tool"),
    ),
    ToolDefinition("analytics", "analytics_seo", "SEO", 1, "future", icon="seo"),
)

# A Pesquisa Quanti ainda não possui uma ferramenta concreta.  A entrada
# existe somente para manter compatibilidade com o catálogo administrativo e
# a navegação atuais; não é um ToolDefinition, não recebe rota nem é semeada.
_LEGACY_GROUP_ONLY_ITEMS = (
    {"id": "quantitative_analysis", "group_id": "research_quanti", "name": "Pesquisa Quanti", "icon": "quantitativa"},
)


def _validate_catalog() -> None:
    group_ids = [group.group_id for group in TOOL_GROUPS]
    group_orders = [group.group_order for group in TOOL_GROUPS]
    tool_ids = [tool.tool_id for tool in TOOL_DEFINITIONS]
    tool_codes = [tool.tool_code for tool in TOOL_DEFINITIONS if tool.tool_code]
    if len(group_ids) != len(set(group_ids)):
        raise RuntimeError("O catálogo contém grupos duplicados.")
    if len(group_orders) != len(set(group_orders)):
        raise RuntimeError("O catálogo contém posições de grupo duplicadas.")
    if len(tool_ids) != len(set(tool_ids)):
        raise RuntimeError("O catálogo contém ferramentas duplicadas.")
    if len(tool_codes) != len(set(tool_codes)):
        raise RuntimeError("O catálogo contém referências Tool duplicadas.")
    valid_groups = set(group_ids)
    group_tool_orders: set[tuple[str, int]] = set()
    for tool in TOOL_DEFINITIONS:
        if tool.group_id not in valid_groups:
            raise RuntimeError(f"Ferramenta com grupo inexistente: {tool.tool_id}")
        if tool.implementation_status not in IMPLEMENTATION_STATUSES:
            raise RuntimeError(f"Estado de implementação inválido: {tool.tool_id}")
        if tool.implementation_status == "available" and (not tool.tool_code or not tool.route or not tool.endpoint):
            raise RuntimeError(f"Ferramenta disponível sem Tool ou rota: {tool.tool_id}")
        if tool.implementation_status != "available" and (tool.tool_code or tool.route or tool.endpoint):
            raise RuntimeError(f"Ferramenta não implementada não pode declarar Tool ou rota: {tool.tool_id}")
        group_tool_order = (tool.group_id, tool.tool_order)
        if group_tool_order in group_tool_orders:
            raise RuntimeError(f"Ferramentas com a mesma posição: {tool.group_id}")
        group_tool_orders.add(group_tool_order)


def groups_in_display_order() -> tuple[ToolGroupDefinition, ...]:
    return tuple(sorted(TOOL_GROUPS, key=lambda group: (group.group_order, group.group_id)))


def tools_in_display_order() -> tuple[ToolDefinition, ...]:
    group_order = {group.group_id: group.group_order for group in TOOL_GROUPS}
    return tuple(sorted(
        TOOL_DEFINITIONS,
        key=lambda tool: (group_order[tool.group_id], tool.tool_order, tool.tool_id),
    ))


def tool_definition(tool_id: str) -> ToolDefinition | None:
    return next((tool for tool in TOOL_DEFINITIONS if tool.tool_id == tool_id), None)


def group_definition(group_id: str) -> ToolGroupDefinition | None:
    return next((group for group in TOOL_GROUPS if group.group_id == group_id), None)


def validate_visibility_target(scope_type: str, scope_id: str) -> None:
    """Valida uma chave persistível sem consultar o banco.

    A configuração de visibilidade só pode referir metadados estáveis do
    catálogo. Estados de implementação, ``Tool.active`` e permissões seguem
    sendo avaliados em suas camadas próprias.
    """
    if scope_type == VISIBILITY_SCOPE_GROUP and group_definition(scope_id) is not None:
        return
    if scope_type == VISIBILITY_SCOPE_TOOL and tool_definition(scope_id) is not None:
        return
    if scope_type not in VISIBILITY_SCOPE_TYPES:
        raise ValueError("Tipo de configuração de visibilidade inválido.")
    raise ValueError("Grupo ou ferramenta de visibilidade inexistente.")


def persistent_tool_seed_rows() -> tuple[tuple[str, str, str], ...]:
    """Linhas de seed dos ``Tool`` realmente implementados, em ordem estável."""
    return tuple(
        (tool.tool_code, tool.name, tool.route)
        for tool in tools_in_display_order()
        if tool.implementation_status == "available"
        and tool.tool_code is not None and tool.route is not None
    )


def legacy_unavailable_tool_entries() -> tuple[dict[str, object], ...]:
    """Adaptador temporário para a UI administrativa ainda não migrada."""
    entries = [dict(item) for item in _LEGACY_GROUP_ONLY_ITEMS]
    entries.extend(
        {
            "id": tool.tool_id,
            "group_id": tool.group_id,
            "name": tool.name,
            "icon": tool.icon,
        }
        for tool in tools_in_display_order()
        if tool.implementation_status != "available"
    )
    return tuple(entries)


def _visibility_value(settings: Mapping[str, bool] | None, identifier: str, *, kind: str) -> bool:
    if settings is None or identifier not in settings:
        return True
    value = settings[identifier]
    if not isinstance(value, bool):
        raise ValueError(f"A visibilidade de {kind} deve ser booleana.")
    return value


def _validate_visibility_keys(settings: Mapping[str, bool] | None, valid_keys: set[str], *, kind: str) -> None:
    if settings is None:
        return
    unknown = set(settings) - valid_keys
    if unknown:
        raise ValueError(f"Configuração de visibilidade para {kind} desconhecido: {sorted(unknown)[0]}")
    for identifier in settings:
        _visibility_value(settings, identifier, kind=kind)


def visible_navigation_groups(
    *,
    group_visibility: Mapping[str, bool] | None = None,
    tool_visibility: Mapping[str, bool] | None = None,
) -> tuple[VisibleToolGroup, ...]:
    """Filtra o catálogo sem mutá-lo e remove grupos sem itens exibíveis.

    O resultado considera apenas a configuração de visibilidade e a situação
    de implementação. Elegibilidade por plano, overrides e ``Tool.active``
    permanece deliberadamente fora desta política.
    """
    _validate_visibility_keys(group_visibility, {group.group_id for group in TOOL_GROUPS}, kind="grupo")
    _validate_visibility_keys(tool_visibility, {tool.tool_id for tool in TOOL_DEFINITIONS}, kind="ferramenta")
    tools_by_group: dict[str, list[ToolDefinition]] = {group.group_id: [] for group in TOOL_GROUPS}
    for tool in tools_in_display_order():
        if tool.implementation_status != "available":
            continue
        if not _visibility_value(tool_visibility, tool.tool_id, kind="ferramenta"):
            continue
        tools_by_group[tool.group_id].append(tool)

    return tuple(
        VisibleToolGroup(group=group, tools=tuple(tools_by_group[group.group_id]))
        for group in groups_in_display_order()
        if _visibility_value(group_visibility, group.group_id, kind="grupo")
        and tools_by_group[group.group_id]
    )


def administrative_catalog_entries(persisted_tools: Iterable[object]) -> list[dict[str, object]]:
    """Projeção compatível com a administração atual, sem consultar o banco."""
    persisted_by_id = {str(tool.id): tool for tool in persisted_tools}
    rows: list[tuple[tuple[int, int, str], dict[str, object]]] = []
    groups = {group.group_id: group for group in TOOL_GROUPS}
    for tool in tools_in_display_order():
        group = groups[tool.group_id]
        persisted = persisted_by_id.pop(tool.tool_code, None) if tool.tool_code else None
        if tool.implementation_status == "available":
            if persisted is None:
                continue
            rows.append(((group.group_order, tool.tool_order, tool.tool_id), {
                "id": str(persisted.id), "group_id": tool.group_id, "name": tool.name,
                "active": bool(persisted.active), "implemented": True, "icon": tool.icon,
            }))
        else:
            rows.append(((group.group_order, tool.tool_order, tool.tool_id), {
                "id": tool.tool_id, "group_id": tool.group_id, "name": tool.name,
                "active": False, "implemented": False, "icon": tool.icon,
            }))
    for item in _LEGACY_GROUP_ONLY_ITEMS:
        group = groups[str(item["group_id"])]
        rows.append(((group.group_order, -1, str(item["id"])), {
            **item, "active": False, "implemented": False,
        }))
    for tool_id, tool in persisted_by_id.items():
        rows.append(((len(TOOL_GROUPS), len(rows), tool_id), {
            "id": tool_id, "group_id": None, "name": str(tool.name),
            "active": bool(tool.active), "implemented": True, "icon": None,
        }))
    return [row for _sort_key, row in sorted(rows, key=lambda item: item[0])]


_validate_catalog()
