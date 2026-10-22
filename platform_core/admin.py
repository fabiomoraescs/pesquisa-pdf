"""Administração auditável, com exclusão protegida de bibliotecas independentes."""

from __future__ import annotations

from datetime import datetime, time, timezone
from functools import wraps
from pathlib import Path
from uuid import UUID

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .extensions import db
from .commercial_catalog import (
    COMMERCIAL_LIMIT_RESOURCES,
    commercial_groups_in_display_order,
    commercial_plan_definition,
    commercial_plans_in_display_order,
    configurable_limit_resources,
    is_commercial_plan_assignable,
)
from .commercial_limit_services import (
    CommercialLimitError,
    current_plan_limits,
    plan_limit_policy_history,
    publish_plan_limit_policy,
)
from .commercial_trial_services import (
    CommercialTrialError,
    activate_commercial_trial,
    all_commercial_trials,
    trial_cpf_mask_display,
    trial_state,
    user_commercial_trials,
)
from .library_access import library_editor, library_url, private_library_flow, user_libraries_bp
from .models import (
    AccessGrant, Analysis, AssistantAISettings, AuditLog, PasswordRecoveryToken, Plan, PlanTool, Project, ProjectLibrary, ProjectVocabularyVersion,
    QualitativeCode, QualitativeCoding, QualitativeExcerpt, QualitativeMemo, QualitativeRejection, Tool, User,
    UserProfile, UserToolOverride, VocabularyLibrary, utcnow,
)
from .assistant_ai_manager import (
    AIProviderManager,
    AISettingsValidationError,
    CONNECTION_AVAILABLE,
    CONNECTION_AUDIT_ACTION,
    CONNECTION_FAILED,
    CONNECTION_NATIVE_AVAILABLE,
    PROVIDERS,
)
from .password_policy import TEMPORARY_PASSWORD
from .project_lifecycle import ProjectActionError, archive, delete_archived, restore
from .profile import remove_profile_photo
from .scraping_types import LABEL_BY_TOOL
from .official_libraries import LibraryError, add_entity, add_group, add_variant, change_publication, create_draft, create_imported_draft, set_item_active
from .library_spreadsheets import MAX_XLSX_BYTES, SpreadsheetImportError, parse_library_xlsx, previews
from .instagram_meta import InstagramMetaSettings
from .services import (
    ACCOUNT_LIFECYCLE_LOCK, access_is_active, current_grant, record_audit,
    replace_grant, set_visibility_preference, tool_catalog,
    standalone_visibility_control_catalog, visibility_control_catalog,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_only(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "admin" or not current_user.is_active:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _commercial_plan_groups(plans: list[Plan]) -> tuple[dict[str, object], ...]:
    """Projeta os planos comerciais sem transformar grupos em dados de navegação."""
    plans_by_id = {plan.id: plan for plan in plans}
    tools_by_id = {tool.id: tool for tool in db.session.scalars(select(Tool)).all()}
    definitions_by_group: dict[str, list[object]] = {
        group.group_id: [] for group in commercial_groups_in_display_order()
    }
    for definition in commercial_plans_in_display_order():
        definitions_by_group[definition.group_id].append(definition)

    return tuple(
        {
            "group": group,
            "plans": tuple(
                {
                    "definition": definition,
                    "plan": plans_by_id.get(definition.plan_id),
                    "limits": (
                        current_plan_limits(plans_by_id[definition.plan_id])
                        if definition.plan_id in plans_by_id and configurable_limit_resources(definition.plan_id) else None
                    ),
                    "limit_policy_history": (
                        plan_limit_policy_history(plans_by_id[definition.plan_id], limit=2)
                        if definition.plan_id in plans_by_id and configurable_limit_resources(definition.plan_id) else ()
                    ),
                    "configurable_limit_resources": configurable_limit_resources(definition.plan_id),
                    "available_tool_names": tuple(
                        tools_by_id[tool_id].name
                        for tool_id in definition.available_tool_ids
                        if tool_id in tools_by_id
                    ),
                }
                for definition in definitions_by_group[group.group_id]
            ),
        }
        for group in commercial_groups_in_display_order()
    )


def _persisted_commercial_plans() -> list[Plan]:
    """Planos ofertados na ordem do catálogo, nunca na ordem do banco."""
    plans_by_id = {
        plan.id: plan for plan in db.session.scalars(select(Plan)).all()
    }
    return [
        plans_by_id[definition.plan_id]
        for definition in commercial_plans_in_display_order()
        if definition.plan_id in plans_by_id
    ]


def _commercial_plan_selection_groups() -> tuple[dict[str, object], ...]:
    """Expõe todos os planos comerciais, sem confundir listagem e concessão.

    Um plano em desenvolvimento continua visível nos seletores administrativos,
    mas a sua elegibilidade para concessão ou período de teste permanece uma
    decisão separada, aplicada também pelos endpoints.
    """
    plans_by_id = {plan.id: plan for plan in _persisted_commercial_plans()}
    definitions = commercial_plans_in_display_order()
    return tuple(
        {
            "group": group,
            "plans": tuple(
                {
                    "definition": definition,
                    "plan": plans_by_id.get(definition.plan_id),
                    "action_available": bool(
                        plans_by_id.get(definition.plan_id)
                        and plans_by_id[definition.plan_id].active
                        and is_commercial_plan_assignable(definition.plan_id)
                    ),
                }
                for definition in definitions
                if definition.group_id == group.group_id
            ),
        }
        for group in commercial_groups_in_display_order()
    )


def _commercial_access_plan_groups(plans: list[Plan]) -> tuple[dict[str, object], ...]:
    """Cabeçalhos da matriz de acessos derivados do catálogo comercial."""
    plans_by_id = {plan.id: plan for plan in plans}
    return tuple(
        {
            "group": group,
            "plans": tuple(
                plans_by_id[definition.plan_id]
                for definition in commercial_plans_in_display_order()
                if definition.group_id == group.group_id and definition.plan_id in plans_by_id
            ),
        }
        for group in commercial_groups_in_display_order()
    )


@admin_bp.get("")
@admin_only
def home():
    counts = {
        "users": db.session.scalar(select(func.count()).select_from(User)),
        "active": db.session.scalar(select(func.count()).select_from(User).where(User.status == "active", User.deleted_at.is_(None))),
        "blocked": db.session.scalar(select(func.count()).select_from(User).where(User.status == "blocked")),
        "projects": db.session.scalar(select(func.count()).select_from(Project).where(Project.deleted_at.is_(None))),
    }
    distribution = db.session.execute(
        select(AccessGrant.plan_id, func.count()).where(AccessGrant.ended_at.is_(None)).group_by(AccessGrant.plan_id)
    ).all()
    return render_template("platform/admin.html", section="home", counts=counts, distribution=distribution)


@admin_bp.get("/integracao-meta")
@admin_only
def meta_integration():
    """Diagnóstico administrativo de configuração, sem revelar segredos."""
    return render_template(
        "platform/admin.html",
        section="meta_integration",
        meta_settings=InstagramMetaSettings.from_mapping(current_app.config),
    )


@admin_bp.get("/usuarios")
@admin_only
def users():
    term = request.args.get("q", "").strip()[:100]
    page = max(1, request.args.get("page", 1, type=int))
    query = select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc())
    if term:
        query = query.where(or_(User.name.ilike(f"%{term}%"), User.email.ilike(f"%{term}%")))
    pagination = db.paginate(query, page=page, per_page=30, error_out=False)
    return render_template("platform/admin.html", section="users", users=pagination.items, pagination=pagination, q=term)


@admin_bp.get("/usuarios/excluidos")
@admin_only
def deleted_users():
    term = request.args.get("q", "").strip()[:100]
    page = max(1, request.args.get("page", 1, type=int))
    query = select(User).where(User.deleted_at.is_not(None)).order_by(User.deleted_at.desc())
    if term:
        query = query.where(or_(User.name.ilike(f"%{term}%"), User.email.ilike(f"%{term}%")))
    pagination = db.paginate(query, page=page, per_page=30, error_out=False)
    grants = {user.id: current_grant(user) for user in pagination.items}
    return render_template("platform/admin.html", section="deleted_users", users=pagination.items,
                           pagination=pagination, q=term, grants=grants)


@admin_bp.get("/usuarios/<uuid:user_id>")
@admin_only
def user_detail(user_id: UUID):
    user = db.session.get(User, str(user_id))
    if user is None:
        abort(404)
    if user.deleted_at is not None:
        return redirect(url_for("admin.deleted_users"))
    grant = current_grant(user)
    # A ordem e os rótulos comerciais não dependem da ordenação alfabética do
    # banco. Os sete planos são sempre exibidos na sequência do catálogo; a
    # elegibilidade de uma ação permanece indicada e validada separadamente.
    return render_template(
        "platform/admin.html", section="user_detail", user=user, grant=current_grant(user),
        grants=db.session.scalars(select(AccessGrant).where(AccessGrant.user_id == user.id).order_by(AccessGrant.created_at.desc())).all(),
        commercial_plan_selection_groups=_commercial_plan_selection_groups(),
        commercial_trials=user_commercial_trials(user.id),
        trial_cpf_mask_display=trial_cpf_mask_display,
        trial_state=trial_state,
        tools=db.session.scalars(select(Tool).order_by(Tool.name)).all(),
        overrides={item.tool_id: item.decision for item in db.session.scalars(select(UserToolOverride).where(UserToolOverride.user_id == user.id))},
        projects=db.session.scalars(select(Project).where(Project.owner_user_id == user.id).order_by(Project.created_at.desc())).all(),
    )


@admin_bp.post("/usuarios/<uuid:user_id>/status")
@admin_only
def set_user_status(user_id: UUID):
    user = db.session.get(User, str(user_id))
    status = request.form.get("status")
    if not user or status not in {"active", "suspended", "blocked"}:
        abort(400)
    if user.id == current_user.id and status != "active":
        abort(400)
    old = user.status
    if user.deleted_at is not None:
        abort(409)
    user.status = status
    record_audit(current_user, "user_status_changed", "user", user.id,
                 {"status": old}, {"status": status})
    db.session.commit()
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.post("/usuarios/<uuid:user_id>/acesso")
@admin_only
def set_user_access(user_id: UUID):
    user = db.session.get(User, str(user_id))
    plan = db.session.get(Plan, request.form.get("plan_id", ""))
    mode = request.form.get("access_mode")
    status = request.form.get("access_status")
    if (
        not user
        or not plan
        or not plan.active
        or not is_commercial_plan_assignable(plan.id)
        or mode not in {"paid", "admin_courtesy"}
        or status not in {"active", "payment_pending", "expired", "canceled"}
    ):
        abort(400)
    expires = None
    if request.form.get("expires_at"):
        try:
            date = datetime.strptime(request.form["expires_at"], "%Y-%m-%d").date()
            expires = datetime.combine(date, time.max, timezone.utc)
        except ValueError:
            abort(400)
    replace_grant(user, plan.id, mode, status, current_user, expires)
    db.session.commit()
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.post("/usuarios/<uuid:user_id>/periodo-de-teste")
@admin_only
def activate_user_trial(user_id: UUID):
    user = db.session.get(User, str(user_id))
    if user is None:
        abort(404)
    try:
        trial = activate_commercial_trial(
            current_user,
            user,
            request.form.get("plan_id", ""),
            request.form.get("cpf", ""),
        )
        db.session.commit()
    except CommercialTrialError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    else:
        flash(
            f"Período de teste ativado até {trial.expires_at.strftime('%d/%m/%Y %H:%M')}.",
            "success",
        )
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.route("/usuarios/<uuid:user_id>/redefinir-senha", methods=["GET", "POST"])
@admin_only
def reset_user_password(user_id: UUID):
    user = db.session.get(User, str(user_id))
    if user is None:
        abort(404)
    if user.deleted_at is not None:
        block_reason = "Esta conta foi excluída logicamente. Reative-a na ficha do usuário antes de redefinir a senha."
    elif not user.is_active:
        block_reason = "Esta conta está suspensa ou bloqueada. Reative-a antes de redefinir a senha."
    elif not access_is_active(user):
        block_reason = "Esta conta não possui acesso ativo. Ajuste o plano ou a concessão antes de redefinir a senha."
    else:
        block_reason = None
    if block_reason:
        status_code = 400 if request.method == "POST" else 200
        return render_template("platform/user_reset_password.html", user=user,
                               temporary_password=TEMPORARY_PASSWORD, block_reason=block_reason), status_code
    if request.method == "POST":
        if request.form.get("confirmation", "").strip() != "redefinir":
            flash("Digite redefinir para confirmar.", "danger")
            return render_template("platform/user_reset_password.html", user=user,
                                   temporary_password=TEMPORARY_PASSWORD), 400
        previous_required = user.must_change_password
        user.set_password(TEMPORARY_PASSWORD)
        user.must_change_password = True
        record_audit(current_user, "user_password_reset", "user", user.id,
                     {"must_change_password": previous_required}, {"must_change_password": True})
        db.session.commit()
        flash("Senha redefinida. Informe a senha temporária ao usuário por um canal seguro; ele deverá alterá-la no próximo acesso.", "success")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    return render_template("platform/user_reset_password.html", user=user,
                           temporary_password=TEMPORARY_PASSWORD)


@admin_bp.post("/usuarios/<uuid:user_id>/ferramenta/<tool_id>")
@admin_only
def set_override(user_id: UUID, tool_id: str):
    user = db.session.get(User, str(user_id))
    tool = db.session.get(Tool, tool_id)
    decision = request.form.get("decision")
    if not user or not tool or decision not in {"inherit", "allow", "deny"}:
        abort(400)
    override = db.session.get(UserToolOverride, (user.id, tool_id))
    before = override.decision if override else "inherit"
    if decision == "inherit":
        if override:
            db.session.delete(override)
    elif override:
        override.decision = decision
    else:
        db.session.add(UserToolOverride(user_id=user.id, tool_id=tool_id, decision=decision))
    record_audit(current_user, "tool_override_changed", "user", user.id,
                 {"tool": tool_id, "decision": before}, {"tool": tool_id, "decision": decision})
    db.session.commit()
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.route("/usuarios/<uuid:user_id>/excluir", methods=["GET", "POST"])
@admin_only
def delete_user(user_id: UUID):
    user = db.session.get(User, str(user_id))
    if not user:
        abort(404)
    if user.id == current_user.id or user.deleted_at is not None:
        abort(409)
    if request.method == "GET":
        return render_template("platform/user_soft_delete.html", user=user)
    if request.form.get("confirm") != "yes":
        abort(400)
    previous_status = user.status
    user.status_before_deletion = previous_status
    user.deleted_at = utcnow()
    user.status = "blocked"
    record_audit(current_user, "user_soft_deleted", "user", user.id,
                 {"status": previous_status, "deleted": False},
                 {"status": "blocked", "deleted": True})
    db.session.commit()
    flash("Usuário excluído logicamente. A conta poderá ser restaurada.", "success")
    return redirect(url_for("admin.deleted_users"))


@admin_bp.route("/usuarios/<uuid:user_id>/restaurar", methods=["GET", "POST"])
@admin_only
def restore_user(user_id: UUID):
    user = db.session.get(User, str(user_id))
    if user is None:
        abort(404)
    if user.deleted_at is None:
        abort(409)
    previous_status = user.status_before_deletion
    if previous_status not in {"active", "suspended", "blocked"}:
        previous_status = None
    if request.method == "GET":
        return render_template("platform/user_restore.html", user=user, previous_status=previous_status)
    if request.form.get("confirm") != "yes":
        abort(400)
    chosen_status = request.form.get("restore_status")
    if previous_status is None:
        if chosen_status not in {"active", "suspended", "blocked"}:
            flash("Escolha explicitamente o estado da conta antes de restaurá-la.", "danger")
            return render_template("platform/user_restore.html", user=user, previous_status=None), 400
        restored_status = chosen_status
    else:
        if chosen_status and chosen_status != previous_status:
            abort(400)
        restored_status = previous_status
    user.status = restored_status
    user.deleted_at = None
    user.status_before_deletion = None
    record_audit(current_user, "user_restored", "user", user.id,
                 {"status": "blocked", "deleted": True},
                 {"status": restored_status, "deleted": False})
    db.session.commit()
    flash("Usuário restaurado conforme seu estado operacional.", "success")
    return redirect(url_for("admin.users"))


def _permanent_deletion_block(user: User) -> str | None:
    if user.role == "admin" and user.is_active:
        active_admins = db.session.scalar(select(func.count()).select_from(User).where(
            User.role == "admin", User.status == "active", User.deleted_at.is_(None)
        ))
        if active_admins <= 1:
            return "O último administrador do sistema não pode ser excluído."
    if user.id == current_user.id:
        return "Você não pode excluir sua própria conta enquanto estiver autenticado."
    if db.session.scalar(select(Project.id).where(Project.owner_user_id == user.id).limit(1)):
        return "Este usuário possui projetos vinculados e não pode ser excluído permanentemente enquanto esses projetos existirem."
    if db.session.scalar(select(Analysis.id).where(Analysis.user_id == user.id).limit(1)):
        return "Este usuário possui análises vinculadas. Exclua as análises antes de remover a conta."
    if any(db.session.scalar(select(model.id).where(model.created_by_user_id == user.id).limit(1))
           for model in (QualitativeCode, QualitativeExcerpt, QualitativeCoding, QualitativeMemo, QualitativeRejection)):
        return "Este usuário possui registros de codificação qualitativa vinculados e não pode ser excluído permanentemente."
    from app import PROGRESSOS, PROGRESSOS_LOCK
    from historico_racial.routes import JOBS_LOCK, PROGRESSOS_HR

    with PROGRESSOS_LOCK, JOBS_LOCK:
        if any(item.get("owner_user_id") == user.id and item.get("status") not in {"concluido", "erro"}
               for item in PROGRESSOS.values()) or any(
                   item.get("owner_user_id") == user.id and item.get("status") not in {"concluido", "erro"}
                   for item in PROGRESSOS_HR.values()
               ):
            return "Este usuário possui processamento ativo. Aguarde a conclusão antes de excluir a conta."
    return None


@admin_bp.route("/usuarios/<uuid:user_id>/excluir-permanentemente", methods=["GET", "POST"])
@admin_only
def permanently_delete_user(user_id: UUID):
    from app import PROGRESSOS_LOCK
    from historico_racial.routes import JOBS_LOCK

    # Mesma ordem de travas da admissão dos jobs: conta, legado, documental.
    with ACCOUNT_LIFECYCLE_LOCK, PROGRESSOS_LOCK, JOBS_LOCK:
        user = db.session.get(User, str(user_id), populate_existing=True)
        if user is None:
            abort(404)
        block_reason = _permanent_deletion_block(user)
        if request.method == "GET":
            return render_template("platform/user_delete_permanent.html", user=user,
                                   block_reason=block_reason)
        if request.form.get("confirmation", "").strip() != "excluir":
            flash("Digite excluir para confirmar a exclusão permanente.", "danger")
            return render_template("platform/user_delete_permanent.html", user=user,
                                   block_reason=block_reason), 400
        if block_reason:
            flash(block_reason, "danger")
            return render_template("platform/user_delete_permanent.html", user=user,
                                   block_reason=block_reason), 409

        target_user_id = user.id
        try:
            # Não há cascade: dependências exclusivas são eliminadas explicitamente;
            # registros históricos de autoria permanecem com um snapshot mínimo.
            db.session.execute(update(AuditLog).where(AuditLog.admin_user_id == user.id).values(
                admin_user_id=None,
                admin_name_snapshot=func.coalesce(AuditLog.admin_name_snapshot, user.name),
            ))
            db.session.execute(update(AccessGrant).where(AccessGrant.granted_by_id == user.id).values(
                granted_by_id=None,
                granted_by_name_snapshot=func.coalesce(AccessGrant.granted_by_name_snapshot, user.name),
            ))
            db.session.execute(delete(UserToolOverride).where(UserToolOverride.user_id == user.id))
            db.session.execute(delete(AccessGrant).where(AccessGrant.user_id == user.id))
            db.session.execute(delete(PasswordRecoveryToken).where(PasswordRecoveryToken.user_id == user.id))
            db.session.execute(delete(UserProfile).where(UserProfile.user_id == user.id))
            db.session.execute(delete(VocabularyLibrary).where(VocabularyLibrary.owner_user_id == user.id))
            record_audit(current_user, "user_permanently_deleted", "user", user.id,
                         {"name": user.name, "role": user.role}, {"removed": True})
            db.session.execute(delete(User).where(User.id == user.id))
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            flash("Não foi possível excluir a conta com segurança. Verifique seus vínculos e tente novamente.", "danger")
            return redirect(url_for("admin.user_detail", user_id=user_id)), 409
        try:
            remove_profile_photo(target_user_id)
        except OSError:
            current_app.logger.exception("Não foi possível remover a foto da conta excluída %s.", target_user_id)
    flash("Usuário excluído permanentemente.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.get("/ferramentas")
@admin_only
def tools():
    return render_template(
        "platform/admin.html",
        section="tools",
        tools=tool_catalog(),
        visibility_groups=visibility_control_catalog(),
        visibility_standalone_tools=standalone_visibility_control_catalog(),
    )


@admin_bp.post("/visibilidade/<scope_type>/<scope_id>")
@admin_only
def set_tool_visibility(scope_type: str, scope_id: str):
    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.best == "application/json"
    )

    def respond(*, ok: bool, changed: bool, visible: bool | None, message: str, status_code: int):
        if wants_json:
            return jsonify({
                "ok": ok,
                "changed": changed,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "visible": visible,
                "message": message,
            }), status_code
        if ok:
            flash(message, "success" if changed else "info")
            return redirect(url_for("admin.tools"))
        flash(message, "danger")
        return redirect(url_for("admin.tools"))

    raw_visible = request.form.get("visible")
    if raw_visible not in {"true", "false"}:
        if wants_json:
            return respond(
                ok=False,
                changed=False,
                visible=None,
                message="Estado de visibilidade inválido.",
                status_code=400,
            )
        abort(400)
    visible = raw_visible == "true"
    try:
        changed = set_visibility_preference(
            current_user,
            scope_type=scope_type,
            scope_id=scope_id,
            visible=visible,
        )
        if changed:
            db.session.commit()
    except ValueError:
        if wants_json:
            return respond(
                ok=False,
                changed=False,
                visible=None,
                message="Configuração de visibilidade inválida.",
                status_code=400,
            )
        abort(400)
    except SQLAlchemyError:
        db.session.rollback()
        return respond(
            ok=False,
            changed=False,
            visible=None,
            message="Não foi possível atualizar a visibilidade com segurança.",
            status_code=500,
        )
    return respond(
        ok=True,
        changed=changed,
        visible=visible,
        message="Visibilidade atualizada." if changed else "A visibilidade já estava configurada dessa forma.",
        status_code=200 if wants_json else 302,
    )


@admin_bp.post("/ferramentas/<tool_id>/estado")
@admin_only
def set_tool_state(tool_id: str):
    tool = db.session.get(Tool, tool_id)
    if not tool or request.form.get("confirm") != "yes":
        abort(400)
    old = tool.active
    tool.active = not tool.active
    record_audit(current_user, "tool_state_changed", "tool", tool.id, {"active": old}, {"active": tool.active})
    db.session.commit()
    return redirect(url_for("admin.tools"))


@admin_bp.get("/planos")
@admin_only
def plans():
    persisted_plans = db.session.scalars(select(Plan).order_by(Plan.name)).all()
    return render_template(
        "platform/admin.html",
        section="plans",
        commercial_plan_groups=_commercial_plan_groups(persisted_plans),
        commercial_limit_resources=COMMERCIAL_LIMIT_RESOURCES,
    )


@admin_bp.get("/periodos-de-teste")
@admin_only
def trials():
    return render_template(
        "platform/admin.html",
        section="trials",
        commercial_trials=all_commercial_trials(),
        trial_cpf_mask_display=trial_cpf_mask_display,
        trial_state=trial_state,
    )


@admin_bp.route("/definir-acessos", methods=["GET", "POST"])
@admin_only
def define_access():
    # A matriz comercial não oferece e nem modifica vínculos de planos
    # legados que possam permanecer até a migration de remoção ser aplicada.
    plans = _persisted_commercial_plans()
    plan_ids = {plan.id for plan in plans}
    tools = tool_catalog()
    valid = {(plan.id, tool["id"]) for plan in plans for tool in tools if tool["implemented"]}
    if request.method == "POST":
        submitted = set()
        for value in request.form.getlist("access"):
            parts = value.split("|", 1)
            if len(parts) != 2 or tuple(parts) not in valid:
                abort(400)
            submitted.add(tuple(parts))
        existing = {
            (row.plan_id, row.tool_id)
            for row in db.session.scalars(select(PlanTool)).all()
            if row.plan_id in plan_ids
        }
        for plan_id, tool_id in sorted(submitted - existing):
            db.session.add(PlanTool(plan_id=plan_id, tool_id=tool_id))
            record_audit(current_user, "plan_tool_access_changed", "plan_tool", f"{plan_id}:{tool_id}",
                         {"allowed": False}, {"allowed": True})
        for plan_id, tool_id in sorted(existing - submitted):
            db.session.execute(delete(PlanTool).where(PlanTool.plan_id == plan_id, PlanTool.tool_id == tool_id))
            record_audit(current_user, "plan_tool_access_changed", "plan_tool", f"{plan_id}:{tool_id}",
                         {"allowed": True}, {"allowed": False})
        db.session.commit()
        flash("Acessos por plano atualizados.", "success")
        return redirect(url_for("admin.define_access"))
    selected = {
        (row.plan_id, row.tool_id)
        for row in db.session.scalars(select(PlanTool)).all()
        if row.plan_id in plan_ids
    }
    return render_template(
        "platform/access_matrix.html",
        plans=plans,
        plan_groups=_commercial_access_plan_groups(plans),
        tools=tools,
        selected=selected,
        tool_labels=LABEL_BY_TOOL,
    )


@admin_bp.post("/planos/<plan_id>/estado")
@admin_only
def set_plan_state(plan_id: str):
    plan = db.session.get(Plan, plan_id)
    if plan is None or commercial_plan_definition(plan.id) is None or request.form.get("confirm") != "yes":
        abort(400)
    requested = request.form.get("active")
    if requested not in {"true", "false"} or (requested == "true") == plan.active:
        abort(400)
    previous = plan.active
    plan.active = requested == "true"
    record_audit(current_user, "plan_state_changed", "plan", plan.id,
                 {"active": previous}, {"active": plan.active})
    db.session.commit()
    flash("Plano ativado." if plan.active else "Plano desativado. Concessões existentes foram preservadas.", "success")
    return redirect(url_for("admin.plans"))


@admin_bp.post("/planos/<plan_id>/limites")
@admin_only
def publish_plan_limits(plan_id: str):
    """Publica uma política versionada; não altera contratos de workspaces."""
    plan = db.session.get(Plan, plan_id)
    editable_resources = configurable_limit_resources(plan_id)
    if plan is None or not editable_resources:
        abort(404)
    try:
        values = current_plan_limits(plan)
        for resource in editable_resources:
            values[resource.resource_id] = request.form.get(resource.resource_id)
        policy, changed = publish_plan_limit_policy(
            current_user,
            plan_id,
            values,
        )
    except CommercialLimitError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("admin.plans"))
    if not changed:
        flash("Os limites informados já correspondem à política vigente; nada foi alterado.", "info")
        return redirect(url_for("admin.plans"))
    db.session.commit()
    flash(f"Política de limites publicada na versão {policy.version}.", "success")
    return redirect(url_for("admin.plans"))


