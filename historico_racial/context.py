"""Contexto textual adjacente, sem interpretação ou normalização."""

from __future__ import annotations

import re
from typing import Any


def separar_paragrafos(texto: str) -> list[str]:
    """Separa apenas quebras em branco; preserva grafia e quebras internas."""
    return [parte.strip() for parte in re.split(r"\n\s*\n", texto) if parte.strip()]


def construir_paragrafos(paginas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mantém a ordem original de blocos/parágrafos e suas páginas."""
    paragrafos = []
    for pagina in paginas:
        for bloco in pagina["blocos"]:
            for texto in separar_paragrafos(bloco):
                paragrafos.append({"pagina_pdf": pagina["pagina_pdf"], "texto": texto})
    return paragrafos


def obter_contexto(paragrafos: list[dict[str, Any]], indice: int) -> dict[str, str]:
    """Devolve o parágrafo encontrado e seus vizinhos reais, quando existirem."""
    if not 0 <= indice < len(paragrafos):
        raise IndexError("Índice de parágrafo inválido.")
    anterior = paragrafos[indice - 1]["texto"] if indice > 0 else ""
    atual = paragrafos[indice]["texto"]
    posterior = paragrafos[indice + 1]["texto"] if indice + 1 < len(paragrafos) else ""
    return {
        "trecho_anterior": anterior,
        "trecho_ocorrencia": atual,
        "trecho_posterior": posterior,
        "contexto_completo": "\n\n".join(parte for parte in (anterior, atual, posterior) if parte),
    }
