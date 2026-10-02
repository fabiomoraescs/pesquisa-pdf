"""Autocodificação e rejeições sobre as âncoras canônicas existentes."""
import hashlib
import json

from sqlalchemy import select

from .extensions import db
from .models import (Analysis, QualitativeCode, QualitativeCoding, QualitativeExcerpt,
                     QualitativeRejection, normalized_qualitative_code_name)
from .qualitative_annotations import (MAX_SELECTION_LENGTH, SelectionConflict, SelectionError,
                                      find_code_by_name, get_or_create_excerpt)
from .qualitative_corpus import load_qualitative_manifest, read_qualitative_page
from .qualitative_search import MAX_QUERY_LENGTH, QualitativeSearchError, search_qualitative_document
from .qualitative_expanded_search import search_lexical, search_semantic
from .term_input import MULTIPLE_SEPARATOR_MESSAGE, has_invalid_term_separator
from analyzer.lexical_family import lexical_code_name


def prepare_automatic_query(query: str, *, multiple_terms=False, grep=False, case_sensitive=False):
    """Retorna consultas efetivas e identidade estável, sem mudar a consulta simples."""
    if type(multiple_terms) is not bool:
        raise SelectionError("Opção de múltiplos termos inválida.")
    query = query.strip()
    if not multiple_terms:
        return [query], query
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise QualitativeSearchError(f"Informe uma busca de até {MAX_QUERY_LENGTH} caracteres.")
    terms, identities = [], set()
    for item in query.split(";"):
        term = item.strip()
        if not term:
            continue
        if not grep and has_invalid_term_separator(term):
            raise QualitativeSearchError(MULTIPLE_SEPARATOR_MESSAGE)
        # Casefold de uma Regex poderia trocar \\D por \\d, \\S por \\s etc.
        identity = term if grep or case_sensitive else term.casefold()
        if identity not in identities:
            identities.add(identity)
            terms.append(term)
    if not terms:
        raise QualitativeSearchError("Informe ao menos um termo ou expressão entre os pontos e vírgulas (;).")
    canonical = json.dumps(sorted(identities), ensure_ascii=False, separators=(",", ":"))
    # Cabe em String(200), mesmo com expansões Unicode. O espaço inicial separa
    # o namespace múltiplo das consultas simples, que sempre são salvas com strip().
    # Não remover esse espaço nem usar este identificador como expressão de busca.
    source_query = " multiple:v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return terms, source_query


