"""OCR qualitativo: páginas mistas e geometria ligada ao texto canônico."""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
from PIL import Image

from analyzer import v1
from platform_core.qualitative_corpus import CorpusUnavailableError, _prepare_document
from platform_core.qualitative_ocr import needs_ocr


def native_pdf_bytes(page_count=21):
    """Capas curtas, blocos vazios, múltiplas fontes e rotações, todos nativos."""
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, "PNG")
    with pymupdf.open() as document:
        for number in range(page_count):
            page = document.new_page()
            if number == 0:
                page.insert_image(page.rect, stream=image.getvalue())
                page.insert_text((72, 90), "Capa")
            else:
                page.insert_textbox(pymupdf.Rect(72, 72, 500, 380),
                                    f"Página {number + 1}. Ação, raça e memória. " * 12, fontsize=12)
                page.insert_text((72, 420), "   ")
                page.insert_text((72, 450), " ")
                page.insert_text((72, 510), "Fonte estreita nativa",
                                 morph=(pymupdf.Point(72, 510), pymupdf.Matrix(.3, 1)))
                page.insert_text((72, 720), str(number + 1))
            if number in (5, 10, 15):
                page.set_rotation({5: 90, 10: 180, 15: 270}[number])
        return document.tobytes()


def _mixed_pdf(path: Path, *, page_count=5, scanned_indices=(1, 3)):
    document = pymupdf.open()
    for index in range(page_count):
        page = document.new_page()
        sentence = f"Pagina {index + 1} pesquisa qualitativa documento inteiro. " * 8
        if index in scanned_indices:
            source = pymupdf.open()
            image_page = source.new_page(width=page.rect.width, height=page.rect.height)
            image_page.insert_textbox(pymupdf.Rect(60, 60, 530, 420), sentence, fontsize=16)
            image = image_page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")
            page.insert_image(page.rect, stream=image)
            if index == 3:
                page.insert_text((55, 35), "Ruido textual com algumas palavras nativas.")
            source.close()
        else:
            page.insert_textbox(pymupdf.Rect(60, 60, 530, 420), sentence, fontsize=12)
    path.write_bytes(document.tobytes())
    document.close()


