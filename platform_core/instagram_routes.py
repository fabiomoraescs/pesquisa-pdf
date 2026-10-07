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
from .instagram_comment_import import (
    COMMENT_IMPORT_FIELDS,
    build_comment_preview,
    parse_instagram_comment_import,
)
from .instagram_comment_previews import (
    InstagramCommentPreviewExpired,
    comment_previews,
)
from .instagram_comment_storage import (
    InstagramCommentImportPersistenceError,
    comment_import_conflict,
    persist_instagram_comment_import,
)
from .instagram_comments import (
    ASSESSMENT_SOURCE_LABELS,
    INTENSITY_LABELS,
    INTENT_LABELS,
    RISK_LABELS,
    SENTIMENT_LABELS,
    CommentAssessmentValidationError,
    build_instagram_comments_dashboard,
    classify_comment,
    current_comment_observation,
    current_text_version,
    effective_assessment,
)
from .instagram_comment_classification import (
    COMMENT_CLASSIFIER_LABELS,
    CommentClassificationError,
    CommentClassificationPersistenceError,
    LOCAL_COMMENT_CLASSIFIER_LABEL,
    classify_project_comments,
    get_comment_classifier,
    plan_comment_classification,
)
from .instagram_lifecycle import (
    InstagramProjectDeletionError,
    delete_instagram_project_permanently,
)
from .instagram_insights import (
    InstagramInsightsError,
    InstagramInsightsPersistenceError,
    evidence_views,
    finding_for_project,
    generate_insights,
    grouped_findings,
    latest_insight_run,
)
from .instagram_previews import InstagramPreviewExpired, previews
from .instagram_dashboard import PERIOD_DAYS, TABLE_SORTS, build_instagram_dashboard
from .instagram_taxonomy import (
    CLASSIFICATION_STATUSES,
    DEFAULT_TAXONOMY_VERSION,
    TaxonomyValidationError,
    classification_states,
    classify_media,
    create_category,
    create_dimension,
    create_initial_taxonomy,
    current_classifications,
    set_category_active,
    set_dimension_active,
    taxonomy_dimensions,
)
from .instagram_storage import (
    InstagramImportPersistenceError,
    account_conflict,
    persist_instagram_import,
)
from .models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramComment,
    InstagramCommentTextVersion,
    InstagramMedia,
    InstagramTaxonomyCategory,
    InstagramTaxonomyDimension,
)
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
    return redirect(url_for("instagram.home", _anchor="instagram-project-list"))


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
    return redirect(url_for("instagram.archived_projects", _anchor="instagram-archived-project-list"))


@instagram_bp.route("/projects/<uuid:project_id>/delete", methods=("GET", "POST"))
@login_required
def delete_project(project_id: UUID):
    project = _project_for_current_user(project_id)
    if project.status != "archived" or project.archived_at is None:
        abort(400)
    if request.method == "POST":
        try:
            delete_instagram_project_permanently(
                project, current_user, request.form.get("confirmation", ""),
            )
        except InstagramProjectDeletionError as error:
            flash(str(error), "danger")
            return render_template(
                "platform/instagram/project_delete.html", project=project,
            ), 400
        except Exception:
            current_app.logger.exception("Falha ao excluir projeto Instagram permanentemente")
            db.session.rollback()
            flash("Não foi possível excluir o projeto permanentemente. Nenhum dado foi removido.", "danger")
            return render_template(
                "platform/instagram/project_delete.html", project=project,
            ), 500
        flash("Projeto excluído permanentemente.", "success")
        return redirect(url_for("instagram.archived_projects"))
    return render_template("platform/instagram/project_delete.html", project=project)


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


@instagram_bp.get("/projects/<uuid:project_id>/dashboard")
@login_required
def dashboard(project_id: UUID):
    project = _project_for_current_user(project_id)
    period = request.args.get("period", "30d")
    sort = request.args.get("sort", "recent")
    if period not in PERIOD_DAYS or sort not in TABLE_SORTS:
        abort(400)
    try:
        summary = build_instagram_dashboard(
            project, period, current_app.config["PRESENTATION_TIMEZONE"], sort=sort,
            taxonomy_dimension_id=request.args.get("taxonomy_dimension") or None,
        )
    except ValueError:
        abort(400)
    return render_template(
        "platform/instagram/dashboard.html", project=project, account=project.instagram_account,
        dashboard=summary,
        period_options=(("7d", "7 dias"), ("30d", "30 dias"), ("90d", "90 dias"),
                        ("all", "Todo o histórico")),
        sort_options=(("recent", "Mais recentes"), ("reach", "Maior alcance"),
                      ("interactions", "Mais interações"),
                      ("interaction_rate", "Maior taxa de interação por alcance")),
    )


