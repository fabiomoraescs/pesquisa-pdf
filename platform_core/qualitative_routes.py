"""Criação e leitura de Bases qualitativas; nenhuma codificação nesta etapa."""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from uuid import UUID, uuid4

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import select
from werkzeug.utils import secure_filename

from historico_racial.pdf import PDFInvalidoError, contar_paginas

from .analyses import analysis_dir, create_analysis, preserve_documents, save_error
from .extensions import db
from .models import Analysis, AnalysisDocument, Project, utcnow
from .qualitative import QUALITATIVE_STRATEGIES, get_qualitative_analysis, get_qualitative_document
from .qualitative_corpus import (
    CorpusUnavailableError, QualitativePageNotFoundError, append_qualitative_corpus,
    delete_qualitative_document, load_qualitative_manifest,
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


def _project_workspaces(project: Project) -> list[Analysis]:
    return db.session.scalars(select(Analysis).where(
        Analysis.project_id == project.id, Analysis.tool_id == QUALITATIVE_TOOL
    ).order_by(Analysis.created_at, Analysis.id)).all()


def ensure_project_workspace(project: Project) -> Analysis | None:
    """Cria o ambiente técnico vazio somente se não houver histórico ambíguo."""
    with ACCOUNT_LIFECYCLE_LOCK:
        workspaces = _project_workspaces(project)
        if len(workspaces) == 1:
            return workspaces[0]
        if len(workspaces) > 1 or project.status != "active" or project.deleted_at is not None:
            return None
        if not account_accepts_new_work(project.owner_user_id):
            return None
        return create_analysis(user_id=project.owner_user_id, project_id=project.id,
                               tool_id=QUALITATIVE_TOOL, tool_version="manual-v1",
                               name=project.name, parameters={})


def _upload_lock(analysis_id: str) -> Path:
    return analysis_dir(analysis_id) / ".qualitative_upload.lock"


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
            "error": current.get("error") or (analysis.error_message if analysis.status == "erro" else None)}


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


@qualitative_bp.get("/projetos/<uuid:project_id>")
@login_required
def project_workspace(project_id: UUID):
    project = _project(project_id)
    workspaces = _project_workspaces(project)
    if not workspaces and project.owner_user_id == current_user.id:
        created = ensure_project_workspace(project)
        if created is not None:
            workspaces = [created]
    if len(workspaces) == 1:
        return redirect(url_for("qualitative.base", analysis_id=workspaces[0].id))
    if not workspaces:
        return render_template("platform/qualitative_empty.html", project=project, analysis=None)
    # Dados históricos não são fundidos nem atribuídos arbitrariamente.
    return render_template("platform/qualitative_legacy_choice.html", project=project,
                           workspaces=workspaces)


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
    if _project_workspaces(project):
        if request.method == "POST":
            abort(409)
        return redirect(url_for("qualitative.project_workspace", project_id=project.id))
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


def _run_added_documents_job(app, analysis_id: str, incoming_name: str,
                             items: list[tuple[str, str, str]], initial: bool) -> None:
    with app.app_context():
        incoming = analysis_dir(analysis_id) / incoming_name
        try:
            analysis = db.session.get(Analysis, analysis_id)
            if analysis is None:
                return
            if initial:
                preserve_documents(analysis, [(incoming / stored, original)
                                              for _, stored, original in items])
                _run_corpus_job(app, analysis_id)
            else:
                additions = [(AnalysisDocument(id=identifier, analysis_id=analysis_id,
                                               stored_name=stored, original_name=original), incoming / stored)
                             for identifier, stored, original in items]
                def report(event: dict) -> None:
                    with _progress_lock:
                        _progress.setdefault(analysis_id, {}).update(event)
                append_qualitative_corpus(analysis, additions, report)
                with _progress_lock:
                    _progress.setdefault(analysis_id, {})["finished_at"] = time.monotonic()
        except Exception:
            db.session.rollback()
            logging.getLogger(__name__).exception("Falha ao adicionar documentos qualitativos %s", analysis_id)
            if initial:
                save_error(analysis_id, "Não foi possível preparar os documentos enviados.")
            with _progress_lock:
                _progress.setdefault(analysis_id, {}).update(
                    error="Não foi possível preparar os novos documentos; os anteriores foram preservados.",
                    finished_at=time.monotonic())
        finally:
            try:
                if incoming.exists() and not incoming.is_symlink():
                    shutil.rmtree(incoming)
            except OSError:
                logging.getLogger(__name__).exception("Falha ao limpar upload transitório %s", analysis_id)
            finally:
                try:
                    _upload_lock(analysis_id).unlink(missing_ok=True)
                finally:
                    db.session.remove()


