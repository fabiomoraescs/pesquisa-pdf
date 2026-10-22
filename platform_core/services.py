"""Regras de acesso e seeds administrativos, independentes dos analisadores."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from flask import abort, current_app
from sqlalchemy import func, select

from historico_racial.vocabulary import _contagens, _seed, hash_vocabulario

from .commercial_catalog import COMMERCIAL_PLANS, commercial_plans_in_display_order
from .extensions import db
from .models import (
    AccessGrant, AssistantAISettings, AuditLog, Plan, PlanTool, Project, Tool,
    ToolVisibilitySetting, User, UserToolOverride, VocabularyLibrary, utcnow,
)
from .tool_catalog import (
    ANALYTICS_INSTAGRAM_TOOL,
    VISIBILITY_SCOPE_TYPES,
    administrative_catalog_entries,
    groups_in_display_order,
    legacy_unavailable_tool_entries,
    persistent_tool_seed_rows,
    standalone_tools_in_display_order,
    tool_definition,
    tools_in_display_order,
    validate_visibility_target,
    visible_navigation_groups,
    visible_standalone_tools,
)

PLANOS = tuple(
    (plan.plan_id, plan.name, plan.description)
    for plan in commercial_plans_in_display_order()
)
# Adaptadores de compatibilidade: consumidores legados continuam recebendo os
# mesmos formatos, mas a definição canônica passou para ``tool_catalog``.
FERRAMENTAS = persistent_tool_seed_rows()
UNAVAILABLE_TOOLS = legacy_unavailable_tool_entries()
ACCOUNT_LIFECYCLE_LOCK = RLock()


def tool_catalog() -> list[dict]:
    """Catálogo administrativo completo, sem omitir ferramentas inativas/sem uso."""
    return administrative_catalog_entries(db.session.scalars(select(Tool)).all())


def account_accepts_new_work(user_id: str) -> bool:
    """Consulta o banco, não o usuário em cache da requisição concorrente."""
    return db.session.scalar(select(User.id).where(
        User.id == user_id, User.status == "active", User.deleted_at.is_(None)
    )) is not None


def seed_platform() -> None:
    """Semeia catálogo inicial sem reescrever permissões já configuradas."""
    initial_bootstrap = (
        db.session.scalar(select(func.count()).select_from(Plan)) == 0
        and db.session.scalar(select(func.count()).select_from(Tool)) == 0
    )
    # Em uma base já inicializada, planos comerciais são exclusivamente dados de
    # migration. Isso impede que um seed antecipado contorne a cadeia Alembic ou
    # reintroduza metadados antigos. Um bootstrap genuinamente vazio continua
    # recebendo o catálogo completo, de modo idempotente.
    plan_rows = PLANOS if initial_bootstrap else ()
    for code, name, description in plan_rows:
        if db.session.get(Plan, code) is None:
            db.session.add(Plan(id=code, name=name, description=description, active=True))
    for code, name, route in FERRAMENTAS:
        if db.session.get(Tool, code) is None:
            db.session.add(Tool(id=code, name=name, route=route, active=True))
    # Configuração global, idempotente e sem segredos. As credenciais continuam
    # exclusivamente no ambiente do processo.
    if db.session.get(AssistantAISettings, 1) is None:
        db.session.add(AssistantAISettings())
    db.session.flush()
    # O bootstrap vazio recebe somente a matriz inicial. Em instalações já
    # existentes, a migration comercial cria os novos vínculos e este seed não
    # restaura associações que um administrador tenha removido deliberadamente.
    initial_plan_tools = tuple(
        (plan.plan_id, tool_id)
        for plan in COMMERCIAL_PLANS
        for tool_id in plan.available_tool_ids
    )
    for plan_id, tool_id in (() if not initial_bootstrap else initial_plan_tools):
        if db.session.get(PlanTool, (plan_id, tool_id)) is None:
            db.session.add(PlanTool(plan_id=plan_id, tool_id=tool_id))
    if initial_bootstrap:
        # Materializa a matriz antes de consultar políticas. Em SQLite isso
        # evita que uma consulta de inicialização dispare um autoflush parcial
        # de relações plano-ferramenta.
        db.session.flush()
        # A importação tardia preserva o módulo de seed em bancos anteriores à
        # migration de limites e não altera políticas já publicadas.
        from .commercial_limit_services import initialize_commercial_limit_policies
        initialize_commercial_limit_policies()
    snapshot = _seed()
    counts = _contagens(snapshot)
    if counts != {"grupos": 8, "entidades": 75, "variantes": 117}:
        raise RuntimeError(f"O seed Relações raciais divergiu: {counts}")
    existing_library = db.session.get(VocabularyLibrary, "relacoes_raciais")
    if existing_library is None:
        db.session.add(VocabularyLibrary(
            id="relacoes_raciais", name="Relações raciais",
            description="Biblioteca oficial inicial derivada de entities.yml.",
            snapshot_json=snapshot, content_hash=hash_vocabulario(snapshot), counts_json=counts,
            status="published", version="v1", active=True,
        ))
    elif (existing_library.snapshot_json != snapshot or existing_library.content_hash != hash_vocabulario(snapshot)
          or existing_library.counts_json != counts):
        raise RuntimeError("A biblioteca oficial Relações raciais divergiu do seed; intervenção manual necessária.")
    db.session.commit()


def current_grant(user: User) -> AccessGrant | None:
    return db.session.scalar(
        select(AccessGrant).where(AccessGrant.user_id == user.id, AccessGrant.ended_at.is_(None))
        .order_by(AccessGrant.created_at.desc(), AccessGrant.id.desc())
    )


def awaiting_plan_activation(user: User) -> bool:
    """Indica conta autenticável que ainda não recebeu concessão principal."""
    return bool(user.is_active and user.role != "admin" and current_grant(user) is None)


def access_is_active(user: User) -> bool:
    if not user.is_active:
        return False
    if user.role == "admin":
        return True
    grant = current_grant(user)
    if grant is None or grant.status not in {"active", "trial"}:
        return False
    if grant.expires_at is not None:
        expiration = grant.expires_at.replace(tzinfo=timezone.utc) if grant.expires_at.tzinfo is None else grant.expires_at
        if expiration <= datetime.now(timezone.utc):
            return False
    plan = db.session.get(Plan, grant.plan_id)
    return bool(plan and plan.active)


def can_use_tool(user: User, tool_id: str) -> bool:
    if not access_is_active(user):
        return False
    tool = db.session.get(Tool, tool_id)
    if tool is None or not tool.active:
        return False
    if user.role == "admin":
        return True
    override = db.session.get(UserToolOverride, (user.id, tool_id))
    if override is not None:
        return override.decision == "allow"
    grant = current_grant(user)
    return db.session.get(PlanTool, (grant.plan_id, tool_id)) is not None


def tool_is_administratively_visible(tool_id: str) -> bool:
    """Aplica somente as preferências de visibilidade ao catálogo canônico.

    Ferramentas fora do catálogo preservam o comportamento legado. Esta
    decisão não consulta ``Tool.active`` nem regras comerciais: essas camadas
    seguem explicitamente separadas em ``can_use_tool``.
    """
    definition = tool_definition(tool_id)
    if definition is None:
        return True
    group_preferences, tool_preferences = visibility_preferences()
    if definition.group_id is None:
        return tool_preferences.get(definition.tool_id, True)
    return group_preferences.get(definition.group_id, True) and tool_preferences.get(definition.tool_id, True)


def can_access_tool(user: User, tool_id: str) -> bool:
    """Decide acesso operacional sem transformar visibilidade em permissão.

    Administradores continuam podendo manter ferramentas ocultas, mas apenas
    quando a ferramenta está operacional e seu acesso já seria permitido.
    """
    if user.role != "admin" and not tool_is_administratively_visible(tool_id):
        return False
    return can_use_tool(user, tool_id)


def require_tool_access(user: User, tool_id: str) -> None:
    """Centraliza o bloqueio seguro de ferramenta oculta e de acesso normal."""
    if user.role != "admin" and not tool_is_administratively_visible(tool_id):
        abort(404)
    if not can_use_tool(user, tool_id):
        abort(403)


def navigation_groups_for_user(user: User) -> tuple[dict[str, object], ...]:
    """Projeção de navegação sem grupos vazios, pronta para desktop e mobile."""
    if awaiting_plan_activation(user):
        return ()
    group_preferences, tool_preferences = visibility_preferences()
    return tuple(
        {
            "id": group.group.group_id,
            "name": group.group.name,
            "icon": group.group.icon,
            "tools": tuple(
                {
                    "id": tool.tool_id,
                    "name": tool.name,
                    "endpoint": tool.endpoint,
                    # Uma entrada futura pode ser apresentada, mas jamais
                    # recebe link por existir um Tool acidental no banco.
                    "accessible": tool.implementation_status == "available" and can_access_tool(user, tool.tool_id),
                }
                for tool in group.tools
            ),
        }
        for group in visible_navigation_groups(
            group_visibility=group_preferences,
            tool_visibility=tool_preferences,
        )
    )


def navigation_standalone_tools_for_user(user: User) -> tuple[dict[str, object], ...]:
    """Ferramentas de primeiro nível da sidebar, sem grupo sintético."""
    if awaiting_plan_activation(user):
        return ()
    _group_preferences, tool_preferences = visibility_preferences()
    return tuple(
        {
            "id": tool.tool_id,
            "name": tool.name,
            "icon": tool.icon,
            "endpoint": tool.endpoint,
            "accessible": tool.implementation_status == "available" and can_access_tool(user, tool.tool_id),
        }
        for tool in visible_standalone_tools(tool_visibility=tool_preferences)
    )


def get_project_for_user(project_id: str, user: User, *, include_inactive: bool = False) -> Project:
    project = db.session.get(Project, project_id)
    if project is None or (project.owner_user_id != user.id and user.role != "admin"):
        abort(404)
    if not include_inactive and (project.deleted_at is not None or project.status != "active"):
        abort(404)
    return project


def record_audit(admin: User | None, action: str, target_type: str, target_id: str, before: dict | None = None, after: dict | None = None) -> None:
    db.session.add(AuditLog(
        admin_user_id=admin.id if admin else None, action=action, target_type=target_type,
        admin_name_snapshot=admin.name if admin else None,
        target_id=target_id, before_json=before or {}, after_json=after or {},
    ))


def visibility_preferences() -> tuple[dict[str, bool], dict[str, bool]]:
    """Retorna as preferências persistidas, sem misturá-las ao acesso.

    Uma configuração pode tornar-se obsoleta entre duas versões do catálogo
    (por exemplo, após a remoção explícita de uma ferramenta). Ela não pode
    impedir a renderização global da plataforma: a escrita continua validando
    estritamente os alvos, e a leitura ignora apenas esse registro legado.
    """
    groups: dict[str, bool] = {}
    tools: dict[str, bool] = {}
    for setting in db.session.scalars(select(ToolVisibilitySetting)).all():
        try:
            validate_visibility_target(setting.scope_type, setting.scope_id)
        except ValueError:
            if setting.scope_type not in VISIBILITY_SCOPE_TYPES:
                raise
            current_app.logger.warning(
                "Ignoring stale tool visibility setting outside the current catalog: %s:%s",
                setting.scope_type,
                setting.scope_id,
            )
            continue
        target = groups if setting.scope_type == "group" else tools
        target[setting.scope_id] = bool(setting.visible)
    return groups, tools


def visibility_control_catalog() -> list[dict[str, object]]:
    """Projeção administrativa das preferências, sem alterar o catálogo base."""
    group_preferences, tool_preferences = visibility_preferences()
    tools_by_group: dict[str, list[dict[str, object]]] = {
        group.group_id: [] for group in groups_in_display_order()
    }
    for tool in tools_in_display_order():
        if tool.group_id is None:
            continue
        individual_visible = tool_preferences.get(tool.tool_id, True)
        tools_by_group[tool.group_id].append({
            "id": tool.tool_id,
            "name": tool.name,
            "icon": tool.icon,
            "implementation_status": tool.implementation_status,
            "visible": individual_visible,
            "configured": tool.tool_id in tool_preferences,
        })
    return [
        {
            "id": group.group_id,
            "name": group.name,
            "visible": group_preferences.get(group.group_id, True),
            "configured": group.group_id in group_preferences,
            "tools": tools_by_group[group.group_id],
        }
        for group in groups_in_display_order()
    ]


def standalone_visibility_control_catalog() -> list[dict[str, object]]:
    """Controles administrativos das ferramentas que não pertencem a grupo."""
    _group_preferences, tool_preferences = visibility_preferences()
    return [
        {
            "id": tool.tool_id,
            "name": tool.name,
            "icon": tool.icon,
            "implementation_status": tool.implementation_status,
            "visible": tool_preferences.get(tool.tool_id, True),
            "configured": tool.tool_id in tool_preferences,
        }
        for tool in standalone_tools_in_display_order()
    ]


def set_visibility_preference(
    admin: User,
    *,
    scope_type: str,
    scope_id: str,
    visible: bool,
) -> bool:
    """Persiste uma mudança efetiva de visibilidade e registra uma auditoria.

    ``True`` sem registro anterior equivale ao padrão já visível e, portanto,
    não cria ruído nem no banco nem no histórico. Preferências individuais
    nunca são alteradas como efeito colateral de uma mudança de grupo.
    """
    if not isinstance(visible, bool):
        raise ValueError("O estado de visibilidade deve ser booleano.")
    validate_visibility_target(scope_type, scope_id)
    setting = db.session.scalar(select(ToolVisibilitySetting).where(
        ToolVisibilitySetting.scope_type == scope_type,
        ToolVisibilitySetting.scope_id == scope_id,
    ))
    previous_visible = bool(setting.visible) if setting is not None else True
    if previous_visible == visible:
        return False

    if setting is None:
        setting = ToolVisibilitySetting(
            scope_type=scope_type,
            scope_id=scope_id,
            visible=visible,
            changed_by_id=admin.id,
            changed_by_name_snapshot=admin.name,
        )
        db.session.add(setting)
    else:
        setting.visible = visible
        setting.changed_by_id = admin.id
        setting.changed_by_name_snapshot = admin.name

    record_audit(
        admin,
        "tool_visibility_changed",
        "tool_visibility",
        f"{scope_type}:{scope_id}",
        {"scope_type": scope_type, "scope_id": scope_id, "visible": previous_visible},
        {"scope_type": scope_type, "scope_id": scope_id, "visible": visible},
    )
    return True


def replace_grant(user: User, plan_id: str, access_mode: str, status: str, admin: User | None = None, expires_at: datetime | None = None) -> AccessGrant:
    """Substitui a concessão aberta sem apagar o histórico.

    O lock cobre concorrência no processo Flask/CLI atual. A tabela histórica
    ainda não possui índice único parcial para ``ended_at IS NULL``; antes de
    uma implantação multi-processo, a operação exige auditoria de duplicidades
    abertas e uma migration específica que só crie esse índice após a base
    estar comprovadamente consistente. Esta fase não normaliza concessões
    antigas nem altera esse contrato de dados.
    """
    with ACCOUNT_LIFECYCLE_LOCK:
        plan = db.session.get(Plan, plan_id)
        if plan is None or not plan.active:
            raise ValueError("Este plano está inativo e não pode receber novas concessões.")
        previous = current_grant(user)
        if previous:
            previous.ended_at = utcnow()
        grant = AccessGrant(
            user_id=user.id, plan_id=plan_id, access_mode=access_mode,
            status=status, granted_by_id=admin.id if admin else None,
            granted_by_name_snapshot=admin.name if admin else None,
            expires_at=expires_at,
        )
        db.session.add(grant)
        if admin:
            record_audit(admin, "access_grant_changed", "user", user.id,
                         {"plan": previous.plan_id, "mode": previous.access_mode, "status": previous.status} if previous else {},
                         {"plan": plan_id, "mode": access_mode, "status": status, "expires_at": expires_at.isoformat() if expires_at else None})
        return grant
