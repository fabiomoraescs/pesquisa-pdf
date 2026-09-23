"""Análise documental por projeto; o pipeline lexical permanece independente."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from uuid import UUID, uuid4

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select
from werkzeug.utils import secure_filename

from platform_core.extensions import db
from platform_core.models import Project, ProjectLibrary, ProjectVocabularyVersion, VocabularyLibrary
from platform_core.services import can_use_tool, get_project_for_user
from platform_core.vocabularies import project_store, register_version

from .dictionaries import ConfiguracaoInvalidaError, carregar_categorias
from .processor import ArquivoPDF, ProcessamentoError, processar_documentos
from .vocabulary import VocabularyStore, VocabularioError


historico_racial_bp = Blueprint("historico_racial", __name__)
UPLOAD_ROOT = Path(__file__).resolve().parent.parent / "uploads" / "historico_racial"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
EXECUTOR_HR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="historico-racial")
PROGRESSOS_HR: dict[str, dict] = {}
RESULTADOS_HR: dict[str, dict] = {}
JOBS_LOCK = RLock()
RETENCAO_SEGUNDOS = 60 * 60
# Referência legada mantida somente para testes/inspeção do estado anterior.
# Nenhuma nova análise consulta este vocabulário global.
VOCABULARIO_STORE = VocabularyStore()


def _require_project(project_id: UUID):
    if not can_use_tool(current_user, "document_analysis"):
        abort(403)
    return get_project_for_user(str(project_id), current_user)


def _libraries(project_id: str) -> list[VocabularyLibrary]:
    return db.session.scalars(
        select(VocabularyLibrary).join(ProjectLibrary, ProjectLibrary.library_id == VocabularyLibrary.id)
        .where(ProjectLibrary.project_id == project_id).order_by(VocabularyLibrary.name)
    ).all()


def _limpar_jobs() -> None:
    agora = time.monotonic()
    with JOBS_LOCK:
        vencidos = [
            job_id for job_id, dados in PROGRESSOS_HR.items()
            if dados["status"] in {"concluido", "erro"}
            and agora - dados.get("terminado_em", agora) > RETENCAO_SEGUNDOS
        ]
        for job_id in vencidos:
            PROGRESSOS_HR.pop(job_id, None)
            RESULTADOS_HR.pop(job_id, None)


def _nome_original(nome: str) -> str:
    return nome.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _resposta_erro(mensagem: str, project_id: UUID, status: int = 400):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"erro": mensagem}), status
    flash(mensagem, "danger")
    return redirect(url_for("historico_racial.inicio_projeto", project_id=project_id))


def _executar_job(
    job_id: str, arquivos: list[ArquivoPDF], temporario: TemporaryDirectory[str],
    resultado_url: str, vocabulario: dict, project_id: str, user_id: str,
    project_name: str, library_names: list[str],
) -> None:
    def atualizar(evento: dict) -> None:
        with JOBS_LOCK:
            dados = PROGRESSOS_HR.get(job_id)
            if dados is None:
                return
            dados.update(evento)
            percentual = dados.get("percentual")
            if percentual is not None:
                dados["percentual"] = min(99, max(dados.get("percentual_anterior") or 0, percentual))
                dados["percentual_anterior"] = dados["percentual"]

    try:
        resultado = processar_documentos(
            arquivos, progress_callback=atualizar, vocabulario=vocabulario,
            project_id=project_id, vocabulary_version=vocabulario["version"],
            vocabulary_hash=vocabulario["hash"],
        )
        resultado.update({"project_name": project_name, "library_names": library_names, "owner_user_id": user_id})
        with JOBS_LOCK:
            RESULTADOS_HR[job_id] = resultado
            PROGRESSOS_HR[job_id].update({
                "status": "concluido", "etapa": "Concluído", "percentual": 100,
                "resultado_url": resultado_url, "terminado_em": time.monotonic(),
            })
    except (ProcessamentoError, ConfiguracaoInvalidaError) as erro:
        with JOBS_LOCK:
            PROGRESSOS_HR[job_id].update({"status": "erro", "erro": str(erro), "terminado_em": time.monotonic()})
    except Exception:
        logging.getLogger(__name__).exception("Falha no job documental %s", job_id)
        with JOBS_LOCK:
            PROGRESSOS_HR[job_id].update({
                "status": "erro", "erro": "Não foi possível processar os PDFs. Verifique os arquivos e tente novamente.",
                "terminado_em": time.monotonic(),
            })
    finally:
        temporario.cleanup()


@historico_racial_bp.get("/analise-documental")
@login_required
def entrada():
    if not can_use_tool(current_user, "document_analysis"):
        abort(403)
    return redirect(url_for("projects.list_projects"))


@historico_racial_bp.get("/historico-racial")
@login_required
def inicio():
    return redirect(url_for("historico_racial.entrada"), code=302)


@historico_racial_bp.get("/historico-racial/vocabulario")
@login_required
def vocabulario_legado():
    return redirect(url_for("projects.list_projects"), code=302)


@historico_racial_bp.post("/historico-racial/vocabulario/versoes")
@historico_racial_bp.post("/historico-racial/analisar")
@login_required
def post_legado():
    return jsonify({"erro": "Selecione um projeto em /projetos antes de processar ou editar o vocabulário."}), 409


@historico_racial_bp.get("/analise-documental/projetos/<uuid:project_id>")
@login_required
def inicio_projeto(project_id: UUID):
    project = _require_project(project_id)
    try:
        vocabulary = project_store(project.id).capturar_ativa()
    except (VocabularioError, OSError):
        logging.getLogger(__name__).exception("Vocabulário indisponível para projeto %s", project.id)
        abort(503)
    return render_template("historico_racial/index.html", project=project, vocabulario=vocabulary, libraries=_libraries(project.id))


@historico_racial_bp.get("/analise-documental/projetos/<uuid:project_id>/vocabulario")
@login_required
def gerenciar_vocabulario(project_id: UUID):
    project = _require_project(project_id)
    store = project_store(project.id)
    try:
        vocabulary = store.capturar_ativa()
        page = max(1, request.args.get("page", 1, type=int))
        versions = store.listar_versoes()
    except (VocabularioError, ConfiguracaoInvalidaError, OSError):
        logging.getLogger(__name__).exception("Vocabulário indisponível para projeto %s", project.id)
        abort(503)
    return render_template(
        "historico_racial/vocabulario.html", project=project, vocabulario=vocabulary,
        historico=versions[(page - 1) * 10:page * 10], has_older=len(versions) > page * 10,
        page=page, categorias=carregar_categorias(),
    )


@historico_racial_bp.post("/analise-documental/projetos/<uuid:project_id>/vocabulario/versoes")
@login_required
def salvar_vocabulario(project_id: UUID):
    project = _require_project(project_id)
    if project.owner_user_id != current_user.id:
        abort(403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"erro": "Envie um rascunho válido do vocabulário."}), 400
    try:
        record = project_store(project.id).salvar(
            payload.get("base_version"), payload.get("vocabulario"), payload.get("nota", ""),
        )
        register_version(project, record)
        db.session.commit()
    except VocabularioError as error:
        message = str(error)
        return jsonify({"erro": message}), 409 if "mudou durante" in message else 400
    except (ConfiguracaoInvalidaError, OSError):
        db.session.rollback()
        logging.getLogger(__name__).exception("Não foi possível salvar vocabulário do projeto %s", project.id)
        return jsonify({"erro": "Não foi possível salvar a nova versão. Tente novamente."}), 503
    return jsonify({
        "version": record["version"], "hash": record["hash"],
        "created_at": record["created_at"], "counts": record["counts"], "note": record["note"],
        "aviso": "Uma nova versão do vocabulário foi criada. Para manter a comparabilidade dos resultados, documentos anteriores devem ser reprocessados com a versão ativa.",
        "url": url_for("historico_racial.gerenciar_vocabulario", project_id=project.id),
    }), 201


@historico_racial_bp.post("/analise-documental/projetos/<uuid:project_id>/analisar")
@login_required
def analisar(project_id: UUID):
    project = _require_project(project_id)
    enviados = [file for file in request.files.getlist("pdfs") if file and file.filename]
    if not enviados:
        return _resposta_erro("Selecione ao menos um arquivo PDF.", project_id)
    if any(Path(_nome_original(file.filename)).suffix.casefold() != ".pdf" for file in enviados):
        return _resposta_erro("Envie apenas arquivos com extensão .pdf.", project_id)
    try:
        vocabulary = project_store(project.id).capturar_ativa()
    except (VocabularioError, ConfiguracaoInvalidaError, OSError):
        logging.getLogger(__name__).exception("Vocabulário indisponível para projeto %s", project.id)
        return _resposta_erro("O vocabulário do projeto não está disponível.", project_id, 503)
    _limpar_jobs()
    job_id = str(uuid4())
    temporary = TemporaryDirectory(prefix=f"{job_id}-", dir=UPLOAD_ROOT)
    files: list[ArquivoPDF] = []
    try:
        for index, sent in enumerate(enviados, start=1):
            name = _nome_original(sent.filename)
            safe = secure_filename(name) or "documento.pdf"
            path = Path(temporary.name) / f"{index:03d}-{safe}"
            sent.save(path)
            files.append(ArquivoPDF(path, name, str(uuid4())))
    except (OSError, ValueError):
        temporary.cleanup()
        return _resposta_erro("Não foi possível salvar os PDFs enviados. Tente novamente.", project_id)
    with JOBS_LOCK:
        # Uma requisição que começou antes do arquivamento não pode registrar um
        # novo job depois que o projeto mudou de estado ou foi excluído.
        current_project = db.session.get(Project, project.id, populate_existing=True)
        if current_project is None or current_project.status != "active" or current_project.deleted_at is not None:
            temporary.cleanup()
            return jsonify({"erro": "O projeto não está mais ativo para processamento."}), 409
        PROGRESSOS_HR[job_id] = {
            "status": "processando", "etapa": "Preparando arquivos…",
            "percentual": None, "percentual_anterior": None,
            "arquivo_atual": None, "arquivo_indice": 0, "arquivos_total": len(files),
            "pagina_atual": 0, "paginas_total": 0, "tempo_inicio": time.monotonic(),
            "resultado_url": None, "erro": None, "terminado_em": None,
            "vocabulario_version": vocabulary["version"], "vocabulario_hash": vocabulary["hash"],
            "project_id": project.id, "owner_user_id": current_user.id,
        }
    try:
        result_url = url_for("historico_racial.resultado", project_id=project.id, job_id=job_id)
        EXECUTOR_HR.submit(
            _executar_job, job_id, files, temporary, result_url, vocabulary,
            project.id, current_user.id, project.name, [item.name for item in _libraries(project.id)],
        )
    except RuntimeError:
        temporary.cleanup()
        with JOBS_LOCK:
            PROGRESSOS_HR.pop(job_id, None)
        return _resposta_erro("O processamento não pôde ser iniciado. Tente novamente.", project_id, 503)
    return jsonify({
        "job_id": job_id, "status": "processando",
        "progresso_url": url_for("historico_racial.progresso", project_id=project.id, job_id=job_id),
    }), 202


@historico_racial_bp.get("/analise-documental/projetos/<uuid:project_id>/api/progresso/<uuid:job_id>")
@login_required
def progresso(project_id: UUID, job_id: UUID):
    project = _require_project(project_id)
    _limpar_jobs()
    with JOBS_LOCK:
        data = PROGRESSOS_HR.get(str(job_id))
        if data is None or data["project_id"] != project.id or (data["owner_user_id"] != current_user.id and current_user.role != "admin"):
            return jsonify({"erro": "Processamento não encontrado."}), 404
        public = {key: data.get(key) for key in (
            "status", "etapa", "percentual", "arquivo_atual", "arquivo_indice",
            "arquivos_total", "pagina_atual", "paginas_total", "resultado_url", "erro",
            "vocabulario_version", "vocabulario_hash",
        )}
        public["tempo_decorrido"] = round(time.monotonic() - data["tempo_inicio"])
    return jsonify(public)


@historico_racial_bp.get("/analise-documental/projetos/<uuid:project_id>/resultado/<uuid:job_id>")
@login_required
def resultado(project_id: UUID, job_id: UUID):
    project = _require_project(project_id)
    _limpar_jobs()
    with JOBS_LOCK:
        data = RESULTADOS_HR.get(str(job_id))
    if data is None or data["project_id"] != project.id or (data["owner_user_id"] != current_user.id and current_user.role != "admin"):
        abort(404)
    return render_template("historico_racial/resultado.html", resultado=data, project=project)


@historico_racial_bp.get("/historico-racial/resultado/<uuid:job_id>")
@historico_racial_bp.get("/historico-racial/api/progresso/<uuid:job_id>")
@login_required
def old_job_link(job_id: UUID):
    with JOBS_LOCK:
        data = PROGRESSOS_HR.get(str(job_id))
    if data is None or (data["owner_user_id"] != current_user.id and current_user.role != "admin"):
        abort(404)
    if "/resultado/" in request.path:
        if not data["resultado_url"]:
            abort(404)
        return redirect(data["resultado_url"])
    return redirect(url_for("historico_racial.progresso", project_id=data["project_id"], job_id=job_id))


@historico_racial_bp.errorhandler(413)
def arquivo_grande(_error):
    return jsonify({"erro": "O envio ultrapassa o limite de tamanho da aplicação."}), 413
