"""Resposta local e segura do Assistente, preparada para um provider futuro."""

from __future__ import annotations

from collections.abc import Mapping

from .assistant_context import ASSISTANT_CONTEXTS
from .assistant_project_context import methodology_instruction


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _qualitative_observation(context: Mapping) -> str:
    corpus = _mapping(context.get("corpus"))
    analysis = _mapping(context.get("analysis"))
    selected = _mapping(analysis.get("selected"))
    if not selected:
        return (
            f"Dado observado — há {corpus.get('analysis_document_records_total', 0)} registro(s) de documento "
            "distribuído(s) entre as análises registradas; nenhuma Base está selecionada nesta página."
        )
    operations = _mapping(context.get("operations"))
    codes = _mapping(operations.get("codes"))
    codings = _mapping(operations.get("codings"))
    memos = _mapping(operations.get("memos"))
    origins = _mapping(codings.get("origins"))
    origin_text = ", ".join(f"{name}: {count}" for name, count in sorted(origins.items()))
    semantic = (
        " Há registro de automatic_semantic nesta Base."
        if "automatic_semantic" in origins
        else " Não há registro de automatic_semantic nesta Base."
    )
    return (
        f"Dado observado — a Base selecionada tem {corpus.get('selected_analysis_document_count', 0)} documento(s), "
        f"{codes.get('total', 0)} código(s), {codings.get('total', 0)} codificação(ões) "
        f"e {memos.get('total', 0)} memo(s). Origens registradas: {origin_text or 'nenhuma'}.{semantic}"
    )


def _project_observation(context: Mapping) -> str:
    corpus = _mapping(context.get("corpus"))
    analysis = _mapping(context.get("analysis"))
    analyses = _mapping(analysis.get("project_analyses"))
    return (
        f"Dado observado — o projeto possui {corpus.get('analysis_document_records_total', 0)} registro(s) de documento "
        f"em {analyses.get('total', 0)} base(s) de análise; registros podem se repetir entre Bases."
    )


def _methodology_reply(context: Mapping) -> str:
    project = _mapping(context.get("project"))
    tool = _mapping(context.get("tool"))
    observation = (_qualitative_observation(context)
                   if tool.get("id") == "qualitative_analysis" else _project_observation(context))
    return (
        f"{observation} Proposta de redação baseada apenas nesses registros: “No projeto {project.get('name', '')}, "
        f"foram registrados os procedimentos técnicos descritos acima.” Recomendação — complete o texto com critérios "
        "de seleção, decisões interpretativas e justificativas metodológicas que a plataforma não consegue comprovar sozinha."
    )


def _read_only_reply(question: str, context: Mapping) -> str:
    normalized = question.casefold()
    if any(word in normalized for word in ("crie ", "criar ", "exclua", "excluir", "renomeie", "renomear", "execute", "executar", "altere", "alterar")):
        return (
            "O Assistente não tem permissão para executar ações no projeto. Posso orientar como usar os controles "
            "da plataforma, mas não criar, excluir, renomear nem modificar registros."
        )
    if any(word in normalized for word in ("metodolog", "procedimento", "o que fiz", "realizado")):
        return _methodology_reply(context)
    if any(word in normalized for word in ("trecho", "conteúdo", "conteudo", "pdf inteiro")):
        return (
            "O contexto desta fase não inclui o texto integral dos PDFs nem trechos completos. "
            "Posso usar somente os metadados e agregados registrados no projeto."
        )
    tool = _mapping(context.get("tool"))
    observation = (_qualitative_observation(context)
                   if tool.get("id") == "qualitative_analysis" else _project_observation(context))
    return (
        f"{observation} Sua pergunta foi recebida como orientação de leitura: “{question}”. "
        "Recomendação — interprete frequências e metadados no contexto da sua pergunta de pesquisa; eles não provam importância teórica por si só."
    )


def answer_question(question: str, context: str | None, *, page: object = None,
                    reference: object = None, project_context: Mapping | None = None) -> dict[str, str]:
    """Responde localmente; não há provider/modelo de IA configurado nesta fase.

    ``page`` e ``reference`` são mantidos por compatibilidade com a Fase 2, mas
    jamais autorizam dados. A rota entrega apenas ``project_context`` já validado.
    """
    context_key = context if isinstance(context, str) and context in ASSISTANT_CONTEXTS else "fallback"
    if project_context:
        return {
            "answer": _read_only_reply(question, project_context),
            "context": context_key,
            "context_source": "project_records",
        }
    return {
        "answer": (
            f"Resposta provisória, sem IA: recebi sua pergunta no contexto '{context_key}'. "
            f"Pergunta: {question} O Assistente não executa ações no projeto."
        ),
        "context": context_key,
        "context_source": "functional",
    }


__all__ = ["answer_question", "methodology_instruction"]
