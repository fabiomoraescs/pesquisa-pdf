"""Extração por página para o corpus qualitativo, com geometria OCR verificável.

Reutiliza a decisão básica, o Tesseract, os idiomas e o DPI da extração já
existente. A TextPage OCR é produzida uma única vez e serve tanto ao texto
canônico quanto às caixas de caracteres; nunca mistura texto nativo e OCR.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf

from analyzer import v1
from historico_racial.pdf import OCRIndisponivelError, _abrir_pdf


def _image_coverage(page) -> float:
    """Maior imagem sobre a página, sem somar áreas sobrepostas."""
    area = page.rect.width * page.rect.height
    if area <= 0:
        return 0.0
    try:
        return max(((pymupdf.Rect(item["bbox"]) & page.rect).get_area() / area
                    for item in page.get_image_info()), default=0.0)
    except (KeyError, TypeError, ValueError, RuntimeError):
        return 0.0


def needs_ocr(page) -> bool:
    """Texto nativo legível tem prioridade, inclusive capas e páginas curtas.

    Quantidade de texto e presença de imagem não demonstram corrupção. OCR só
    substitui texto ausente ou uma camada com muitos caracteres inválidos.
    Falha da geometria nativa não é, por si só, motivo para trocar o corpus.
    """
    text = page.get_text("text") or ""
    if not text.strip():
        return True
    visible = [char for char in text if not char.isspace()]
    invalid = sum(char == "\ufffd" or (ord(char) < 32 and not char.isspace()) for char in visible)
    return invalid >= 3 and invalid / len(visible) > .2


def _tessdata(command: str) -> str:
    try:
        return pymupdf.get_tessdata()
    except RuntimeError:
        candidate = Path(command).resolve().parent / "tessdata"
        if candidate.is_dir():
            return str(candidate)
        raise OCRIndisponivelError("Dados de idioma do Tesseract indisponíveis.")


def extrair_paginas(caminho: Path):
    """Gera um registro para cada página, inclusive no meio/fim de PDFs mistos."""
    document = _abrir_pdf(caminho)
    language = None
    tessdata = None
    try:
        total = len(document)
        for number, page in enumerate(document, start=1):
            use_ocr = needs_ocr(page)
            textpage = None
            if use_ocr:
                command = v1.configurar_tesseract()
                if not command:
                    raise OCRIndisponivelError("Uma página precisa de OCR, mas o Tesseract não foi encontrado.")
                if language is None:
                    language = v1.escolher_idioma_ocr()
                    tessdata = _tessdata(command)
                textpage = page.get_textpage_ocr(
                    language=language, dpi=v1.OCR_DPI, full=True, tessdata=tessdata)
            options = {"textpage": textpage} if textpage is not None else {}
            blocks = [block[4] for block in page.get_text("blocks", sort=True, **options)
                      if len(block) > 6 and block[6] == 0 and block[4].strip()]
            text = "\n\n".join(blocks)
            if use_ocr and not text.strip():
                if _image_coverage(page) >= .5:
                    raise OCRIndisponivelError(
                        f"OCR não reconheceu texto da página {number}; corpus não foi publicado.")
                logging.getLogger(__name__).warning("Página %s sem texto reconhecível: %s", number, caminho)
            yield {"pagina_pdf": number, "texto": text, "ocr_utilizado": use_ocr,
                   "page": page, "textpage": textpage, "page_count": total}
    finally:
        document.close()
