"""Criação e leitura de Bases qualitativas; nenhuma codificação nesta etapa."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from uuid import UUID

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import select
from werkzeug.utils import secure_filename

from historico_racial.pdf import PDFInvalidoError, contar_paginas

from .analyses import analysis_dir, create_analysis, preserve_documents, save_error
from .extensions import db
from .models import Analysis, Project, utcnow
from .qualitative import QUALITATIVE_STRATEGIES, get_qualitative_analysis, get_qualitative_document
from .qualitative_corpus import (
    CorpusUnavailableError, QualitativePageNotFoundError, load_qualitative_manifest,
    prepare_qualitative_corpus, qualitative_corpus_dir, read_qualitative_page,
    validate_qualitative_corpus,
)
from .qualitative_layout import read_qualitative_layout
from .qualitative_search import QualitativeSearchError, search_qualitative_document
from .scraping_types import QUALITATIVE, QUALITATIVE_TOOL
from .services import ACCOUNT_LIFECYCLE_LOCK, account_accepts_new_work, can_use_tool, get_project_for_user


qualitative_bp = Blueprint("qualitative", __name__, url_prefix="/analise-qualitativa")
_progress: dict[str, dict] = {}
_progress_lock = RLock()
_progress_retention_seconds = 60 * 60


def _require_tool() -> None:
    if not can_use_tool(current_user, QUALITATIVE_TOOL):
        abort(403)


def _project(project_id: UUID, *, active: bool = False) -> Project:
    _require_tool()
    project = get_project_for_user(str(project_id), current_user, include_inactive=not active)
    if project.scrape_type != QUALITATIVE or project.deleted_at is not None:
        abort(404)
    return project


def _base(analysis_id: UUID) -> Analysis:
    return get_qualitative_analysis(str(analysis_id), current_user)


def _current_progress(analysis: Analysis) -> dict:
    with _progress_lock:
        for identifier, value in list(_progress.items()):
            if value.get("finished_at") and time.monotonic() - value["finished_at"] > _progress_retention_seconds:
                _progress.pop(identifier, None)
        current = dict(_progress.get(analysis.id, {}))
    return {"status": analysis.status, "document_index": current.get("document_index", 0),
            "document_count": current.get("document_count", analysis.document_count),
            "document_name": current.get("document_name"), "page_number": current.get("page_number", 0),
            "result_url": url_for("qualitative.base", analysis_id=analysis.id) if analysis.status == "concluida" else None,
            "error": analysis.error_message if analysis.status == "erro" else None}


def _run_corpus_job(app, analysis_id: str) -> None:
    with app.app_context():
        published = False
        def report(event: dict) -> None:
            with _progress_lock:
                _progress.setdefault(analysis_id, {}).update(event)

        try:
            analysis = db.session.get(Analysis, analysis_id)
            if analysis is None:
                return
            prepare_qualitative_corpus(analysis, report)
            published = True
            validate_qualitative_corpus(analysis, allow_processing=True)
            analysis.status = "concluida"  # corpus pronto, não pesquisa interpretativa concluída
            analysis.completed_at = utcnow()
            analysis.error_message = None
            db.session.commit()
        except Exception:
            db.session.rollback()
            logging.getLogger(__name__).exception("Falha ao preparar corpus qualitativo %s", analysis_id)
            if published:
                try:
                    shutil.rmtree(qualitative_corpus_dir(analysis_id))
                except OSError:
                    logging.getLogger(__name__).exception("Falha ao retirar corpus não confirmado %s", analysis_id)
            save_error(analysis_id, "Não foi possível preparar o corpus. Os PDFs originais foram preservados.")
        finally:
            with _progress_lock:
                _progress.setdefault(analysis_id, {})["finished_at"] = time.monotonic()
            db.session.remove()


@qualitative_bp.get("")
@login_required
def index():
    _require_tool()
    return redirect(url_for("projects.list_qualitative_projects"))


@qualitative_bp.get("/projetos/<uuid:project_id>/bases")
@login_required
def project_bases(project_id: UUID):
    project = _project(project_id)
    bases = db.session.scalars(select(Analysis).where(
        Analysis.project_id == project.id, Analysis.tool_id == QUALITATIVE_TOOL
    ).order_by(Analysis.created_at.desc())).all()
    return render_template("platform/qualitative_bases.html", project=project, analyses=bases)


@qualitative_bp.route("/projetos/<uuid:project_id>/bases/nova", methods=["GET", "POST"])
@login_required
def new_base(project_id: UUID):
    project = _project(project_id, active=True)
    if project.owner_user_id != current_user.id:
        abort(403)
    if request.method == "GET":
        return render_template("platform/qualitative_new_base.html", project=project)

    name = request.form.get("name", "").strip()
    strategy = request.form.get("qualitative_strategy", "")
    uploads = [item for item in request.files.getlist("documents") if item.filename]
    if not name or len(name) > 200 or strategy not in QUALITATIVE_STRATEGIES or not uploads:
        return render_template("platform/qualitative_new_base.html", project=project,
                               error="Informe nome, estratégia e ao menos um PDF."), 400
    with TemporaryDirectory(prefix="qualitative-upload-") as temporary:
        files = []
        try:
            for index, upload in enumerate(uploads, start=1):
                original = upload.filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
                if not original.lower().endswith(".pdf") or len(original) > 255:
                    raise PDFInvalidoError("Envie somente arquivos PDF válidos.")
                safe = secure_filename(original) or "documento.pdf"
                source = Path(temporary) / f"{index:03d}-{safe}"
                upload.save(source)
                contar_paginas(source)  # valida o conteúdo real sem extrair o corpus
                files.append((source, original))
        except (PDFInvalidoError, OSError, ValueError):
            return render_template("platform/qualitative_new_base.html", project=project,
                                   error="Um dos arquivos não é um PDF legível, está vazio ou está protegido."), 400

        with ACCOUNT_LIFECYCLE_LOCK:
            current_project = db.session.get(Project, project.id, populate_existing=True)
            if (not account_accepts_new_work(current_user.id) or current_project is None
                    or current_project.status != "active" or current_project.deleted_at is not None
                    or current_project.owner_user_id != current_user.id):
                abort(403)
            analysis = create_analysis(
                user_id=current_user.id, project_id=project.id, tool_id=QUALITATIVE_TOOL,
                tool_version="manual-v1", name=name,
                parameters={"qualitative_strategy": strategy},
            )
            try:
                preserve_documents(analysis, files)
            except Exception:
                db.session.rollback()
                logging.getLogger(__name__).exception("Falha ao preservar PDFs qualitativos %s", analysis.id)
                partial_documents = analysis_dir(analysis.id) / "documents"
                if partial_documents.exists() and not partial_documents.is_symlink():
                    shutil.rmtree(partial_documents)
                save_error(analysis.id, "Não foi possível salvar os PDFs enviados.")
                return render_template("platform/qualitative_new_base.html", project=project,
                                       error="Não foi possível salvar os PDFs enviados."), 503

    from app import EXECUTOR_ANALISES

    try:
        EXECUTOR_ANALISES.submit(_run_corpus_job, current_app._get_current_object(), analysis.id)
    except RuntimeError:
        save_error(analysis.id, "Não foi possível iniciar a preparação do corpus.")
        return render_template("platform/qualitative_new_base.html", project=project,
                               error="Não foi possível iniciar a preparação do corpus."), 503
    return redirect(url_for("qualitative.base", analysis_id=analysis.id))


@qualitative_bp.get("/bases/<uuid:analysis_id>/progresso")
@login_required
def progress(analysis_id: UUID):
    return jsonify(_current_progress(_base(analysis_id)))


@qualitative_bp.get("/bases/<uuid:analysis_id>")
@login_required
def base(analysis_id: UUID):
    analysis = _base(analysis_id)
    project = _project(UUID(analysis.project_id))
    if analysis.status != "concluida":
        return render_template("platform/qualitative_pending.html", analysis=analysis,
                               project=project, progress=_current_progress(analysis))
    try:
        manifest = load_qualitative_manifest(analysis)
    except CorpusUnavailableError:
        logging.getLogger(__name__).error("Base %s marcada concluída sem corpus íntegro", analysis.id)
        return render_template("platform/qualitative_pending.html", analysis=analysis,
                               project=project, inconsistent=True), 409
    first = manifest["documents"][0]
    return redirect(url_for("qualitative.page", analysis_id=analysis.id,
                            document_id=first["document_id"], page_number=1))


@qualitative_bp.get("/bases/<uuid:analysis_id>/documentos/<uuid:document_id>/paginas/<int:page_number>")
@login_required
def page(analysis_id: UUID, document_id: UUID, page_number: int):
    analysis = _base(analysis_id)
    get_qualitative_document(analysis, str(document_id))
    project = _project(UUID(analysis.project_id))
    try:
        manifest = load_qualitative_manifest(analysis)
        current = read_qualitative_page(analysis, str(document_id), page_number, manifest=manifest)
    except QualitativePageNotFoundError:
        abort(404)
    except CorpusUnavailableError:
        abort(409)
    return render_template("platform/qualitative_reader.html", analysis=analysis, project=project,
                           manifest=manifest, current=current)


@qualitative_bp.get("/bases/<uuid:analysis_id>/documentos/<uuid:document_id>/ir")
@login_required
def go_to_page(analysis_id: UUID, document_id: UUID):
    analysis = _base(analysis_id)
    get_qualitative_document(analysis, str(document_id))
    try:
        number = int(request.args.get("page_number", ""))
    except ValueError:
        abort(404)
    try:
        read_qualitative_page(analysis, str(document_id), number)
    except QualitativePageNotFoundError:
        abort(404)
    except CorpusUnavailableError:
        abort(409)
    return redirect(url_for("qualitative.page", analysis_id=analysis.id,
                            document_id=document_id, page_number=number))


@qualitative_bp.get("/bases/<uuid:analysis_id>/documentos/<uuid:document_id>/paginas/<int:page_number>/dados")
@login_required
def page_data(analysis_id: UUID, document_id: UUID, page_number: int):
    analysis = _base(analysis_id)
    get_qualitative_document(analysis, str(document_id))
    try:
        return jsonify(read_qualitative_page(analysis, str(document_id), page_number))
    except QualitativePageNotFoundError:
        abort(404)
    except CorpusUnavailableError:
        abort(409)


@qualitative_bp.get("/bases/<uuid:analysis_id>/documentos/<uuid:document_id>/pdf")
@login_required
def original_pdf(analysis_id: UUID, document_id: UUID):
    """PDF original da Base, com autorização e suporte a Range via send_file."""
    analysis = _base(analysis_id)
    document = get_qualitative_document(analysis, str(document_id))
    if analysis.status != "concluida":
        abort(409)
    root = analysis_dir(analysis.id) / "documents"
    source = root / document.stored_name
    if (root.is_symlink() or source.is_symlink() or source.resolve().parent != root.resolve()
            or not source.is_file()):
        abort(409)
    response = send_file(source, mimetype="application/pdf", conditional=True,
                         as_attachment=False, download_name=document.original_name)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@qualitative_bp.get("/bases/<uuid:analysis_id>/documentos/<uuid:document_id>/buscar")
@login_required
def search(analysis_id: UUID, document_id: UUID):
    analysis = _base(analysis_id)
    get_qualitative_document(analysis, str(document_id))
    try:
        result = search_qualitative_document(
            analysis, str(document_id), request.args.get("q", ""),
            grep=request.args.get("grep") == "1", case_sensitive=request.args.get("case") == "1",
        )
        return jsonify(result)
    except QualitativePageNotFoundError:
        abort(404)
    except CorpusUnavailableError:
        abort(409)
    except QualitativeSearchError as error:
        return jsonify({"error": str(error)}), 400


@qualitative_bp.get("/bases/<uuid:analysis_id>/documentos/<uuid:document_id>/paginas/<int:page_number>/layout")
@login_required
def page_layout(analysis_id: UUID, document_id: UUID, page_number: int):
    analysis = _base(analysis_id)
    get_qualitative_document(analysis, str(document_id))
    try:
        return jsonify(read_qualitative_layout(analysis, str(document_id), page_number))
    except QualitativePageNotFoundError:
        abort(404)
    except CorpusUnavailableError:
        abort(409)
