"""Endpoint autenticado para perguntas livres ao Assistente Análysis."""

from flask import Blueprint, jsonify, request
from flask_login import login_required

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
    return jsonify(answer_question(question, payload.get("context"),
                                   page=payload.get("page"), reference=payload.get("reference")))