def _ai_settings_snapshot(settings: AssistantAISettings) -> dict[str, object]:
    """Payload auditável sem qualquer dado de credencial."""
    return {
        "strategy": settings.strategy,
        "primary_provider": settings.primary_provider,
        "enabled_providers": list(settings.enabled_providers or []),
        "fallback_order": list(settings.fallback_order or []),
        "analysis_native_model": settings.analysis_native_model,
        "gemini_model": settings.gemini_model,
        "openai_model": settings.openai_model,
        "anthropic_model": settings.anthropic_model,
    }


@admin_bp.get("/inteligencia-artificial")
@admin_only
def assistant_ai_settings():
    manager = AIProviderManager()
    settings = manager.get_settings()
    provider_statuses = manager.provider_statuses(settings)
    provider_labels = {status["id"]: status["display_name"] for status in provider_statuses}
    fallback_summary = [provider_labels.get(provider_id, provider_id) for provider_id in (settings.fallback_order or [])]
    return render_template(
        "platform/admin.html",
        section="assistant_ai_settings",
        ai_settings=settings,
        provider_statuses=provider_statuses,
        provider_labels=provider_labels,
        fallback_summary=fallback_summary,
    )


@admin_bp.post("/inteligencia-artificial")
@admin_only
def save_assistant_ai_settings():
    manager = AIProviderManager()
    current = manager.get_settings()
    before = _ai_settings_snapshot(current)
    raw = {
        "strategy": request.form.get("strategy"),
        "primary_provider": request.form.get("primary_provider"),
        "enabled_providers": request.form.getlist("enabled_providers"),
        "fallback_order": request.form.getlist("fallback_order"),
        "analysis_native_model": request.form.get("analysis_native_model"),
        "gemini_model": request.form.get("gemini_model"),
        "openai_model": request.form.get("openai_model"),
        "anthropic_model": request.form.get("anthropic_model"),
    }
    try:
        settings = manager.save_configuration(raw)
        after = _ai_settings_snapshot(settings)
        record_audit(current_user, "assistant_ai_settings_changed", "assistant_ai_settings", "1", before, after)
        db.session.commit()
    except AISettingsValidationError as error:
        db.session.rollback()
        flash(str(error), "danger")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Não foi possível salvar a configuração de IA com segurança.", "danger")
    else:
        flash("Configuração do Assistente Análysis atualizada.", "success")
    return redirect(url_for("admin.assistant_ai_settings"))


