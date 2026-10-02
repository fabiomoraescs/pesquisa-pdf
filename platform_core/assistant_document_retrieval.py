"""Recuperação documental autorizada e limitada para o Assistente Análysis.

Esta camada lê somente o corpus qualitativo já publicado. Evidências são
transitórias: nunca criam trechos, códigos, codificações ou memos.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from typing import Any

from flask import url_for

from .assistant_project_context import resolve_project_context
from .extensions import db
from .models import Analysis
from .qualitative_corpus import CorpusUnavailableError, load_qualitative_manifest, read_qualitative_page
from .qualitative_expanded_search import SemanticModelUnavailable, search_lexical, search_semantic
from .qualitative_search import QualitativeSearchError


# Limites do contexto documental, deliberadamente separados do número de
# candidatos que os mecanismos de busca podem localizar.
MAX_RETRIEVAL_CANDIDATES = 600
MAX_EVIDENCE_ITEMS = 30
MAX_EVIDENCE_CHARS = 12_000
MAX_EVIDENCE_PREVIEW_CHARS = 260
MAX_EVIDENCE_PER_DOCUMENT = 2
MAX_EVIDENCE_PER_PAGE = 1
EVIDENCE_CONTEXT_CHARS = 180

_DOCUMENTAL_LANGUAGE = re.compile(
    r"\b(?:documento|documentos|corpus|pdf|trecho|trechos|passagem|passagens|"
    r"p[aá]gina|p[aá]ginas|onde aparece|em qual|compar\w*|diferen[çc]\w*|"
    r"discut\w*|sustent\w*|contrad\w*|conceito|autores?|resuma|resumir)\b", re.IGNORECASE,
)
_SEMANTIC_LANGUAGE = re.compile(
    r"\b(?:discut\w*|compar\w*|diferen[çc]\w*|sustent\w*|contrad\w*|resuma|resumir|"
    r"interpreta|significa|quer dizer)\b", re.IGNORECASE,
)
_CURRENT_PAGE_LANGUAGE = re.compile(r"\b(?:esta|nessa|nesta)\s+p[aá]gina\b", re.IGNORECASE)
_CURRENT_DOCUMENT_LANGUAGE = re.compile(r"\b(?:este|nesse|neste)\s+documento\b", re.IGNORECASE)
_COMPARISON_LANGUAGE = re.compile(r"\b(?:compar\w*|diferen[çc]\w*)", re.IGNORECASE)
_FULL_SUMMARY_LANGUAGE = re.compile(
    r"\b(?:resuma|resumir|resumo)\b.*\b(?:inteiro|integral|todo|toda|corpus\s+inteiro)\b",
    re.IGNORECASE,
)
_ACTION_LANGUAGE = re.compile(
    r"\b(?:crie|criar|codifique|codificar|exclua|excluir|renomeie|renomear|altere|alterar|execute|executar)\b",
    re.IGNORECASE,
)
_TOPIC_PREFIX = re.compile(
    r"^\s*(?:onde\s+aparece(?:\s+a\s+express[aã]o)?|em\s+quais\s+documentos?\s+aparece|"
    r"o\s+que\s+(?:os\s+)?documentos?\s+dizem\s+sobre|quais\s+trechos?\s+discutem|"
    r"localize\s+trechos?(?:\s+que\s+sustentem)?|(?:o\s+)?conceito\s+de)\s+",
    re.IGNORECASE,
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def is_document_question(question: object) -> bool:
    """Classificação conservadora; não cria termos nem muda autorização."""
    return isinstance(question, str) and bool(_DOCUMENTAL_LANGUAGE.search(question))


def _topic(question: str) -> str:
    quoted = re.search(r"[\"“]([^\"”]{1,200})[\"”]", question)
    candidate = quoted.group(1) if quoted else _TOPIC_PREFIX.sub("", question)
    topic = candidate.strip(" \t\r\n.?!:;—–")
    if " sobre " in topic.casefold():
        topic = topic.rsplit(" sobre ", 1)[-1].strip(" \t\r\n.?!:;")
    return topic[:200] or question.strip()[:200]


def _empty_pack(query: str, *, status: str, limitation: str) -> dict[str, Any]:
    return {
        "query": query,
        "scope": {},
        "evidence": [],
        "retrieval": {"candidate_count": 0, "evidence_count": 0, "methods": []},
        "limitations": [limitation],
        "status": status,
    }


def _normalized_name(value: str) -> str:
    return " ".join(value.casefold().removesuffix(".pdf").split())


def _document_scope(question: str, manifest: Mapping[str, Any], page_context: Mapping[str, Any]) -> tuple[list[str], int | None, str]:
    documents = manifest["documents"]
    current_document = _mapping(page_context.get("document")).get("id")
    current_page = page_context.get("current_page")
    if _CURRENT_PAGE_LANGUAGE.search(question) and current_document and isinstance(current_page, int):
        return [current_document], current_page, "current_page"
    if _CURRENT_DOCUMENT_LANGUAGE.search(question) and current_document:
        return [current_document], None, "current_document"

    normalized_question = _normalized_name(question)
    named = [item["document_id"] for item in documents
             if _normalized_name(item["original_name"]) in normalized_question]
    if named:
        return named, None, "named_documents"
    return [item["document_id"] for item in documents], None, "analysis"


def _scope_has_no_text(manifest: Mapping[str, Any], document_ids: list[str]) -> bool:
    """Distingue corpus sem texto de uma consulta cujo tema não foi localizado."""
    selected = [item for item in manifest["documents"] if item["document_id"] in set(document_ids)]
    return bool(selected) and all(
        not any(int(page.get("char_count") or 0) > 0 for page in item["pages"])
        for item in selected
    )


def _search_candidates(search, analysis: Analysis, document_ids: list[str], query: str,
                       *, scope_kind: str) -> list[dict[str, Any]]:
    """Reutiliza o buscador existente, sem varrer a Base fora do escopo pedido."""
    if scope_kind == "analysis":
        return list(search(analysis, None, query)["results"])
    results: list[dict[str, Any]] = []
    for document_id in document_ids:
        results.extend(search(analysis, document_id, query)["results"])
    return results


def _candidate_key(item: Mapping[str, Any]) -> tuple[str, int, int, int, str]:
    return (str(item["document_id"]), int(item["page_number"]), int(item["start_offset"]),
            int(item["end_offset"]), str(item.get("page_text_hash", "")))


def _score(item: Mapping[str, Any]) -> float:
    semantic = item.get("semantic_score")
    if isinstance(semantic, (int, float)):
        return float(semantic)
    return {"literal": 1.0, "lexical": 0.9, "morphological": 0.86,
            "derivational": 0.82, "semantic": 0.5}.get(str(item.get("match_type")), 0.4)


def _deduplicate(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, int, int, int, str], dict[str, Any]] = {}
    for item in candidates:
        key = _candidate_key(item)
        method = str(item.get("match_type") or "unknown")
        current = merged.get(key)
        if current is None:
            merged[key] = {**item, "retrieval_methods": [method], "retrieval_score": _score(item)}
            continue
        current["retrieval_methods"] = sorted(set([*current["retrieval_methods"], method]))
        current["retrieval_score"] = max(current["retrieval_score"], _score(item))
    return sorted(merged.values(), key=lambda item: (-item["retrieval_score"], item["document_id"],
                                                       item["page_number"], item["start_offset"]))


def _evidence_text(analysis: Analysis, item: Mapping[str, Any], cache: dict[tuple[str, int], str]) -> tuple[str, bool]:
    key = (str(item["document_id"]), int(item["page_number"]))
    page = cache.get(key)
    if page is None:
        page = read_qualitative_page(analysis, key[0], key[1])["text"]
        cache[key] = page
    start_offset, end_offset = int(item["start_offset"]), int(item["end_offset"])
    # Quando a extração preserva parágrafos, disponibiliza a unidade atual e
    # seu entorno imediato. Isso evita interpretar uma frase sem a ressalva
    # anterior ou a continuação seguinte. PDFs sem separação de parágrafos
    # mantêm a janela curta e previsível usada até aqui.
    paragraphs = [match for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", page)]
    current = next((index for index, match in enumerate(paragraphs)
                    if match.start() <= start_offset < match.end()), None)
    if current is not None and len(paragraphs) > 1:
        window = paragraphs[max(0, current - 1):min(len(paragraphs), current + 2)]
        text = "\n\n".join(" ".join(match.group(0).split()) for match in window if match.group(0).strip())
        if text:
            return text, len(window) < len(paragraphs)
    start = max(0, start_offset - EVIDENCE_CONTEXT_CHARS)
    end = min(len(page), end_offset + EVIDENCE_CONTEXT_CHARS)
    text = " ".join(page[start:end].split())
    return text, start > 0 or end < len(page)


def _select_evidence(analysis: Analysis, manifest: Mapping[str, Any], candidates: list[dict[str, Any]],
                     document_ids: list[str], *, page_number: int | None) -> tuple[list[dict[str, Any]], bool]:
    allowed = set(document_ids)
    filtered = [item for item in candidates[:MAX_RETRIEVAL_CANDIDATES]
                if item["document_id"] in allowed and (page_number is None or item["page_number"] == page_number)]
    names = {item["document_id"]: item["original_name"] for item in manifest["documents"]}
    selected: list[dict[str, Any]] = []
    per_document: dict[str, int] = {}
    per_page: dict[tuple[str, int], int] = {}
    cache: dict[tuple[str, int], str] = {}
    used_chars = 0
    for pass_limit in (MAX_EVIDENCE_PER_DOCUMENT, MAX_EVIDENCE_ITEMS):
        for item in filtered:
            if len(selected) >= MAX_EVIDENCE_ITEMS:
                continue
            identifier = str(item["document_id"])
            if per_document.get(identifier, 0) >= pass_limit:
                continue
            page_key = (identifier, int(item["page_number"]))
            if per_page.get(page_key, 0) >= MAX_EVIDENCE_PER_PAGE:
                continue
            duplicate = any((entry["document_id"], entry["page_number"], entry["start_offset"], entry["end_offset"])
                            == (identifier, item["page_number"], item["start_offset"], item["end_offset"])
                            for entry in selected)
            if duplicate:
                continue
            text, text_truncated = _evidence_text(analysis, item, cache)
            remaining = MAX_EVIDENCE_CHARS - used_chars
            if remaining <= 0:
                return selected, True
            if len(text) > remaining:
                text = text[:remaining].rstrip()
                text_truncated = True
            if not text:
                continue
            selected.append({
                "document_id": identifier,
                "document_name": names[identifier],
                "page_number": int(item["page_number"]),
                "start_offset": int(item["start_offset"]),
                "end_offset": int(item["end_offset"]),
                "text": text,
                "text_truncated": text_truncated,
                "preview": text[:MAX_EVIDENCE_PREVIEW_CHARS].rstrip(),
                "retrieval_methods": item["retrieval_methods"],
                "score": round(float(item["retrieval_score"]), 4),
                "url": url_for("qualitative.page", analysis_id=analysis.id, document_id=identifier,
                               page_number=int(item["page_number"])),
            })
            per_document[identifier] = per_document.get(identifier, 0) + 1
            per_page[page_key] = per_page.get(page_key, 0) + 1
            used_chars += len(text)
            if len(selected) >= MAX_EVIDENCE_ITEMS:
                return selected, len(filtered) > len(selected)
    return selected, len(filtered) > len(selected)


def retrieve_document_evidence(user: object, project_context: Mapping[str, Any], question: str) -> dict[str, Any]:
    """Recupera evidências do corpus autorizado para a pergunta atual.

    A Base é revalidada com ``resolve_project_context`` antes de abrir qualquer
    arquivo. IDs e nomes recebidos do navegador não chegam a esta função.
    """
    started = time.monotonic()
    context_project = _mapping(project_context.get("project"))
    context_analysis = _mapping(_mapping(project_context.get("analysis")).get("selected"))
    if not context_analysis:
        records = int(_mapping(project_context.get("corpus")).get("analysis_document_records_total") or 0)
        message = ("Este projeto ainda não possui documentos disponíveis para consulta."
                   if records == 0 else "Abra uma Base específica para consultar o conteúdo documental.")
        return _empty_pack(_topic(question), status="unavailable", limitation=message)
    if int(_mapping(project_context.get("corpus")).get("selected_analysis_document_count") or 0) == 0:
        return _empty_pack(
            _topic(question),
            status="unavailable",
            limitation="Este projeto ainda não possui documentos disponíveis para consulta.",
        )
    verified = resolve_project_context(user, {
        "project_id": context_project.get("id"), "analysis_id": context_analysis.get("id"),
    })
    if verified is None or _mapping(verified.get("tool")).get("id") != "qualitative_analysis":
        return _empty_pack(_topic(question), status="unauthorized",
                           limitation="A consulta documental não está disponível neste contexto autorizado.")
    analysis_id = _mapping(_mapping(verified.get("analysis")).get("selected")).get("id")
    analysis = db.session.get(Analysis, analysis_id)
    if analysis is None:
        return _empty_pack(_topic(question), status="unavailable",
                           limitation="A Base selecionada não está disponível para consulta.")
    query = _topic(question)
    if _FULL_SUMMARY_LANGUAGE.search(question):
        return _empty_pack(
            query,
            status="unavailable",
            limitation=("A consulta documental recupera trechos relacionados a um tema, mas não produz um resumo "
                        "integral sem ler o documento ou corpus completo."),
        )
    try:
        manifest = load_qualitative_manifest(analysis)
        # A página transitória já foi validada contra a Base ao montar o
        # contexto; a revalidação acima cobre novamente usuário/projeto/Base.
        page_context = _mapping(project_context.get("page"))
        document_ids, current_page, scope_kind = _document_scope(question, manifest, page_context)
        semantic = bool(_SEMANTIC_LANGUAGE.search(question))
        try:
            found = _search_candidates(search_semantic if semantic else search_lexical,
                                       analysis, document_ids, query, scope_kind=scope_kind)
            methods = ["semantic", "lexical"] if semantic else ["lexical"]
            limitations: list[str] = []
        except SemanticModelUnavailable:
            found = _search_candidates(search_lexical, analysis, document_ids, query,
                                       scope_kind=scope_kind)
            methods = ["lexical"]
            limitations = ["A recuperação semântica não estava disponível; foram buscadas correspondências lexicais."]
        candidates = _deduplicate(found)
        evidence, truncated = _select_evidence(analysis, manifest, candidates, document_ids,
                                                page_number=current_page)
    except (CorpusUnavailableError, QualitativeSearchError, OSError, ValueError) as error:
        logging.getLogger(__name__).warning("Falha na recuperação documental da Base %s: %s", analysis.id,
                                            type(error).__name__)
        return _empty_pack(query, status="error",
                           limitation="Não foi possível consultar o corpus neste momento. Tente novamente.")

    if _COMPARISON_LANGUAGE.search(question):
        names = {item["document_id"]: item["original_name"] for item in manifest["documents"]}
        missing = [names[identifier] for identifier in document_ids
                   if not any(item["document_id"] == identifier for item in evidence)]
        if missing:
            limitations.append("Não encontrei evidência suficiente em: " + ", ".join(missing) + ".")
    if truncated:
        limitations.append("Foram selecionadas apenas as evidências mais relevantes dentro do limite de contexto.")
    status = "ok" if evidence else "no_evidence"
    if not evidence:
        if _scope_has_no_text(manifest, document_ids):
            limitations.append("Os documentos selecionados não possuem conteúdo textual disponível para recuperação.")
        else:
            limitations.append("Não encontrei trechos suficientes no corpus para sustentar essa afirmação.")
    elapsed_ms = round((time.monotonic() - started) * 1000)
    logging.getLogger(__name__).info("Recuperação documental: base=%s métodos=%s candidatos=%s evidências=%s ms=%s",
                                    analysis.id, ",".join(methods), len(candidates), len(evidence), elapsed_ms)
    return {
        "query": query,
        "scope": {"kind": scope_kind, "analysis_id": analysis.id, "document_ids": document_ids,
                  **({"page_number": current_page} if current_page is not None else {})},
        "evidence": evidence,
        "retrieval": {"methods": methods, "candidate_count": len(candidates), "evidence_count": len(evidence),
                      "candidate_limit": MAX_RETRIEVAL_CANDIDATES, "evidence_limit": MAX_EVIDENCE_ITEMS,
                      "character_limit": MAX_EVIDENCE_CHARS, "elapsed_ms": elapsed_ms},
        "limitations": limitations,
        "status": status,
        "read_only": True,
        "document_content_untrusted": True,
        "action_requested": bool(_ACTION_LANGUAGE.search(question)),
    }


def retrieve_corpus_evidence(
    user: object,
    project_context: Mapping[str, Any],
    *,
    query: str,
    scope: str = "analysis",
    document_ids: object = None,
    retrieval_mode: str = "auto",
) -> dict[str, Any]:
    """Recupera evidências para a ferramenta ``search_corpus``.

    Diferentemente da compatibilidade da Fase 4, esta fronteira não classifica
    a pergunta do usuário por palavras-chave. O modelo escolhe o escopo e o
    modo, e o servidor reduz essa escolha ao corpus já autorizado.
    """
    query = " ".join(query.split()) if isinstance(query, str) else ""
    if not query or len(query) > 200:
        return _empty_pack(query[:200], status="invalid", limitation="Informe um tema de até 200 caracteres.")
    started = time.monotonic()
    context_project = _mapping(project_context.get("project"))
    context_analysis = _mapping(_mapping(project_context.get("analysis")).get("selected"))
    if not context_analysis:
        return _empty_pack(query, status="unavailable",
                           limitation="Abra uma Base específica para consultar o conteúdo documental.")
    verified = resolve_project_context(user, {
        "project_id": context_project.get("id"), "analysis_id": context_analysis.get("id"),
    })
    if verified is None or _mapping(verified.get("tool")).get("id") != "qualitative_analysis":
        return _empty_pack(query, status="unauthorized",
                           limitation="A consulta documental não está disponível neste contexto autorizado.")
    analysis_id = _mapping(_mapping(verified.get("analysis")).get("selected")).get("id")
    analysis = db.session.get(Analysis, analysis_id)
    if analysis is None:
        return _empty_pack(query, status="unavailable",
                           limitation="A Base selecionada não está disponível para consulta.")
    try:
        manifest = load_qualitative_manifest(analysis)
        all_document_ids = [item["document_id"] for item in manifest["documents"]]
        page_context = _mapping(project_context.get("page"))
        current_document = _mapping(page_context.get("document")).get("id")
        current_page = page_context.get("current_page")
        requested = document_ids if isinstance(document_ids, list) else []
        requested = [str(identifier) for identifier in requested if str(identifier) in set(all_document_ids)]
        scope = scope if scope in {"analysis", "current_page", "current_document", "named_documents"} else "analysis"
        page_number: int | None = None
        if scope == "current_page":
            if current_document not in all_document_ids or not isinstance(current_page, int):
                return _empty_pack(query, status="unavailable",
                                   limitation="Não há uma página atual autorizada para leitura.")
            selected_ids = [current_document]
            page_number = current_page
        elif scope == "current_document":
            if current_document not in all_document_ids:
                return _empty_pack(query, status="unavailable",
                                   limitation="Não há um documento atual autorizado para consulta.")
            selected_ids = [current_document]
        elif scope == "named_documents":
            if not requested:
                return _empty_pack(query, status="invalid",
                                   limitation="Nenhum documento autorizado foi informado para este escopo.")
            selected_ids = list(dict.fromkeys(requested))
        else:
            selected_ids = all_document_ids
        mode = retrieval_mode if retrieval_mode in {"auto", "lexical", "semantic"} else "auto"
        methods: list[str] = ["lexical"]
        found = _search_candidates(search_lexical, analysis, selected_ids, query,
                                   scope_kind="analysis" if len(selected_ids) == len(all_document_ids) else "documents")
        limitations: list[str] = []
        if mode == "semantic":
            try:
                found.extend(_search_candidates(search_semantic, analysis, selected_ids, query,
                                                scope_kind="analysis" if len(selected_ids) == len(all_document_ids) else "documents"))
                methods.append("semantic")
            except SemanticModelUnavailable:
                limitations.append("A recuperação semântica não estava disponível; foram buscadas correspondências lexicais.")
        candidates = _deduplicate(found)
        evidence, truncated = _select_evidence(analysis, manifest, candidates, selected_ids,
                                                page_number=page_number)
    except (CorpusUnavailableError, QualitativeSearchError, OSError, ValueError) as error:
        logging.getLogger(__name__).warning("Falha na ferramenta de corpus: base=%s erro=%s", analysis.id,
                                            type(error).__name__)
        return _empty_pack(query, status="error",
                           limitation="Não foi possível consultar o corpus neste momento. Tente novamente.")
    if truncated:
        limitations.append("Foram selecionadas apenas as evidências mais relevantes dentro do limite de contexto.")
    if not evidence:
        if _scope_has_no_text(manifest, selected_ids):
            limitations.append("Os documentos selecionados não possuem conteúdo textual disponível para recuperação.")
        else:
            limitations.append("Não encontrei trechos suficientes no corpus para sustentar essa afirmação.")
    elapsed_ms = round((time.monotonic() - started) * 1000)
    logging.getLogger(__name__).info("Ferramenta corpus: base=%s métodos=%s candidatos=%s evidências=%s ms=%s",
                                    analysis.id, ",".join(methods), len(candidates), len(evidence), elapsed_ms)
    return {
        "query": query,
        "scope": {"kind": scope, "analysis_id": analysis.id, "document_ids": selected_ids,
                  **({"page_number": page_number} if page_number is not None else {})},
        "evidence": evidence,
        "retrieval": {"methods": methods, "candidate_count": len(candidates), "evidence_count": len(evidence),
                      "candidate_limit": MAX_RETRIEVAL_CANDIDATES, "evidence_limit": MAX_EVIDENCE_ITEMS,
                      "character_limit": MAX_EVIDENCE_CHARS, "elapsed_ms": elapsed_ms},
        "limitations": limitations,
        "status": "ok" if evidence else "no_evidence",
        "read_only": True,
        "document_content_untrusted": True,
    }


__all__ = [
    "MAX_EVIDENCE_CHARS", "MAX_EVIDENCE_ITEMS", "MAX_RETRIEVAL_CANDIDATES",
    "is_document_question", "retrieve_corpus_evidence", "retrieve_document_evidence",
]
