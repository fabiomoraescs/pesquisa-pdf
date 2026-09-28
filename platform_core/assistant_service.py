"""Resposta provisória do Assistente; substituível por um provider futuro."""

from .assistant_context import ASSISTANT_CONTEXTS


def answer_question(question: str, context: str | None, *, page: object = None,
                    reference: object = None) -> dict[str, str]:
    """Responde sem IA nem acesso a dados; page/reference permanecem sem uso nesta fase."""
    context_key = context if isinstance(context, str) and context in ASSISTANT_CONTEXTS else "fallback"
    return {
        "answer": (
            f"Resposta provisória, sem IA: recebi sua pergunta no contexto '{context_key}'. "
            f"Pergunta: {question}"
        ),
        "context": context_key,
    }
