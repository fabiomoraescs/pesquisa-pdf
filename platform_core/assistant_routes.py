"""Endpoint autenticado para perguntas livres ao Assistente Análysis."""

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required

from .assistant_context import ASSISTANT_CONTEXTS
from .assistant_ai_provider import AIProviderError
from .assistant_project_context import resolve_project_context
from .assistant_service import ask_with_ai
from .assistant_suggestions import contextual_suggestions, suggestion_scope_for_id


assistant_bp = Blueprint("assistant", __name__, url_prefix="/assistant")


def _project_context_from_payload(payload: dict, context_key: str):
    """Reconstrói o escopo autorizado, sem confiar no estado do navegador."""
    try:
        candidate = resolve_project_context(
            current_user,
            payload.get("reference"),
            page_context=payload.get("page_context"),
        )
        expected_tool = {
            "term_search": "pdf_scraper",
            "term_analysis": "pdf_scraper",
            "structured_search": "document_analysis",
            "structured_analysis": "document_analysis",
            "qualitative": "qualitative_analysis",
            "qualitative_reader": "qualitative_analysis",
            "qualitative_search": "qualitative_analysis",
            "coding_report": "qualitative_analysis",
        }.get(context_key)
        if candidate is not None and (expected_tool is None or candidate["tool"]["id"] == expected_tool):
            return candidate
    except Exception:
        # Não inclui referências, contexto privado ou conteúdo documental nos logs.
        current_app.logger.exception("Falha ao resolver contexto factual do Assistente")
    return None


@assistant_bp.post("/ask")
@login_required
def ask():
    if not request.is_json:
        return jsonify({"error": "Envie a pergunta em JSON."}), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON inválido."}), 400
    question = payload.get("question")
    if not isinstance(question, str):
        return jsonify({"error": "Informe uma pergunta em texto."}), 400
    question = question.strip()
    if not question or len(question) > 1000:
        return jsonify({"error": "Informe uma pergunta de até 1000 caracteres."}), 400
    context_key = payload.get("context")
    context_key = context_key if isinstance(context_key, str) and context_key in ASSISTANT_CONTEXTS else "fallback"
    project_context = _project_context_from_payload(payload, context_key)
    suggestion_scope = suggestion_scope_for_id(
        user=current_user,
        context_key=context_key,
        project_context=project_context,
        suggestion_id=payload.get("suggestion_id"),
        question=question,
    )
    try:
        return jsonify(ask_with_ai(user=current_user, question=question, context_key=context_key,
                                   project_context=project_context, suggestion_scope=suggestion_scope))
    except AIProviderError as error:
        return jsonify({"error": error.public_message}), error.status_code


@assistant_bp.post("/suggestions")
@login_required
def suggestions():
    """Atualiza onboarding sem atrasar ou alterar o chat normal."""
    if not request.is_json:
        return jsonify({"error": "Envie a solicitação em JSON."}), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON inválido."}), 400
    context_key = payload.get("context")
    context_key = context_key if isinstance(context_key, str) and context_key in ASSISTANT_CONTEXTS else "fallback"
    outcome = contextual_suggestions(
        user=current_user,
        context_key=context_key,
        project_context=_project_context_from_payload(payload, context_key),
    )
    return jsonify({
        "questions": list(outcome.questions),
        "suggestions": [
            {"id": suggestion.id, "text": suggestion.text}
            for suggestion in outcome.suggestions if outcome.dynamic
        ],
        "dynamic": outcome.dynamic,
        "cached": outcome.cached,
    })
