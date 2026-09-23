"""Aplicação local Flask para as versões V1, V2 e V3 da Varredura de PDFs."""

from __future__ import annotations

import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from uuid import uuid4

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, logout_user
from flask_wtf.csrf import CSRFError

from platform_core.extensions import csrf, db, login_manager, migrate
from platform_core.models import User
from platform_core.auth import auth_bp
from platform_core.projects import projects_bp
from platform_core.admin import admin_bp
from platform_core.profile import profile_bp
from platform_core.presentation import register_presentation
from platform_core.cli import register_cli
from platform_core.services import ACCOUNT_LIFECYCLE_LOCK, access_is_active, account_accepts_new_work, can_use_tool
from platform_core.semantic_threshold import normalize as normalize_semantic_threshold, template_settings

from analyzer.common import (
    ANALISADORES,
    criar_dashboard,
    executar_analises,
    ler_arquivo_termos,
    montar_termos,
)
from historico_racial.routes import historico_racial_bp


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
PLATFORM_DATA_DIR = Path(os.environ.get("PESQUISAPDF_DATA_DIR", str(OUTPUT_DIR / "platform")))
PLATFORM_DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URI = os.environ.get("PESQUISAPDF_DATABASE_URL") or f"sqlite:///{(PLATFORM_DATA_DIR / 'platform.sqlite3').as_posix()}"
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=1024 * 1024 * 1024,
    SQLALCHEMY_DATABASE_URI=DATABASE_URI,
    SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 30}} if DATABASE_URI.startswith("sqlite:") else {},
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    PLATFORM_DATA_DIR=str(PLATFORM_DATA_DIR),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PESQUISAPDF_HTTPS") == "1",
)
db.init_app(app)
login_manager.init_app(app)
migrate.init_app(app, db)
csrf.init_app(app)
register_cli(app)
register_presentation(app)


@app.context_processor
def _semantic_threshold_template_context():
    return {"semantic_threshold": template_settings()}


app.register_blueprint(auth_bp)
app.register_blueprint(projects_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(profile_bp)
app.register_blueprint(historico_racial_bp)


@login_manager.user_loader
def _load_user(user_id: str):
    return db.session.get(User, user_id)


@app.before_request
def _enforce_platform_access():
    if request.endpoint in {"static", "auth.login", "auth.register"}:
        return None
    if not current_user.is_authenticated or not current_user.is_active:
        if current_user.is_authenticated:
            logout_user()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.path.startswith("/api/"):
            return jsonify({"erro": "Entre para continuar."}), 401
        return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else ""))
    if current_user.must_change_password and request.endpoint not in {"auth.change_password", "auth.logout"}:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.path.startswith("/api/"):
            return jsonify({"erro": "Altere sua senha antes de continuar."}), 403
        return redirect(url_for("auth.change_password"))
    if not access_is_active(current_user):
        if request.endpoint == "auth.logout":
            return None
        abort(403)
    if request.path.startswith("/admin"):
        if current_user.role != "admin":
            abort(403)
    elif request.path == "/" or request.path.startswith(("/resultado/", "/download/", "/api/progresso/")):
        if not can_use_tool(current_user, "pdf_scraper"):
            abort(403)
    elif request.path.startswith(("/historico-racial", "/analise-documental", "/projetos")):
        if not can_use_tool(current_user, "document_analysis"):
            abort(403)
    return None


@app.errorhandler(CSRFError)
def _csrf_error(_error):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify({"erro": "Sessão expirada ou token de segurança inválido. Recarregue a página."}), 400
    return "Sessão expirada ou token de segurança inválido. Recarregue a página.", 400

# A aplicação não possui histórico permanente. O cache só mantém a análise
# atual enquanto o servidor estiver aberto, para exibir o dashboard e baixar
# os arquivos gerados pela mesma sessão.
ANALISES: dict[str, dict] = {}
ANALISES_LOCK = RLock()