@admin_bp.post("/inteligencia-artificial/<provider_id>/testar-conexao")
@admin_only
def test_assistant_ai_connection(provider_id: str):
    """Testa um provider e persiste somente seu estado operacional seguro."""
    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.best == "application/json"
    )

    def respond(*, ok: bool, state: str | None, message: str, status_code: int):
        if wants_json:
            return jsonify({
                "ok": ok,
                "provider_id": provider_id,
                "connection_state": state,
                "connection_status": AIProviderManager.connection_status_label(state),
                "message": message,
            }), status_code
        # Mantém o contrato visual já existente do flash, sem alterar o rótulo
        # padronizado usado pelo cartão e pelo retorno AJAX.
        flash(message if not ok or message.endswith(".") else f"{message}.", "success" if ok else "danger")
        return redirect(url_for("admin.assistant_ai_settings"))

    if provider_id not in PROVIDERS or not PROVIDERS[provider_id]["implemented"]:
        return respond(
            ok=False,
            state=None,
            message="Este provider ainda não possui integração disponível.",
            status_code=400,
        )

    manager = AIProviderManager()
    try:
        manager.test_provider_connection(provider_id)
    except AISettingsValidationError as error:
        return respond(ok=False, state=None, message=str(error), status_code=400)
    except Exception as error:
        # O provider normaliza as mensagens técnicas; nenhuma resposta externa,
        # credencial, prompt ou payload de teste é retornado ao navegador.
        from .assistant_ai_provider import AIProviderError
        state = CONNECTION_FAILED
        record_audit(
            current_user,
            CONNECTION_AUDIT_ACTION,
            "assistant_ai_provider",
            provider_id,
            after={"state": state},
        )
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.warning("assistant_ai_connection_status_persist_failed provider=%s", provider_id)
        if isinstance(error, AIProviderError):
            message, status_code = error.public_message, error.status_code
        else:
            current_app.logger.warning(
                "assistant_ai_connection_test_failed provider=%s exception_type=%s",
                provider_id,
                type(error).__name__,
            )
            message, status_code = "Não foi possível testar a conexão neste momento.", 503
        return respond(ok=False, state=state, message=message, status_code=status_code)

    state = CONNECTION_NATIVE_AVAILABLE if PROVIDERS[provider_id].get("native") else CONNECTION_AVAILABLE
    record_audit(
        current_user,
        CONNECTION_AUDIT_ACTION,
        "assistant_ai_provider",
        provider_id,
        after={"state": state},
    )
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.warning("assistant_ai_connection_status_persist_failed provider=%s", provider_id)
        return respond(
            ok=False,
            state=None,
            message="Não foi possível registrar o resultado do teste de conexão.",
            status_code=500,
        )
    message = AIProviderManager.connection_status_label(state)
    return respond(ok=True, state=state, message=message, status_code=200)


