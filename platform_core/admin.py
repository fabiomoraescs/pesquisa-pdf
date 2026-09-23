"""Administração simples, auditável e sem exclusão física de dados."""

from __future__ import annotations

from datetime import datetime, time, timezone
from functools import wraps
from uuid import UUID

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select

from .extensions import db
from .models import (
    AccessGrant, AuditLog, Plan, Project, ProjectLibrary, ProjectVocabularyVersion, Tool, User,
    UserToolOverride, VocabularyLibrary, utcnow,
)
from .project_lifecycle import ProjectActionError, archive, delete_archived, restore
from .official_libraries import LibraryError, add_entity, add_group, add_variant, change_publication, create_draft, set_item_active
from .services import current_grant, record_audit, replace_grant

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_only(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "admin" or not current_user.is_active:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


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


@admin_bp.get("/usuarios")
@admin_only
def users():
    term = request.args.get("q", "").strip()[:100]
    page = max(1, request.args.get("page", 1, type=int))
    query = select(User).order_by(User.created_at.desc())
    if term:
        query = query.where(or_(User.name.ilike(f"%{term}%"), User.email.ilike(f"%{term}%")))
    pagination = db.paginate(query, page=page, per_page=30, error_out=False)
    return render_template("platform/admin.html", section="users", users=pagination.items, pagination=pagination, q=term)


@admin_bp.get("/usuarios/<uuid:user_id>")
@admin_only
def user_detail(user_id: UUID):
    user = db.session.get(User, str(user_id))
    if user is None:
        abort(404)
    return render_template(
        "platform/admin.html", section="user_detail", user=user, grant=current_grant(user),
        grants=db.session.scalars(select(AccessGrant).where(AccessGrant.user_id == user.id).order_by(AccessGrant.created_at.desc())).all(),
        plans=db.session.scalars(select(Plan).order_by(Plan.name)).all(),
        tools=db.session.scalars(select(Tool).order_by(Tool.name)).all(),
        overrides={item.tool_id: item.decision for item in db.session.scalars(select(UserToolOverride).where(UserToolOverride.user_id == user.id))},
        projects=db.session.scalars(select(Project).where(Project.owner_user_id == user.id)).all(),
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
    user.status = status
    record_audit(current_user, "user_status_changed", "user", user.id, {"status": old}, {"status": status})
    db.session.commit()
    return redirect(url_for("admin.user_detail", user_id=user.id))


@admin_bp.post("/usuarios/<uuid:user_id>/acesso")
@admin_only
def set_user_access(user_id: UUID):
    user = db.session.get(User, str(user_id))
    plan = db.session.get(Plan, request.form.get("plan_id", ""))
    mode = request.form.get("access_mode")
    status = request.form.get("access_status")
    if not user or not plan or mode not in {"paid", "student_free", "admin_courtesy", "trial"} or status not in {"trial", "active", "payment_pending", "expired", "canceled"}:
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


@admin_bp.post("/usuarios/<uuid:user_id>/excluir")
@admin_only
def delete_user(user_id: UUID):
    user = db.session.get(User, str(user_id))
    if not user or user.id == current_user.id or request.form.get("confirm") != "yes":
        abort(400)
    user.deleted_at = utcnow()
    user.status = "blocked"
    record_audit(current_user, "user_soft_deleted", "user", user.id, {}, {"deleted": True})
    db.session.commit()
    return redirect(url_for("admin.users"))


@admin_bp.get("/ferramentas")
@admin_only
def tools():
    return render_template("platform/admin.html", section="tools", tools=db.session.scalars(select(Tool)).all())


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
    return render_template("platform/admin.html", section="plans", plans=db.session.scalars(select(Plan)).all())


@admin_bp.get("/projetos")
@admin_only
def projects():
    page = max(1, request.args.get("page", 1, type=int))
    pagination = db.paginate(select(Project).order_by(Project.created_at.desc()), page=page, per_page=30, error_out=False)
    return render_template("platform/admin.html", section="projects", projects=pagination.items, pagination=pagination)


@admin_bp.get("/projetos/<uuid:project_id>")
@admin_only
def project_detail(project_id: UUID):
    project = db.session.get(Project, str(project_id))
    if project is None:
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
    return redirect(url_for("admin.projects"))


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
        flash("Projeto excluído permanentemente.", "success")
        return redirect(url_for("admin.projects"))
    return render_template("platform/project_delete.html", projects=[project],
                           action=url_for("admin.delete_project", project_id=project.id), batch=False)


@admin_bp.get("/bibliotecas")
@admin_only
def libraries():
    return render_template("platform/admin.html", section="libraries", libraries=db.session.scalars(
        select(VocabularyLibrary).order_by(VocabularyLibrary.created_at.desc())
    ).all())


@admin_bp.route("/bibliotecas/nova", methods=["GET", "POST"])
@admin_only
def new_library():
    if request.method == "POST":
        try:
            library = create_draft(request.form.get("name", ""), request.form.get("description", ""))
            db.session.flush()
            record_audit(current_user, "library_created", "library", library.id, {},
                         {"status": "draft", "name": library.name})
            db.session.commit()
        except LibraryError as error:
            db.session.rollback()
            flash(str(error), "danger")
        else:
            return redirect(url_for("admin.library_detail", library_id=library.id))
    return render_template("platform/library_new.html")


@admin_bp.get("/bibliotecas/<library_id>")
@admin_only
def library_detail(library_id: str):
    library = db.session.get(VocabularyLibrary, library_id)
    if library is None:
        abort(404)
    from historico_racial.dictionaries import carregar_categorias
    return render_template("platform/library_detail.html", library=library,
                           categories=carregar_categorias())


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
    return redirect(url_for("admin.library_detail", library_id=library_id))


@admin_bp.post("/bibliotecas/<library_id>/grupos")
@admin_only
def library_add_group(library_id: str):
    return _edit_library(library_id, "group_added", lambda library: add_group(
        library, request.form.get("base_hash", ""), request.form.get("name", ""),
        request.form.get("description", ""), request.form.get("active") == "on",
    ))


@admin_bp.post("/bibliotecas/<library_id>/entidades")
@admin_only
def library_add_entity(library_id: str):
    return _edit_library(library_id, "entity_added", lambda library: add_entity(
        library, request.form.get("base_hash", ""), request.form.get("canonical", ""),
        request.form.get("entity_key", ""), request.form.getlist("groups"),
        request.form.get("entity_type", ""), request.form.get("tradition", ""),
        request.form.get("region", ""), request.form.get("active") == "on",
    ))


@admin_bp.post("/bibliotecas/<library_id>/variantes")
@admin_only
def library_add_variant(library_id: str):
    return _edit_library(library_id, "variant_added", lambda library: add_variant(
        library, request.form.get("base_hash", ""), request.form.get("entity_key", ""),
        request.form.get("text", ""), request.form.get("active") == "on",
    ))


@admin_bp.post("/bibliotecas/<library_id>/itens/estado")
@admin_only
def library_item_state(library_id: str):
    return _edit_library(library_id, "item_state_changed", lambda library: set_item_active(
        library, request.form.get("base_hash", ""), request.form.get("kind", ""),
        request.form.get("key", ""), request.form.get("active") == "true",
        request.form.get("variant_index", ""),
    ))


@admin_bp.post("/bibliotecas/<library_id>/publicar")
@admin_only
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
    return redirect(url_for("admin.library_detail", library_id=library.id))


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
    return render_template("platform/admin.html", section="audit", logs=logs)