# Metadados pequenos, independentes do cache de resultados. Nunca guardam
# PDFs, textos, DataFrames, embeddings ou conteúdos analíticos.
PROGRESSOS: dict[str, dict] = {}
PROGRESSOS_LOCK = RLock()
EXECUTOR_ANALISES = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pesquisa-pdf")
RETENCAO_PROGRESSO_SEGUNDOS = 60 * 60


def _agora_monotonicamente() -> float:
    return time.monotonic()


def _limpar_progressos_expirados() -> None:
    """Descarta somente metadados concluídos/errados após retenção limitada."""
    agora = _agora_monotonicamente()
    with PROGRESSOS_LOCK:
        expirados = [
            job_id
            for job_id, dados in PROGRESSOS.items()
            if dados["status"] in {"concluido", "erro"}
            and dados.get("terminado_em")
            and agora - dados["terminado_em"] > RETENCAO_PROGRESSO_SEGUNDOS
        ]
        for job_id in expirados:
            del PROGRESSOS[job_id]


def _fracao_local_do_evento(evento: dict, metodo: str, configuracoes: dict | None) -> float | None:
    """Converte marcos reais já concluídos em pesos de apresentação.

    Os pesos não guiam a análise: páginas respondem pela maior parte da V1;
    blocos lexicais e semânticos representam suas etapas efetivamente concluídas.
    """
    fase = evento.get("fase")
    lexical = bool((configuracoes or {}).get("incluir_lexical", True))
    semantica = bool((configuracoes or {}).get("incluir_semantica", False))

    if metodo == "v2":
        # A V2 efetivamente faz duas passagens pelos mesmos PDFs: a leitura
        # para estimar a fonte e a extração por página. A busca por blocos vem
        # em seguida. Os pesos apenas apresentam unidades já concluídas.
        if fase == "estimando_fonte":
            atual = evento.get("pagina_atual")
            total = evento.get("paginas_total")
            if isinstance(atual, int) and isinstance(total, int) and total > 0:
                return 0.05 + 0.20 * min(atual / total, 1)
            return None
        if fase in {"paginas", "ocr"}:
            atual = evento.get("pagina_atual")
            total = evento.get("paginas_total")
            if isinstance(atual, int) and isinstance(total, int) and total > 0:
                return 0.25 + 0.60 * min(atual / total, 1)
            return None
        if fase == "busca_lexical":
            atual = evento.get("bloco_atual")
            total = evento.get("blocos_total")
            if isinstance(atual, int) and isinstance(total, int) and total > 0:
                return 0.85 + 0.09 * min(atual / total, 1)
            return None
        if fase == "analise_v2_concluida":
            return 0.94

    if metodo == "v3" and lexical != semantica:
        # O híbrido mantém os pesos já validados. Nos modos isolados, cada
        # intervalo pertence exclusivamente a uma etapa que está realmente
        # ativa, sem reservar espaço para a modalidade desativada.
        if fase == "estimando_fonte":
            atual = evento.get("pagina_atual")
            total = evento.get("paginas_total")
            if isinstance(atual, int) and isinstance(total, int) and total > 0:
                return 0.05 + 0.25 * min(atual / total, 1)
            return None
        if fase in {"paginas", "ocr"}:
            atual = evento.get("pagina_atual")
            total = evento.get("paginas_total")
            if isinstance(atual, int) and isinstance(total, int) and total > 0:
                return 0.30 + 0.25 * min(atual / total, 1)
            return None
        if lexical and fase == "busca_lexical":
            atual = evento.get("bloco_atual")
            total = evento.get("blocos_total")
            if isinstance(atual, int) and isinstance(total, int) and total > 0:
                return 0.55 + 0.39 * min(atual / total, 1)
            return None
        if semantica and fase == "preparando_blocos_semanticos":
            atual = evento.get("bloco_atual")
            total = evento.get("blocos_total")
            if isinstance(atual, int) and isinstance(total, int) and total > 0:
                return 0.55 + 0.10 * min(atual / total, 1)
            return None
        if semantica and fase == "codificando_blocos_semanticos":
            atual = evento.get("bloco_atual")
            total = evento.get("blocos_total")
            if isinstance(atual, int) and isinstance(total, int) and total > 0:
                return 0.65 + 0.25 * min(atual / total, 1)
            return None
        if semantica and fase == "comparando_semantica":
            atual = evento.get("consulta_atual")
            total = evento.get("consultas_total")
            if isinstance(atual, int) and isinstance(total, int) and total > 0:
                return 0.90 + 0.04 * min(atual / total, 1)
            return None

    if fase == "estimando_fonte":
        atual = evento.get("pagina_atual")
        total = evento.get("paginas_total")
        if isinstance(atual, int) and isinstance(total, int) and total > 0:
            return 0.05 + 0.30 * min(atual / total, 1)
        return None

    if fase in {"paginas", "ocr"}:
        atual = evento.get("pagina_atual")
        total = evento.get("paginas_total")
        if isinstance(atual, int) and isinstance(total, int) and total > 0:
            return 0.35 + 0.25 * min(atual / total, 1)
        return None

    if fase == "busca_lexical":
        atual = evento.get("bloco_atual")
        total = evento.get("blocos_total")
        if not isinstance(atual, int) or not isinstance(total, int) or total <= 0:
            return None
        inicio = 0.60
        fim = 0.82 if metodo == "v3" and semantica else 0.90
        return inicio + (fim - inicio) * min(atual / total, 1)

    if fase == "preparando_blocos_semanticos":
        atual = evento.get("bloco_atual")
        total = evento.get("blocos_total")
        if not isinstance(atual, int) or not isinstance(total, int) or total <= 0:
            return None
        inicio = 0.82 if lexical else 0.60
        return inicio + 0.06 * min(atual / total, 1)

    if fase == "codificando_blocos_semanticos":
        atual = evento.get("bloco_atual")
        total = evento.get("blocos_total")
        if not isinstance(atual, int) or not isinstance(total, int) or total <= 0:
            return None
        inicio = 0.88
        return inicio + 0.06 * min(atual / total, 1)

    if fase == "comparando_semantica":
        atual = evento.get("consulta_atual")
        total = evento.get("consultas_total")
        if not isinstance(atual, int) or not isinstance(total, int) or total <= 0:
            return None
        return 0.94 + 0.02 * min(atual / total, 1)

    if fase == "gerando_planilha":
        return 0.96
    if fase == "pdf_concluido":
        return 1.0
    if fase == "gerando_consolidado":
        return 0.99
    if fase == "preparando_resultados":
        return 0.995
    return None


