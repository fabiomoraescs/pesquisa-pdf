"""Histórico persistente e ações sobre execuções, sem reexecutar os motores."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from uuid import UUID

from flask import Blueprint, abort, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, or_, select

from .analyses import analysis_dir, delete_analysis, documents_for, get_analysis, load_result
from .extensions import db
from .models import Analysis, Project
from .scraping_types import FREE, SYSTEMATIC, TOOL_BY_TYPE, tool_for_project
from .services import can_use_tool, get_project_for_user


analyses_bp = Blueprint("analyses", __name__, url_prefix="/analises")


def _authorized(analysis_id: UUID) -> Analysis:
    analysis = get_analysis(str(analysis_id), current_user)
    if not can_use_tool(current_user, analysis.tool_id):
        abort(403)
    if analysis.project_id:
        get_project_for_user(analysis.project_id, current_user, include_inactive=True)
    return analysis


def _can_manage(analysis: Analysis) -> bool:
    if analysis.user_id == current_user.id:
        return True
    if analysis.project_id:
        project = db.session.get(Project, analysis.project_id)
        return bool(project and project.owner_user_id == current_user.id)
    return False


@analyses_bp.get("")
@login_required
def standard_history():
    if not can_use_tool(current_user, "pdf_scraper"):
        abort(403)
    items = db.session.scalars(select(Analysis).where(
        Analysis.user_id == current_user.id, Analysis.project_id.is_(None)
    ).order_by(Analysis.created_at.desc())).all()
    projects = db.session.scalars(select(Project).where(
        Project.owner_user_id == current_user.id, Project.scrape_type == FREE,
        Project.status == "active", Project.deleted_at.is_(None)
    ).order_by(Project.name)).all()
    return render_template("platform/analysis_history.html", analyses=items, project=None,
                           projects=projects)


@analyses_bp.get("/livres")
@login_required
def free_history():
    return _type_history(FREE)


@analyses_bp.get("/sistematicas")
@login_required
def systematic_history():
    return _type_history(SYSTEMATIC)


def _type_history(scrape_type: str):
    tool_id = TOOL_BY_TYPE[scrape_type]
    if not can_use_tool(current_user, tool_id):
        abort(403)
    owned_projects = db.session.scalars(select(Project).where(
        Project.owner_user_id == current_user.id, Project.scrape_type == scrape_type,
        Project.deleted_at.is_(None)
    )).all()
    owned_ids = [project.id for project in owned_projects]
    ownership = Analysis.project_id.in_(owned_ids)
    if scrape_type == FREE:
        ownership = or_(ownership, and_(Analysis.project_id.is_(None),
                                        Analysis.user_id == current_user.id))
    items = db.session.scalars(select(Analysis).where(
        Analysis.tool_id == tool_id, ownership
    ).order_by(Analysis.created_at.desc())).all()
    return render_template(
        "platform/analysis_history.html", analyses=items, project=None,
        projects=[project for project in owned_projects if project.status == "active"] if scrape_type == FREE else [],
        scope_type=scrape_type, project_names={project.id: project.name for project in owned_projects},
        manageable_project_ids=owned_ids,
    )


@analyses_bp.get("/projeto/<uuid:project_id>")
@login_required
def project_history(project_id: UUID):
    project = get_project_for_user(str(project_id), current_user, include_inactive=True)
    if not can_use_tool(current_user, tool_for_project(project)):
        abort(403)
    items = db.session.scalars(select(Analysis).where(
        Analysis.project_id == project.id
    ).order_by(Analysis.created_at.desc())).all()
    return render_template("platform/analysis_history.html", analyses=items, project=project,
                           projects=[])


@analyses_bp.get("/<uuid:analysis_id>")
@login_required
def dashboard(analysis_id: UUID):
    analysis = _authorized(analysis_id)
    if analysis.status != "concluida":
        return render_template("platform/analysis_pending.html", analysis=analysis,
                               can_manage=_can_manage(analysis), projects=[])
    if not analysis.name_confirmed and _can_manage(analysis):
        return redirect(url_for("analyses.name_base", analysis_id=analysis.id))
    if not analysis.name_confirmed:
        return render_template("platform/analysis_pending.html", analysis=analysis,
                               can_manage=False, projects=[])
    result = load_result(analysis)
    projects = db.session.scalars(select(Project).where(
        Project.owner_user_id == analysis.user_id, Project.scrape_type == FREE,
        Project.status == "active", Project.deleted_at.is_(None)
    ).order_by(Project.name)).all()
    if analysis.tool_id == "pdf_scraper":
        return render_template("resultado.html", resultado=result, identificador=analysis.id,
                               analysis=analysis, projects=projects, documents=documents_for(analysis),
                               can_manage=_can_manage(analysis))
    occurrences = result.get("ocorrencias", [])
    entities = Counter(item.get("entidade_canonica") or "Não informada" for item in occurrences)
    methods = Counter(item.get("tipo_correspondencia") or (
        "Lexical" if item.get("metodo_localizacao") == "lexical" else "Não informado"
    ) for item in occurrences)
    chart_data = {
        "entities": sorted(entities.items(), key=lambda pair: (-pair[1], pair[0])),
        "methods": sorted(methods.items(), key=lambda pair: (-pair[1], pair[0])),
    }
    return render_template("platform/analysis_dashboard.html", analysis=analysis,
                           result=result, projects=projects, documents=documents_for(analysis),
                           can_manage=_can_manage(analysis), chart_data=chart_data)


@analyses_bp.route("/<uuid:analysis_id>/nomear", methods=["GET", "POST"])
@login_required
def name_base(analysis_id: UUID):
    analysis = _authorized(analysis_id)
    if not _can_manage(analysis) or analysis.status != "concluida":
        abort(403)
    if analysis.name_confirmed:
        return redirect(url_for("analyses.dashboard", analysis_id=analysis.id))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name or len(name) > 200:
            flash("Informe um nome para a base de análise (até 200 caracteres).", "danger")
        else:
            analysis.name = name
            analysis.name_confirmed = True
            db.session.commit()
            return redirect(url_for("analyses.dashboard", analysis_id=analysis.id))
    return render_template("platform/analysis_name.html", analysis=analysis)


@analyses_bp.get("/<uuid:analysis_id>/excel/<path:filename>")
@login_required
def excel(analysis_id: UUID, filename: str):
    analysis = _authorized(analysis_id)
    if analysis.status != "concluida" or filename != Path(filename).name or filename not in analysis.excel_files_json:
        abort(404)
    return send_from_directory(analysis_dir(analysis.id), filename, as_attachment=True)


@analyses_bp.post("/<uuid:analysis_id>/renomear")
@login_required
def rename(analysis_id: UUID):
    analysis = _authorized(analysis_id)
    if not _can_manage(analysis) or analysis.status != "concluida":
        abort(403)
    name = request.form.get("name", "").strip()
    if not name or len(name) > 200:
        abort(400)
    analysis.name = name
    analysis.name_confirmed = True
    db.session.commit()
    return redirect(url_for("analyses.dashboard", analysis_id=analysis.id))


@analyses_bp.post("/<uuid:analysis_id>/mover")
@login_required
def move(analysis_id: UUID):
    analysis = _authorized(analysis_id)
    if not _can_manage(analysis) or analysis.status != "concluida" or analysis.project_id is not None:
        abort(403)
    try:
        project_id = str(UUID(request.form.get("project_id", "")))
    except ValueError:
        abort(400)
    project = get_project_for_user(project_id, current_user)
    if project.owner_user_id != analysis.user_id or project.scrape_type != FREE:
        abort(403)
    analysis.project_id = project.id
    analysis.source_type = "project"
    db.session.commit()
    flash("Base de análise movida para o projeto. Resultados e planilhas foram preservados.", "success")
    return redirect(url_for("analyses.dashboard", analysis_id=analysis.id))


@analyses_bp.post("/<uuid:analysis_id>/excluir")
@login_required
def delete(analysis_id: UUID):
    analysis = _authorized(analysis_id)
    if not _can_manage(analysis) or request.form.get("confirm") != "yes":
        abort(403)
    if analysis.status == "processando":
        abort(409)
    project_id = analysis.project_id
    tool_id = analysis.tool_id
    delete_analysis(analysis)
    if tool_id == "pdf_scraper":
        from app import ANALISES, ANALISES_LOCK, PROGRESSOS, PROGRESSOS_LOCK
        with ANALISES_LOCK, PROGRESSOS_LOCK:
            ANALISES.pop(str(analysis_id), None)
            PROGRESSOS.pop(str(analysis_id), None)
    else:
        from historico_racial.routes import JOBS_LOCK, RESULTADOS_HR, PROGRESSOS_HR
        with JOBS_LOCK:
            RESULTADOS_HR.pop(str(analysis_id), None)
            PROGRESSOS_HR.pop(str(analysis_id), None)
    flash("Base de análise excluída permanentemente.", "success")
    if project_id:
        return redirect(url_for("analyses.project_history", project_id=project_id))
    return redirect(url_for("analyses.standard_history"))


@analyses_bp.get("/<uuid:analysis_id>/duplicar")
@login_required
def duplicate(analysis_id: UUID):
    analysis = _authorized(analysis_id)
    if not _can_manage(analysis) or analysis.status != "concluida":
        abort(403)
    if analysis.tool_id == "pdf_scraper":
        return redirect(url_for("inicio", duplicate=str(analysis_id)))
    if analysis.project_id:
        return redirect(url_for("historico_racial.inicio_projeto", project_id=analysis.project_id,
                                duplicate=str(analysis_id)))
    abort(409)
