"""Busca lexical e semântica sobre páginas canônicas, com offsets originais."""

from __future__ import annotations

import hashlib
import io
import math
import os
from collections import Counter, defaultdict
from zipfile import BadZipFile
from threading import RLock
from uuid import uuid4

import numpy as np
import regex

from analyzer import v3
from analyzer.lexical_family import WORDS, find_lexical_matches

from .qualitative_corpus import load_qualitative_manifest, qualitative_corpus_dir, read_qualitative_page
from .qualitative_search import QualitativeSearchError


SEGMENTATION_VERSION = "sentence-word-eligible-v2"
MODEL_CACHE_VERSION = "v3-multilingual-minilm-v1"
SEGMENT_TARGET = 400  # comprimento textual, jamais limite de resultados
EMBEDDING_BATCH = 32
DEFAULT_SEMANTIC_THRESHOLD = 0.50
_MODEL_LOCK = RLock()  # inicialização e inferência seguras entre threads Gunicorn
_ALPHABETIC_WORD = regex.compile(r"\p{L}[\p{L}\p{M}]*")
_EDITORIAL_LINE = regex.compile(
    r"(?i)^(?:cap[ií]tulo|figura|tabela|quadro|p[aá]gina|editora)\s+"
    r"[\p{L}\p{M}\p{N}\s./–-]{1,60}[.!]?$"
)


class SemanticModelUnavailable(RuntimeError):
    """Modelo/cache indisponível; a operação não pode cair para Literal."""


def semantic_threshold() -> float:
    value = os.environ.get("QUALITATIVE_SEMANTIC_THRESHOLD", str(DEFAULT_SEMANTIC_THRESHOLD))
    try:
        threshold = float(value)
    except ValueError as error:
        raise QualitativeSearchError("QUALITATIVE_SEMANTIC_THRESHOLD inválido.") from error
    if not 0.5 <= threshold <= 0.9:
        raise QualitativeSearchError("QUALITATIVE_SEMANTIC_THRESHOLD deve estar entre 0.50 e 0.90.")
    return threshold


def _pages(analysis, document_id, manifest):
    documents = [item for item in manifest["documents"]
                 if document_id is None or item["document_id"] == document_id]
    if not documents:
        from .qualitative_corpus import QualitativePageNotFoundError
        raise QualitativePageNotFoundError("Documento não encontrado nesta Base.")
    for document in documents:
        for number in range(1, document["page_count"] + 1):
            yield document["document_id"], number, read_qualitative_page(
                analysis, document["document_id"], number, manifest=manifest)


def _key(match):
    return (match["document_id"], match["page_number"], match["start_offset"],
            match["end_offset"], match["page_text_hash"])


def _match(document_id, number, page, start, end, kind, *, semantic_score=None):
    text = page["text"]
    item = {"document_id": document_id, "page_number": number, "start_offset": start,
            "end_offset": end, "page_text_hash": page["sha256"],
            "match_text": text[start:end],
            "snippet": text[max(0, start - 55):min(len(text), end + 55)],
            "match_type": kind}
    if semantic_score is not None:
        item["semantic_score"] = float(semantic_score)
    return item


def search_lexical(analysis, document_id, query, *, case_sensitive=False, progress_callback=None):
    """Literal, flexões e família derivacional, sempre com offsets originais."""
    manifest = load_qualitative_manifest(analysis)
    matches = {}
    order = {item["document_id"]: index for index, item in enumerate(manifest["documents"])}
    total_pages = sum(doc["page_count"] for doc in manifest["documents"]
                      if document_id is None or doc["document_id"] == document_id)
    has_words = bool(WORDS.search(query))
    completed = 0
    for current_document_id, number, page in _pages(analysis, document_id, manifest):
        text = page["text"]
        for start, end, kind in find_lexical_matches(text, query, case_sensitive=case_sensitive):
            item = _match(current_document_id, number, page, start, end,
                          "literal" if kind == "literal" else "lexical")
            matches.setdefault(_key(item), item)
        completed += 1
        if progress_callback:
            # A busca lexical mantém a etapa literal visível para o cliente,
            # como antes da centralização do motor de matching.
            progress_callback({"stage": "literal", "completed": completed, "total": total_pages,
                               "document_id": current_document_id, "page_number": number})
        if progress_callback and has_words:
            progress_callback({"stage": "lexical", "completed": completed, "total": total_pages,
                               "document_id": current_document_id, "page_number": number})
    results = sorted(matches.values(), key=lambda item: (order[item["document_id"]],
                      item["page_number"], item["start_offset"], item["end_offset"]))
    return {"results": results, "total": len(results), "offset_unit": "unicode_codepoint"}


