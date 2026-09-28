"""Busca completa no corpus canônico, com proteção de execução para Regex."""

from __future__ import annotations

import regex

from .models import Analysis
from .qualitative_corpus import (
    QualitativePageNotFoundError, load_qualitative_manifest, read_qualitative_page,
)


MAX_QUERY_LENGTH = 200
SEARCH_TIMEOUT_SECONDS = 2.0


class QualitativeSearchError(ValueError):
    """Consulta inválida ou custosa demais para esta pesquisa interativa."""


def search_qualitative_document(
    analysis: Analysis, document_id: str | None, query: str, *, grep: bool = False,
    case_sensitive: bool = False, longest_regex: bool = False, progress_callback=None,
) -> dict:
    """Pesquisa páginas persistidas; document_id=None percorre o projeto.

    Não há teto de ocorrências. O iterador percorre cada página por completo.
    Apenas Regex recebe timeout de avaliação por página, contra backtracking;
    uma interrupção falha a operação inteira, nunca retorna resultados parciais.
    Spans de strings Python são offsets Unicode code points do corpus.
    """
    query = query.strip()
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise QualitativeSearchError(f"Informe uma busca de até {MAX_QUERY_LENGTH} caracteres.")
    flags = regex.VERSION1 | (0 if case_sensitive else regex.IGNORECASE | regex.FULLCASE)
    # Autocodificação escolhe o match completo mais longo (raç(a|as) -> raças).
    # A consulta permanece intacta; a busca consultiva mantém sua semântica atual.
    if grep and longest_regex:
        flags |= regex.POSIX
    try:
        pattern = regex.compile(query if grep else regex.escape(query), flags)
    except regex.error as error:
        raise QualitativeSearchError("Expressão GREP inválida.") from error
    manifest = load_qualitative_manifest(analysis)
    documents = [item for item in manifest["documents"] if document_id is None or item["document_id"] == document_id]
    if not documents:
        raise QualitativePageNotFoundError("Documento não encontrado nesta Base.")
    results = []
    total_pages = sum(document["page_count"] for document in documents)
    completed_pages = 0
    try:
        pages = ((document["document_id"], number) for document in documents
                 for number in range(1, document["page_count"] + 1))
        for current_document_id, page_number in pages:
            page = read_qualitative_page(analysis, current_document_id, page_number, manifest=manifest)
            text = page["text"]
            matches = pattern.finditer(text, timeout=SEARCH_TIMEOUT_SECONDS) if grep else pattern.finditer(text)
            for match in matches:
                start, end = match.span()
                if end <= start:
                    continue  # uma correspondência vazia não ancora um trecho
                results.append({
                    "document_id": current_document_id,
                    "page_number": page_number,
                    "start_offset": start,
                    "end_offset": end,
                    "snippet": text[max(0, start - 55):min(len(text), end + 55)],
                    "page_text_hash": page["sha256"],
                    **({"match_text": text[start:end]} if grep else {}),
                })
            completed_pages += 1
            if progress_callback:
                progress_callback({"stage": "literal", "completed": completed_pages,
                                   "total": total_pages, "document_id": current_document_id,
                                   "page_number": page_number})
    except TimeoutError as error:
        raise QualitativeSearchError(
            "A busca Regex não foi concluída: a avaliação de uma página excedeu o tempo de segurança. "
            "Nenhum resultado parcial foi aplicado. Revise a expressão."
        ) from error
    return {"results": results, "total": len(results),
            "offset_unit": "unicode_codepoint"}
