"""Geometria opcional, verificável contra o corpus canônico qualitativo.

Páginas nativas e OCR recebem layout somente quando os glifos
reconstroem *exatamente* o texto salvo. Mapeamento ambíguo permanece
pesquisável, sem coordenadas inventadas.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import pymupdf

from .qualitative_corpus import canonicalize_page, qualitative_corpus_dir, read_qualitative_page


LAYOUT_VERSION = 1


def build_native_layout(page, text: str, page_number: int, digest: str, *, textpage=None) -> dict | None:
    """Mapeia glifos nativos ou OCR aos offsets do *mesmo* texto salvo."""
    def unavailable(reason):
        logging.getLogger(__name__).debug("Layout da página %s indisponível: %s", page_number, reason)
        return None

    try:
        options = {"textpage": textpage} if textpage is not None else {}
        blocks = [block for block in page.get_text("blocks", sort=True, **options)
                  if len(block) > 6 and block[6] == 0 and block[4].strip()]
        # Blocos só de espaços não entram no corpus. rawdict pode contê-los:
        # não comparar quantidades nem fazer zip entre listas filtradas de modo
        # diferente. O número do bloco identifica a mesma fonte nas duas APIs.
        raw = {block["number"]: block
               # Mesmas flags de blocks: imagens não podem deslocar a numeração
               # dos blocos textuais entre as duas representações.
               for block in page.get_text("rawdict", sort=True, flags=pymupdf.TEXTFLAGS_BLOCKS, **options)["blocks"]
               if block.get("type") == 0 and block.get("lines")}
        if not blocks or canonicalize_page("\n\n".join(block[4] for block in blocks)) != text:
            return unavailable("texto dos blocos diverge do canônico ou página vazia")
        width, height = float(page.rect.width), float(page.rect.height)
        if width <= 0 or height <= 0:
            return unavailable("dimensões inválidas")
        items = []
        offset = 0
        for index, source in enumerate(blocks):
            block_text = canonicalize_page(source[4])
            block = raw.get(source[5])
            if block is None:
                return unavailable(f"geometria ausente para bloco {source[5]}")
            reconstructed = canonicalize_page("".join(
                "".join(char["c"] for span in line["spans"] for char in span["chars"]) + "\n"
                for line in block["lines"]
            ))
            if reconstructed != block_text:
                return unavailable(f"glifos divergem do texto do bloco {source[5]}")
            local = 0
            for line in block["lines"]:
                for span in line["spans"]:
                    for char in span["chars"]:
                        value = canonicalize_page(char["c"])
                        if value and value not in ("\n", "\t"):
                            # Geometria real do glifo, também em páginas/texto
                            # rotacionados. Os offsets permanecem no corpus;
                            # somente as coordenadas seguem a rotação do PDF.
                            quad = pymupdf.recover_char_quad(line["dir"], span, char) * page.rotation_matrix
                            rect = quad.rect & page.rect
                            start = offset + local
                            if rect.is_empty:
                                if not value.isspace():
                                    return unavailable(f"glifo sem área visível no offset {start}")
                            else:
                                items.append({"start": start, "end": start + len(value),
                                              "text": value, "bbox": list(rect),
                                              "quad": [[float(point.x), float(point.y)] for point in quad]})
                        local += len(value)
                local += 1  # quebra de linha presente em get_text("blocks")
            if local != len(block_text):
                return unavailable(f"offsets divergem no bloco {source[5]}")
            offset += local + (2 if index + 1 < len(blocks) else 0)
        if offset != len(text) or not items:
            return unavailable("cobertura de offsets incompleta ou sem glifos")
        return {"layout_version": LAYOUT_VERSION, "page_number": page_number,
                "page_text_hash": digest, "offset_unit": "unicode_codepoint",
                "width": width, "height": height, "items": items}
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, RuntimeError) as error:
        return unavailable(str(error))


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
    def rejected(reason):
        logging.getLogger(__name__).debug("Layout rejeitado %s/%s/página %s: %s",
                                         analysis.id, document_id, page_number, reason)
        return unavailable
    if not page.get("layout_available"):
        return rejected("manifest sem layout disponível")
    path = qualitative_corpus_dir(analysis.id) / document_id / "pages" / f"{page_number:06d}.layout.json"
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(qualitative_corpus_dir(analysis.id).resolve()):
        return rejected("arquivo de layout ausente ou caminho inseguro")
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
            return rejected("versão, hash, dimensões ou estrutura divergentes")
        text = persisted["text"]
        for item in layout["items"]:
            start, end, bbox = item["start"], item["end"], item["bbox"]
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                    or item["text"] != text[start:end] or len(bbox) != 4
                    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in bbox)
                    or not (0 <= bbox[0] < bbox[2] <= width + 1
                            and 0 <= bbox[1] < bbox[3] <= height + 1)):
                return rejected("glifo, offsets ou bbox incompatíveis com o corpus")
            if "quad" in item and (len(item["quad"]) != 4 or any(
                    not isinstance(point, list) or len(point) != 2
                    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in point)
                    for point in item["quad"])):
                return rejected("quadrilátero inválido")
        return {"layout_available": True, "width": width, "height": height,
                "page_text_hash": persisted["sha256"], "items": layout["items"]}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError) as error:
        logging.getLogger(__name__).warning("Layout opcional indisponível para %s/%s: %s",
                                           document_id, page_number, error)
        return unavailable