class RelatorDeProgresso:
    """Atualiza de forma thread-safe um registro leve de progresso."""

    def __init__(self, job_id: str, metodo: str, configuracoes: dict | None):
        self.job_id = job_id
        self.metodo = metodo
        self.configuracoes = configuracoes

    def __call__(self, evento: dict) -> None:
        agora = _agora_monotonicamente()
        with PROGRESSOS_LOCK:
            dados = PROGRESSOS.get(self.job_id)
            if not dados or dados["status"] != "processando":
                return

            # O tempo usado para ler páginas não prediz o carregamento do
            # modelo. Até uma unidade semântica efetivamente terminar, a ETA
            # deve permanecer indeterminada em vez de reaproveitar amostras
            # da etapa lexical/de extração.
            preparando_modelo = evento.get("preparando_modelo") or evento.get(
                "fase"
            ) in {"preparando_modelo_semantico", "modelo_semantico_carregado"}
            if preparando_modelo:
                dados["eta_segundos"] = None
                dados["_amostras_eta"] = 0

            for origem, destino in (
                ("fase", "fase"),
                ("arquivo", "arquivo_atual"),
                ("arquivo_indice", "arquivo_indice"),
                ("arquivos_total", "arquivos_total"),
                ("pagina_atual", "pagina_atual"),
                ("paginas_total", "paginas_total"),
                ("paginas_processadas_total", "paginas_processadas_total"),
                ("paginas_total_global", "paginas_total_global"),
                ("paginas_anteriores", "paginas_anteriores"),
                ("paginas_arquivo", "paginas_arquivo"),
                ("bloco_atual", "bloco_atual"),
                ("blocos_total", "blocos_total"),
                ("consulta_atual", "consulta_atual"),
                ("consultas_total", "consultas_total"),
            ):
                if origem in evento:
                    dados[destino] = evento[origem]

            if evento.get("etapa"):
                dados["etapa"] = str(evento["etapa"])
            if evento.get("mensagem"):
                dados["mensagem"] = str(evento["mensagem"])
            dados["preparando_modelo"] = bool(evento.get("preparando_modelo", False))

            fracao_local = _fracao_local_do_evento(
                evento, self.metodo, self.configuracoes
            )
            indice = dados.get("arquivo_indice") or 1
            total_arquivos = max(1, int(dados.get("arquivos_total") or 1))
            if fracao_local is not None:
                if (
                    self.metodo == "v2"
                    and evento.get("fase") in {"paginas", "ocr"}
                    and dados.get("paginas_total_global")
                    and dados.get("paginas_processadas_total") is not None
                ):
                    # Durante a extração da V2, a barra reflete diretamente
                    # páginas concluídas no conjunto inteiro de PDFs.
                    candidato = 100 * (
                        float(dados["paginas_processadas_total"])
                        / float(dados["paginas_total_global"])
                    )
                elif (
                    self.metodo == "v2"
                    and dados.get("paginas_total_global")
                    and dados.get("paginas_arquivo")
                ):
                    candidato = 100 * (
                        (
                            float(dados.get("paginas_anteriores") or 0)
                            + fracao_local * float(dados["paginas_arquivo"])
                        )
                        / float(dados["paginas_total_global"])
                    )
                else:
                    candidato = 100 * ((indice - 1 + fracao_local) / total_arquivos)
                percentual_anterior = dados.get("percentual")
                dados["percentual"] = max(
                    float(percentual_anterior or 0), min(99.9, candidato)
                )
                self._atualizar_eta(dados, agora)

    @staticmethod
    def _atualizar_eta(dados: dict, agora: float) -> None:
        percentual = dados.get("percentual")
        if percentual is None:
            return

        anterior = dados.get("_ultimo_percentual")
        instante_anterior = dados.get("_ultima_amostra")
        if anterior is not None and instante_anterior is not None:
            delta_percentual = percentual - anterior
            delta_tempo = agora - instante_anterior
            if delta_percentual > 0 and delta_tempo > 0:
                taxa_instantanea = delta_percentual / delta_tempo
                taxa_anterior = dados.get("_taxa_suavizada")
                dados["_taxa_suavizada"] = (
                    taxa_instantanea
                    if taxa_anterior is None
                    else 0.30 * taxa_instantanea + 0.70 * taxa_anterior
                )
                dados["_amostras_eta"] += 1

        dados["_ultimo_percentual"] = percentual
        dados["_ultima_amostra"] = agora
        taxa = dados.get("_taxa_suavizada")
        tempo_decorrido = agora - dados["tempo_inicio"]
        if dados["_amostras_eta"] >= 2 and tempo_decorrido >= 3 and taxa and taxa > 0:
            dados["eta_segundos"] = max(0, (100 - percentual) / taxa)

    def concluir(self, resultado_url: str) -> None:
        with PROGRESSOS_LOCK:
            dados = PROGRESSOS.get(self.job_id)
            if not dados:
                return
            dados.update(
                {
                    "status": "concluido",
                    "etapa": "Análise concluída",
                    "percentual": 100.0,
                    "eta_segundos": 0,
                    "resultado_url": resultado_url,
                    "terminado_em": _agora_monotonicamente(),
                    "preparando_modelo": False,
                }
            )

    def erro(self, mensagem_publica: str) -> None:
        with PROGRESSOS_LOCK:
            dados = PROGRESSOS.get(self.job_id)
            if not dados:
                return
            dados.update(
                {
                    "status": "erro",
                    "erro": mensagem_publica,
                    "terminado_em": _agora_monotonicamente(),
                    "preparando_modelo": False,
                }
            )


