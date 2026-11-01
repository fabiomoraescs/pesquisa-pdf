"""Preparação administrativa explícita e local para a ativação do OJS SEO.

Este módulo não é um seed e não é chamado durante a inicialização do Flask.
Cada operação exige um administrador ativo, uma confirmação de ambiente local
e uma transação confirmada pelo chamador. Assim, disponibilizar a rota no
catálogo não cria uma ``Tool``, não cria vínculos ``PlanTool`` e não concede
acesso até que o operador execute deliberadamente cada passo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import current_app
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url

from .commercial_catalog import commercial_plans_in_display_order
from .commercial_limit_services import effective_subscription_limits
from .extensions import db
from .models import Plan, PlanTool, Tool, User, Workspace, WorkspaceMembership, WorkspaceSubscription
from .services import access_is_active, can_access_tool, current_grant, record_audit, replace_grant, set_visibility_preference
from .tool_catalog import tool_definition
from .workspace_services import create_individual_workspace, create_workspace_subscription, current_workspace_subscription


OJS_SEO_TOOL_ID = "ojs_seo"
# A ativação explícita exige, no mínimo, a migration que fecha a cadeia OJS
# atual. Revisões futuras são aceitas desde que descendam desta revisão.
OJS_SEO_MINIMUM_ALEMBIC_REVISION = "91a2b3c4d5e6"
OJS_SEO_REQUIRED_TABLES = frozenset({
    "ojs_journals",
    "ojs_seo_audit_runs",
    "ojs_articles",
    "ojs_article_observations",
    "ojs_html_audit_runs",
    "ojs_article_html_analyses",
    "workspace_subscription_ojs_journals",
})
OJS_SEO_ELIGIBLE_PLAN_IDS = ("marketing_sites", "marketing_complete", "integral")
OJS_SEO_DEVELOPMENT_ACCESS_MODE = "development_ojs_seo"
OJS_SEO_DEVELOPMENT_WORKSPACE_PREFIX = "OJS SEO — desenvolvimento — "


class OjsSeoActivationError(RuntimeError):
    """Erro de domínio seguro para os procedimentos administrativos locais."""


@dataclass(frozen=True, slots=True)
class OjsSeoProvisioningResult:
    tool_id: str
    tool_created: bool
    plan_links_created: tuple[str, ...]
    tool_active: bool


@dataclass(frozen=True, slots=True)
class OjsSeoDevelopmentEnvironment:
    workspace_id: str
    subscription_id: str
    grant_id: str
    workspace_created: bool
    subscription_created: bool
    grant_created: bool
    ojs_journal_limit: int


@dataclass(frozen=True, slots=True)
class OjsSeoDevelopmentAccessCheck:
    user_id: str
    tool_provisioned: bool
    tool_active: bool
    operational_access: bool
    eligible_subscription_ids: tuple[str, ...]


def _require_active_administrator(actor: User) -> None:
    if not isinstance(actor, User) or not actor.id or actor.role != "admin":
        raise OjsSeoActivationError("A operação exige um administrador autenticado.")
    if not actor.is_active or actor.deleted_at is not None:
        raise OjsSeoActivationError("A conta administrativa não está ativa.")


def _require_active_user(user: User) -> None:
    if not isinstance(user, User) or not user.id or not user.is_active or user.deleted_at is not None:
        raise OjsSeoActivationError("A conta de teste deve estar ativa.")


def require_local_development_environment() -> None:
    """Recusa gravar se o processo não puder ser comprovado como local.

    Os testes isolados são permitidos. Fora deles, além da confirmação explícita
    de configuração, a conexão precisa ser SQLite e o arquivo precisa ficar
    dentro de ``PLATFORM_DATA_DIR``. Uma URL pública não local também recusa a
    operação para evitar que uma variável de desenvolvimento seja usada por
    engano contra uma instância publicada.
    """
    if current_app.config.get("TESTING"):
        return
    if not current_app.config.get("PESQUISAPDF_LOCAL_DEVELOPMENT", False):
        raise OjsSeoActivationError(
            "A preparação OJS SEO exige PESQUISAPDF_LOCAL_DEVELOPMENT=1."
        )
    try:
        database_url = make_url(str(current_app.config["SQLALCHEMY_DATABASE_URI"]))
    except Exception as exc:  # pragma: no cover - defesa para configurações inválidas
        raise OjsSeoActivationError("A conexão de banco local não pôde ser validada.") from exc
    if database_url.get_backend_name() != "sqlite" or not database_url.database or database_url.database == ":memory:":
        raise OjsSeoActivationError(
            "A preparação OJS SEO só é permitida em um SQLite local persistente."
        )
    try:
        database_path = Path(database_url.database).resolve(strict=False)
        data_directory = Path(str(current_app.config["PLATFORM_DATA_DIR"])).resolve(strict=False)
        database_path.relative_to(data_directory)
    except (OSError, ValueError) as exc:
        raise OjsSeoActivationError(
            "O arquivo SQLite precisa estar dentro de PLATFORM_DATA_DIR para esta operação."
        ) from exc
    public_url = str(current_app.config.get("PESQUISAPDF_PUBLIC_URL") or "").strip()
    if public_url:
        hostname = (urlparse(public_url).hostname or "").casefold()
        if hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise OjsSeoActivationError(
                "A preparação OJS SEO não é permitida quando há uma URL pública não local configurada."
            )


def _required_plan_ids_from_catalog() -> tuple[str, ...]:
    plan_ids = tuple(
        plan.plan_id
        for plan in commercial_plans_in_display_order()
        if OJS_SEO_TOOL_ID in plan.available_tool_ids
    )
    if plan_ids != OJS_SEO_ELIGIBLE_PLAN_IDS:
        raise OjsSeoActivationError("O catálogo comercial do OJS SEO está inconsistente.")
    return plan_ids


def _tool_definition_or_error():
    definition = tool_definition(OJS_SEO_TOOL_ID)
    if (
        definition is None
        or definition.implementation_status != "available"
        or definition.tool_code != OJS_SEO_TOOL_ID
        or definition.route != "/analytics/ojs-seo"
        or definition.endpoint != "ojs_seo.home"
        or not definition.requires_explicit_provisioning
    ):
        raise OjsSeoActivationError("O catálogo do OJS SEO não está pronto para provisionamento explícito.")
    return definition


def _revision_includes_ojs_schema(revision: str) -> bool:
    """Indica se ``revision`` contém a migration mínima do domínio OJS.

    A comparação percorre a cadeia Alembic em vez de comparar identificadores
    textual ou numericamente. Assim, um novo head que descenda da revisão
    mínima continua apto à ativação, enquanto uma revisão lateral ou anterior
    permanece bloqueada.
    """
    migration_directory = Path(current_app.root_path) / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migration_directory))
    try:
        scripts = ScriptDirectory.from_config(config)
        return any(
            script.revision == OJS_SEO_MINIMUM_ALEMBIC_REVISION
            for script in scripts.iterate_revisions(revision, "base")
        )
    except Exception as exc:  # falha fechada para uma cadeia Alembic inválida ou incompleta
        raise OjsSeoActivationError(
            "A cadeia Alembic não pôde ser validada para a ativação do OJS SEO."
        ) from exc


def assert_ojs_seo_schema_ready() -> None:
    """Confirma a cadeia OJS antes de ativar ou preparar assinaturas."""
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    missing = OJS_SEO_REQUIRED_TABLES - table_names
    if missing or "alembic_version" not in table_names:
        raise OjsSeoActivationError(
            "As migrations OJS SEO ainda não foram aplicadas neste banco local."
        )
    revision = db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if not revision or not _revision_includes_ojs_schema(revision):
        raise OjsSeoActivationError(
            "A revisão Alembic do banco não inclui o esquema mínimo necessário ao OJS SEO."
        )


def _require_provisioning_environment(*, allow_explicit_production: bool) -> None:
    """Mantém o bloqueio local, salvo a operação de CLI explicitamente confirmada.

    Não há rota HTTP para solicitar esse desvio. Ele existe exclusivamente para
    a publicação controlada, que informa o administrador responsável e registra
    a auditoria da operação no próprio banco.
    """
    if not allow_explicit_production:
        require_local_development_environment()


def provision_ojs_seo_tool(
    actor: User, *, allow_explicit_production: bool = False
) -> OjsSeoProvisioningResult:
    """Cria apenas a Tool inativa e suas três relações comerciais, sem commit.

    Reexecutar esta função preserva os registros existentes e não duplica
    vínculos. O chamador deve confirmar ou reverter a transação inteira.
    """
    _require_provisioning_environment(allow_explicit_production=allow_explicit_production)
    _require_active_administrator(actor)
    definition = _tool_definition_or_error()
    plan_ids = _required_plan_ids_from_catalog()
    plans = {plan.id: plan for plan in db.session.scalars(select(Plan).where(Plan.id.in_(plan_ids)))}
    if set(plans) != set(plan_ids):
        raise OjsSeoActivationError("Os planos comerciais elegíveis não estão presentes no banco.")

    tool = db.session.get(Tool, OJS_SEO_TOOL_ID)
    tool_created = False
    if tool is None:
        tool = Tool(
            id=OJS_SEO_TOOL_ID,
            name=definition.name,
            description="Auditoria bibliográfica OAI-PMH/Dublin Core para periódicos OJS.",
            route=definition.route,
            active=False,
        )
        db.session.add(tool)
        db.session.flush()
        tool_created = True
    elif tool.route != definition.route:
        raise OjsSeoActivationError("A Tool OJS SEO existente possui uma rota incompatível.")

    links_created: list[str] = []
    for plan_id in plan_ids:
        if db.session.get(PlanTool, (plan_id, OJS_SEO_TOOL_ID)) is None:
            db.session.add(PlanTool(plan_id=plan_id, tool_id=OJS_SEO_TOOL_ID))
            links_created.append(plan_id)
    db.session.flush()

    if tool_created or links_created:
        record_audit(actor, "ojs_seo_tool_provisioned", "tool", OJS_SEO_TOOL_ID, {}, {
            "tool_created": tool_created,
            "plan_links_created": links_created,
            "tool_active": bool(tool.active),
        })
    return OjsSeoProvisioningResult(
        tool_id=tool.id,
        tool_created=tool_created,
        plan_links_created=tuple(links_created),
        tool_active=bool(tool.active),
    )


def _development_workspace_name(label: str) -> str:
    normalized = " ".join((label or "").split())
    if not normalized or len(normalized) > 140:
        raise OjsSeoActivationError("Informe um nome de área de desenvolvimento entre 1 e 140 caracteres.")
    return f"{OJS_SEO_DEVELOPMENT_WORKSPACE_PREFIX}{normalized}"


def _development_workspace_for_user(user: User, name: str) -> Workspace | None:
    return db.session.scalar(select(Workspace).where(
        Workspace.owner_user_id == user.id,
        Workspace.name == name,
    ).order_by(Workspace.created_at.asc(), Workspace.id.asc()))


def _validate_development_workspace(workspace: Workspace, user: User) -> None:
    membership = db.session.scalar(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == workspace.id,
        WorkspaceMembership.user_id == user.id,
        WorkspaceMembership.role == "owner",
        WorkspaceMembership.status == "active",
    ))
    if (
        workspace.workspace_type != "individual"
        or workspace.status != "active"
        or membership is None
    ):
        raise OjsSeoActivationError("A área de desenvolvimento existente não possui titularidade ativa compatível.")


def _subscription_ojs_limit(subscription: WorkspaceSubscription) -> int:
    resolved = effective_subscription_limits(subscription)
    resource = resolved["resources"].get("ojs_journals", {})
    limit = resource.get("effective_limit", 0)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise OjsSeoActivationError("A assinatura de desenvolvimento não possui capacidade OJS disponível.")
    return limit


def prepare_ojs_seo_development_environment(
    actor: User,
    *,
    test_user_id: str,
    workspace_label: str,
    plan_id: str,
) -> OjsSeoDevelopmentEnvironment:
    """Prepara um usuário de teste dedicado, sem trial, cobrança ou commit.

    Para não substituir contratos existentes, a conta indicada precisa não ter
    grant aberto ou já possuir exatamente o grant de desenvolvimento criado por
    este procedimento. A identificação persistente é a combinação do nome de
    workspace com o modo de grant ``development_ojs_seo`` e a trilha de audit.
    """
    require_local_development_environment()
    _require_active_administrator(actor)
    assert_ojs_seo_schema_ready()
    _required_plan_ids_from_catalog()
    if plan_id not in OJS_SEO_ELIGIBLE_PLAN_IDS:
        raise OjsSeoActivationError("O plano informado não é elegível para OJS SEO.")
    plan = db.session.get(Plan, plan_id)
    if plan is None or not plan.active:
        raise OjsSeoActivationError("O plano informado não está ativo.")
    if db.session.get(Tool, OJS_SEO_TOOL_ID) is None or db.session.get(PlanTool, (plan_id, OJS_SEO_TOOL_ID)) is None:
        raise OjsSeoActivationError("A Tool OJS SEO e o vínculo do plano precisam ser provisionados primeiro.")
    user = db.session.get(User, test_user_id)
    if user is None:
        raise OjsSeoActivationError("A conta de teste não foi encontrada.")
    _require_active_user(user)
    workspace_name = _development_workspace_name(workspace_label)
    grant_created = False
    workspace = _development_workspace_for_user(user, workspace_name)
    workspace_created = False
    subscription_created = False
    with db.session.begin_nested():
        grant = current_grant(user)
        if grant is None:
            grant = replace_grant(user, plan_id, OJS_SEO_DEVELOPMENT_ACCESS_MODE, "active", actor)
            grant_created = True
        elif not (
            grant.plan_id == plan_id
            and grant.access_mode == OJS_SEO_DEVELOPMENT_ACCESS_MODE
            and grant.status == "active"
            and access_is_active(user)
        ):
            raise OjsSeoActivationError(
                "A conta indicada já possui uma concessão aberta; use uma conta de teste dedicada."
            )
        if workspace is None:
            workspace = create_individual_workspace(user, workspace_name)
            workspace_created = True
        else:
            _validate_development_workspace(workspace, user)

        subscription = current_workspace_subscription(workspace.id)
        if subscription is None:
            subscription = create_workspace_subscription(actor, workspace.id, plan_id)
            subscription_created = True
        elif subscription.plan_id != plan_id:
            raise OjsSeoActivationError(
                "A área de desenvolvimento existente possui uma assinatura ativa de outro plano."
            )
        limit = _subscription_ojs_limit(subscription)
        record_audit(actor, "ojs_seo_development_environment_prepared", "workspace_subscription", subscription.id, {}, {
            "workspace_id": workspace.id,
            "test_user_id": user.id,
            "plan_id": plan_id,
            "workspace_created": workspace_created,
            "subscription_created": subscription_created,
            "grant_created": grant_created,
        })
        db.session.flush()

    return OjsSeoDevelopmentEnvironment(
        workspace_id=workspace.id,
        subscription_id=subscription.id,
        grant_id=grant.id,
        workspace_created=workspace_created,
        subscription_created=subscription_created,
        grant_created=grant_created,
        ojs_journal_limit=limit,
    )


def activate_ojs_seo_tool(actor: User, *, allow_explicit_production: bool = False) -> bool:
    """Ativa somente a Tool já provisionada depois de validar as migrations."""
    _require_provisioning_environment(allow_explicit_production=allow_explicit_production)
    _require_active_administrator(actor)
    assert_ojs_seo_schema_ready()
    _required_plan_ids_from_catalog()
    tool = db.session.get(Tool, OJS_SEO_TOOL_ID)
    if tool is None:
        raise OjsSeoActivationError("A Tool OJS SEO precisa ser provisionada antes da ativação.")
    missing_links = [plan_id for plan_id in OJS_SEO_ELIGIBLE_PLAN_IDS if db.session.get(PlanTool, (plan_id, tool.id)) is None]
    if missing_links:
        raise OjsSeoActivationError("Os vínculos comerciais do OJS SEO ainda não foram provisionados.")
    if tool.active:
        return False
    tool.active = True
    record_audit(actor, "ojs_seo_tool_activated", "tool", tool.id, {"active": False}, {"active": True})
    return True


def deactivate_ojs_seo_tool(actor: User) -> bool:
    """Desativa a Tool sem apagar vínculos, projetos, assinaturas ou histórico."""
    require_local_development_environment()
    _require_active_administrator(actor)
    tool = db.session.get(Tool, OJS_SEO_TOOL_ID)
    if tool is None:
        raise OjsSeoActivationError("A Tool OJS SEO não está provisionada.")
    if not tool.active:
        return False
    tool.active = False
    record_audit(actor, "ojs_seo_tool_deactivated", "tool", tool.id, {"active": True}, {"active": False})
    return True


def set_ojs_seo_navigation_visibility(actor: User, *, scope_type: str, visible: bool) -> bool:
    """Altera uma camada de visibilidade por vez, sem conceder autorização."""
    require_local_development_environment()
    _require_active_administrator(actor)
    scope_id_by_type = {"group": "analytics", "tool": OJS_SEO_TOOL_ID}
    scope_id = scope_id_by_type.get(scope_type)
    if scope_id is None:
        raise OjsSeoActivationError("A visibilidade deve informar escopo group ou tool.")
    return set_visibility_preference(actor, scope_type=scope_type, scope_id=scope_id, visible=visible)


def verify_ojs_seo_development_access(actor: User, *, test_user_id: str) -> OjsSeoDevelopmentAccessCheck:
    """Consulta as camadas de acesso sem expor dados pessoais ou mutar o banco."""
    require_local_development_environment()
    _require_active_administrator(actor)
    user = db.session.get(User, test_user_id)
    if user is None:
        raise OjsSeoActivationError("A conta de teste não foi encontrada.")
    _require_active_user(user)
    tool = db.session.get(Tool, OJS_SEO_TOOL_ID)
    subscriptions = db.session.scalars(
        select(WorkspaceSubscription)
        .join(Workspace, WorkspaceSubscription.workspace_id == Workspace.id)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .where(
            WorkspaceMembership.user_id == user.id,
            WorkspaceMembership.role == "owner",
            WorkspaceMembership.status == "active",
            Workspace.status == "active",
            WorkspaceSubscription.status == "active",
            WorkspaceSubscription.ended_at.is_(None),
        )
        .order_by(WorkspaceSubscription.created_at.asc(), WorkspaceSubscription.id.asc())
    ).unique()
    eligible = tuple(
        subscription.id
        for subscription in subscriptions
        if db.session.get(PlanTool, (subscription.plan_id, OJS_SEO_TOOL_ID)) is not None
        and _subscription_ojs_limit(subscription) >= 1
    )
    return OjsSeoDevelopmentAccessCheck(
        user_id=user.id,
        tool_provisioned=tool is not None,
        tool_active=bool(tool and tool.active),
        operational_access=can_access_tool(user, OJS_SEO_TOOL_ID),
        eligible_subscription_ids=eligible,
    )


__all__ = [
    "OJS_SEO_DEVELOPMENT_ACCESS_MODE",
    "OJS_SEO_DEVELOPMENT_WORKSPACE_PREFIX",
    "OJS_SEO_ELIGIBLE_PLAN_IDS",
    "OJS_SEO_MINIMUM_ALEMBIC_REVISION",
    "OjsSeoActivationError",
    "OjsSeoDevelopmentAccessCheck",
    "OjsSeoDevelopmentEnvironment",
    "OjsSeoProvisioningResult",
    "activate_ojs_seo_tool",
    "assert_ojs_seo_schema_ready",
    "deactivate_ojs_seo_tool",
    "prepare_ojs_seo_development_environment",
    "provision_ojs_seo_tool",
    "require_local_development_environment",
    "set_ojs_seo_navigation_visibility",
    "verify_ojs_seo_development_access",
]
