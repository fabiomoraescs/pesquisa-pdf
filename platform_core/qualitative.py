"""Integridade e autorização da futura codificação qualitativa manual.

Não processa PDFs. Para Bases qualitativas, ``concluida`` significará apenas
corpus preparado; não significa conclusão interpretativa pelo pesquisador.
"""

from __future__ import annotations

from collections.abc import Iterable

from flask import abort
from sqlalchemy import delete

from .extensions import db
from .models import (
    Analysis, AnalysisDocument, Project, QualitativeCode, QualitativeCoding,
    QualitativeExcerpt, QualitativeMemo, User,
)
from .scraping_types import QUALITATIVE, QUALITATIVE_TOOL
from .services import can_use_tool, get_project_for_user


QUALITATIVE_STRATEGIES = frozenset({"deductive", "inductive", "hybrid"})


def validate_qualitative_parameters(parameters: dict) -> None:
    """A estratégia é opcional nesta fundação, mas seus valores são estáveis."""
    strategy = parameters.get("qualitative_strategy")
    if strategy is not None and strategy not in QUALITATIVE_STRATEGIES:
        raise ValueError("Estratégia qualitativa inválida.")


def validate_qualitative_project(project_id: str | None, user_id: str) -> Project:
    project = db.session.get(Project, project_id) if project_id else None
    if (project is None or project.scrape_type != QUALITATIVE
            or project.status != "active" or project.deleted_at is not None
            or project.owner_user_id != user_id):
        raise ValueError("A Base qualitativa exige um projeto qualitativo ativo pertencente ao usuário.")
    return project


def get_qualitative_analysis(analysis_id: str, user: User, *, manage: bool = False) -> Analysis:
    """Reaproveita autorização de Base/projeto e confere a ferramenta no backend."""
    from .analyses import get_analysis

    analysis = get_analysis(analysis_id, user)
    if analysis.tool_id != QUALITATIVE_TOOL or not can_use_tool(user, QUALITATIVE_TOOL):
        abort(404)
    if not analysis.project_id:
        abort(404)
    project = get_project_for_user(analysis.project_id, user, include_inactive=not manage)
    if project.scrape_type != QUALITATIVE or project.deleted_at is not None:
        abort(404)
    if manage and user.id not in {analysis.user_id, project.owner_user_id}:
        abort(403)
    return analysis


def get_qualitative_document(analysis: Analysis, document_id: str) -> AnalysisDocument:
    document = db.session.get(AnalysisDocument, document_id)
    if document is None or document.analysis_id != analysis.id:
        abort(404)
    return document


def get_qualitative_code(analysis: Analysis, code_id: str) -> QualitativeCode:
    code = db.session.get(QualitativeCode, code_id)
    if code is None or code.analysis_id != analysis.id:
        abort(404)
    return code


def get_qualitative_excerpt(analysis: Analysis, excerpt_id: str) -> QualitativeExcerpt:
    excerpt = db.session.get(QualitativeExcerpt, excerpt_id)
    if excerpt is None or excerpt.analysis_id != analysis.id:
        abort(404)
    return excerpt


def get_qualitative_memo(analysis: Analysis, memo_id: str) -> QualitativeMemo:
    memo = db.session.get(QualitativeMemo, memo_id)
    if memo is None or memo.analysis_id != analysis.id:
        abort(404)
    return memo


def delete_qualitative_dependents(analysis_ids: Iterable[str]) -> None:
    """Exclui apenas filhos próprios das Bases indicadas, na ordem das FKs.

    O chamador controla o commit/rollback juntamente com a exclusão da Base.
    """
    identifiers = tuple(dict.fromkeys(analysis_ids))
    if not identifiers:
        return
    db.session.execute(delete(QualitativeCoding).where(QualitativeCoding.analysis_id.in_(identifiers)))
    db.session.execute(delete(QualitativeMemo).where(QualitativeMemo.analysis_id.in_(identifiers)))
    db.session.execute(delete(QualitativeExcerpt).where(QualitativeExcerpt.analysis_id.in_(identifiers)))
    db.session.execute(delete(QualitativeCode).where(QualitativeCode.analysis_id.in_(identifiers)))