@qualitative_bp.post("/bases/<uuid:analysis_id>/documentos")
@login_required
def add_documents(analysis_id: UUID):
    analysis = _base(analysis_id)
    project = _project(UUID(analysis.project_id), active=True)
    if project.owner_user_id != current_user.id:
        abort(403)
    initial = analysis.document_count == 0 and analysis.status == "processando"
    if not initial and analysis.status != "concluida":
        abort(409)
    if not initial:
        try:
            load_qualitative_manifest(analysis)
        except CorpusUnavailableError:
            abort(409)
    uploads = [item for item in request.files.getlist("documents") if item.filename]
    if not uploads:
        flash("Selecione ao menos um PDF.", "danger")
        return redirect(url_for("qualitative.project_workspace", project_id=project.id))
    lock = _upload_lock(analysis.id)
    with ACCOUNT_LIFECYCLE_LOCK:
        if not account_accepts_new_work(current_user.id):
            abort(403)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            abort(409)
        else:
            os.close(fd)
        db.session.refresh(analysis)
        initial = analysis.document_count == 0 and analysis.status == "processando"
        if not initial and analysis.status != "concluida":
            lock.unlink(missing_ok=True)
            abort(409)
        if not initial:
            try:
                load_qualitative_manifest(analysis)
            except CorpusUnavailableError:
                lock.unlink(missing_ok=True)
                abort(409)
    incoming = analysis_dir(analysis.id) / f".qualitative_upload.{uuid4().hex}.tmp"
    try:
        incoming.mkdir()
        items = []
        for upload in uploads:
            original = upload.filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
            if not original.lower().endswith(".pdf") or len(original) > 255:
                raise PDFInvalidoError("Envie somente PDFs válidos.")
            stored = f"{uuid4().hex}-{secure_filename(original) or 'documento.pdf'}"
            path = incoming / stored
            upload.save(path)
            contar_paginas(path)
            items.append((str(uuid4()), stored, original))
        from app import EXECUTOR_ANALISES
        with _progress_lock:
            _progress[analysis.id] = {"document_count": len(items), "document_index": 0}
        EXECUTOR_ANALISES.submit(_run_added_documents_job, current_app._get_current_object(),
                                 analysis.id, incoming.name, items, initial)
    except (PDFInvalidoError, OSError, ValueError, RuntimeError):
        if incoming.exists() and not incoming.is_symlink():
            shutil.rmtree(incoming)
        lock.unlink(missing_ok=True)
        flash("Um dos arquivos não é um PDF legível ou não foi possível iniciar o preparo.", "danger")
        return redirect(url_for("qualitative.project_workspace", project_id=project.id))
    return redirect(url_for("qualitative.upload_pending", analysis_id=analysis.id))


@qualitative_bp.post("/bases/<uuid:analysis_id>/documentos/<uuid:document_id>/excluir")
@login_required
def delete_document(analysis_id: UUID, document_id: UUID):
    analysis = _base(analysis_id)
    project = _project(UUID(analysis.project_id), active=True)
    if project.owner_user_id != current_user.id:
        abort(403)
    document = get_qualitative_document(analysis, str(document_id))
    lock = _upload_lock(analysis.id)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return jsonify({"error": "Aguarde a preparação dos documentos terminar."}), 409
    else:
        os.close(fd)
    try:
        next_id = delete_qualitative_document(analysis, document)
    except CorpusUnavailableError:
        return jsonify({"error": "O corpus está inconsistente. Nenhum documento foi removido."}), 409
    except Exception:
        logging.getLogger(__name__).exception("Falha ao excluir documento %s", document_id)
        return jsonify({"error": "Não foi possível excluir o documento. Tente novamente após revisão."}), 500
    finally:
        lock.unlink(missing_ok=True)
    destination = (url_for("qualitative.page", analysis_id=analysis.id,
                           document_id=next_id, page_number=1) if next_id else
                   url_for("qualitative.project_workspace", project_id=project.id))
    return jsonify({"document_id": document.id, "document_count": analysis.document_count,
                    "next_url": destination})


@qualitative_bp.get("/bases/<uuid:analysis_id>/documentos/preparando")
@login_required
def upload_pending(analysis_id: UUID):
    analysis = _base(analysis_id)
    project = _project(UUID(analysis.project_id))
    progress_data = _current_progress(analysis)
    if not _upload_lock(analysis.id).exists():
        if progress_data["error"]:
            return render_template("platform/qualitative_pending.html", analysis=analysis,
                                   project=project, progress=progress_data, upload_error=progress_data["error"])
        return redirect(url_for("qualitative.project_workspace", project_id=project.id))
    return render_template("platform/qualitative_pending.html", analysis=analysis,
                           project=project, progress=progress_data, uploading=True)


@qualitative_bp.get("/bases/<uuid:analysis_id>/progresso")
@login_required
def progress(analysis_id: UUID):
    return jsonify(_current_progress(_base(analysis_id)))


@qualitative_bp.get("/bases/<uuid:analysis_id>")
@login_required
def base(analysis_id: UUID):
    analysis = _base(analysis_id)
    project = _project(UUID(analysis.project_id))
    if analysis.document_count == 0:
        return render_template("platform/qualitative_empty.html", project=project, analysis=analysis)
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