def _code_from_provenance(analysis, term, matches, *, code_name, grep, case_sensitive, mode, legacy_query):
    """Mantém o code_id da operação automática mesmo após renomear o código.

    A consulta original pertence à coding/rejeição, não ao nome editável do código.
    Distinct limita a leitura inicial a pares código/consulta, sem limitar resultados.
    """
    origin = "automatic_regex" if grep else f"automatic_{mode}"
    def same_query(stored):
        if legacy_query is not None and stored == legacy_query:
            return True
        return stored == term if grep or case_sensitive else bool(stored) and stored.casefold() == term.casefold()
    source_rows = db.session.execute(select(
        QualitativeCoding.code_id, QualitativeCoding.source_query).where(
        QualitativeCoding.analysis_id == analysis.id, QualitativeCoding.origin == origin,
        QualitativeCoding.source_case_sensitive == case_sensitive).distinct()).all()
    rejection_rows = db.session.execute(select(
        QualitativeRejection.code_id, QualitativeRejection.query).where(
        QualitativeRejection.analysis_id == analysis.id, QualitativeRejection.origin == origin,
        QualitativeRejection.case_sensitive == case_sensitive).distinct()).all()
    identifiers = {code_id for code_id, stored in (*source_rows, *rejection_rows) if same_query(stored)}
    direct_identifiers = {code_id for code_id, stored in (*source_rows, *rejection_rows)
                          if stored != legacy_query and same_query(stored)}
    # No modo lexical, uma mesma consulta pode criar um código para cada
    # forma localizada. Só a consulta não identifica inequivocamente o código.
    preserves_match_identity = not grep and mode == "lexical"
    if len(direct_identifiers) == 1 and not grep and not preserves_match_identity:
        return db.session.get(QualitativeCode, next(iter(direct_identifiers)))
    if not identifiers:
        return None
    match_keys = {(item["document_id"], item["page_number"], item["start_offset"],
                   item["end_offset"], item["page_text_hash"]) for item in matches}
    exact = set()
    same_matched_text = set()
    for coding, excerpt in db.session.execute(select(QualitativeCoding, QualitativeExcerpt).join(
            QualitativeExcerpt, QualitativeExcerpt.id == QualitativeCoding.excerpt_id).where(
            QualitativeCoding.analysis_id == analysis.id, QualitativeCoding.code_id.in_(identifiers),
            QualitativeCoding.origin == origin,
            QualitativeCoding.source_case_sensitive == case_sensitive)).all():
        key = (excerpt.document_id, excerpt.page_number, coding.source_start,
               coding.source_end, coding.source_page_hash)
        if same_query(coding.source_query) and key in match_keys:
            exact.add(coding.code_id)
        if (grep and same_query(coding.source_query)
                and normalized_qualitative_code_name(excerpt.quoted_text)
                == normalized_qualitative_code_name(code_name)):
            same_matched_text.add(coding.code_id)
    for rejection in db.session.scalars(select(QualitativeRejection).where(
            QualitativeRejection.analysis_id == analysis.id,
            QualitativeRejection.code_id.in_(identifiers),
            QualitativeRejection.origin == origin,
            QualitativeRejection.case_sensitive == case_sensitive)):
        key = (rejection.document_id, rejection.page_number, rejection.start_offset,
               rejection.end_offset, rejection.page_text_hash)
        if same_query(rejection.query) and key in match_keys:
            exact.add(rejection.code_id)
    if len(exact) > 1:
        raise SelectionError("A consulta automática está associada a mais de um código. Revise os códigos antes de repetir a operação.")
    if exact:
        return db.session.get(QualitativeCode, next(iter(exact)))
    return (db.session.get(QualitativeCode, next(iter(same_matched_text)))
            if len(same_matched_text) == 1 else None)


