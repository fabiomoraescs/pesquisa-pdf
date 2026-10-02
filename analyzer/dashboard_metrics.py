"""Agregações de visualização baseadas nos resultados já processados."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def frequencia_relativa_por_documento(
    documentos: list[str], matriz: list[list[int]], diagnosticos: Any,
) -> dict[str, list[int | float | None] | list[str]]:
    """Calcula ocorrências por mil palavras sem reler nem reprocessar PDFs."""
    registros = diagnosticos.to_dict("records") if hasattr(diagnosticos, "to_dict") else list(diagnosticos or [])
    palavras_por_documento: dict[str, int] = {}
    for registro in registros:
        arquivo = registro.get("arquivo")
        if not arquivo:
            continue
        try:
            palavras = max(0, int(registro.get("palavras_analisadas") or 0))
        except (TypeError, ValueError):
            palavras = 0
        palavras_por_documento[Path(str(arquivo)).stem] = palavras

    ocorrencias = [sum(linha[indice] for linha in matriz) for indice in range(len(documentos))]
    palavras = [palavras_por_documento.get(documento, 0) for documento in documentos]
    por_mil = [
        round(total * 1000 / quantidade, 4) if quantidade else None
        for total, quantidade in zip(ocorrencias, palavras)
    ]
    return {
        "documentos": documentos,
        "ocorrencias": ocorrencias,
        "palavras": palavras,
        "por_mil": por_mil,
    }
