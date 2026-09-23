"""Projetos pertencem a um usuário; a autorização nunca depende só do UUID."""

from __future__ import annotations

from uuid import UUID

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from historico_racial.vocabulary import VocabularioError

from .extensions import db
from .models import Project, ProjectLibrary, ProjectVocabularyVersion, VocabularyLibrary
from .project_lifecycle import ProjectActionError, archive, delete_archived, restore
from .services import can_use_tool, get_project_for_user
from .vocabularies import create_project_vocabulary, project_store

projects_bp = Blueprint("projects", __name__)


def _require_document_tool() -> None:
    if not can_use_tool(current_user, "document_analysis"):
        abort(403)


def _summaries(projects: list[Project]) -> dict:
    summaries = {project.id: {"libraries": [], "version": None} for project in projects}
    if summaries:
        ids = tuple(summaries)
        for project_id, library_name in db.session.execute(
            select(ProjectLibrary.project_id, VocabularyLibrary.name)
            .join(VocabularyLibrary, ProjectLibrary.library_id == VocabularyLibrary.id)
            .where(ProjectLibrary.project_id.in_(ids))
        ):
            summaries[project_id]["libraries"].append(library_name)
        for project_id, version in db.session.execute(
            select(ProjectVocabularyVersion.project_id, ProjectVocabularyVersion.version)
            .where(ProjectVocabularyVersion.project_id.in_(ids), ProjectVocabularyVersion.active.is_(True))
        ):
            summaries[project_id]["version"] = version
    return summaries


@projects_bp.get("/projetos")
@login_required
def list_projects():
    _require_document_tool()
    projects = db.session.scalars(
        select(Project).where(Project.owner_user_id == current_user.id,
                              Project.status.in_(("active", "blocked")), Project.deleted_at.is_(None))
        .order_by(Project.created_at.desc())
    ).all()
    return render_template("platform/projects.html", projects=projects, summaries=_summaries(projects))


@projects_bp.get("/projetos/arquivados")
@login_required
def archived_projects():
    _require_document_tool()
    projects = db.session.scalars(
        select(Project).where(Project.owner_user_id == current_user.id, Project.status == "archived", Project.deleted_at.is_(None))
        .order_by(Project.archived_at.desc(), Project.created_at.desc())
    ).all()
    return render_template("platform/archived_projects.html", projects=projects, summaries=_summaries(projects))


@projects_bp.route("/projetos/novo", methods=["GET", "POST"])
@login_required
def new_project():
    _require_document_tool()
    libraries = db.session.scalars(
        select(VocabularyLibrary).where(VocabularyLibrary.active.is_(True), VocabularyLibrary.status == "published")
        .order_by(VocabularyLibrary.name)
    ).all()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        selected_ids = list(dict.fromkeys(request.form.getlist("libraries")))
        selected = [item for item in libraries if item.id in selected_ids]
        if not name or len(name) > 200:
            flash("Informe o nome do projeto (até 200 caracteres).", "danger")
        elif len(description) > 4000:
            flash("A descrição é muito longa.", "danger")
        elif len(selected) != len(selected_ids) or not selected:
            flash("Selecione ao menos uma biblioteca disponível.", "danger")
        else:
            project = Project(owner_user_id=current_user.id, name=name, description=description)
            db.session.add(project)
            db.session.flush()
            try:
                create_project_vocabulary(project, selected)
                db.session.commit()
            except (VocabularioError, OSError) as error:
                db.session.rollback()
                flash(str(error), "danger")
            else:
                return redirect(url_for("historico_racial.inicio_projeto", project_id=project.id))
    return render_template("platform/project_new.html", libraries=libraries)


@projects_bp.post("/projetos/<uuid:project_id>/arquivar")
@login_required
def archive_project(project_id: UUID):
    _require_document_tool()
    project = get_project_for_user(str(project_id), current_user)
    if project.owner_user_id != current_user.id:
        abort(403)
    if request.form.get("confirm") != "yes":
        abort(400)
    try:
        archive(project, current_user)
    except ProjectActionError:
        abort(400)
    flash("Projeto arquivado; dados e versões foram preservados.", "success")
    return redirect(url_for("projects.list_projects"))


@projects_bp.post("/projetos/<uuid:project_id>/desarquivar")
@login_required
def restore_project(project_id: UUID):
    _require_document_tool()
    project = get_project_for_user(str(project_id), current_user, include_inactive=True)
    if project.owner_user_id != current_user.id:
        abort(404)
    if request.form.get("confirm") != "yes":
        abort(400)
    try:
        restore(project, current_user)
    except ProjectActionError:
        abort(400)
    flash("Projeto desarquivado com todos os dados preservados.", "success")
    return redirect(url_for("projects.archived_projects"))


def _owned_archived_selection() -> list[Project]:
    ids = request.form.getlist("project_ids")
    if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
        abort(400)
    try:
        ids = [str(UUID(item)) for item in ids]
    except ValueError:
        abort(400)
    projects = db.session.scalars(select(Project).where(Project.id.in_(ids))).all()
    if len(projects) != len(ids) or any(project.owner_user_id != current_user.id for project in projects):
        abort(404)
    if any(project.status != "archived" or project.deleted_at is not None for project in projects):
        abort(400)
    return sorted(projects, key=lambda project: ids.index(project.id))


@projects_bp.route("/projetos/<uuid:project_id>/excluir", methods=["GET", "POST"])
@login_required
def delete_project(project_id: UUID):
    _require_document_tool()
    project = get_project_for_user(str(project_id), current_user, include_inactive=True)
    if project.owner_user_id != current_user.id:
        abort(404)
    if project.status != "archived" or project.deleted_at is not None:
        abort(400)
    if request.method == "POST":
        try:
            delete_archived([project], current_user, request.form.get("confirmation", ""))
        except ProjectActionError as error:
            flash(str(error), "danger")
            return render_template("platform/project_delete.html", projects=[project],
                                   action=url_for("projects.delete_project", project_id=project.id), batch=False), 400
        else:
            flash("Projeto excluído permanentemente.", "success")
            return redirect(url_for("projects.archived_projects"))
    return render_template("platform/project_delete.html", projects=[project],
                           action=url_for("projects.delete_project", project_id=project.id), batch=False)


@projects_bp.post("/projetos/arquivados/excluir/confirmar")
@login_required
def confirm_batch_delete():
    _require_document_tool()
    projects = _owned_archived_selection()
    return render_template("platform/project_delete.html", projects=projects,
                           action=url_for("projects.delete_batch"), batch=True)


@projects_bp.post("/projetos/arquivados/excluir")
@login_required
def delete_batch():
    _require_document_tool()
    projects = _owned_archived_selection()
    try:
        count = delete_archived(projects, current_user, request.form.get("confirmation", ""))
    except ProjectActionError as error:
        flash(str(error), "danger")
        return render_template("platform/project_delete.html", projects=projects,
                               action=url_for("projects.delete_batch"), batch=True), 400
    flash(f"{count} projeto(s) excluído(s) permanentemente.", "success")
    return redirect(url_for("projects.archived_projects"))
