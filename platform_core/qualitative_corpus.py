"""Corpus canônico persistido para leitura qualitativa, sem extração na leitura.

``concluida`` indica apenas que o corpus está tecnicamente preparado. Os
índices futuros de trechos usarão code points da string Python de cada página.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import pymupdf
from sqlalchemy import delete, or_, select

from historico_racial import pdf as pdf_extractor

from .analyses import analysis_dir, documents_for
from .extensions import db
from .models import Analysis, AnalysisDocument, QualitativeCoding, QualitativeExcerpt, QualitativeMemo


SCHEMA_VERSION = 1
EXTRACTION_VERSION = "qualitative-corpus-v1"
OFFSET_UNIT = "unicode_codepoint"


class CorpusUnavailableError(ValueError):
    """O corpus publicado está ausente, incompleto ou inconsistente."""


class QualitativePageNotFoundError(ValueError):
    """O documento ou número de página solicitado não existe nesta Base."""


def canonicalize_page(text: str) -> str:
    """Preserva o conteúdo; estabiliza somente os terminadores de linha."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def qualitative_corpus_dir(analysis_id: str) -> Path:
    return analysis_dir(analysis_id) / "qualitative_corpus"


def qualitative_manifest_path(analysis_id: str) -> Path:
    return qualitative_corpus_dir(analysis_id) / "manifest.json"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_child(root: Path, relative: str) -> Path:
    candidate = root / relative
    if (root.is_symlink() or Path(relative).is_absolute() or ".." in Path(relative).parts
            or candidate.is_symlink() or candidate.resolve().is_relative_to(root.resolve()) is False):
        raise CorpusUnavailableError("Caminho inválido no corpus qualitativo.")
    return candidate