@instagram_bp.get("/projects/<uuid:project_id>/insights")
@login_required
def insights(project_id: UUID):
    """Consulta a última execução explícita de achados para um recorte."""
    project = _project_for_current_user(project_id)
    period = request.args.get("period", "30d")
    if period not in PERIOD_DAYS:
        abort(400)
    run = latest_insight_run(project, period)
    period_options = (("7d", "7 dias"), ("30d", "30 dias"), ("90d", "90 dias"),
                      ("all", "Todo o histórico"))
    return render_template(
        "platform/instagram/insights.html",
        project=project,
        insight_run=run,
        findings_by_group=grouped_findings(run.findings) if run else {},
        period=period, period_options=period_options, period_label=dict(period_options)[period],
        can_generate=project.status == "active" and project.archived_at is None,
    )


@instagram_bp.post("/projects/<uuid:project_id>/insights/generate")
@login_required
def generate_project_insights(project_id: UUID):
    """Cria um run analítico somente mediante ação protegida por CSRF."""
    project = _project_for_current_user(project_id, active=True)
    period = request.form.get("period", "30d")
    if period not in PERIOD_DAYS:
        abort(400)
    try:
        run = generate_insights(project, period, current_app.config["PRESENTATION_TIMEZONE"])
    except InstagramInsightsError as error:
        flash(str(error), "danger")
    except InstagramInsightsPersistenceError:
        current_app.logger.exception("Falha ao gerar insights do Instagram")
        flash("Não foi possível gerar os insights. Nenhuma execução parcial foi gravada.", "danger")
    else:
        flash(f"Insights gerados: {run.finding_count} achado(s) descritivo(s).", "success")
    return redirect(url_for("instagram.insights", project_id=project.id, period=period,
                            _anchor="instagram-insights-content"))


@instagram_bp.get("/projects/<uuid:project_id>/insights/<uuid:finding_id>")
@login_required
def insight_detail(project_id: UUID, finding_id: UUID):
    project = _project_for_current_user(project_id)
    finding = finding_for_project(project, str(finding_id))
    if finding is None:
        abort(404)
    return render_template(
        "platform/instagram/insight_detail.html",
        project=project,
        finding=finding,
        insight_run=finding.insight_run,
        evidence=evidence_views(project, finding),
    )


def _taxonomy_dimension_anchor(dimension_id: UUID | str) -> str:
    return f"instagram-taxonomy-dimension-{dimension_id}"


def _taxonomy_category_anchor(project: AnalyticsProject, category_id: UUID) -> str:
    dimension_id = db.session.scalar(
        select(InstagramTaxonomyCategory.instagram_taxonomy_dimension_id)
        .join(InstagramTaxonomyDimension)
        .where(
            InstagramTaxonomyCategory.id == str(category_id),
            InstagramTaxonomyDimension.analytics_project_id == project.id,
        )
    )
    return _taxonomy_dimension_anchor(dimension_id) if dimension_id else "instagram-taxonomy-dimensions"


def _taxonomy_error_redirect(
    project: AnalyticsProject, error: TaxonomyValidationError, *, anchor: str = "instagram-taxonomy-dimensions",
):
    flash(str(error), "danger")
    return redirect(url_for("instagram.taxonomies", project_id=project.id, _anchor=anchor))


def _classification_media(project: AnalyticsProject) -> tuple[InstagramMedia, ...]:
    return tuple(db.session.scalars(
        select(InstagramMedia)
        .join(InstagramAccount, InstagramAccount.id == InstagramMedia.instagram_account_id)
        .where(InstagramAccount.analytics_project_id == project.id)
        .order_by(InstagramMedia.published_at.desc(), InstagramMedia.id.desc())
    ).all())


def _classification_media_for_project(project: AnalyticsProject, media_id: UUID) -> InstagramMedia:
    media = db.session.scalar(
        select(InstagramMedia)
        .join(InstagramAccount, InstagramAccount.id == InstagramMedia.instagram_account_id)
        .where(
            InstagramMedia.id == str(media_id),
            InstagramAccount.analytics_project_id == project.id,
        )
    )
    if media is None:
        abort(404)
    return media


