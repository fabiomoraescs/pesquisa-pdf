"""Fluxo mínimo de projetos e importação inicial do Análysis Instagram."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from uuid import UUID

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select

from .extensions import db
from .instagram_import import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_ROWS,
    IMPORT_FIELDS,
    InstagramImportError,
    build_preview,
    parse_instagram_import,
)
from .instagram_previews import InstagramPreviewExpired, previews
from .instagram_storage import (
    InstagramImportPersistenceError,
    account_conflict,
    persist_instagram_import,
)
from .models import AnalyticsProject, AnalyticsRun, InstagramMedia
from .services import ANALYTICS_INSTAGRAM_TOOL, can_use_tool


instagram_bp = Blueprint("instagram", __name__, url_prefix="/analytics/instagram")


def _require_tool() -> None:
    if not can_use_tool(current_user, ANALYTICS_INSTAGRAM_TOOL):
        abort(403)


def _project_for_current_user(project_id: UUID, *, active: bool = False) -> AnalyticsProject:
    _require_tool()
    query = select(AnalyticsProject).where(
        AnalyticsProject.id == str(project_id),
        AnalyticsProject.owner_user_id == current_user.id,
        AnalyticsProject.module_key == "instagram",
    )
    if active:
        query = query.where(AnalyticsProject.status == "active", AnalyticsProject.archived_at.is_(None))
    project = db.session.scalar(query)
    if project is None:
        abort(404)
    return project


@instagram_bp.get("")
@login_required
def home():
    """Lista somente os projetos Instagram pertencentes ao usuário atual."""
    _require_tool()
    projects = db.session.scalars(
        select(AnalyticsProject).where(
            AnalyticsProject.owner_user_id == current_user.id,
            AnalyticsProject.module_key == "instagram",
            AnalyticsProject.status == "active",
            AnalyticsProject.archived_at.is_(None),
        ).order_by(AnalyticsProject.updated_at.desc())
    ).all()
    return render_template("platform/instagram/index.html", projects=projects)


@instagram_bp.get("/projects/archived")
@login_required
def archived_projects():
    _require_tool()
    projects = db.session.scalars(
        select(AnalyticsProject).where(
            AnalyticsProject.owner_user_id == current_user.id,
            AnalyticsProject.module_key == "instagram",
            AnalyticsProject.status == "archived",
            AnalyticsProject.archived_at.is_not(None),
        ).order_by(AnalyticsProject.archived_at.desc())
    ).all()
    return render_template("platform/instagram/archived_projects.html", projects=projects)


@instagram_bp.post("/projects/<uuid:project_id>/archive")
@login_required
def archive_project(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    if request.form.get("confirm") != "yes":
        abort(400)
    project.status = "archived"
    project.archived_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Projeto arquivado. O histórico foi preservado.", "success")
    return redirect(url_for("instagram.archived_projects"))


@instagram_bp.post("/projects/<uuid:project_id>/restore")
@login_required
def restore_project(project_id: UUID):
    project = _project_for_current_user(project_id)
    if project.status != "archived" or project.archived_at is None:
        abort(404)
    if request.form.get("confirm") != "yes":
        abort(400)
    project.status = "active"
    project.archived_at = None
    db.session.commit()
    flash("Projeto restaurado.", "success")
    return redirect(url_for("instagram.home"))


@instagram_bp.get("/projects/new")
@login_required
def new_project():
    _require_tool()
    return render_template("platform/instagram/project_new.html")


@instagram_bp.post("/projects")
@login_required
def create_project():
    _require_tool()
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name or len(name) > 200:
        flash("Informe o nome do projeto (até 200 caracteres).", "danger")
        return render_template("platform/instagram/project_new.html"), 400
    if len(description) > 4_000:
        flash("A descrição excede o limite de 4.000 caracteres.", "danger")
        return render_template("platform/instagram/project_new.html"), 400
    project = AnalyticsProject(
        owner_user_id=current_user.id,
        module_key="instagram",
        name=name,
        description=description,
    )
    db.session.add(project)
    db.session.commit()
    flash("Projeto Instagram criado.", "success")
    return redirect(url_for("instagram.project_detail", project_id=project.id))


@instagram_bp.get("/projects/<uuid:project_id>")
@login_required
def project_detail(project_id: UUID):
    project = _project_for_current_user(project_id)
    account = project.instagram_account
    runs = db.session.scalars(
        select(AnalyticsRun).where(AnalyticsRun.analytics_project_id == project.id)
        .order_by(AnalyticsRun.completed_at.desc(), AnalyticsRun.started_at.desc())
    ).all()
    return render_template(
        "platform/instagram/project_detail.html",
        project=project,
        account=account,
        runs=runs,
        run_count=len(runs),
        last_run=runs[0] if runs else None,
    )


@instagram_bp.get("/projects/<uuid:project_id>/imports/new")
@login_required
def new_import(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    return render_template("platform/instagram/import_new.html", project=project, import_fields=IMPORT_FIELDS)


@instagram_bp.get("/projects/<uuid:project_id>/imports/modelo")
@login_required
def import_template(project_id: UUID):
    _project_for_current_user(project_id, active=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Dados Instagram"
    for index, field in enumerate(IMPORT_FIELDS, start=1):
        cell = sheet.cell(1, index, field.name)
        cell.fill = PatternFill("solid", fgColor="184775" if field.required else "52677A")
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.comment = Comment(field.note, "Análysis")
        sheet.column_dimensions[get_column_letter(index)].width = min(34, max(19, len(field.name) + 3))
        if field.kind == "datetime":
            for row in range(2, MAX_IMPORT_ROWS + 2):
                sheet.cell(row, index).number_format = "@"
    sheet.row_dimensions[1].height = 31
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(IMPORT_FIELDS))}1"
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="modelo_importacao_instagram_analysis.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@instagram_bp.post("/projects/<uuid:project_id>/imports/preview")
@login_required
def preview_import(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    uploaded = request.files.get("dataset")
    if uploaded is None or not uploaded.filename:
        flash("Selecione um arquivo CSV ou XLSX.", "danger")
        return render_template("platform/instagram/import_new.html", project=project, import_fields=IMPORT_FIELDS), 400
    data = uploaded.read(MAX_IMPORT_BYTES + 1)
    try:
        dataset = parse_instagram_import(uploaded.filename, data)
        conflict = account_conflict(project, dataset)
        if conflict:
            raise InstagramImportError(conflict)
        known_media_external_ids = set()
        if project.instagram_account is not None:
            known_media_external_ids = set(db.session.scalars(
                select(InstagramMedia.external_id).where(
                    InstagramMedia.instagram_account_id == project.instagram_account.id,
                    InstagramMedia.external_id.is_not(None),
                )
            ))
        repeated_source_hash = db.session.scalar(
            select(AnalyticsRun.id).where(
                AnalyticsRun.analytics_project_id == project.id,
                AnalyticsRun.source_hash == dataset.source_hash,
            ).limit(1)
        ) is not None
        preview = build_preview(
            dataset,
            known_media_external_ids=known_media_external_ids,
            repeated_source_hash=repeated_source_hash,
        )
    except InstagramImportError as error:
        flash(str(error), "danger")
        return render_template("platform/instagram/import_new.html", project=project, import_fields=IMPORT_FIELDS), 400
    try:
        token = previews.put(current_user.id, project.id, preview)
    except OSError:
        current_app.logger.exception("Não foi possível guardar a prévia Instagram")
        flash("Não foi possível preparar a prévia. Tente enviar o arquivo novamente.", "danger")
        return render_template("platform/instagram/import_new.html", project=project, import_fields=IMPORT_FIELDS), 500
    return render_template("platform/instagram/import_preview.html", project=project, preview=preview, token=token)


@instagram_bp.post("/projects/<uuid:project_id>/imports/confirm")
@login_required
def confirm_import(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    token = request.form.get("preview_token", "")
    try:
        claim = previews.claim(token, current_user.id, project.id)
    except InstagramPreviewExpired:
        flash("A prévia expirou. Envie o arquivo novamente.", "warning")
        return redirect(url_for("instagram.new_import", project_id=project.id))
    if claim is None:
        flash("Prévia indisponível. Envie o arquivo novamente.", "warning")
        return redirect(url_for("instagram.new_import", project_id=project.id))
    preview = claim.preview
    try:
        result = persist_instagram_import(project, preview.dataset)
    except InstagramImportPersistenceError as error:
        claim.release()
        flash(str(error), "danger")
        return render_template("platform/instagram/import_preview.html", project=project, preview=preview, token=token), 409
    except Exception:
        claim.release()
        flash("Não foi possível concluir a importação. Nenhum dado foi salvo.", "danger")
        return render_template("platform/instagram/import_preview.html", project=project, preview=preview, token=token), 500
    try:
        claim.complete()
    except OSError:
        # O .claim permanece inacessível: uma falha de limpeza não permite novo run.
        current_app.logger.exception("Importação concluída; falha ao remover prévia consumida")
    return render_template("platform/instagram/import_complete.html", project=project, result=result, preview=preview)


@instagram_bp.post("/projects/<uuid:project_id>/imports/cancel")
@login_required
def cancel_import(project_id: UUID):
    project = _project_for_current_user(project_id)
    token = request.form.get("preview_token", "")
    previews.discard(token, current_user.id, project.id)
    return redirect(url_for("instagram.project_detail", project_id=project.id))