def _trim(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def is_semantic_segment_eligible(text):
    """Exige linguagem suficiente, não apenas uma legenda, número ou palavra-chave."""
    words = _ALPHABETIC_WORD.findall(text)
    letters = sum(len(word) for word in words)
    digits = sum(char.isdigit() for char in text)
    replacement_chars = text.count("\ufffd")
    lines = [line for line in text.splitlines() if line.strip()]
    table_like = (len(lines) >= 3 and len(words) <= 2.5 * len(lines)
                  and not text.rstrip().endswith((".", "!", "?")))
    return (len(words) >= 3 and letters >= 18 and letters >= digits
            and replacement_chars * 6 <= letters + digits
            and not table_like
            and not _EDITORIAL_LINE.fullmatch(text.strip()))


def _edge_lines(text):
    lines = [(match.start(), match.end(), match.group().strip())
             for match in regex.finditer(r"(?m)^.*(?:\n|$)", text) if match.group().strip()]
    return lines, {index for index in (*range(min(2, len(lines))),
                                    *range(max(0, len(lines) - 2), len(lines)))}


def _line_signature(value):
    return regex.sub(r"\p{N}+", "#", " ".join(value.casefold().split()))


def _repeated_edge_signatures(pages):
    """Somente linhas de borda presentes em muitas páginas do mesmo documento."""
    counts = defaultdict(Counter)
    page_counts = Counter()
    for document_id, _number, page in pages:
        page_counts[document_id] += 1
        lines, edge_indices = _edge_lines(page["text"])
        counts[document_id].update({_line_signature(lines[index][2]) for index in edge_indices
                                    if (is_semantic_segment_eligible(lines[index][2])
                                        and len(lines[index][2]) <= 120
                                        and not lines[index][2].endswith((".", "!", "?")))})
    return {document_id: {signature for signature, count in signatures.items()
                          if count >= max(3, math.ceil(page_counts[document_id] * 0.1))}
            for document_id, signatures in counts.items()}


def _excluded_lines(text, repeated_signatures):
    lines, edge_indices = _edge_lines(text)
    excluded = []
    for index, (start, end, value) in enumerate(lines):
        words = _ALPHABETIC_WORD.findall(value)
        if (not words or _EDITORIAL_LINE.fullmatch(value)
                or (index in edge_indices and len(words) <= 2 and not value.endswith((".", "!", "?")))
                or (index in edge_indices and _line_signature(value) in repeated_signatures)):
            excluded.append((start, end))
    return excluded


def semantic_segments(text, *, repeated_signatures=frozenset()):
    """Spans semânticos elegíveis; preserva índices do texto canônico original."""
    lowered = text.casefold()
    if "ficha catalogr" in lowered and ("isbn" in lowered or "cdd" in lowered):
        return []  # página de metadados editoriais, não de prosa do documento
    segments = []
    boundaries = _excluded_lines(text, repeated_signatures)
    runs = []
    previous = 0
    for start, end in boundaries:
        if previous < start:
            runs.append((previous, start))
        previous = end
    if previous < len(text):
        runs.append((previous, len(text)))
    for run_start, run_end in runs:
        for paragraph in regex.finditer(r"\S[\s\S]*?(?=\n[ \t]*\n|\Z)", text[run_start:run_end]):
            paragraph_start = run_start + paragraph.start()
            for sentence in regex.finditer(r"[^.!?]+(?:[.!?]+|$)", paragraph.group()):
                start, end = _trim(text, paragraph_start + sentence.start(),
                                   paragraph_start + sentence.end())
                if start >= end:
                    continue
                if end - start <= SEGMENT_TARGET:
                    if is_semantic_segment_eligible(text[start:end]):
                        segments.append((start, end))
                    continue
                tokens = list(regex.finditer(r"\S+", text[start:end]))
                chunk_start = chunk_end = None
                for token in tokens:
                    token_start, token_end = start + token.start(), start + token.end()
                    if chunk_start is not None and token_end - chunk_start > SEGMENT_TARGET:
                        if is_semantic_segment_eligible(text[chunk_start:chunk_end]):
                            segments.append((chunk_start, chunk_end))
                        chunk_start = None
                    if chunk_start is None:
                        chunk_start = token_start
                    chunk_end = token_end
                if chunk_start is not None and is_semantic_segment_eligible(text[chunk_start:chunk_end]):
                    segments.append((chunk_start, chunk_end))
    return segments


def _model():
    try:
        with _MODEL_LOCK:
            return v3.carregar_modelo_semantico()
    except Exception as error:
        raise SemanticModelUnavailable(
            "Não foi possível carregar o modelo semântico multilíngue. Verifique o cache do modelo na instalação. "
            "Nenhuma codificação foi salva.") from error


def _encode(model, texts):
    try:
        with _MODEL_LOCK:
            vectors = np.asarray(model.encode(texts, batch_size=EMBEDDING_BATCH,
                                               convert_to_numpy=True, show_progress_bar=False), dtype=np.float32)
    except Exception as error:
        raise SemanticModelUnavailable("Falha ao gerar embeddings semânticos. Nenhuma codificação foi salva.") from error
    if vectors.ndim != 2 or vectors.shape[0] != len(texts) or not np.isfinite(vectors).all():
        raise SemanticModelUnavailable("Embeddings semânticos inválidos. Nenhuma codificação foi salva.")
    norm = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norm, 1e-12)