@instagram_bp.get("/projects/<uuid:project_id>/taxonomies")
@login_required
def taxonomies(project_id: UUID):
    project = _project_for_current_user(project_id)
    return render_template(
        "platform/instagram/taxonomies.html",
        project=project,
        dimensions=taxonomy_dimensions(project.id),
        can_mutate=project.status == "active" and project.archived_at is None,
        taxonomy_version=DEFAULT_TAXONOMY_VERSION,
    )


@instagram_bp.post("/projects/<uuid:project_id>/taxonomies/initial")
@login_required
def initialize_taxonomy(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    create_initial_taxonomy(project)
    db.session.commit()
    flash("Estrutura inicial de taxonomias criada.", "success")
    return redirect(url_for("instagram.taxonomies", project_id=project.id, _anchor="instagram-taxonomy-dimensions"))


@instagram_bp.post("/projects/<uuid:project_id>/taxonomies/dimensions")
@login_required
def add_taxonomy_dimension(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    try:
        create_dimension(
            project,
            name=request.form.get("name", ""),
            key=request.form.get("key") or None,
            description=request.form.get("description") or None,
            selection_mode=request.form.get("selection_mode", ""),
            position=request.form.get("position"),
        )
        db.session.commit()
    except TaxonomyValidationError as error:
        db.session.rollback()
        return _taxonomy_error_redirect(project, error, anchor="instagram-new-dimension")
    flash("Dimensão criada.", "success")
    return redirect(url_for("instagram.taxonomies", project_id=project.id, _anchor="instagram-taxonomy-dimensions"))


@instagram_bp.post("/projects/<uuid:project_id>/taxonomies/dimensions/<uuid:dimension_id>/categories")
@login_required
def add_taxonomy_category(project_id: UUID, dimension_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    try:
        create_category(
            project, str(dimension_id),
            name=request.form.get("name", ""),
            key=request.form.get("key") or None,
            description=request.form.get("description") or None,
            position=request.form.get("position"),
        )
        db.session.commit()
    except TaxonomyValidationError as error:
        db.session.rollback()
        return _taxonomy_error_redirect(project, error, anchor=_taxonomy_dimension_anchor(dimension_id))
    flash("Categoria criada.", "success")
    return redirect(url_for(
        "instagram.taxonomies", project_id=project.id, _anchor=_taxonomy_dimension_anchor(dimension_id),
    ))


def _active_form_value() -> bool:
    value = request.form.get("active")
    if value == "true":
        return True
    if value == "false":
        return False
    abort(400)


@instagram_bp.post("/projects/<uuid:project_id>/taxonomies/dimensions/<uuid:dimension_id>/state")
@login_required
def set_taxonomy_dimension_state(project_id: UUID, dimension_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    try:
        set_dimension_active(project, str(dimension_id), active=_active_form_value())
        db.session.commit()
    except TaxonomyValidationError as error:
        db.session.rollback()
        return _taxonomy_error_redirect(project, error, anchor=_taxonomy_dimension_anchor(dimension_id))
    flash("Estado da dimensão atualizado.", "success")
    return redirect(url_for(
        "instagram.taxonomies", project_id=project.id, _anchor=_taxonomy_dimension_anchor(dimension_id),
    ))


@instagram_bp.post("/projects/<uuid:project_id>/taxonomies/categories/<uuid:category_id>/state")
@login_required
def set_taxonomy_category_state(project_id: UUID, category_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    anchor = _taxonomy_category_anchor(project, category_id)
    try:
        set_category_active(project, str(category_id), active=_active_form_value())
        db.session.commit()
    except TaxonomyValidationError as error:
        db.session.rollback()
        return _taxonomy_error_redirect(project, error, anchor=anchor)
    flash("Estado da categoria atualizado.", "success")
    return redirect(url_for("instagram.taxonomies", project_id=project.id, _anchor=anchor))


@instagram_bp.get("/projects/<uuid:project_id>/classifications")
@login_required
def classifications(project_id: UUID):
    project = _project_for_current_user(project_id)
    status_filter = request.args.get("status", "all")
    if status_filter not in CLASSIFICATION_STATUSES:
        abort(400)
    media = _classification_media(project)
    states = classification_states(project, tuple(item.id for item in media))
    if status_filter != "all":
        media = tuple(item for item in media if states[item.id].status == status_filter)
    return render_template(
        "platform/instagram/classifications.html",
        project=project,
        media=media,
        states=states,
        status_filter=status_filter,
        can_mutate=project.status == "active" and project.archived_at is None,
    )


@instagram_bp.route("/projects/<uuid:project_id>/classifications/<uuid:media_id>", methods=("GET", "POST"))
@login_required
def edit_classification(project_id: UUID, media_id: UUID):
    project = _project_for_current_user(project_id, active=request.method == "POST")
    media = _classification_media_for_project(project, media_id)
    dimensions = taxonomy_dimensions(project.id, active_only=True)
    if request.method == "POST":
        selections = {
            dimension.id: request.form.getlist(f"dimension_{dimension.id}")
            for dimension in dimensions
        }
        try:
            classify_media(project, media.id, selections, current_user)
            db.session.commit()
        except TaxonomyValidationError as error:
            db.session.rollback()
            flash(str(error), "danger")
        else:
            flash("Classificação atualizada. O histórico anterior foi preservado.", "success")
            return redirect(url_for(
                "instagram.classifications",
                project_id=project.id,
                _anchor=f"instagram-classification-{media.id}",
            ))
    current = current_classifications(project.id, media_ids=(media.id,))
    current_category_ids = {item.instagram_taxonomy_category_id for item in current}
    return render_template(
        "platform/instagram/classification_edit.html",
        project=project,
        media=media,
        dimensions=dimensions,
        current=current,
        current_category_ids=current_category_ids,
        can_mutate=project.status == "active" and project.archived_at is None,
    )


def _comment_for_project(project: AnalyticsProject, comment_id: UUID) -> InstagramComment:
    comment = db.session.scalar(
        select(InstagramComment)
        .join(InstagramMedia, InstagramMedia.id == InstagramComment.instagram_media_id)
        .join(InstagramAccount, InstagramAccount.id == InstagramMedia.instagram_account_id)
        .where(
            InstagramComment.id == str(comment_id),
            InstagramAccount.analytics_project_id == project.id,
        )
    )
    if comment is None:
        abort(404)
    # As relações são consumidas por funções centrais de estado e pelo
    # histórico no template. Carregá-las explicitamente evita queries por linha.
    _ = comment.instagram_media, comment.text_versions, comment.observations, comment.assessments, comment.replies
    return comment


@instagram_bp.get("/projects/<uuid:project_id>/comments")
@login_required
def comments(project_id: UUID):
    project = _project_for_current_user(project_id)
    filters = {
        "period": request.args.get("period", "30d"),
        "sort": request.args.get("sort", "recent"),
        "status": request.args.get("status", "all"),
        "sentiment": request.args.get("sentiment", "all"),
        "needs_response": request.args.get("needs_response", "all"),
        "risk": request.args.get("risk", "all"),
    }
    try:
        summary = build_instagram_comments_dashboard(project, **filters)
    except ValueError:
        abort(400)
    return render_template(
        "platform/instagram/comments.html",
        project=project,
        comments_dashboard=summary,
        filters=filters,
        period_options=(("7d", "7 dias"), ("30d", "30 dias"), ("90d", "90 dias"),
                        ("all", "Todo o histórico")),
        sentiment_labels=SENTIMENT_LABELS,
        intent_labels=INTENT_LABELS,
        risk_labels=RISK_LABELS,
        can_mutate=project.status == "active" and project.archived_at is None,
    )


@instagram_bp.get("/projects/<uuid:project_id>/comments/classify")
@login_required
def preview_comment_classification(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    classifier = get_comment_classifier()
    try:
        summary = plan_comment_classification(project, classifier)
    except CommentClassificationError as error:
        flash(str(error), "danger")
        return redirect(url_for("instagram.comments", project_id=project.id, _anchor="instagram-comments-content"))
    return render_template(
        "platform/instagram/comment_classification_preview.html",
        project=project,
        classifier=classifier,
        classifier_label=LOCAL_COMMENT_CLASSIFIER_LABEL,
        summary=summary,
    )


@instagram_bp.post("/projects/<uuid:project_id>/comments/classify")
@login_required
def confirm_comment_classification(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    reprocess = request.form.get("reprocess") == "yes"
    try:
        summary = classify_project_comments(project, reprocess=reprocess)
    except CommentClassificationError as error:
        flash(str(error), "danger")
    except CommentClassificationPersistenceError:
        current_app.logger.exception("Falha ao persistir classificação automática de comentários")
        flash("Não foi possível concluir a classificação automática. Nenhuma avaliação foi gravada.", "danger")
    else:
        if summary.classified:
            flash(
                "Classificação automática concluída: "
                f"{summary.eligible} elegível(is), {summary.classified} classificado(s), "
                f"{summary.skipped} ignorado(s), {summary.already_classified} já classificado(s), "
                f"{summary.human_reviewed} com revisão humana e {summary.errors} erro(s).",
                "success",
            )
        elif summary.already_classified:
            flash("Nenhum comentário novo para classificar; os elegíveis já possuem esta versão do classificador.", "info")
        elif summary.skipped or summary.errors:
            flash(
                "Nenhuma avaliação foi criada: "
                f"{summary.skipped} comentário(s) ignorado(s) e {summary.errors} erro(s).",
                "info",
            )
        else:
            flash("Nenhum comentário disponível para classificação automática.", "info")
        for message in summary.error_messages[:3]:
            flash(message, "warning")
    return redirect(url_for("instagram.comments", project_id=project.id, _anchor="instagram-comments-content"))


@instagram_bp.route("/projects/<uuid:project_id>/comments/<uuid:comment_id>", methods=("GET", "POST"))
@login_required
def comment_detail(project_id: UUID, comment_id: UUID):
    project = _project_for_current_user(project_id, active=request.method == "POST")
    comment = _comment_for_project(project, comment_id)
    if request.method == "POST":
        try:
            classify_comment(comment, current_user.id, {
                "sentiment": request.form.get("sentiment"),
                "intensity": request.form.get("intensity"),
                "intent": request.form.get("intent"),
                "topic": request.form.get("topic"),
                "topic_normalized": request.form.get("topic_normalized"),
                "needs_response": request.form.get("needs_response"),
                "risk": request.form.get("risk"),
                "rationale": request.form.get("rationale"),
            })
            db.session.commit()
        except CommentAssessmentValidationError as error:
            db.session.rollback()
            flash(str(error), "danger")
        else:
            flash("Classificação do comentário atualizada. O histórico anterior foi preservado.", "success")
            return redirect(url_for(
                "instagram.comment_detail", project_id=project.id, comment_id=comment.id,
                _anchor="instagram-comment-classification",
            ))
    assessment = effective_assessment(comment.assessments)
    return render_template(
        "platform/instagram/comment_detail.html",
        project=project,
        comment=comment,
        media=comment.instagram_media,
        text_version=current_text_version(comment.text_versions),
        observation=current_comment_observation(comment.observations),
        assessment=assessment,
        history=sorted(comment.assessments, key=lambda item: (item.created_at, item.id), reverse=True),
        text_versions=sorted(comment.text_versions, key=lambda item: (item.first_observed_at, item.id), reverse=True),
        sentiment_labels=SENTIMENT_LABELS,
        intensity_labels=INTENSITY_LABELS,
        intent_labels=INTENT_LABELS,
        risk_labels=RISK_LABELS,
        assessment_source_labels=ASSESSMENT_SOURCE_LABELS,
        classifier_labels=COMMENT_CLASSIFIER_LABELS,
        can_mutate=project.status == "active" and project.archived_at is None,
    )


@instagram_bp.get("/projects/<uuid:project_id>/comments/imports/new")
@login_required
def new_comment_import(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    return render_template(
        "platform/instagram/comment_import_new.html", project=project, import_fields=COMMENT_IMPORT_FIELDS,
    )


@instagram_bp.get("/projects/<uuid:project_id>/comments/imports/modelo")
@login_required
def comment_import_template(project_id: UUID):
    _project_for_current_user(project_id, active=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Comentários Instagram"
    for index, field in enumerate(COMMENT_IMPORT_FIELDS, start=1):
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
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(COMMENT_IMPORT_FIELDS))}1"
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return send_file(
        output, as_attachment=True, download_name="modelo_importacao_comentarios_instagram_analysis.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _comment_known_hashes(project: AnalyticsProject) -> dict[tuple[str, str], set[str]]:
    account = project.instagram_account
    if account is None:
        return {}
    comments = db.session.execute(
        select(InstagramComment, InstagramMedia.external_id)
        .join(InstagramMedia, InstagramMedia.id == InstagramComment.instagram_media_id)
        .where(InstagramMedia.instagram_account_id == account.id)
    ).all()
    hashes = {(media_external_id, comment.external_id): set() for comment, media_external_id in comments}
    key_by_comment_id = {
        comment.id: (media_external_id, comment.external_id)
        for comment, media_external_id in comments
    }
    if comments:
        ids = list(key_by_comment_id)
        for version in db.session.scalars(select(InstagramCommentTextVersion).where(
            InstagramCommentTextVersion.instagram_comment_id.in_(ids)
        )):
            hashes[key_by_comment_id[version.instagram_comment_id]].add(version.content_hash)
    return hashes


@instagram_bp.post("/projects/<uuid:project_id>/comments/imports/preview")
@login_required
def preview_comment_import(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    uploaded = request.files.get("dataset")
    if uploaded is None or not uploaded.filename:
        flash("Selecione um arquivo CSV ou XLSX.", "danger")
        return render_template(
            "platform/instagram/comment_import_new.html", project=project, import_fields=COMMENT_IMPORT_FIELDS,
        ), 400
    data = uploaded.read(MAX_IMPORT_BYTES + 1)
    try:
        dataset = parse_instagram_comment_import(uploaded.filename, data)
        conflict = comment_import_conflict(project, dataset)
        if conflict:
            raise InstagramImportError(conflict)
        repeated_source_hash = db.session.scalar(
            select(AnalyticsRun.id).where(
                AnalyticsRun.analytics_project_id == project.id,
                AnalyticsRun.source_hash == dataset.source_hash,
            ).limit(1)
        ) is not None
        preview = build_comment_preview(
            dataset, known_comment_hashes=_comment_known_hashes(project),
            repeated_source_hash=repeated_source_hash,
        )
    except InstagramImportError as error:
        flash(str(error), "danger")
        return render_template(
            "platform/instagram/comment_import_new.html", project=project, import_fields=COMMENT_IMPORT_FIELDS,
        ), 400
    try:
        token = comment_previews.put(current_user.id, project.id, preview)
    except OSError:
        current_app.logger.exception("Não foi possível guardar a prévia de comentários Instagram")
        flash("Não foi possível preparar a prévia. Tente enviar o arquivo novamente.", "danger")
        return render_template(
            "platform/instagram/comment_import_new.html", project=project, import_fields=COMMENT_IMPORT_FIELDS,
        ), 500
    return render_template("platform/instagram/comment_import_preview.html", project=project, preview=preview, token=token)


@instagram_bp.post("/projects/<uuid:project_id>/comments/imports/confirm")
@login_required
def confirm_comment_import(project_id: UUID):
    project = _project_for_current_user(project_id, active=True)
    token = request.form.get("preview_token", "")
    try:
        claim = comment_previews.claim(token, current_user.id, project.id)
    except InstagramCommentPreviewExpired:
        flash("A prévia expirou. Envie o arquivo novamente.", "warning")
        return redirect(url_for("instagram.new_comment_import", project_id=project.id))
    if claim is None:
        flash("Prévia indisponível. Envie o arquivo novamente.", "warning")
        return redirect(url_for("instagram.new_comment_import", project_id=project.id))
    preview = claim.preview
    try:
        result = persist_instagram_comment_import(project, preview.dataset)
    except InstagramCommentImportPersistenceError as error:
        claim.release()
        flash(str(error), "danger")
        return render_template(
            "platform/instagram/comment_import_preview.html", project=project, preview=preview, token=token,
        ), 409
    except Exception:
        claim.release()
        current_app.logger.exception("Falha ao confirmar importação de comentários Instagram")
        flash("Não foi possível concluir a importação. Nenhum dado foi salvo.", "danger")
        return render_template(
            "platform/instagram/comment_import_preview.html", project=project, preview=preview, token=token,
        ), 500
    try:
        claim.complete()
    except OSError:
        current_app.logger.exception("Importação de comentários concluída; falha ao remover prévia consumida")
    return render_template("platform/instagram/comment_import_complete.html", project=project, result=result, preview=preview)


@instagram_bp.post("/projects/<uuid:project_id>/comments/imports/cancel")
@login_required
def cancel_comment_import(project_id: UUID):
    project = _project_for_current_user(project_id)
    comment_previews.discard(request.form.get("preview_token", ""), current_user.id, project.id)
    return redirect(url_for("instagram.comments", project_id=project.id))


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