@admin_bp.get("/projetos")
@admin_only
def projects():
    page = max(1, request.args.get("page", 1, type=int))
    pagination = db.paginate(select(Project).where(
        Project.deleted_at.is_(None), Project.status != "deleted",
    ).order_by(Project.created_at.desc()), page=page, per_page=30, error_out=False)
    return render_template("platform/admin.html", section="projects", projects=pagination.items, pagination=pagination)


@admin_bp.get("/projetos/<uuid:project_id>")
@admin_only
def project_detail(project_id: UUID):
    project = db.session.get(Project, str(project_id))
    if project is None or project.deleted_at is not None or project.status == "deleted":
        abort(404)
    libraries = db.session.scalars(
        select(VocabularyLibrary).join(ProjectLibrary, ProjectLibrary.library_id == VocabularyLibrary.id)
        .where(ProjectLibrary.project_id == project.id)
    ).all()
    version = db.session.scalar(
        select(ProjectVocabularyVersion).where(ProjectVocabularyVersion.project_id == project.id,
                                               ProjectVocabularyVersion.active.is_(True))
    )
    return render_template("platform/admin.html", section="project_detail", project=project,
                           libraries=libraries, version=version)


@admin_bp.post("/projetos/<uuid:project_id>/estado")
@admin_only
def set_project_state(project_id: UUID):
    project = db.session.get(Project, str(project_id))
    status = request.form.get("status")
    if not project or request.form.get("confirm") != "yes" or status not in {"active", "blocked", "archived"}:
        abort(400)
    if project.deleted_at is not None:
        abort(400)
    old = project.status
    if old == status:
        return redirect(url_for("admin.project_detail", project_id=project.id))
    if old == "active" and status == "archived":
        archive(project, current_user)
    elif old == "archived" and status == "active":
        restore(project, current_user)
    elif old in {"active", "blocked"} and status in {"active", "blocked"}:
        project.status = status
        record_audit(current_user, "project_status_changed", "project", project.id, {"status": old}, {"status": status})
        db.session.commit()
    else:
        abort(400)
    return redirect(url_for("admin.user_detail", user_id=project.owner_user_id))