def automatic_coding(analysis: Analysis, document_id: str | None, query: str,
                     user_id: str, *, grep=False, case_sensitive=False,
                     contextual_rejection_enabled=False, multiple_terms=False,
                     mode="literal", progress_callback=None) -> dict:
    """Uma transação controlada pelo chamador; reaproveita busca, trechos e coding.

    Percorre todas as correspondências, sem teto numérico. A busca termina antes
    de qualquer escrita: erro de Regex não confirma codificações parciais.
    Código não é convertido de manual para automático se já estiver associado.
    """
    if type(contextual_rejection_enabled) is not bool:
        raise SelectionError("Preferência de rejeição contextual inválida.")
    if type(mode) is not str or mode not in {"literal", "lexical", "semantic"} or (grep and mode != "literal"):
        raise SelectionError("Regex está disponível somente na autocodificação Literal.")
    terms, legacy_query = prepare_automatic_query(query, multiple_terms=multiple_terms, grep=grep,
                                                case_sensitive=case_sensitive)
    if progress_callback:
        progress_callback({"stage": "preparing", "percent": 0})
    searches = []
    for term_index, term in enumerate(terms):
        try:
            if mode == "literal":
                found = search_qualitative_document(analysis, document_id, term, grep=grep,
                    case_sensitive=case_sensitive, longest_regex=grep)
            else:
                engine = search_lexical if mode == "lexical" else search_semantic
                def on_search(event):
                    if not progress_callback:
                        return
                    fraction = event["completed"] / max(1, event["total"])
                    within_term = (0.15 * fraction if event["stage"] == "literal" else
                                   0.15 + 0.25 * fraction if event["stage"] == "lexical" else
                                   0.40 if event["stage"] == "model" else 0.40 + 0.60 * fraction)
                    progress_callback({**event, "percent": round(
                        5 + 75 * (term_index + within_term) / len(terms))})
                found = engine(analysis, document_id, term, case_sensitive=case_sensitive,
                               progress_callback=on_search if mode == "semantic" else None)
        except QualitativeSearchError as error:
            if multiple_terms:
                raise QualitativeSearchError(f"Consulta “{term}”: {error}") from error
            raise
        groups = {}
        for match in found["results"]:
            # Regex já usava a forma capturada. A expansão lexical passa a
            # fazer o mesmo, preservando source_query como termo-base.
            name = (
                match["match_text"].strip() if grep
                else lexical_code_name(match["match_text"])
                if mode == "lexical"
                else term
            )
            identity = normalized_qualitative_code_name(name)
            if (name and match["end_offset"] - match["start_offset"] <= MAX_SELECTION_LENGTH
                    and (len(name) > 160 or len(identity) > 160)):
                raise SelectionError("O texto encontrado excede 160 caracteres para o nome do código. "
                                     "Refine a consulta; nenhuma codificação foi salva.")
            groups.setdefault(identity, (name, []))[1].append(match)
        searches.append((term, found, groups))
    # Todas as buscas/Regex terminaram antes de criar códigos ou trechos.
    manifest = load_qualitative_manifest(analysis)
    totals = dict(created=0, existing=0, rejected=0, invalid=0, documents=0)
    details, results, modified_documents = [], [], set()
    total_matches = sum(len(matches) for _, _, groups in searches
                        for _, matches in groups.values())
    processed_matches = 0
    def on_match(match):
        nonlocal processed_matches
        processed_matches += 1
        if progress_callback:
            progress_callback({"stage": "coding", "percent": 80 + round(
                19 * processed_matches / max(1, total_matches)),
                "document_id": match["document_id"], "page_number": match["page_number"]})
    for term, found, groups in searches:
        summary = dict(created=0, existing=0, rejected=0, invalid=0, documents=0)
        term_documents, codes = set(), []
        for name, matches in groups.values():
            code = (_code_from_provenance(analysis, term, matches, code_name=name, grep=grep,
                case_sensitive=case_sensitive, mode=mode,
                legacy_query=legacy_query if multiple_terms else None)
                or find_code_by_name(analysis, name)) if name else None
            if code is not None and not code.active:
                raise SelectionError(f"O texto “{name}” corresponde a um código inativo.")
            counts, changed, code = _code_term_matches(
                analysis, term, matches, code, user_id, manifest, code_name=name,
                grep=grep, case_sensitive=case_sensitive,
                mode=mode,
                contextual_rejection_enabled=contextual_rejection_enabled,
                legacy_query=legacy_query if multiple_terms else None,
                progress_callback=on_match if progress_callback else None)
            for key in ("created", "existing", "rejected", "invalid"):
                summary[key] += counts[key]
            term_documents.update(changed)
            if code is not None:
                codes.append({"code_id": code.id, "code_name": code.name})
            for match in matches:
                match.update(term=term, code_id=code.id if code else None,
                             code_name=code.name if code else None)
        for key in ("created", "existing", "rejected", "invalid"):
            totals[key] += summary[key]
        summary["documents"] = len(term_documents)
        modified_documents.update(term_documents)
        single = codes[0] if len(codes) == 1 else {"code_id": None, "code_name": None}
        details.append({"term": term, "total": found["total"], **single, "codes": codes, **summary})
        results.extend(found["results"])  # conserva ordem documental, não agrupa a navegação por código
    totals["documents"] = len(modified_documents)
    if multiple_terms:
        totals["terms"] = len(terms)
    if progress_callback:
        progress_callback({"stage": "finalizing", "percent": 99})
    return {"summary": totals, "terms": details,
            "search": {"results": results, "total": len(results), "offset_unit": "unicode_codepoint"}}


