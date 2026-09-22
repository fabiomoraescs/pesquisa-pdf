"""Rotas e jobs próprios da ferramenta histórico-racial, sem acoplamento V1–V3."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from uuid import UUID, uuid4

from flask import Blueprint, abort, jsonify, render_template, request, url_for
from werkzeug.utils import secure_filename

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
VOCABULARIO_STORE = VocabularyStore()


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
    """Descarta componentes de caminho enviados por clientes diferentes."""
    return nome.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _resposta_erro(mensagem: str, status: int = 400):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"erro": mensagem}), status
    try:
        vocabulario = VOCABULARIO_STORE.capturar_ativa()
    except (VocabularioError, ConfiguracaoInvalidaError, OSError):
        vocabulario = None
    return render_template("historico_racial/index.html", erro=mensagem, vocabulario=vocabulario), status


def _executar_job(
    job_id: str,
    arquivos: list[ArquivoPDF],
    temporario: TemporaryDirectory[str],
    resultado_url: str,
    vocabulario: dict,
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
        resultado = processar_documentos(arquivos, progress_callback=atualizar, vocabulario=vocabulario)
        with JOBS_LOCK:
            RESULTADOS_HR[job_id] = resultado
            PROGRESSOS_HR[job_id].update({
                "status": "concluido",
                "etapa": "Concluído",
                "percentual": 100,
                "resultado_url": resultado_url,
                "terminado_em": time.monotonic(),
            })
    except (ProcessamentoError, ConfiguracaoInvalidaError) as erro:
        with JOBS_LOCK:
            PROGRESSOS_HR[job_id].update({
                "status": "erro", "erro": str(erro), "terminado_em": time.monotonic()
            })
    except Exception:
        logging.getLogger(__name__).exception("Falha no job histórico-racial %s", job_id)
        with JOBS_LOCK:
            PROGRESSOS_HR[job_id].update({
                "status": "erro",
                "erro": "Não foi possível processar os PDFs. Verifique os arquivos e tente novamente.",
                "terminado_em": time.monotonic(),
            })
    finally:
        temporario.cleanup()


@historico_racial_bp.get("/historico-racial")
def inicio():
    """Exibe apenas os controles próprios da busca lexical histórico-racial."""
    try:
        vocabulario = VOCABULARIO_STORE.capturar_ativa()
    except (VocabularioError, ConfiguracaoInvalidaError, OSError):
        logging.getLogger(__name__).exception("Não foi possível carregar o vocabulário histórico-racial")
        return _resposta_erro("O vocabulário da pesquisa não está disponível.", 503)
    return render_template("historico_racial/index.html", vocabulario=vocabulario)


@historico_racial_bp.get("/historico-racial/vocabulario")
def gerenciar_vocabulario():
    try:
        vocabulario = VOCABULARIO_STORE.capturar_ativa()
        historico = VOCABULARIO_STORE.listar_versoes()
    except (VocabularioError, ConfiguracaoInvalidaError, OSError):
        logging.getLogger(__name__).exception("Não foi possível abrir o gerenciador de vocabulário")
        return _resposta_erro("O vocabulário da pesquisa não está disponível.", 503)
    return render_template(
        "historico_racial/vocabulario.html", vocabulario=vocabulario,
        historico=historico, categorias=carregar_categorias(),
    )


@historico_racial_bp.post("/historico-racial/vocabulario/versoes")
def salvar_vocabulario():
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        return jsonify({"erro": "Envie um rascunho válido do vocabulário."}), 400
    try:
        criado = VOCABULARIO_STORE.salvar(
            dados.get("base_version"), dados.get("vocabulario"), dados.get("nota", ""),
        )
    except VocabularioError as erro:
        mensagem = str(erro)
        return jsonify({"erro": mensagem}), 409 if "mudou durante a edição" in mensagem else 400
    except (ConfiguracaoInvalidaError, OSError):
        logging.getLogger(__name__).exception("Não foi possível salvar o vocabulário")
        return jsonify({"erro": "Não foi possível salvar a nova versão. Tente novamente."}), 503
    return jsonify({
        "version": criado["version"], "hash": criado["hash"],
        "created_at": criado["created_at"], "counts": criado["counts"], "note": criado["note"],
        "aviso": "Uma nova versão do vocabulário foi criada. Para manter a comparabilidade dos resultados, documentos processados com versões anteriores devem ser reprocessados com a versão ativa.",
        "url": url_for("historico_racial.gerenciar_vocabulario"),
    }), 201


@historico_racial_bp.post("/historico-racial/analisar")
def analisar():
    """Salva os uploads antes de iniciar o job, sem depender do request na thread."""
    enviados = [arquivo for arquivo in request.files.getlist("pdfs") if arquivo and arquivo.filename]
    if not enviados:
        return _resposta_erro("Selecione ao menos um arquivo PDF.")
    if any(Path(_nome_original(arquivo.filename)).suffix.casefold() != ".pdf" for arquivo in enviados):
        return _resposta_erro("Envie apenas arquivos com extensão .pdf.")

    try:
        vocabulario = VOCABULARIO_STORE.capturar_ativa()
    except (VocabularioError, ConfiguracaoInvalidaError, OSError):
        logging.getLogger(__name__).exception("Não foi possível capturar o vocabulário ativo")
        return _resposta_erro("O vocabulário da pesquisa não está disponível.", 503)

    _limpar_jobs()
    job_id = str(uuid4())
    temporario = TemporaryDirectory(prefix=f"{job_id}-", dir=UPLOAD_ROOT)
    arquivos: list[ArquivoPDF] = []
    try:
        for indice, enviado in enumerate(enviados, start=1):
            nome = _nome_original(enviado.filename)
            nome_seguro = secure_filename(nome) or "documento.pdf"
            caminho = Path(temporario.name) / f"{indice:03d}-{nome_seguro}"
            enviado.save(caminho)
            arquivos.append(ArquivoPDF(caminho, nome, str(uuid4())))
    except (OSError, ValueError):
        temporario.cleanup()
        return _resposta_erro("Não foi possível salvar os PDFs enviados. Tente novamente.")

    with JOBS_LOCK:
        PROGRESSOS_HR[job_id] = {
            "status": "processando", "etapa": "Preparando arquivos…",
            "percentual": None, "percentual_anterior": None,
            "arquivo_atual": None, "arquivo_indice": 0, "arquivos_total": len(arquivos),
            "pagina_atual": 0, "paginas_total": 0,
            "tempo_inicio": time.monotonic(), "resultado_url": None,
            "erro": None, "terminado_em": None,
            "vocabulario_version": vocabulario["version"],
            "vocabulario_hash": vocabulario["hash"],
        }
    try:
        resultado_url = url_for("historico_racial.resultado", job_id=job_id)
        EXECUTOR_HR.submit(_executar_job, job_id, arquivos, temporario, resultado_url, vocabulario)
    except RuntimeError:
        temporario.cleanup()
        with JOBS_LOCK:
            PROGRESSOS_HR.pop(job_id, None)
        return _resposta_erro("O processamento não pôde ser iniciado. Tente novamente.", 503)
    return jsonify({
        "job_id": job_id,
        "status": "processando",
        "progresso_url": url_for("historico_racial.progresso", job_id=job_id),
    }), 202


@historico_racial_bp.get("/historico-racial/api/progresso/<uuid:job_id>")
def progresso(job_id: UUID):
    _limpar_jobs()
    with JOBS_LOCK:
        dados = PROGRESSOS_HR.get(str(job_id))
        if dados is None:
            return jsonify({"erro": "Processamento não encontrado."}), 404
        publico = {chave: dados.get(chave) for chave in (
            "status", "etapa", "percentual", "arquivo_atual", "arquivo_indice",
            "arquivos_total", "pagina_atual", "paginas_total", "resultado_url", "erro",
            "vocabulario_version", "vocabulario_hash",
        )}
        publico["tempo_decorrido"] = round(time.monotonic() - dados["tempo_inicio"])
    return jsonify(publico)


@historico_racial_bp.get("/historico-racial/resultado/<uuid:job_id>")
def resultado(job_id: UUID):
    _limpar_jobs()
    with JOBS_LOCK:
        dados = RESULTADOS_HR.get(str(job_id))
    if dados is None:
        abort(404)
    return render_template("historico_racial/resultado.html", resultado=dados)


@historico_racial_bp.errorhandler(413)
def arquivo_grande(_erro):
    return _resposta_erro("O envio ultrapassa o limite de tamanho da aplicação.", 413)