def _cached_page_vectors(analysis, document_id, number, page, segments, model, *, on_batch=None):
    identity = hashlib.sha256("|".join((v3.MODELO_SEMANTICO, MODEL_CACHE_VERSION,
        SEGMENTATION_VERSION, page["sha256"])).encode("utf-8")).hexdigest()
    directory = qualitative_corpus_dir(analysis.id) / document_id / "embeddings"
    if directory.is_symlink() or directory.parent.is_symlink():
        raise SemanticModelUnavailable("Diretório de cache semântico inválido.")
    path = directory / f"{number:06d}-{identity}.npz"
    offsets = np.asarray(segments, dtype=np.int64).reshape(-1, 2)
    if path.is_file() and not path.is_symlink():
        try:
            with np.load(path, allow_pickle=False) as saved:
                cached_offsets = saved["offsets"]
                vectors = saved["vectors"]
                if (np.array_equal(cached_offsets, offsets) and vectors.ndim == 2
                        and vectors.shape[0] == len(segments) and np.isfinite(vectors).all()):
                    if on_batch:
                        on_batch(len(segments), len(segments))
                    return vectors
        except (OSError, ValueError, KeyError, BadZipFile):
            pass  # cache derivado corrompido: recalcular a partir do corpus verificado
    batches = []
    for index in range(0, len(segments), EMBEDDING_BATCH):
        batch = segments[index:index + EMBEDDING_BATCH]
        batches.append(_encode(model, [page["text"][start:end] for start, end in batch]))
        if on_batch:
            on_batch(min(index + len(batch), len(segments)), len(segments))
    vectors = np.vstack(batches)
    try:
        directory.mkdir(exist_ok=True)
        buffer = io.BytesIO()
        np.savez_compressed(buffer, offsets=offsets, vectors=vectors)
        temporary = directory / f".{uuid4().hex}.tmp"
        try:
            temporary.write_bytes(buffer.getvalue())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise SemanticModelUnavailable("Não foi possível armazenar o cache semântico. Nenhuma codificação foi salva.") from error
    return vectors


def search_semantic(analysis, document_id, query, *, case_sensitive=False, progress_callback=None):
    """Literal ⊂ Lexical ⊂ Semântica; todos os segmentos acima do limiar."""
    lexical = search_lexical(analysis, document_id, query, case_sensitive=case_sensitive,
                             progress_callback=progress_callback)
    threshold = semantic_threshold()
    manifest = load_qualitative_manifest(analysis)
    total_pages = sum(doc["page_count"] for doc in manifest["documents"]
                      if document_id is None or doc["document_id"] == document_id)
    completed_pages = 0
    model = query_vector = None
    order = {item["document_id"]: index for index, item in enumerate(manifest["documents"])}
    results = list(lexical["results"])
    prior = {}
    for item in results:
        prior.setdefault((item["document_id"], item["page_number"]), []).append(
            (item["start_offset"], item["end_offset"]))
    repeated = _repeated_edge_signatures(_pages(analysis, document_id, manifest))
    for doc, number, page in _pages(analysis, document_id, manifest):
        segments = semantic_segments(page["text"], repeated_signatures=repeated.get(doc, ()))
        if not segments:
            completed_pages += 1
            if progress_callback:
                progress_callback({"stage": "semantic", "completed": completed_pages,
                                   "total": total_pages, "document_id": doc, "page_number": number})
            continue
        if model is None:
            if progress_callback:
                progress_callback({"stage": "model", "completed": 0, "total": 1,
                                   "document_id": doc, "page_number": number})
            model = _model()
            query_vector = _encode(model, [query])[0]
        if progress_callback:
            progress_callback({"stage": "semantic", "completed": completed_pages,
                               "total": total_pages, "document_id": doc, "page_number": number})
        def on_batch(done, count):
            if progress_callback:
                progress_callback({"stage": "semantic", "completed": completed_pages + done / count,
                                   "total": total_pages, "document_id": doc, "page_number": number})
        vectors = _cached_page_vectors(analysis, doc, number, page, segments, model, on_batch=on_batch)
        scores = vectors @ query_vector  # vetores L2-normalizados: produto = cosseno
        for (start, end), score in zip(segments, scores):
            if score < threshold or any(start < earlier_end and end > earlier_start
                                        for earlier_start, earlier_end in prior.get((doc, number), ())):
                continue  # contexto já representado por um match literal/lexical do mesmo código
            results.append(_match(doc, number, page, start, end, "semantic", semantic_score=score))
        completed_pages += 1
        if progress_callback:
            progress_callback({"stage": "semantic", "completed": completed_pages,
                               "total": total_pages, "document_id": doc, "page_number": number})
    results.sort(key=lambda item: (order[item["document_id"]], item["page_number"],
                                 item["start_offset"], item["end_offset"]))
    return {"results": results, "total": len(results), "offset_unit": "unicode_codepoint"}
