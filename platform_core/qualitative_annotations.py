"""Âncoras e vínculos manuais sobre o corpus canônico já persistido.

Não lê PDFs. O ID do trecho é independente dos offsets para permitir ajuste
futuro da seleção sem perder códigos ou memos associados.
"""

from __future__ import annotations

from sqlalchemy import select

from .extensions import db
from .models import (
    Analysis, QualitativeCode, QualitativeCoding, QualitativeExcerpt,
    QualitativeMemo, normalized_qualitative_code_name,
)
from .qualitative_corpus import read_qualitative_page


MAX_SELECTION_LENGTH = 10_000


class SelectionError(ValueError):
    """Seleção que não corresponde a uma passagem canônica válida."""


class SelectionConflict(SelectionError):
    """O corpus mudou desde que a seleção foi capturada."""


def validated_selection(analysis: Analysis, document_id: str, page_number: int,
                        payload: dict) -> tuple[dict, int, int, str]:
    """Confere hash, offsets Python/codepoint e snapshot textual no servidor."""
    page = read_qualitative_page(analysis, document_id, page_number)
    start, end = payload.get("start"), payload.get("end")
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(page["text"])
            or end - start > MAX_SELECTION_LENGTH):
        raise SelectionError("Selecione um trecho válido de até 10.000 caracteres, em uma página.")
    if payload.get("page_hash") != page["sha256"]:
        raise SelectionConflict("O texto desta página mudou. Reabra o documento antes de codificar.")
    quote = page["text"][start:end]
    if not quote.strip() or payload.get("selected_text") != quote:
        raise SelectionError("A seleção não corresponde ao texto preparado desta página.")
    return page, start, end, quote


def get_or_create_excerpt(analysis: Analysis, document_id: str, page_number: int,
                          start: int, end: int, quote: str, page_hash: str,
                          user_id: str) -> QualitativeExcerpt:
    excerpt = db.session.scalar(select(QualitativeExcerpt).where(
        QualitativeExcerpt.analysis_id == analysis.id,
        QualitativeExcerpt.document_id == document_id,
        QualitativeExcerpt.page_number == page_number,
        QualitativeExcerpt.start_offset == start,
        QualitativeExcerpt.end_offset == end,
        QualitativeExcerpt.page_text_hash == page_hash,
    ).order_by(QualitativeExcerpt.created_at, QualitativeExcerpt.id))
    if excerpt is not None:
        if excerpt.quoted_text != quote:
            raise SelectionConflict("O trecho salvo não corresponde ao corpus atual.")
        return excerpt
    excerpt = QualitativeExcerpt(
        analysis_id=analysis.id, document_id=document_id, page_number=page_number,
        start_offset=start, end_offset=end, quoted_text=quote,
        page_text_hash=page_hash, created_by_user_id=user_id,
    )
    db.session.add(excerpt)
    db.session.flush()
    return excerpt


def apply_codes(analysis: Analysis, excerpt: QualitativeExcerpt,
                codes: list[QualitativeCode], user_id: str) -> None:
    existing = set(db.session.scalars(select(QualitativeCoding.code_id).where(
        QualitativeCoding.analysis_id == analysis.id,
        QualitativeCoding.excerpt_id == excerpt.id,
    )).all())
    for code in codes:
        if code.analysis_id != analysis.id or not code.active:
            raise SelectionError("Código indisponível neste projeto.")
        if code.id not in existing:
            db.session.add(QualitativeCoding(
                analysis_id=analysis.id, excerpt_id=excerpt.id, code_id=code.id,
                created_by_user_id=user_id, origin="manual",
            ))
            existing.add(code.id)


def find_code_by_name(analysis: Analysis, name: str) -> QualitativeCode | None:
    return db.session.scalar(select(QualitativeCode).where(
        QualitativeCode.analysis_id == analysis.id,
        QualitativeCode.normalized_name == normalized_qualitative_code_name(name),
    ))


def update_excerpt_bounds(analysis: Analysis, excerpt: QualitativeExcerpt,
                          start: int, end: int, page_hash: str) -> QualitativeExcerpt:
    """Move a mesma unidade analítica; nunca copia códigos nem memos."""
    page = read_qualitative_page(analysis, excerpt.document_id, excerpt.page_number)
    if page_hash != excerpt.page_text_hash or page_hash != page["sha256"]:
        raise SelectionConflict("O texto desta página mudou. Reabra o documento antes de ajustar o trecho.")
    text = page["text"]
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or end - start > MAX_SELECTION_LENGTH or not text[start:end].strip()):
        raise SelectionError("Selecione um trecho válido de até 10.000 caracteres, em uma página.")
    duplicate = db.session.scalar(select(QualitativeExcerpt.id).where(
        QualitativeExcerpt.analysis_id == analysis.id,
        QualitativeExcerpt.document_id == excerpt.document_id,
        QualitativeExcerpt.page_number == excerpt.page_number,
        QualitativeExcerpt.page_text_hash == page_hash,
        QualitativeExcerpt.start_offset == start,
        QualitativeExcerpt.end_offset == end,
        QualitativeExcerpt.id != excerpt.id,
    ))
    if duplicate is not None:
        raise SelectionConflict("Já existe uma marcação com estes limites.")
    excerpt.start_offset = start
    excerpt.end_offset = end
    excerpt.quoted_text = text[start:end]
    return excerpt


def page_excerpts(analysis: Analysis, document_id: str, page_number: int) -> dict:
    """Carrega só uma página; nunca reabre o PDF nem percorre o corpus."""
    page = read_qualitative_page(analysis, document_id, page_number)
    excerpts = db.session.scalars(select(QualitativeExcerpt).where(
        QualitativeExcerpt.analysis_id == analysis.id,
        QualitativeExcerpt.document_id == document_id,
        QualitativeExcerpt.page_number == page_number,
    ).order_by(QualitativeExcerpt.start_offset, QualitativeExcerpt.id)).all()
    valid = [item for item in excerpts if item.page_text_hash == page["sha256"]
             and 0 <= item.start_offset < item.end_offset <= len(page["text"])
             and page["text"][item.start_offset:item.end_offset] == item.quoted_text]
    ids = [item.id for item in valid]
    coding_map = {identifier: [] for identifier in ids}
    code_map = {identifier: [] for identifier in ids}
    memo_map = {identifier: 0 for identifier in ids}
    if ids:
        for coding, code in db.session.execute(select(QualitativeCoding, QualitativeCode).join(
                QualitativeCode, QualitativeCode.id == QualitativeCoding.code_id).where(
                QualitativeCoding.analysis_id == analysis.id,
                QualitativeCoding.excerpt_id.in_(ids)).order_by(QualitativeCode.name)).all():
            coding_map[coding.excerpt_id].append(coding.code_id)
            code_map[coding.excerpt_id].append({"id": code.id, "name": code.name})
        for memo in db.session.scalars(select(QualitativeMemo).where(
                QualitativeMemo.analysis_id == analysis.id,
                QualitativeMemo.excerpt_id.in_(ids))).all():
            memo_map[memo.excerpt_id] += 1
    return {"page_text_hash": page["sha256"], "excerpts": [
        {"id": item.id, "document_id": item.document_id,
         "page_number": item.page_number, "start": item.start_offset,
         "end": item.end_offset, "selected_text": item.quoted_text,
         "page_hash": item.page_text_hash, "code_ids": coding_map[item.id],
         "codes": code_map[item.id],
         "memo_count": memo_map[item.id]}
        for item in valid
    ]}