class QualitativeOcrTests(unittest.TestCase):
    def test_native_long_pdf_preserves_every_page_and_full_glyph_coverage_without_ocr(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nativo.pdf"
            path.write_bytes(native_pdf_bytes())
            metadata = SimpleNamespace(id="00000000-0000-0000-0000-000000000004",
                                       original_name=path.name, stored_name=path.name)
            with patch("platform_core.qualitative_ocr.v1.configurar_tesseract",
                       side_effect=AssertionError("Texto nativo não deve acionar OCR")):
                entry = _prepare_document(metadata, path, Path(temporary), 1, 1, None)
            self.assertEqual(entry["page_count"], 21)
            for page in entry["pages"]:
                self.assertEqual(page["extraction_method"], "text")
                self.assertTrue(page["layout_available"], page["page_number"])
                text = (Path(temporary) / page["file"]).read_text(encoding="utf-8")
                layout = json.loads((Path(temporary) / page["file"]).with_suffix(".layout.json").read_text(encoding="utf-8"))
                self.assertGreater(layout["width"], 0)
                self.assertGreater(layout["height"], 0)
                self.assertEqual(layout["page_text_hash"], hashlib.sha256(text.encode("utf-8")).hexdigest())
                covered = set()
                for item in layout["items"]:
                    self.assertTrue(0 <= item["start"] < item["end"] <= len(text))
                    self.assertEqual(text[item["start"]:item["end"]], item["text"])
                    self.assertEqual(len(item["quad"]), 4)
                    covered.update(range(item["start"], item["end"]))
                self.assertTrue(all(offset in covered for offset, char in enumerate(text) if not char.isspace()), page["page_number"])

    def test_ocr_decision_preserves_short_native_text_and_rejects_corrupt_layer(self):
        for text in ("Capa", "1", "Ação", "\nRaça\n"):
            self.assertFalse(needs_ocr(SimpleNamespace(get_text=lambda _kind, text=text: text)))
        for text in ("", " \n", "\ufffd" * 15 + "abc"):
            self.assertTrue(needs_ocr(SimpleNamespace(get_text=lambda _kind, text=text: text)))

    def test_entirely_scanned_document_ocr_includes_final_page(self):
        if not v1.configurar_tesseract():
            self.skipTest("Tesseract não disponível neste ambiente")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "digitalizado.pdf"
            _mixed_pdf(path, page_count=3, scanned_indices=(0, 1, 2))
            metadata = SimpleNamespace(id="00000000-0000-0000-0000-000000000002",
                                       original_name="digitalizado.pdf", stored_name="digitalizado.pdf")
            entry = _prepare_document(metadata, path, Path(temporary), 1, 1, None)
            self.assertEqual(entry["page_count"], 3)
            self.assertEqual([page["page_number"] for page in entry["pages"]], [1, 2, 3])
            self.assertEqual([page["extraction_method"] for page in entry["pages"]],
                             ["ocr", "ocr", "ocr"])
            self.assertTrue(all(page["layout_available"] for page in entry["pages"]))
            self.assertIn("3", (Path(temporary) / entry["pages"][-1]["file"]).read_text(encoding="utf-8"))

    def test_extractor_cannot_silently_stop_before_pdf_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tres-paginas.pdf"
            _mixed_pdf(path, page_count=3, scanned_indices=())
            metadata = SimpleNamespace(id="00000000-0000-0000-0000-000000000003",
                                       original_name=path.name, stored_name=path.name)
            def truncated(_source):
                yield {"pagina_pdf": 1, "texto": "Página inicial", "ocr_utilizado": False}
            with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
                       side_effect=truncated), self.assertRaises(CorpusUnavailableError):
                _prepare_document(metadata, path, Path(temporary), 1, 1, None)

    def test_page_decision_and_full_mixed_document_with_verified_ocr_layout(self):
        if not v1.configurar_tesseract():
            self.skipTest("Tesseract não disponível neste ambiente")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "misto.pdf"
            _mixed_pdf(path)
            with pymupdf.open(path) as document:
                self.assertFalse(needs_ocr(document[0]))
                self.assertTrue(needs_ocr(document[1]))
                # Texto curto válido sobre imagem não é prova de corrupção.
                self.assertFalse(needs_ocr(document[3]))
            document_id = "00000000-0000-0000-0000-000000000001"
            metadata = SimpleNamespace(id=document_id, original_name="misto.pdf", stored_name="misto.pdf")
            events = []
            entry = _prepare_document(metadata, path, Path(temporary), 1, 1, events.append)
            self.assertEqual(entry["page_count"], 5)
            self.assertEqual([page["page_number"] for page in entry["pages"]], [1, 2, 3, 4, 5])
            self.assertEqual([page["extraction_method"] for page in entry["pages"]],
                             ["text", "ocr", "text", "text", "text"])
            self.assertEqual(events[-1]["page_count"], 5)
            for page in entry["pages"]:
                text = (Path(temporary) / page["file"]).read_text(encoding="utf-8")
                layout_path = Path(temporary) / document_id / "pages" / f"{page['page_number']:06d}.layout.json"
                layout = json.loads(layout_path.read_text(encoding="utf-8"))
                self.assertTrue(text.strip())
                self.assertEqual(page["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
                self.assertTrue(page["layout_available"])
                self.assertEqual(layout["page_text_hash"], page["sha256"])
                self.assertTrue(layout["items"])
                self.assertTrue(all(text[item["start"]:item["end"]] == item["text"]
                                    and item["end"] - item["start"] == 1
                                    for item in layout["items"]))


if __name__ == "__main__":
    unittest.main()
