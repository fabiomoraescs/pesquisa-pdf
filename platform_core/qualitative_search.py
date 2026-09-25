"""Busca limitada no texto canônico persistido de um documento qualitativo."""

from __future__ import annotations

from time import monotonic

import regex

from .models import Analysis
from .qualitative_corpus import (
    QualitativePageNotFoundError, load_qualitative_manifest, read_qualitative_page,
)


MAX_QUERY_LENGTH = 200
MAX_RESULTS = 200
SEARCH_TIMEOUT_SECONDS = 2.0


class QualitativeSearchError(ValueError):
    """Consulta inválida ou custosa demais para esta pesquisa interativa."""


def search_qualitative_document(
    analysis: Analysis, document_id: str, query: str, *, grep: bool = False,
    case_sensitive: bool = False,
) -> dict:
    """Pesquisa apenas as páginas persistidas do documento solicitado.

    `regex` oferece timeout por operação (inclusive backtracking); o prazo
    global impede que muitas páginas consumam o worker indefinidamente.
    Spans de strings Python são offsets Unicode code points do corpus.
    """
    query = query.strip()
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise QualitativeSearchError(f"Informe uma busca de até {MAX_QUERY_LENGTH} caracteres.")
    flags = regex.VERSION1 | (0 if case_sensitive else regex.IGNORECASE | regex.FULLCASE)
    try:
        pattern = regex.compile(query if grep else regex.escape(query), flags)
    except regex.error as error:
        raise QualitativeSearchError("Expressão GREP inválida.") from error
    manifest = load_qualitative_manifest(analysis)
    document = next((item for item in manifest["documents"] if item["document_id"] == document_id), None)
    if document is None:
        raise QualitativePageNotFoundError("Documento não encontrado nesta Base.")
    results = []
    deadline = monotonic() + SEARCH_TIMEOUT_SECONDS
    try:
        for page_number in range(1, document["page_count"] + 1):
            if deadline - monotonic() <= 0:
                raise QualitativeSearchError("A busca excedeu o tempo permitido. Refine a expressão.")
            page = read_qualitative_page(analysis, document_id, page_number, manifest=manifest)
            text = page["text"]
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise QualitativeSearchError("A busca excedeu o tempo permitido. Refine a expressão.")
            for match in pattern.finditer(text, timeout=remaining):
                start, end = match.span()
                if end <= start:
                    continue  # uma correspondência vazia não ancora um trecho
                results.append({
                    "document_id": document_id,
                    "page_number": page_number,
                    "start_offset": start,
                    "end_offset": end,
                    "snippet": text[max(0, start - 55):min(len(text), end + 55)],
                    "page_text_hash": page["sha256"],
                })
                if len(results) >= MAX_RESULTS:
                    return {"results": results, "total": len(results), "truncated": True,
                            "offset_unit": "unicode_codepoint"}
    except TimeoutError as error:
        raise QualitativeSearchError("A busca excedeu o tempo permitido. Refine a expressão.") from error
    return {"results": results, "total": len(results), "truncated": False,
            "offset_unit": "unicode_codepoint"}
