"""Leitura de PDFs com o mesmo fallback OCR já validado na V1.

Este módulo reutiliza apenas as funções de OCR da V1; não chama sua análise,
classificação ou exportação. O texto extraído não é normalizado aqui.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pymupdf

from analyzer import v1


class PDFInvalidoError(ValueError):
    """PDF ilegível, vazio ou protegido por senha."""


class OCRIndisponivelError(RuntimeError):
    """Uma página exige OCR, mas o Tesseract não está disponível."""


def _abrir_pdf(caminho: Path) -> pymupdf.Document:
    try:
        documento = pymupdf.open(caminho)
    except (OSError, RuntimeError, ValueError, pymupdf.FileDataError) as erro:
        raise PDFInvalidoError("O arquivo não é um PDF legível.") from erro
    if not documento.is_pdf or documento.needs_pass or len(documento) == 0:
        documento.close()
        raise PDFInvalidoError("O PDF está vazio, protegido ou não é válido.")
    return documento


def contar_paginas(caminho: Path) -> int:
    """Consulta somente metadados para o progresso global, sem extrair texto."""
    documento = _abrir_pdf(caminho)
    try:
        return len(documento)
    finally:
        documento.close()


def extrair_paginas(
    caminho: Path,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Produz texto e blocos originais por página, usando OCR quando necessário."""
    documento = _abrir_pdf(caminho)
    idioma_ocr: str | None = None
    try:
        for numero, pagina in enumerate(documento, start=1):
            usar_ocr = v1.pagina_precisa_ocr(pagina)
            if usar_ocr:
                if progress_callback is not None:
                    progress_callback({"etapa": "Executando OCR…", "pagina_atual": numero})
                if not v1.configurar_tesseract():
                    raise OCRIndisponivelError("Uma página precisa de OCR, mas o Tesseract não foi encontrado.")
                if idioma_ocr is None:
                    idioma_ocr = v1.escolher_idioma_ocr()
                blocos = [bloco["texto"] for bloco in v1.ocr_pagina(pagina, idioma_ocr) if bloco["texto"]]
            else:
                blocos = [
                    bloco[4]
                    for bloco in pagina.get_text("blocks", sort=True)
                    if len(bloco) > 6 and bloco[6] == 0 and bloco[4].strip()
                ]
            yield {
                "pagina_pdf": numero,
                "texto": "\n\n".join(blocos),
                "blocos": blocos,
                "ocr_utilizado": usar_ocr,
                "ocr_idioma_utilizado": idioma_ocr if usar_ocr else None,
            }
    finally:
        documento.close()
