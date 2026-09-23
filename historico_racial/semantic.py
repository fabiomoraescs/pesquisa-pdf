"""Adaptador do motor semântico V3 para o vocabulário de um projeto.

Não implementa embeddings, similaridade, chunking ou limiar próprios.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from .occurrences import normalizar_com_mapa


def _comparavel(texto: str) -> str:
    return " ".join(normalizar_com_mapa(texto)[0].split())


def combinar_semantica(
    arquivo: Path,
    paginas: list[dict[str, Any]],
    entidades: tuple,
    ocorrencias_lexicais: list[dict[str, Any]],
    id_documento: str,
    nome_pdf: str,
    id_project: str | None,
    entity_sources: dict[str, dict[str, Any]],
    limiar: float,
    callback: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Acrescenta candidatos semânticos sem duplicar um achado lexical coincidente."""
    from analyzer import v3  # Importação tardia: modo Lexical não carrega o motor.

    blocos = [
        {"texto": texto, "pagina": pagina["pagina_pdf"], "pagina_pdf": pagina["pagina_pdf"]}
        for pagina in paginas for texto in pagina["blocos"] if texto.strip()
    ]
    termos = [{"termo": entidade.forma_canonica} for entidade in entidades]
    por_consulta = {entidade.forma_canonica: entidade for entidade in entidades}
    trechos = v3.criar_trechos_semanticos(blocos, progress_callback=callback)
    registros = v3._registros_semanticos(
        arquivo, trechos, termos, limiar, blocos=blocos,
        progress_callback=callback,
    )
    novos = []
    for registro in registros:
        entidade = por_consulta[registro["Consulta"]]
        texto = registro["Trecho da ocorrência"]
        texto_normalizado = _comparavel(texto)
        pagina_inicio = registro["_pagina_inicial_pdf"]
        pagina_fim = registro["_pagina_final_pdf"]
        coincidentes = [
            item for item in ocorrencias_lexicais
            if item["id_documento"] == id_documento
            and item["id_entidade"] == entidade.id_entidade
            and pagina_inicio <= item["pagina_pdf"] <= pagina_fim
            and _comparavel(item["trecho_ocorrencia"]) in texto_normalizado
        ]
        if coincidentes:
            for item in coincidentes:
                item["tipo_correspondencia"] = "Lexical + semântica"
                item["similaridade_semantica"] = max(
                    item.get("similaridade_semantica") or 0,
                    registro["Similaridade semântica"],
                )
            continue
        source = entity_sources.get(entidade.id_entidade, {})
        anterior = registro["Bloco anterior"]
        posterior = registro["Bloco posterior"]
        novos.append({
            "id_project": id_project,
            "id_ocorrencia": str(uuid4()),
            "id_documento": id_documento,
            "arquivo_pdf": nome_pdf,
            "pagina_pdf": pagina_inicio,
            "pagina_pdf_final": pagina_fim,
            "categoria_busca": "entidades",
            "tipo_entidade": entidade.tipo_entidade,
            "id_entidade": entidade.id_entidade,
            "entity_key": source.get("entity_key", entidade.id_entidade),
            "bibliotecas_origem": list(source.get("source_libraries", [])),
            "entidade_canonica": entidade.forma_canonica,
            "variantes": list(entidade.variantes),
            "grupo": list(entidade.grupo),
            "termo_encontrado": "",
            "forma_original_no_texto": "",
            "metodo_localizacao": "semantica",
            "tipo_correspondencia": "Semântica",
            "similaridade_semantica": registro["Similaridade semântica"],
            "trecho_anterior": anterior,
            "trecho_ocorrencia": texto,
            "trecho_posterior": posterior,
            "contexto_completo": "\n\n".join(part for part in (anterior, texto, posterior) if part),
            "tradicao_intelectual": entidade.tradicao_intelectual,
            "pais_regiao_matriz": entidade.pais_regiao,
            "relevancia_ocorrencia": "revisar",
            "validar": "pendente",
            "requer_validacao": True,
            "forma_referencia": "",
            "operacao_repertorio": "",
        })
    return novos