def _prepare_document(document: AnalysisDocument, source: Path, staging: Path,
                      document_index: int, document_count: int,
                      progress: Callable[[dict], None] | None) -> dict:
    """A mesma extração atende publicação inicial e acréscimo incremental."""
    if source.is_symlink() or not source.is_file():
        raise CorpusUnavailableError("PDF original indisponível.")
    pages_dir = staging / document.id / "pages"
    pages_dir.mkdir(parents=True)
    page_entries = []
    # A leitura geométrica não repete OCR; layouts sem correspondência são omitidos.
    from .qualitative_layout import build_native_layout
    with pymupdf.open(source) as visual_document:
        for expected_number, extracted in enumerate(pdf_extractor.extrair_paginas(source), start=1):
            if extracted["pagina_pdf"] != expected_number:
                raise CorpusUnavailableError("Numeração de páginas inconsistente na extração.")
            text = canonicalize_page(extracted["texto"])
            relative = f"{document.id}/pages/{expected_number:06d}.txt"
            (staging / relative).write_bytes(text.encode("utf-8"))
            digest = _digest(text)
            layout = None
            if not extracted["ocr_utilizado"] and expected_number <= len(visual_document):
                layout = build_native_layout(visual_document[expected_number - 1], text,
                                             expected_number, digest)
            if layout is not None:
                (pages_dir / f"{expected_number:06d}.layout.json").write_text(
                    json.dumps(layout, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            page_entries.append({
                "page_number": expected_number, "file": relative, "sha256": digest,
                "char_count": len(text),
                "extraction_method": "ocr" if extracted["ocr_utilizado"] else "text",
                "layout_available": layout is not None,
            })
            if progress:
                progress({"document_index": document_index, "document_count": document_count,
                          "document_name": document.original_name, "page_number": expected_number})
    if not page_entries:
        raise CorpusUnavailableError("Um PDF não contém páginas.")
    return {"document_id": document.id, "original_name": document.original_name,
            "stored_name": document.stored_name, "page_count": len(page_entries),
            "pages": page_entries}


def prepare_qualitative_corpus(
    analysis: Analysis, progress: Callable[[dict], None] | None = None,
) -> dict:
    """Extrai uma vez, escreve em staging na mesma Analysis e publica ao concluir.

    O status no banco só deve mudar para ``concluida`` depois do retorno desta
    função. Em caso de erro, o diretório final nunca ganha manifest parcial.
    """
    root = analysis_dir(analysis.id)
    final = qualitative_corpus_dir(analysis.id)
    if final.exists() or final.is_symlink():
        raise CorpusUnavailableError("Já existe um corpus publicado; reparo explícito necessário.")
    staging = root / f".qualitative_corpus.{uuid4().hex}.tmp"
    staging.mkdir()
    documents = documents_for(analysis)
    if not documents:
        staging.rmdir()
        raise ValueError("A Base qualitativa não possui PDFs.")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "extraction_version": EXTRACTION_VERSION,
        "analysis_id": analysis.id,
        "offset_unit": OFFSET_UNIT,
        "documents": [],
    }
    try:
        for document_index, document in enumerate(documents, start=1):
            source = _safe_child(root / "documents", document.stored_name)
            manifest["documents"].append(_prepare_document(
                document, source, staging, document_index, len(documents), progress))
        (staging / "manifest.json").write_bytes(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        os.replace(staging, final)
        return manifest
    except Exception as error:
        try:
            shutil.rmtree(staging)
        except Exception as cleanup_error:
            raise ExceptionGroup("Falha na preparação e na limpeza do corpus parcial.",
                                 [error, cleanup_error]) from error
        raise


def append_qualitative_corpus(
    analysis: Analysis, additions: list[tuple[AnalysisDocument, Path]],
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Prepara somente PDFs novos; publica sem tocar nas páginas já indexadas.

    O marcador transitório no JSON permite que leitores continuem usando o
    manifest antigo no curto intervalo entre o commit das novas linhas e o
    replace atômico do manifest. Nenhuma página antiga é extraída novamente.
    """
    # O corpus antigo já foi conferido na publicação; acréscimos não relêem
    # centenas de páginas antigas. A leitura individual continua validando hash.
    previous = load_qualitative_manifest(analysis)
    root = analysis_dir(analysis.id)
    corpus = qualitative_corpus_dir(analysis.id)
    staging = root / f".qualitative_append.{uuid4().hex}.tmp"
    staging.mkdir()
    old_manifest = qualitative_manifest_path(analysis.id).read_bytes()
    original_count = analysis.document_count
    added_ids = [document.id for document, _ in additions]
    published_paths: list[Path] = []
    database_committed = False
    manifest_replaced = False
    try:
        entries = [_prepare_document(document, source, staging, index, len(additions), progress)
                   for index, (document, source) in enumerate(additions, start=1)]
        updated = {**previous, "documents": [*previous["documents"], *entries]}
        manifest_path = qualitative_manifest_path(analysis.id)
        pending_manifest = corpus / f".manifest.{uuid4().hex}.tmp"
        pending_manifest.write_bytes(json.dumps(
            updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        for document, source in additions:
            target_pdf = root / "documents" / document.stored_name
            target_pages = corpus / document.id
            if target_pdf.exists() or target_pages.exists() or target_pdf.is_symlink() or target_pages.is_symlink():
                raise CorpusUnavailableError("Destino de documento novo já existe.")
            os.replace(source, target_pdf)
            published_paths.append(target_pdf)
            os.replace(staging / document.id, target_pages)
            published_paths.append(target_pages)

        parameters = dict(analysis.parameters_json)
        parameters["_qualitative_append_pending"] = added_ids
        analysis.parameters_json = parameters
        analysis.document_count = original_count + len(additions)
        db.session.add_all(document for document, _ in additions)
        db.session.commit()
        database_committed = True
        os.replace(pending_manifest, manifest_path)
        manifest_replaced = True
        load_qualitative_manifest(analysis)
        for entry in entries:
            for page in entry["pages"]:
                _read_verified_page(corpus, page)
        parameters = dict(analysis.parameters_json)
        parameters.pop("_qualitative_append_pending", None)
        analysis.parameters_json = parameters
        db.session.commit()
        return updated
    except Exception as error:
        db.session.rollback()
        cleanup_errors = []
        try:
            if manifest_replaced:
                restore = corpus / f".manifest.restore.{uuid4().hex}.tmp"
                restore.write_bytes(old_manifest)
                os.replace(restore, qualitative_manifest_path(analysis.id))
            if database_committed:
                db.session.execute(delete(AnalysisDocument).where(AnalysisDocument.id.in_(added_ids)))
                restored = db.session.get(Analysis, analysis.id)
                restored.document_count = original_count
                parameters = dict(restored.parameters_json)
                parameters.pop("_qualitative_append_pending", None)
                restored.parameters_json = parameters
                db.session.commit()
        except Exception as rollback_error:
            db.session.rollback()
            cleanup_errors.append(rollback_error)
        for path in reversed(published_paths):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise ExceptionGroup("Falha ao restaurar acréscimo de documentos.",
                                 [error, *cleanup_errors]) from error
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if "pending_manifest" in locals():
            pending_manifest.unlink(missing_ok=True)


def delete_qualitative_document(analysis: Analysis, document: AnalysisDocument) -> str | None:
    """Remove um PDF e seus dependentes sem reprocessar os demais documentos.

    O manifest é publicado antes do commit destrutivo; um marcador temporário
    permite a leitura dos outros PDFs durante essa curta transição. Arquivos
    ficam em quarentena até a operação no banco confirmar.
    """
    if document.analysis_id != analysis.id or analysis.status != "concluida":
        raise CorpusUnavailableError("Documento ou corpus indisponível.")
    manifest = load_qualitative_manifest(analysis)
    listed = [item for item in manifest["documents"] if item["document_id"] == document.id]
    if len(listed) != 1:
        raise CorpusUnavailableError("Documento ausente do corpus.")
    original_index = next(index for index, item in enumerate(manifest["documents"])
                          if item["document_id"] == document.id)
    remaining = [item for item in manifest["documents"] if item["document_id"] != document.id]
    next_id = remaining[min(original_index, len(remaining) - 1)]["document_id"] if remaining else None
    root = analysis_dir(analysis.id)
    corpus = qualitative_corpus_dir(analysis.id)
    pdf = _safe_child(root / "documents", document.stored_name)
    pages = _safe_child(corpus, document.id)
    if not pdf.is_file() or pdf.is_symlink() or not pages.is_dir() or pages.is_symlink():
        raise CorpusUnavailableError("Arquivos do documento indisponíveis.")
    original_manifest = qualitative_manifest_path(analysis.id).read_bytes()
    quarantine = root / f".qualitative_delete.{uuid4().hex}.tmp"
    quarantine.mkdir()
    moved: list[tuple[Path, Path]] = []
    published = False
    committed = False
    try:
        parameters = dict(analysis.parameters_json)
        parameters["_qualitative_delete_pending"] = [document.id]
        analysis.parameters_json = parameters
        db.session.commit()
        artifacts = ((pdf, quarantine / "document.pdf"),
                     (pages, quarantine / "pages") if remaining else
                     (corpus, quarantine / "corpus"))
        for source, target in artifacts:
            os.replace(source, target)
            moved.append((source, target))
        if remaining:
            replacement = corpus / f".manifest.{uuid4().hex}.tmp"
            replacement.write_bytes(json.dumps(
                {**manifest, "documents": remaining}, ensure_ascii=False,
                separators=(",", ":")).encode("utf-8"))
            os.replace(replacement, qualitative_manifest_path(analysis.id))
        published = True

        excerpt_ids = select(QualitativeExcerpt.id).where(
            QualitativeExcerpt.analysis_id == analysis.id,
            QualitativeExcerpt.document_id == document.id)
        db.session.execute(delete(QualitativeCoding).where(
            QualitativeCoding.analysis_id == analysis.id,
            QualitativeCoding.excerpt_id.in_(excerpt_ids)))
        db.session.execute(delete(QualitativeMemo).where(
            QualitativeMemo.analysis_id == analysis.id,
            or_(QualitativeMemo.document_id == document.id,
                QualitativeMemo.excerpt_id.in_(excerpt_ids))))
        db.session.execute(delete(QualitativeExcerpt).where(
            QualitativeExcerpt.analysis_id == analysis.id,
            QualitativeExcerpt.document_id == document.id))
        db.session.execute(delete(AnalysisDocument).where(
            AnalysisDocument.id == document.id, AnalysisDocument.analysis_id == analysis.id))
        analysis.document_count -= 1
        if not remaining:
            analysis.status = "processando"  # ambiente vazio, pronto para novos PDFs
            analysis.completed_at = None
        parameters = dict(analysis.parameters_json)
        parameters.pop("_qualitative_delete_pending", None)
        analysis.parameters_json = parameters
        db.session.commit()
        committed = True
    except Exception as error:
        db.session.rollback()
        if committed:
            raise
        rollback_errors = []
        try:
            if published and remaining:
                restore = corpus / f".manifest.restore.{uuid4().hex}.tmp"
                restore.write_bytes(original_manifest)
                os.replace(restore, qualitative_manifest_path(analysis.id))
            for source, target in reversed(moved):
                if target.exists():
                    os.replace(target, source)
            restored = db.session.get(Analysis, analysis.id)
            parameters = dict(restored.parameters_json)
            parameters.pop("_qualitative_delete_pending", None)
            restored.parameters_json = parameters
            db.session.commit()
        except Exception as rollback_error:
            db.session.rollback()
            rollback_errors.append(rollback_error)
        if rollback_errors:
            raise ExceptionGroup("Falha ao restaurar exclusão de documento.",
                                 [error, *rollback_errors]) from error
        raise
    finally:
        if "replacement" in locals():
            replacement.unlink(missing_ok=True)
        if committed:
            try:
                shutil.rmtree(quarantine)
            except OSError:
                logging.getLogger(__name__).exception(
                    "Quarentena do documento não pôde ser limpa: %s", quarantine)
        elif not any(target.exists() for _, target in moved):
            quarantine.rmdir()
    return next_id


def _load_structural_manifest(analysis: Analysis, *, require_completed: bool) -> dict:
    """Confere apenas o índice e os documentos do banco, nunca abre páginas."""
    root = qualitative_corpus_dir(analysis.id)
    path = qualitative_manifest_path(analysis.id)
    if ((require_completed and analysis.status != "concluida")
            or root.is_symlink() or path.is_symlink() or not path.is_file()):
        raise CorpusUnavailableError("Corpus qualitativo indisponível ou incompleto.")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (type(manifest.get("schema_version")) is not int
                or manifest["schema_version"] != SCHEMA_VERSION
                or manifest.get("extraction_version") != EXTRACTION_VERSION
                or manifest.get("analysis_id") != analysis.id
                or manifest.get("offset_unit") != OFFSET_UNIT):
            raise CorpusUnavailableError("Versão ou identidade do corpus inválida.")
        actual_documents = {item.id: item for item in documents_for(analysis)}
        listed = manifest["documents"]
        if not isinstance(listed, list) or not listed:
            raise CorpusUnavailableError("Documentos do corpus não correspondem à Base.")
        pending = set(analysis.parameters_json.get("_qualitative_append_pending", []))
        deleting = set(analysis.parameters_json.get("_qualitative_delete_pending", []))
        listed_ids = {item["document_id"] for item in listed}
        extra_ids = set(actual_documents) - listed_ids
        missing_ids = listed_ids - set(actual_documents)
        if (len(listed_ids) != len(listed)
                or missing_ids
                or (extra_ids and extra_ids not in (pending, deleting))):
            raise CorpusUnavailableError("Documentos do corpus não correspondem à Base.")
        for entry in listed:
            if str(UUID(entry["document_id"])) != entry["document_id"]:
                raise CorpusUnavailableError("Identificador de documento inválido.")
            document = actual_documents[entry["document_id"]]
            if (entry["original_name"] != document.original_name
                    or entry["stored_name"] != document.stored_name
                    or type(entry["page_count"]) is not int
                    or not isinstance(entry["pages"], list)
                    or entry["page_count"] != len(entry["pages"]) or not entry["pages"]):
                raise CorpusUnavailableError("Metadados de documento inconsistentes.")
            for number, page in enumerate(entry["pages"], start=1):
                expected = f"{document.id}/pages/{number:06d}.txt"
                if (type(page["page_number"]) is not int or page["page_number"] != number
                        or page["file"] != expected
                        or page.get("extraction_method") not in {"text", "ocr"}
                        or page.get("layout_available", False) not in (True, False)
                        or type(page.get("char_count")) is not int or page["char_count"] < 0
                        or not isinstance(page.get("sha256"), str)
                        or re.fullmatch(r"[0-9a-f]{64}", page["sha256"]) is None):
                    raise CorpusUnavailableError("Metadados de página inconsistentes.")
        return manifest
    except (OSError, UnicodeError, KeyError, TypeError, AttributeError, IndexError, ValueError) as error:
        logging.getLogger(__name__).warning("Corpus inconsistente para a Base %s: %s", analysis.id, error)
        raise CorpusUnavailableError("Corpus qualitativo indisponível ou inconsistente.") from error


def load_qualitative_manifest(analysis: Analysis) -> dict:
    """Valida somente manifest e metadados; não lê conteúdo de páginas."""
    return _load_structural_manifest(analysis, require_completed=True)


def _read_verified_page(root: Path, page: dict) -> str:
    """Abre e confere exatamente um arquivo de página já indexado."""
    try:
        file = _safe_child(root, page["file"])
        if not file.is_file() or file.is_symlink():
            raise CorpusUnavailableError("Página do corpus ausente.")
        text = file.read_bytes().decode("utf-8")
        if (text != canonicalize_page(text) or page["sha256"] != _digest(text)
                or page["char_count"] != len(text)):
            raise CorpusUnavailableError("Página do corpus alterada ou corrompida.")
        return text
    except (OSError, UnicodeError, ValueError) as error:
        raise CorpusUnavailableError("Página do corpus ausente ou inconsistente.") from error


def validate_qualitative_corpus(analysis: Analysis, *, allow_processing: bool = False) -> dict:
    """Diagnóstico profundo explícito; percorre todos os textos persistidos.

    ``allow_processing`` é usado somente pelo job após publicar o diretório e
    antes de confirmar ``concluida`` no banco. Não executa PDF nem OCR.
    """
    manifest = _load_structural_manifest(analysis, require_completed=not allow_processing)
    root = qualitative_corpus_dir(analysis.id)
    for document in manifest["documents"]:
        for page in document["pages"]:
            _read_verified_page(root, page)
    return manifest


def is_qualitative_corpus_ready(analysis: Analysis) -> bool:
    try:
        validate_qualitative_corpus(analysis)
        return True
    except CorpusUnavailableError:
        return False


def read_qualitative_page(analysis: Analysis, document_id: str, page_number: int,
                          *, manifest: dict | None = None) -> dict:
    """Fronteira estável para a futura codificação; texto sem pós-processamento."""
    try:
        identifier = str(UUID(document_id))
    except ValueError as error:
        raise QualitativePageNotFoundError("Documento inválido.") from error
    # O manifest opcional vem apenas do helper estrutural na mesma requisição.
    manifest = manifest if manifest is not None else load_qualitative_manifest(analysis)
    document = next((item for item in manifest["documents"] if item["document_id"] == identifier), None)
    if document is None or not isinstance(page_number, int) or not 1 <= page_number <= document["page_count"]:
        raise QualitativePageNotFoundError("Página não encontrada nesta Base.")
    page = document["pages"][page_number - 1]
    text = _read_verified_page(qualitative_corpus_dir(analysis.id), page)
    return {
        "analysis_id": analysis.id,
        "document_id": identifier,
        "page_number": page_number,
        "page_count": document["page_count"],
        "text": text,
        "sha256": page["sha256"],
        "char_count": page["char_count"],
        "extraction_method": page.get("extraction_method"),
    }