def _novo_progresso(job_id: str, versao: str, total_arquivos: int, owner_user_id: str | None = None) -> None:
    _limpar_progressos_expirados()
    with PROGRESSOS_LOCK:
        PROGRESSOS[job_id] = {
            "owner_user_id": owner_user_id,
            "status": "processando",
            "metodo": versao,
            "etapa": "Preparando análise…",
            "fase": "preparando_analise",
            "percentual": None,
            "arquivo_atual": None,
            "arquivo_indice": 0,
            "arquivos_total": total_arquivos,
            "pagina_atual": 0,
            "paginas_total": 0,
            "paginas_processadas_total": 0,
            "paginas_total_global": 0,
            "paginas_anteriores": 0,
            "paginas_arquivo": 0,
            "bloco_atual": 0,
            "blocos_total": 0,
            "consulta_atual": 0,
            "consultas_total": 0,
            "tempo_inicio": _agora_monotonicamente(),
            "eta_segundos": None,
            "mensagem": "Aguarde enquanto os PDFs são analisados. Esse processo pode levar alguns minutos.",
            "resultado_url": None,
            "erro": None,
            "preparando_modelo": False,
            "_ultimo_percentual": None,
            "_ultima_amostra": None,
            "_taxa_suavizada": None,
            "_amostras_eta": 0,
        }