@admin_bp.route("/projetos/<uuid:project_id>/excluir", methods=["GET", "POST"])
@admin_only
def delete_project(project_id: UUID):
    project = db.session.get(Project, str(project_id))
    if project is None:
        abort(404)
    if project.status != "archived" or project.deleted_at is not None:
        abort(400)
    if request.method == "POST":
        try:
            delete_archived([project], current_user, request.form.get("confirmation", ""))
        except ProjectActionError as error:
            flash(str(error), "danger")
            return render_template("platform/project_delete.html", projects=[project],
                                   action=url_for("admin.delete_project", project_id=project.id), batch=False), 400
        owner_id = project.owner_user_id
        flash("Projeto excluído permanentemente.", "success")
        return redirect(url_for("admin.user_detail", user_id=owner_id))
    return render_template("platform/project_delete.html", projects=[project],
                           action=url_for("admin.delete_project", project_id=project.id), batch=False)


@admin_bp.get("/bibliotecas")
@admin_only
def libraries():
    linked_ids = set(db.session.scalars(select(ProjectLibrary.library_id)).all())
    return render_template("platform/admin.html", section="libraries", linked_ids=linked_ids,
                           libraries=db.session.scalars(select(VocabularyLibrary).order_by(VocabularyLibrary.created_at.desc())).all())


