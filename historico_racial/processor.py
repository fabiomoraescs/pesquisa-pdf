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
    *,
    project_id: str | None = None,
    vocabulary_version: str | None = None,
    vocabulary_hash: str | None = None,
    metodo_analise: str = "lexical",
    limiar_semantico: float | None = None,
) -> dict[str, Any]:
    """Um PDF = um documento; resultados são candidatos lexicais à revisão.

    O progresso usa duas unidades reais por página: leitura/OCR e identificação
    lexical sobre o texto já extraído. Nenhum PDF é relido para procurar termos.
    """
    if not arquivos:
        raise ProcessamentoError("Envie ao menos um PDF.")
    if metodo_analise not in {"lexical", "hibrido"}:
        raise ProcessamentoError("Selecione um método de análise válido.")
    if metodo_analise == "hibrido" and limiar_semantico is None:
        from analyzer.v3 import LIMIAR_PADRAO
        limiar_semantico = LIMIAR_PADRAO
    if vocabulario is None:
        configuracao_entidades = carregar_entidades()
        buscador = BuscadorLexical(listar_entidades())
        grupos = configuracao_entidades["grupos"]
        vocabulario_version = None
        vocabulario_hash = None
        entity_sources: dict[str, dict[str, Any]] = {}
        entidades_ativas = listar_entidades()
    else:
        conteudo = vocabulario["vocabulario"]
        entidades_ativas = entidades_pesquisaveis(conteudo)
        buscador = BuscadorLexical(entidades_ativas)
        grupos = {codigo: grupo["nome"] for codigo, grupo in conteudo["grupos"].items()}
        vocabulario_version = vocabulario["version"]
        vocabulario_hash = vocabulario["hash"]
        entity_sources = {item["id_entidade"]: item for item in conteudo["entidades"]}
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
    total_unidades = (2 if metodo_analise == "lexical" else 3) * total_paginas
    escala_lexical = 100
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
            percentual=min(99, int(unidades_concluidas * escala_lexical / total_unidades)),
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
                percentual=min(99, int(unidades_concluidas * escala_lexical / total_unidades)),
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
                    percentual=min(99, int(unidades_concluidas * escala_lexical / total_unidades)),
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
            "id_project": project_id,
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
                    source = entity_sources.get(entidade.id_entidade, {})
                    ocorrencias.append({
                        "id_project": project_id,
                        "id_ocorrencia": str(uuid4()),
                        "id_documento": id_documento,
                        "arquivo_pdf": arquivo.nome_original,
                        "pagina_pdf": pagina["pagina_pdf"],
                        "categoria_busca": "entidades",
                        "tipo_entidade": entidade.tipo_entidade,
                        "id_entidade": entidade.id_entidade,
                        "entity_key": source.get("entity_key", entidade.id_entidade),
                        "bibliotecas_origem": list(source.get("source_libraries", [])),
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
                percentual=min(99, int(unidades_concluidas * escala_lexical / total_unidades)),
            )

        if metodo_analise == "hibrido":
            from .semantic import combinar_semantica

            def evento_semantico(evento: dict[str, Any]) -> None:
                atual = evento.get("bloco_atual")
                total = evento.get("blocos_total")
                fracao_blocos = atual / total if isinstance(atual, int) and isinstance(total, int) and total > 0 else 0
                fase = evento.get("fase")
                # Pesos apenas de exibição sobre etapas efetivas: 20% criação
                # de chunks, 60% codificação, 20% comparação das consultas.
                if fase == "preparando_blocos_semanticos":
                    fracao = .20 * fracao_blocos
                elif fase == "codificando_blocos_semanticos":
                    fracao = .20 + .60 * fracao_blocos
                elif fase == "comparando_semantica":
                    consultas = evento.get("consultas_total") or 0
                    fracao = .80 + .20 * (evento.get("consulta_atual", 0) / consultas if consultas else 0)
                else:
                    fracao = .20
                _emitir(
                    progress_callback, etapa=evento.get("etapa", "Analisando correspondências semânticas…"),
                    arquivo_atual=arquivo.nome_original, arquivo_indice=indice_arquivo,
                    arquivos_total=total_arquivos, pagina_atual=paginas_arquivo,
                    paginas_total=paginas_arquivo, bloco_atual=atual, blocos_total=total,
                    percentual=min(99, int((unidades_concluidas + paginas_arquivo * fracao) * 100 / total_unidades)),
                )

            ocorrencias.extend(combinar_semantica(
                arquivo.caminho, paginas, entidades_ativas, ocorrencias, id_documento,
                arquivo.nome_original, project_id, entity_sources, limiar_semantico,
                evento_semantico if progress_callback else None,
            ))
            unidades_concluidas += paginas_arquivo

    if not documentos:
        raise ProcessamentoError("Nenhum PDF com texto recuperável pôde ser processado.")
    return {
        "project_id": project_id,
        "documentos": documentos,
        "ocorrencias": ocorrencias,
        "grupos": grupos,
        "erros": erros,
        "total_pdfs": len(documentos),
        "total_ocorrencias": len(ocorrencias),
        "entidades_distintas": len({item["id_entidade"] for item in ocorrencias}),
        "metodo_analise": metodo_analise,
        "limiar_semantico": limiar_semantico if metodo_analise == "hibrido" else None,
        "modelo_semantico": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2" if metodo_analise == "hibrido" else None,
        "vocabulario_version": vocabulary_version or vocabulario_version,
        "vocabulario_hash": vocabulary_hash or vocabulario_hash,
    }