def _progresso_publico(job_id: str) -> dict | None:
    _limpar_progressos_expirados()
    with PROGRESSOS_LOCK:
        dados = PROGRESSOS.get(job_id)
        if dados is None:
            return None
        agora = _agora_monotonicamente()
        percentual_publico = (
            round(dados["percentual"])
            if dados["percentual"] is not None
            else None
        )
        if dados["status"] == "processando" and percentual_publico is not None:
            percentual_publico = min(99, percentual_publico)
        return {
            "job_id": job_id,
            "status": dados["status"],
            "metodo": dados["metodo"],
            "etapa": dados["etapa"],
            "fase": dados["fase"],
            "percentual": percentual_publico,
            "arquivo_atual": dados["arquivo_atual"],
            "arquivo_indice": dados["arquivo_indice"],
            "arquivos_total": dados["arquivos_total"],
            "pagina_atual": dados["pagina_atual"],
            "paginas_total": dados["paginas_total"],
            "paginas_processadas_total": dados["paginas_processadas_total"],
            "paginas_total_global": dados["paginas_total_global"],
            "bloco_atual": dados["bloco_atual"],
            "blocos_total": dados["blocos_total"],
            "consulta_atual": dados["consulta_atual"],
            "consultas_total": dados["consultas_total"],
            "tempo_decorrido": max(0, round(agora - dados["tempo_inicio"])),
            "eta_segundos": (
                round(dados["eta_segundos"])
                if dados["eta_segundos"] is not None
                else None
            ),
            "mensagem": dados["mensagem"],
            "resultado_url": dados["resultado_url"],
            "erro": dados["erro"],
            "preparando_modelo": dados["preparando_modelo"],
        }


def _resposta_erro(mensagem: str, status: int = 400):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"erro": mensagem}), status
    flash(mensagem, "danger")
    return render_template("index.html")


def _executar_job(
    job_id: str,
    pdfs_salvos: list[Path],
    termos: list[dict[str, str]],
    pasta_saida: Path,
    versao: str,
    configuracoes_v3: dict | None,
    resultado_url: str,
    owner_user_id: str | None = None,
) -> None:
    relator = RelatorDeProgresso(job_id, versao, configuracoes_v3)
    try:
        resultado = executar_analises(
            pdfs_salvos,
            termos,
            pasta_saida,
            versao,
            configuracoes_v3,
            progress_callback=relator,
        )
        relator(
            {"fase": "preparando_resultados", "etapa": "Preparando resultados…"}
        )
        resultado["dashboard"] = criar_dashboard(resultado)
        resultado["pasta_saida"] = pasta_saida
        resultado["owner_user_id"] = owner_user_id
        with ANALISES_LOCK:
            ANALISES[job_id] = resultado
        relator.concluir(resultado_url)
    except Exception:
        app.logger.exception("Falha no job local %s", job_id)
        relator.erro(
            "Não foi possível concluir a análise. Verifique os arquivos enviados e tente novamente."
        )


