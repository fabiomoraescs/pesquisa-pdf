"""Coordenação independente da leitura e da identificação lexical."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .context import construir_paragrafos, obter_contexto
from .dictionaries import carregar_entidades
from .entities import listar_entidades
from .occurrences import BuscadorLexical
from .pdf import OCRIndisponivelError, PDFInvalidoError, contar_paginas, extrair_paginas
from .vocabulary import entidades_pesquisaveis


class ProcessamentoError(ValueError):
    """Não há documento com texto recuperável para apresentar."""


@dataclass(frozen=True, slots=True)
class ArquivoPDF:
    caminho: Path
    nome_original: str
    id_arquivo: str


def _emitir(callback: Callable[[dict[str, Any]], None] | None, **evento: Any) -> None:
    if callback is not None:
        callback(evento)


def processar_documentos(
    arquivos: list[ArquivoPDF],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    vocabulario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Um PDF = um documento; resultados são candidatos lexicais à revisão.

    O progresso usa duas unidades reais por página: leitura/OCR e identificação
    lexical sobre o texto já extraído. Nenhum PDF é relido para procurar termos.
    """
    if not arquivos:
        raise ProcessamentoError("Envie ao menos um PDF.")
    if vocabulario is None:
        configuracao_entidades = carregar_entidades()
        buscador = BuscadorLexical(listar_entidades())
        grupos = configuracao_entidades["grupos"]
        vocabulario_version = None
        vocabulario_hash = None
    else:
        conteudo = vocabulario["vocabulario"]
        buscador = BuscadorLexical(entidades_pesquisaveis(conteudo))
        grupos = {codigo: grupo["nome"] for codigo, grupo in conteudo["grupos"].items()}
        vocabulario_version = vocabulario["version"]
        vocabulario_hash = vocabulario["hash"]
    erros: list[dict[str, str]] = []
    validos: list[tuple[int, ArquivoPDF, int]] = []
    for indice, arquivo in enumerate(arquivos, start=1):
        try:
            validos.append((indice, arquivo, contar_paginas(arquivo.caminho)))
        except PDFInvalidoError as erro:
            erros.append({"arquivo_pdf": arquivo.nome_original, "mensagem": str(erro)})
    if not validos:
        raise ProcessamentoError("Nenhum PDF válido pôde ser lido.")

    total_paginas = sum(total for _, _, total in validos)
    total_unidades = 2 * total_paginas
    unidades_concluidas = 0
    documentos: list[dict[str, Any]] = []
    ocorrencias: list[dict[str, Any]] = []
    total_arquivos = len(arquivos)

    for indice_arquivo, arquivo, paginas_arquivo in validos:
        _emitir(
            progress_callback,
            etapa="Lendo PDF…",
            arquivo_atual=arquivo.nome_original,
            arquivo_indice=indice_arquivo,
            arquivos_total=total_arquivos,
            pagina_atual=0,
            paginas_total=paginas_arquivo,
            percentual=min(99, int(unidades_concluidas * 100 / total_unidades)),
        )
        paginas: list[dict[str, Any]] = []

        def evento_ocr(evento: dict[str, Any]) -> None:
            _emitir(
                progress_callback,
                etapa=evento["etapa"],
                arquivo_atual=arquivo.nome_original,
                arquivo_indice=indice_arquivo,
                arquivos_total=total_arquivos,
                pagina_atual=evento["pagina_atual"],
                paginas_total=paginas_arquivo,
                percentual=min(99, int(unidades_concluidas * 100 / total_unidades)),
            )

        try:
            for pagina in extrair_paginas(arquivo.caminho, evento_ocr if progress_callback else None):
                paginas.append(pagina)
                unidades_concluidas += 1
                _emitir(
                    progress_callback,
                    etapa="Executando OCR…" if pagina["ocr_utilizado"] else "Lendo PDF…",
                    arquivo_atual=arquivo.nome_original,
                    arquivo_indice=indice_arquivo,
                    arquivos_total=total_arquivos,
                    pagina_atual=pagina["pagina_pdf"],
                    paginas_total=paginas_arquivo,
                    percentual=min(99, int(unidades_concluidas * 100 / total_unidades)),
                )
        except (PDFInvalidoError, OCRIndisponivelError, RuntimeError, OSError) as erro:
            erros.append({"arquivo_pdf": arquivo.nome_original, "mensagem": str(erro)})
            continue

        paragrafos = construir_paragrafos(paginas)
        if not paragrafos:
            erros.append({
                "arquivo_pdf": arquivo.nome_original,
                "mensagem": "Nenhum texto recuperável foi encontrado no PDF.",
            })
            continue

        id_documento = str(uuid4())
        documentos.append({
            "id_documento": id_documento,
            "arquivo_pdf": arquivo.nome_original,
            "id_arquivo": arquivo.id_arquivo,
            "pagina_pdf_inicio": 1,
            "pagina_pdf_fim": paginas_arquivo,
            "ocr_utilizado": any(pagina["ocr_utilizado"] for pagina in paginas),
            "ocr_idioma_utilizado": next((pagina.get("ocr_idioma_utilizado") for pagina in paginas if pagina.get("ocr_idioma_utilizado")), None),
            "qualidade_ocr": None,
            "publicacao": None,
            "organizacao": None,
            "ano": None,
            "autor": None,
            "titulo": None,
            "pagina_impressa": None,
        })
        indices_por_pagina: dict[int, list[int]] = defaultdict(list)
        for indice_paragrafo, paragrafo in enumerate(paragrafos):
            indices_por_pagina[paragrafo["pagina_pdf"]].append(indice_paragrafo)

        for pagina in paginas:
            for indice_paragrafo in indices_por_pagina[pagina["pagina_pdf"]]:
                texto = paragrafos[indice_paragrafo]["texto"]
                contexto = obter_contexto(paragrafos, indice_paragrafo)
                for correspondencia in buscador.localizar(texto):
                    entidade = correspondencia.entidade
                    ocorrencias.append({
                        "id_ocorrencia": str(uuid4()),
                        "id_documento": id_documento,
                        "arquivo_pdf": arquivo.nome_original,
                        "pagina_pdf": pagina["pagina_pdf"],
                        "categoria_busca": "entidades",
                        "tipo_entidade": entidade.tipo_entidade,
                        "id_entidade": entidade.id_entidade,
                        "entidade_canonica": entidade.forma_canonica,
                        "variantes": list(entidade.variantes),
                        "grupo": list(entidade.grupo),
                        "termo_encontrado": correspondencia.termo_encontrado,
                        "forma_original_no_texto": correspondencia.forma_original_no_texto,
                        "metodo_localizacao": "lexical",
                        **contexto,
                        "tradicao_intelectual": entidade.tradicao_intelectual,
                        "pais_regiao_matriz": entidade.pais_regiao,
                        "relevancia_ocorrencia": "revisar",
                        "validar": "pendente",
                        "requer_validacao": True,
                        "forma_referencia": "",
                        "operacao_repertorio": "",
                    })
            unidades_concluidas += 1
            _emitir(
                progress_callback,
                etapa="Identificando ocorrências…",
                arquivo_atual=arquivo.nome_original,
                arquivo_indice=indice_arquivo,
                arquivos_total=total_arquivos,
                pagina_atual=pagina["pagina_pdf"],
                paginas_total=paginas_arquivo,
                percentual=min(99, int(unidades_concluidas * 100 / total_unidades)),
            )

    if not documentos:
        raise ProcessamentoError("Nenhum PDF com texto recuperável pôde ser processado.")
    return {
        "documentos": documentos,
        "ocorrencias": ocorrencias,
        "grupos": grupos,
        "erros": erros,
        "total_pdfs": len(documentos),
        "total_ocorrencias": len(ocorrencias),
        "entidades_distintas": len({item["id_entidade"] for item in ocorrencias}),
        "vocabulario_version": vocabulario_version,
        "vocabulario_hash": vocabulario_hash,
    }