def _code_term_matches(analysis, term, matches, code, user_id, manifest, *, code_name, grep, mode,
                       case_sensitive, contextual_rejection_enabled, legacy_query,
                       progress_callback=None):
    """Aplica somente matches deste termo a seu código; preserva proveniência legada."""
    origin = "automatic_regex" if grep else f"automatic_{mode}"
    def same_query(stored):
        if legacy_query is not None and stored == legacy_query:
            return True  # rejeições antigas da mesma operação múltipla continuam respeitadas
        return stored == term if grep or case_sensitive else stored.casefold() == term.casefold()
    rejected = db.session.scalars(select(QualitativeRejection).where(
        QualitativeRejection.analysis_id == analysis.id, QualitativeRejection.code_id == code.id,
        QualitativeRejection.origin == origin,
        QualitativeRejection.case_sensitive == case_sensitive)).all() if code else []
    rejection_keys = {(r.document_id, r.page_number, r.start_offset, r.end_offset, r.page_text_hash)
                      for r in rejected if same_query(r.query)}
    # Continua idempotente mesmo após ajuste manual dos limites do excerpt.
    prior_sources = db.session.execute(select(QualitativeCoding, QualitativeExcerpt).join(
        QualitativeExcerpt, QualitativeExcerpt.id == QualitativeCoding.excerpt_id).where(
        QualitativeCoding.analysis_id == analysis.id, QualitativeCoding.code_id == code.id,
        QualitativeCoding.origin == origin,
        QualitativeCoding.source_case_sensitive == case_sensitive)).all() if code else []
    prior_keys = {(e.document_id, e.page_number, c.source_start, c.source_end, c.source_page_hash)
                  for c, e in prior_sources if same_query(c.source_query)}
    summary = {"created": 0, "existing": 0, "rejected": 0, "invalid": 0, "documents": 0}
    modified_documents = set()
    current_key, page = None, None
    for match in matches:
        doc, number = match["document_id"], match["page_number"]
        start, end, page_hash = match["start_offset"], match["end_offset"], match["page_text_hash"]
        key = (doc, number, start, end, page_hash)
        if key in rejection_keys:
            summary["rejected"] += 1
            if progress_callback:
                progress_callback(match)
            continue
        if key in prior_keys:
            summary["existing"] += 1
            if progress_callback:
                progress_callback(match)
            continue
        if current_key != (doc, number):
            page = read_qualitative_page(analysis, doc, number, manifest=manifest)
            current_key = (doc, number)
        if page["sha256"] != page_hash:
            raise SelectionConflict("O corpus mudou durante a busca. Nenhuma codificação foi salva.")
        quote = page["text"][start:end]
        if not 0 <= start < end <= len(page["text"]) or end - start > MAX_SELECTION_LENGTH or not quote.strip():
            summary["invalid"] += 1
            if progress_callback:
                progress_callback(match)
            continue
        if code is None:
            code = QualitativeCode(analysis_id=analysis.id, name=code_name, description="",
                                   created_by_user_id=user_id, active=True)
            db.session.add(code)
            db.session.flush()
        excerpt = get_or_create_excerpt(analysis, doc, number, start, end, quote, page_hash, user_id)
        existing = db.session.scalar(select(QualitativeCoding.id).where(
            QualitativeCoding.excerpt_id == excerpt.id, QualitativeCoding.code_id == code.id))
        if existing:
            summary["existing"] += 1
            if progress_callback:
                progress_callback(match)
            continue
        db.session.add(QualitativeCoding(analysis_id=analysis.id, excerpt_id=excerpt.id, code_id=code.id,
            created_by_user_id=user_id, origin=origin, source_query=term, source_case_sensitive=case_sensitive,
            source_start=start, source_end=end, source_page_hash=page_hash,
            contextual_rejection_enabled=contextual_rejection_enabled))
        summary["created"] += 1
        modified_documents.add(doc)
        if progress_callback:
            progress_callback(match)
    summary["documents"] = len(modified_documents)
    return summary, modified_documents, code


def remove_coding(analysis: Analysis, coding: QualitativeCoding, user_id: str) -> QualitativeExcerpt:
    """Remove só o vínculo. Trecho/memos são preservados; âncora sem vínculos fica invisível."""
    if coding.analysis_id != analysis.id:
        raise SelectionError("Codificação não pertence a este projeto.")
    excerpt = db.session.get(QualitativeExcerpt, coding.excerpt_id)
    if coding.origin.startswith("automatic_") and coding.contextual_rejection_enabled:
        context = dict(analysis_id=analysis.id, document_id=excerpt.document_id, code_id=coding.code_id,
                       page_number=excerpt.page_number, start_offset=coding.source_start,
                       end_offset=coding.source_end, page_text_hash=coding.source_page_hash,
                       origin=coding.origin, query=coding.source_query, case_sensitive=coding.source_case_sensitive)
        if db.session.scalar(select(QualitativeRejection.id).filter_by(**context)) is None:
            db.session.add(QualitativeRejection(**context, created_by_user_id=user_id))
    db.session.delete(coding)
    return excerpt