def _arquivo_pdf_valido(nome: str) -> bool:
    return Path(nome).suffix.casefold() == ".pdf"


def _nome_upload_seguro(nome: str) -> str:
    """Remove componentes de caminho sem descartar acentos do nome do PDF."""
    base = nome.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(". ")
    return base or "documento.pdf"


def _nome_disponivel(pasta: Path, nome: str) -> Path:
    candidato = pasta / nome
    contador = 2
    while candidato.exists():
        candidato = pasta / f"{Path(nome).stem}_{contador}{Path(nome).suffix}"
        contador += 1
    return candidato


def _configuracoes_v3() -> dict[str, object]:
    """Lê controles exclusivos da V3 sem afetar as versões lexicais."""
    return {
        "incluir_lexical": request.form.get("incluir_lexical") == "on",
        "incluir_semantica": request.form.get("incluir_semantica") == "on",
        "limiar_semantico": normalize_semantic_threshold(request.form.get("limiar_semantico", template_settings()["default"])),
    }


def _termos_digitados_tem_separador_invalido(texto: str) -> bool:
    """Impede que o campo manual use separadores diferentes de ponto e vírgula."""
    return "," in texto or "." in texto


@app.errorhandler(413)
def arquivo_grande(_erro):
    return _resposta_erro("O envio ultrapassa o limite de 1 GB da aplicação local.", 413)


@app.route("/", methods=["GET", "POST"])
def inicio():
    if request.method == "GET":
        return render_template("index.html", job_inicial=request.args.get("job", ""))

    versao = request.form.get("versao", "").casefold()
    if versao not in ANALISADORES:
        return _resposta_erro("Escolha a metodologia da varredura antes de iniciar a análise.")

    texto_termos = request.form.get("termos", "")
    if _termos_digitados_tem_separador_invalido(texto_termos):
        return _resposta_erro("Use ponto e vírgula (;) para separar os termos de pesquisa.")

    configuracoes_v3 = _configuracoes_v3() if versao == "v3" else None
    if (
        configuracoes_v3
        and not configuracoes_v3["incluir_lexical"]
        and not configuracoes_v3["incluir_semantica"]
    ):
        return _resposta_erro("Na V3, selecione a busca lexical, a semântica ou ambas.")

    arquivos_pdf = [
        arquivo
        for arquivo in request.files.getlist("pdfs")
        if arquivo and arquivo.filename
    ]
    if not arquivos_pdf:
        return _resposta_erro("Envie ao menos um arquivo PDF.")

    invalidos = [arquivo.filename for arquivo in arquivos_pdf if not _arquivo_pdf_valido(arquivo.filename)]
    if invalidos:
        return _resposta_erro("Apenas arquivos com extensão .pdf são aceitos.")

    arquivo_termos = request.files.get("arquivo_termos")
    texto_arquivo = ""
    if arquivo_termos and arquivo_termos.filename:
        if Path(arquivo_termos.filename).suffix.casefold() != ".txt":
            return _resposta_erro("O arquivo de termos deve ter extensão .txt.")
        texto_arquivo = ler_arquivo_termos(arquivo_termos.read())

    termos = montar_termos(texto_termos, texto_arquivo, versao)
    if not termos:
        return _resposta_erro(
            "Informe pelo menos um termo no campo de texto ou no arquivo TXT."
        )

    job_id = str(uuid4())
    pasta_upload = UPLOAD_DIR / job_id
    pasta_saida = OUTPUT_DIR / job_id
    pasta_upload.mkdir(parents=True)

    pdfs_salvos: list[Path] = []
    for arquivo in arquivos_pdf:
        nome = _nome_upload_seguro(arquivo.filename)
        destino = _nome_disponivel(pasta_upload, nome)
        arquivo.save(destino)
        pdfs_salvos.append(destino)

    with ACCOUNT_LIFECYCLE_LOCK:
        if not account_accepts_new_work(current_user.id):
            for caminho in pdfs_salvos:
                caminho.unlink(missing_ok=True)
            pasta_upload.rmdir()
            return _resposta_erro("A conta não está mais disponível para processamento.")
        _novo_progresso(job_id, versao, len(pdfs_salvos), current_user.id)
        resultado_url = url_for("resultado", identificador=job_id)
        EXECUTOR_ANALISES.submit(
            _executar_job,
            job_id,
            pdfs_salvos,
            termos,
            pasta_saida,
            versao,
            configuracoes_v3,
            resultado_url,
            current_user.id,
        )
    # O executor já recebeu todos os valores simples necessários; a resposta
    # retorna imediatamente para que o navegador inicie o polling.
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(
            {
                "job_id": job_id,
                "status": "processando",
                "progresso_url": url_for("progresso", job_id=job_id),
            }
        ), 202

    return redirect(url_for("inicio", job=job_id))


