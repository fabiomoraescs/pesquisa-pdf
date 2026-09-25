"""Geometria opcional, verificável contra o corpus canônico qualitativo.

Somente páginas textuais de rotação zero com reconstrução *exata* dos blocos
recebem layout. Páginas OCR ou com mapeamento ambíguo permanecem pesquisáveis,
mas não recebem coordenadas inventadas.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from .qualitative_corpus import qualitative_corpus_dir, read_qualitative_page


LAYOUT_VERSION = 1


def build_native_layout(page, text: str, page_number: int, digest: str) -> dict | None:
    """Mapeia palavras dos blocos nativos para offsets do *mesmo* texto salvo."""
    try:
        if page.rotation != 0:
            return None
        blocks = [block[4] for block in page.get_text("blocks", sort=True)
                  if len(block) > 6 and block[6] == 0 and block[4].strip()]
        raw = [block for block in page.get_text("rawdict", sort=True)["blocks"]
               if block.get("type") == 0 and block.get("lines")]
        if not blocks or "\n\n".join(blocks) != text or len(blocks) != len(raw):
            return None
        width, height = float(page.rect.width), float(page.rect.height)
        if width <= 0 or height <= 0:
            return None
        items = []
        offset = 0
        for index, (block_text, block) in enumerate(zip(blocks, raw)):
            reconstructed = "".join(
                "".join(char["c"] for span in line["spans"] for char in span["chars"]) + "\n"
                for line in block["lines"]
            )
            if reconstructed != block_text:
                return None
            local = 0
            for line in block["lines"]:
                word_start = None
                boxes = []

                def flush():
                    nonlocal word_start, boxes
                    if word_start is not None:
                        # Um glifo na borda pode ultrapassar ligeiramente o
                        # recorte visível do PDF; só mapeamos sua área visível.
                        x0 = max(0.0, min(width, min(box[0] for box in boxes)))
                        y0 = max(0.0, min(height, min(box[1] for box in boxes)))
                        x1 = max(0.0, min(width, max(box[2] for box in boxes)))
                        y1 = max(0.0, min(height, max(box[3] for box in boxes)))
                        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                            raise ValueError("Caixa fora da página")
                        start, end = offset + word_start, offset + local
                        items.append({"start": start, "end": end,
                                      "text": text[start:end], "bbox": [x0, y0, x1, y1]})
                    word_start, boxes = None, []

                for span in line["spans"]:
                    for char in span["chars"]:
                        value = char["c"]
                        if len(value) != 1:
                            return None
                        if value.isspace():
                            flush()
                        else:
                            if word_start is None:
                                word_start = local
                            boxes.append(tuple(float(value) for value in char["bbox"]))
                        local += 1
                flush()
                local += 1  # quebra de linha presente em get_text("blocks")
            if local != len(block_text):
                return None
            offset += local + (2 if index + 1 < len(blocks) else 0)
        if offset != len(text) or not items:
            return None
        return {"layout_version": LAYOUT_VERSION, "page_number": page_number,
                "page_text_hash": digest, "offset_unit": "unicode_codepoint",
                "width": width, "height": height, "items": items}
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as error:
        logging.getLogger(__name__).debug("Layout nativo indisponível: %s", error)
        return None


def read_qualitative_layout(analysis, document_id: str, page_number: int, *, manifest=None) -> dict:
    """Lê layout opcional; recusa caixas que não correspondam ao texto salvo."""
    if manifest is None:
        from .qualitative_corpus import load_qualitative_manifest
        manifest = load_qualitative_manifest(analysis)
    document = next((item for item in manifest["documents"] if item["document_id"] == document_id), None)
    if document is None or not 1 <= page_number <= document["page_count"]:
        from .qualitative_corpus import QualitativePageNotFoundError
        raise QualitativePageNotFoundError("Página não encontrada nesta Base.")
    page = document["pages"][page_number - 1]
    unavailable = {"layout_available": False, "items": []}
    if not page.get("layout_available"):
        return unavailable
    path = qualitative_corpus_dir(analysis.id) / document_id / "pages" / f"{page_number:06d}.layout.json"
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(qualitative_corpus_dir(analysis.id).resolve()):
        return unavailable
    try:
        layout = json.loads(path.read_text(encoding="utf-8"))
        persisted = read_qualitative_page(analysis, document_id, page_number, manifest=manifest)
        width, height = layout["width"], layout["height"]
        if (layout["layout_version"] != LAYOUT_VERSION or layout["page_number"] != page_number
                or layout["page_text_hash"] != persisted["sha256"]
                or layout["offset_unit"] != "unicode_codepoint"
                or not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
                            for value in (width, height))
                or not isinstance(layout["items"], list)):
            return unavailable
        text = persisted["text"]
        for item in layout["items"]:
            start, end, bbox = item["start"], item["end"], item["bbox"]
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                    or item["text"] != text[start:end] or len(bbox) != 4
                    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in bbox)
                    or not (0 <= bbox[0] < bbox[2] <= width + 1
                            and 0 <= bbox[1] < bbox[3] <= height + 1)):
                return unavailable
        return {"layout_available": True, "width": width, "height": height,
                "page_text_hash": persisted["sha256"], "items": layout["items"]}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError) as error:
        logging.getLogger(__name__).warning("Layout opcional indisponível para %s/%s: %s",
                                           document_id, page_number, error)
        return unavailable
