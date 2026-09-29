"""Endpoint autenticado para perguntas livres ao Assistente Análysis."""

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required

from .assistant_context import ASSISTANT_CONTEXTS
from .assistant_project_context import resolve_project_context
from .assistant_service import answer_question


assistant_bp = Blueprint("assistant", __name__, url_prefix="/assistant")


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
    project_context = None
    try:
        candidate = resolve_project_context(
            current_user,
            payload.get("reference"),
            page_context=payload.get("page_context"),
        )
        expected_tool = {
            "term_search": "pdf_scraper",
            "structured_search": "document_analysis",
            "qualitative": "qualitative_analysis",
            "coding_report": "qualitative_analysis",
        }.get(context_key)
        if candidate is not None and (expected_tool is None or candidate["tool"]["id"] == expected_tool):
            project_context = candidate
    except Exception:
        # Não inclui perguntas, referências ou contexto privado nos logs.
        current_app.logger.exception("Falha ao resolver contexto factual do Assistente")
    return jsonify(answer_question(question, context_key,
                                   page=payload.get("page"), reference=payload.get("reference"),
                                   project_context=project_context))