@app.get("/resultado/<identificador>")
def resultado(identificador: str):
    with ANALISES_LOCK:
        dados = ANALISES.get(identificador)
    if not dados:
        abort(404)
    if dados.get("owner_user_id") != current_user.id and current_user.role != "admin":
        abort(404)
    return render_template("resultado.html", resultado=dados, identificador=identificador)


@app.get("/download/<identificador>/<nome_arquivo>")
def download(identificador: str, nome_arquivo: str):
    with ANALISES_LOCK:
        dados = ANALISES.get(identificador)
    if not dados or nome_arquivo != Path(nome_arquivo).name:
        abort(404)
    if dados.get("owner_user_id") != current_user.id and current_user.role != "admin":
        abort(404)

    nomes_permitidos = {arquivo["nome"] for arquivo in dados["arquivos"]}
    if nome_arquivo not in nomes_permitidos:
        abort(404)

    return send_from_directory(dados["pasta_saida"], nome_arquivo, as_attachment=True)


@app.get("/api/progresso/<job_id>")
def progresso(job_id: str):
    with PROGRESSOS_LOCK:
        owner = PROGRESSOS.get(job_id, {}).get("owner_user_id")
    if owner is None or (owner != current_user.id and current_user.role != "admin"):
        return jsonify({"erro": "Análise não encontrada."}), 404
    dados = _progresso_publico(job_id)
    if dados is None:
        return jsonify({"erro": "Análise não encontrada."}), 404
    return jsonify(dados)


def create_app(test_config: dict | None = None) -> Flask:
    """Cria instância isolada para testes sem tocar no banco/dados reais.

    A instância de produção continua sendo ``app`` para Gunicorn. Os handlers
    legados são registrados na nova instância sem duplicar sua lógica.
    """
    if test_config is None:
        return app
    isolated = Flask(__name__, template_folder="templates", static_folder="static")
    isolated.config.update(app.config)
    isolated.config.update(test_config)
    if "SQLALCHEMY_ENGINE_OPTIONS" not in test_config:
        isolated.config["SQLALCHEMY_ENGINE_OPTIONS"] = (
            {"connect_args": {"timeout": 30}}
            if isolated.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:") else {}
        )
    db.init_app(isolated)
    login_manager.init_app(isolated)
    migrate.init_app(isolated, db)
    csrf.init_app(isolated)
    register_cli(isolated)
    register_presentation(isolated)
    isolated.context_processor(_semantic_threshold_template_context)
    isolated.register_blueprint(auth_bp)
    isolated.register_blueprint(projects_bp)
    isolated.register_blueprint(admin_bp)
    isolated.register_blueprint(profile_bp)
    isolated.register_blueprint(historico_racial_bp)
    isolated.before_request(_enforce_platform_access)
    isolated.register_error_handler(CSRFError, _csrf_error)
    isolated.register_error_handler(413, arquivo_grande)
    for rule in app.url_map.iter_rules():
        if rule.endpoint in {"static"} or "." in rule.endpoint:
            continue
        isolated.add_url_rule(
            rule.rule, endpoint=rule.endpoint, view_func=app.view_functions[rule.endpoint],
            methods=rule.methods,
        )
    return isolated


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