@admin_bp.route("/bibliotecas/nova", methods=["GET", "POST"])
@user_libraries_bp.route("/nova", methods=["GET", "POST"])
@library_editor
def new_library():
    if request.method == "POST":
        try:
            library = create_draft(request.form.get("name", ""), request.form.get("description", ""),
                                   owner_user_id=current_user.id if private_library_flow() else None)
            db.session.flush()
            record_audit(current_user, "library_created", "library", library.id, {},
                         {"status": "draft", "name": library.name})
            db.session.commit()
        except LibraryError as error:
            db.session.rollback()
            flash(str(error), "danger")
        else:
            return redirect(library_url("library_detail", library_id=library.id))
    return render_template("platform/library_new.html")


@admin_bp.get("/bibliotecas/modelo")
@user_libraries_bp.get("/modelo")
@library_editor
def library_template():
    path = Path(__file__).resolve().parent.parent / "resources" / "modelo_biblioteca.xlsx"
    if not path.is_file():
        abort(503)
    return send_file(path, as_attachment=True, download_name="modelo_biblioteca.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@admin_bp.route("/bibliotecas/importar", methods=["GET", "POST"])
@user_libraries_bp.route("/importar", methods=["GET", "POST"])
@library_editor
def library_import():
    if request.method == "GET":
        return render_template("platform/library_import.html")
    uploaded = request.files.get("spreadsheet")
    if uploaded is None or not uploaded.filename or Path(uploaded.filename).suffix.casefold() != ".xlsx":
        flash("Selecione uma planilha com extensão .xlsx.", "danger")
        return render_template("platform/library_import.html"), 400
    data = uploaded.read(MAX_XLSX_BYTES + 1)
    try:
        imported = parse_library_xlsx(data)
    except SpreadsheetImportError as error:
        flash(str(error), "danger")
        return render_template("platform/library_import.html"), 400
    token = previews.put(current_user.id, imported, request.blueprint)
    return render_template("platform/library_import.html", preview=imported, preview_token=token)


@admin_bp.post("/bibliotecas/importar/confirmar")
@user_libraries_bp.post("/importar/confirmar")
@library_editor
def library_import_confirm():
    token = request.form.get("preview_token", "")
    imported = previews.get(token, current_user.id, request.blueprint)
    if imported is None:
        flash("A pré-visualização expirou. Envie a planilha novamente.", "danger")
        return redirect(library_url("library_import"))
    try:
        library = create_imported_draft(imported.name, imported.description, imported.snapshot,
                                        owner_user_id=current_user.id if private_library_flow() else None)
        db.session.flush()
        record_audit(current_user, "library_imported", "library", library.id, {},
                     {"status": "draft", "hash": library.content_hash, "counts": library.counts_json})
        db.session.commit()
    except (LibraryError, IntegrityError) as error:
        db.session.rollback()
        flash(str(error) if isinstance(error, LibraryError) else "Já existe uma biblioteca com esse identificador.", "danger")
        return render_template("platform/library_import.html", preview=imported, preview_token=token), 400
    previews.discard(token)
    flash("Biblioteca importada como rascunho. Revise antes de publicar.", "success")
    return redirect(library_url("library_detail", library_id=library.id))


@admin_bp.get("/bibliotecas/<library_id>")
@user_libraries_bp.get("/<library_id>")
@library_editor
def library_detail(library_id: str):
    library = db.session.get(VocabularyLibrary, library_id)
    if library is None:
        abort(404)
    from historico_racial.dictionaries import carregar_categorias
    return render_template("platform/library_detail.html", library=library,
                           categories=carregar_categorias(),
                           linked=bool(db.session.scalar(select(ProjectLibrary.project_id).where(ProjectLibrary.library_id == library.id))))


@admin_bp.route("/bibliotecas/<library_id>/excluir", methods=["GET", "POST"])
@admin_only
def delete_library(library_id: str):
    library = db.session.get(VocabularyLibrary, library_id)
    if library is None:
        abort(404)
    linked = db.session.scalar(select(func.count()).select_from(ProjectLibrary).where(ProjectLibrary.library_id == library.id))
    if library.id == "relacoes_raciais":
        flash("A biblioteca padrão do sistema não pode ser excluída.", "danger")
        return redirect(library_url("library_detail", library_id=library.id))
    if linked:
        flash("Esta biblioteca não pode ser excluída porque está vinculada a um ou mais projetos.", "danger")
        return redirect(library_url("library_detail", library_id=library.id))
    if request.method == "POST":
        if request.form.get("confirmation", "").strip() != library.name or request.form.get("base_hash") != library.content_hash:
            flash("Confirmação inválida. Digite o nome exato da biblioteca.", "danger")
            return render_template("platform/library_delete.html", library=library), 400
        record_audit(current_user, "library_deleted", "library", library.id,
                     {"name": library.name, "hash": library.content_hash, "status": library.status}, {})
        db.session.delete(library)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Esta biblioteca não pode ser excluída porque está vinculada a um ou mais projetos.", "danger")
            return redirect(library_url("library_detail", library_id=library.id))
        flash("Biblioteca excluída.", "success")
        return redirect(url_for("admin.libraries"))
    return render_template("platform/library_delete.html", library=library)


def _edit_library(library_id: str, action: str, edit) -> object:
    library = db.session.get(VocabularyLibrary, library_id)
    if library is None:
        abort(404)
    before = library.content_hash
    try:
        edit(library)
        record_audit(current_user, "library_draft_changed", "library", library.id,
                     {"hash": before}, {"hash": library.content_hash, "action": action})
        db.session.commit()
    except LibraryError as error:
        db.session.rollback()
        flash(str(error), "danger")
    return redirect(library_url("library_detail", library_id=library_id))


@admin_bp.post("/bibliotecas/<library_id>/grupos")
@user_libraries_bp.post("/<library_id>/grupos")
@library_editor
def library_add_group(library_id: str):
    return _edit_library(library_id, "group_added", lambda library: add_group(
        library, request.form.get("base_hash", ""), request.form.get("name", ""),
        request.form.get("description", ""), request.form.get("active") == "on",
    ))


@admin_bp.post("/bibliotecas/<library_id>/entidades")
@user_libraries_bp.post("/<library_id>/entidades")
@library_editor
def library_add_entity(library_id: str):
    return _edit_library(library_id, "entity_added", lambda library: add_entity(
        library, request.form.get("base_hash", ""), request.form.get("canonical", ""),
        request.form.get("entity_key", ""), request.form.getlist("groups"),
        request.form.get("entity_type", ""), request.form.get("tradition", ""),
        request.form.get("region", ""), request.form.get("active") == "on",
    ))


@admin_bp.post("/bibliotecas/<library_id>/variantes")
@user_libraries_bp.post("/<library_id>/variantes")
@library_editor
def library_add_variant(library_id: str):
    return _edit_library(library_id, "variant_added", lambda library: add_variant(
        library, request.form.get("base_hash", ""), request.form.get("entity_key", ""),
        request.form.get("text", ""), request.form.get("active") == "on",
    ))


@admin_bp.post("/bibliotecas/<library_id>/itens/estado")
@user_libraries_bp.post("/<library_id>/itens/estado")
@library_editor
def library_item_state(library_id: str):
    return _edit_library(library_id, "item_state_changed", lambda library: set_item_active(
        library, request.form.get("base_hash", ""), request.form.get("kind", ""),
        request.form.get("key", ""), request.form.get("active") == "true",
        request.form.get("variant_index", ""),
    ))


@admin_bp.post("/bibliotecas/<library_id>/publicar")
@user_libraries_bp.post("/<library_id>/publicar")
@library_editor
def publish_library(library_id: str):
    library = db.session.get(VocabularyLibrary, library_id)
    if library is None:
        abort(404)
    if request.form.get("base_hash") != library.content_hash or request.form.get("confirm") != "yes":
        abort(400)
    try:
        change_publication(library, "published")
    except LibraryError as error:
        flash(str(error), "danger")
    else:
        record_audit(current_user, "library_published", "library", library.id,
                     {"status": "draft"}, {"status": "published", "hash": library.content_hash})
        db.session.commit()
        if private_library_flow():
            return redirect(url_for("projects.new_project", library_id=library.id))
    return redirect(library_url("library_detail", library_id=library.id))


@admin_bp.post("/bibliotecas/<library_id>/estado")
@admin_only
def set_library_state(library_id: str):
    library = db.session.get(VocabularyLibrary, library_id)
    if not library or request.form.get("confirm") != "yes":
        abort(400)
    old = library.status
    try:
        change_publication(library, "inactive" if library.status == "published" else "published")
    except LibraryError:
        abort(400)
    record_audit(current_user, "library_state_changed", "library", library.id,
                 {"status": old}, {"status": library.status})
    db.session.commit()
    return redirect(url_for("admin.libraries"))


@admin_bp.get("/auditoria")
@admin_only
def audit():
    page = max(1, request.args.get("page", 1, type=int))
    logs = db.paginate(select(AuditLog).order_by(AuditLog.timestamp.desc()), page=page, per_page=30, error_out=False)
    ids = {item.admin_user_id for item in logs.items if item.admin_user_id}
    admin_names = dict(db.session.execute(select(User.id, User.name).where(User.id.in_(ids))).all()) if ids else {}
    return render_template("platform/admin.html", section="audit", logs=logs, admin_names=admin_names)
