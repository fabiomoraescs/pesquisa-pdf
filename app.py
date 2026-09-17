"""Aplicação local Flask para as versões V1, V2 e V3 da Varredura de PDFs."""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for

from analyzer.common import (
    ANALISADORES,
    criar_dashboard,
    executar_analises,
    ler_arquivo_termos,
    montar_termos,
)


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", "varredura-local"),
    MAX_CONTENT_LENGTH=1024 * 1024 * 1024,
)

# A aplicação não possui histórico permanente. O cache só mantém a análise
# atual enquanto o servidor estiver aberto, para exibir o dashboard e baixar
# os arquivos gerados pela mesma sessão.
ANALISES: dict[str, dict] = {}


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
    try:
        limiar = float(request.form.get("limiar_semantico", "0.70"))
    except ValueError:
        limiar = 0.70
    return {
        "incluir_lexical": request.form.get("incluir_lexical") == "on",
        "incluir_semantica": request.form.get("incluir_semantica") == "on",
        "limiar_semantico": min(0.90, max(0.50, limiar)),
    }


def _termos_digitados_tem_separador_invalido(texto: str) -> bool:
    """Impede que o campo manual use separadores diferentes de ponto e vírgula."""
    return "," in texto or "." in texto


@app.errorhandler(413)
def arquivo_grande(_erro):
    flash("O envio ultrapassa o limite de 1 GB da aplicação local.", "danger")
    return redirect(url_for("inicio"))


@app.route("/", methods=["GET", "POST"])
def inicio():
    if request.method == "GET":
        return render_template("index.html")

    versao = request.form.get("versao", "").casefold()
    if versao not in ANALISADORES:
        flash("Escolha a metodologia da varredura antes de iniciar a análise.", "danger")
        return render_template("index.html")

    texto_termos = request.form.get("termos", "")
    if _termos_digitados_tem_separador_invalido(texto_termos):
        flash("Use ponto e vírgula (;) para separar os termos de pesquisa.", "danger")
        return render_template("index.html")

    configuracoes_v3 = _configuracoes_v3() if versao == "v3" else None
    if (
        configuracoes_v3
        and not configuracoes_v3["incluir_lexical"]
        and not configuracoes_v3["incluir_semantica"]
    ):
        flash("Na V3, selecione a busca lexical, a semântica ou ambas.", "danger")
        return render_template("index.html")

    arquivos_pdf = [
        arquivo
        for arquivo in request.files.getlist("pdfs")
        if arquivo and arquivo.filename
    ]
    if not arquivos_pdf:
        flash("Envie ao menos um arquivo PDF.", "danger")
        return render_template("index.html")

    invalidos = [arquivo.filename for arquivo in arquivos_pdf if not _arquivo_pdf_valido(arquivo.filename)]
    if invalidos:
        flash("Apenas arquivos com extensão .pdf são aceitos.", "danger")
        return render_template("index.html")

    arquivo_termos = request.files.get("arquivo_termos")
    texto_arquivo = ""
    if arquivo_termos and arquivo_termos.filename:
        if Path(arquivo_termos.filename).suffix.casefold() != ".txt":
            flash("O arquivo de termos deve ter extensão .txt.", "danger")
            return render_template("index.html")
        texto_arquivo = ler_arquivo_termos(arquivo_termos.read())

    termos = montar_termos(texto_termos, texto_arquivo, versao)
    if not termos:
        flash("Informe pelo menos um termo no campo de texto ou no arquivo TXT.", "danger")
        return render_template("index.html")

    identificador = uuid4().hex
    pasta_upload = UPLOAD_DIR / identificador
    pasta_saida = OUTPUT_DIR / identificador
    pasta_upload.mkdir(parents=True)

    pdfs_salvos: list[Path] = []
    for arquivo in arquivos_pdf:
        nome = _nome_upload_seguro(arquivo.filename)
        destino = _nome_disponivel(pasta_upload, nome)
        arquivo.save(destino)
        pdfs_salvos.append(destino)

    resultado = executar_analises(
        pdfs_salvos,
        termos,
        pasta_saida,
        versao,
        configuracoes_v3,
    )
    resultado["dashboard"] = criar_dashboard(resultado)
    resultado["pasta_saida"] = pasta_saida
    ANALISES[identificador] = resultado

    if not resultado["quantidade_pdfs"]:
        flash("Nenhum PDF pôde ser analisado. Veja os detalhes abaixo.", "danger")
    return redirect(url_for("resultado", identificador=identificador))


@app.get("/resultado/<identificador>")
def resultado(identificador: str):
    dados = ANALISES.get(identificador)
    if not dados:
        abort(404)
    return render_template("resultado.html", resultado=dados, identificador=identificador)


@app.get("/download/<identificador>/<nome_arquivo>")
def download(identificador: str, nome_arquivo: str):
    dados = ANALISES.get(identificador)
    if not dados or nome_arquivo != Path(nome_arquivo).name:
        abort(404)

    nomes_permitidos = {arquivo["nome"] for arquivo in dados["arquivos"]}
    if nome_arquivo not in nomes_permitidos:
        abort(404)

    return send_from_directory(dados["pasta_saida"], nome_arquivo, as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
