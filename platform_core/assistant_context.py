"""Contexto mínimo e autorizado da interface do Assistente Análysis.

Este módulo escolhe somente o contexto de tela e uma referência segura para a
requisição seguinte. Dados de projeto, corpus e documentos não são enviados ao
navegador: a camada de ferramentas do Assistente os reconstrói e valida no
servidor a cada pergunta.
"""

from __future__ import annotations

from typing import Any

from .assistant_project_context import TOOL_TO_CONTEXT, resolve_project_context


ASSISTANT_GREETING = (
    "Olá. Posso ajudar com o Análysis, com este projeto ou com os documentos do seu corpus."
)

# Prompts de onboarding são UX estática da plataforma: não contêm respostas e
# não são enviados a nenhum provider até que a pessoa escolha um deles.
CONTEXTUAL_PROMPTS: dict[str, tuple[str, str, str]] = {
    "home": (
        "O que é o Análysis?",
        "Que ferramentas estão disponíveis no meu plano?",
        "Explique as funcionalidades das ferramentas disponíveis.",
    ),
    "projects": (
        "O que é um projeto no Análysis?",
        "Como crio e organizo um projeto?",
        "O que posso fazer depois de criar um projeto?",
    ),
    "project": (
        "O que posso fazer neste projeto?",
        "Como organizo documentos e análises neste projeto?",
        "Qual ferramenta do Análysis posso usar aqui?",
    ),
    "term_search": (
        "Para que serve a Busca por termos?",
        "Como faço uma busca nesta ferramenta?",
        "Que tipos de resultados esta ferramenta produz?",
    ),
    "term_analysis": (
        "O que posso fazer nesta análise?",
        "Como faço novas buscas nos documentos?",
        "Como consulto e exporto os resultados?",
    ),
    "structured_search": (
        "Para que serve a Busca estruturada?",
        "Como configuro uma análise nesta ferramenta?",
        "Que tipos de resultados posso obter aqui?",
    ),
    "structured_analysis": (
        "O que posso fazer nesta análise?",
        "Como funcionam os campos e critérios desta busca?",
        "Como utilizo os resultados produzidos?",
    ),
    "qualitative": (
        "O que posso fazer no Quali-dados?",
        "Como funcionam documentos, códigos e memos?",
        "Como começo a analisar meus documentos aqui?",
    ),
    "qualitative_reader": (
        "Como funciona este leitor?",
        "Como codifico e faço anotações neste documento?",
        "Como posso usar o Assistente para trabalhar com este documento?",
    ),
    "qualitative_search": (
        "Que tipos de busca posso fazer aqui?",
        "Qual a diferença entre Literal, Regex, Lexical e Semântica?",
        "Quando faz sentido usar cada tipo de busca?",
    ),
    "coding_report": (
        "O que este relatório apresenta?",
        "Como navego das codificações para os documentos?",
        "Como posso utilizar este relatório na análise?",
    ),
    "libraries": (
        "O que é uma biblioteca no Análysis?",
        "Como adiciono e organizo documentos em uma biblioteca?",
        "Como as bibliotecas são utilizadas nos projetos?",
    ),
    "admin_ai_settings": (
        "O que é um provider de IA?",
        "Qual a diferença entre Provider único e fallback?",
        "Como esta configuração afeta o Assistente Análysis?",
    ),
    "admin": (
        "O que esta área administra?",
        "Como utilizo as configurações disponíveis aqui?",
        "Qual efeito essas configurações têm na plataforma?",
    ),
    "fallback": (
        "O que posso fazer nesta página?",
        "Como utilizo os recursos disponíveis aqui?",
        "Para onde posso seguir a partir desta página?",
    ),
}

# A interface envia apenas o texto do botão. Estes hints ficam no servidor e
# permitem que a política reconstrua a fonte obrigatória sem confiar no browser.
CONTEXTUAL_PROMPT_TOOL_HINTS: dict[str, tuple[str, str, str]] = {
    "home": ("get_platform_help", "get_platform_help", "get_platform_help"),
    "projects": ("get_platform_help", "get_platform_help", "get_platform_help"),
    "project": ("get_project_context", "get_project_context", "get_project_context"),
    "term_search": ("get_platform_help", "get_platform_help", "get_platform_help"),
    "term_analysis": ("get_project_context", "get_project_context", "get_project_context"),
    "structured_search": ("get_platform_help", "get_platform_help", "get_platform_help"),
    "structured_analysis": ("get_project_context", "get_project_context", "get_project_context"),
    "qualitative": ("get_platform_help", "get_platform_help", "get_platform_help"),
    "qualitative_reader": ("get_current_ui_context", "get_current_ui_context", "get_platform_help"),
    "qualitative_search": ("get_platform_help", "get_platform_help", "get_platform_help"),
    "coding_report": ("get_current_ui_context", "get_current_ui_context", "get_project_context"),
    "libraries": ("get_platform_help", "get_platform_help", "get_platform_help"),
    "admin_ai_settings": ("get_current_ui_context", "get_current_ui_context", "get_current_ui_context"),
    "admin": ("get_current_ui_context", "get_current_ui_context", "get_current_ui_context"),
    "fallback": ("get_current_ui_context", "get_current_ui_context", "get_current_ui_context"),
}

# Escopos fechados servem para prompts conhecidos e para as sugestões
# documentais. Eles nunca são escolhidos pelo navegador nem pelo provider.
SUGGESTION_SCOPES = frozenset({
    "platform", "current_ui", "project", "current_page", "current_document", "corpus", "report",
})
CONTEXTUAL_PROMPT_SCOPES: dict[str, tuple[str, str, str]] = {
    "home": ("platform", "platform", "platform"),
    "projects": ("platform", "platform", "platform"),
    "project": ("project", "project", "project"),
    "term_search": ("platform", "platform", "platform"),
    "term_analysis": ("project", "project", "project"),
    "structured_search": ("platform", "platform", "platform"),
    "structured_analysis": ("project", "project", "project"),
    "qualitative": ("platform", "platform", "platform"),
    "qualitative_reader": ("current_ui", "current_ui", "platform"),
    "qualitative_search": ("platform", "platform", "platform"),
    "coding_report": ("report", "report", "project"),
    "libraries": ("platform", "platform", "platform"),
    "admin_ai_settings": ("current_ui", "current_ui", "current_ui"),
    "admin": ("current_ui", "current_ui", "current_ui"),
    "fallback": ("current_ui", "current_ui", "current_ui"),
}

# O mapa preserva a decisão segura por endpoint. Respostas continuam sendo
# produzidas somente pelo provider de IA depois de uma pergunta ser enviada.
ASSISTANT_CONTEXTS: dict[str, dict[str, str]] = {
    key: {"intro": ASSISTANT_GREETING}
    for key in CONTEXTUAL_PROMPTS
}


def assistant_contextual_prompts(context_key: object) -> tuple[str, str, str]:
    """Devolve somente os três prompts estáticos do contexto já validado."""
    key = context_key if isinstance(context_key, str) and context_key in ASSISTANT_CONTEXTS else "fallback"
    return CONTEXTUAL_PROMPTS[key]


def assistant_contextual_prompt_scope(context_key: object, question: object) -> str | None:
    """Recupera o escopo de um onboarding estático pelo texto conhecido."""
    key = context_key if isinstance(context_key, str) and context_key in ASSISTANT_CONTEXTS else "fallback"
    if not isinstance(question, str):
        return None
    for prompt, scope in zip(CONTEXTUAL_PROMPTS[key], CONTEXTUAL_PROMPT_SCOPES[key]):
        if question.strip() == prompt:
            return scope
    return None


def assistant_context_for_endpoint(endpoint: str | None, view_args: dict | None = None) -> dict[str, Any]:
    """Escolhe o contexto pelo endpoint Flask, nunca por parsing da URL no browser."""
    endpoint = endpoint or ""
    if endpoint == "home":
        key = "home"
    elif endpoint == "admin.assistant_ai_settings":
        key = "admin_ai_settings"
    elif endpoint.startswith("admin."):
        key = "admin"
    elif endpoint.startswith("user_libraries."):
        key = "libraries"
    elif endpoint == "qualitative.coding_report":
        key = "coding_report"
    elif endpoint == "qualitative.page":
        key = "qualitative_reader"
    elif endpoint == "qualitative.search":
        key = "qualitative_search"
    elif endpoint == "qualitative.project_workspace":
        key = "project"
    elif endpoint.startswith("qualitative."):
        key = "qualitative"
    elif endpoint in {"resultado", "progresso"} or endpoint.startswith("analyses."):
        key = "term_analysis"
    elif endpoint in {"inicio", "legacy_free_submit"}:
        key = "term_search"
    elif endpoint in {"historico_racial.entrada", "historico_racial.inicio"}:
        key = "structured_search"
    elif endpoint.startswith("historico_racial."):
        key = "structured_analysis"
    elif endpoint == "projects.free_project":
        key = "project"
    elif endpoint.startswith("projects."):
        key = "projects"
    else:
        key = "fallback"

    args = view_args or {}
    return {
        "key": key,
        "intro": ASSISTANT_CONTEXTS[key]["intro"],
        "onboarding_prompts": list(assistant_contextual_prompts(key)),
        "page": endpoint,
        "tool": {
            "term_search": "pdf_scraper",
            "term_analysis": "pdf_scraper",
            "structured_search": "document_analysis",
            "structured_analysis": "document_analysis",
            "qualitative": "qualitative_analysis",
            "qualitative_reader": "qualitative_analysis",
            "qualitative_search": "qualitative_analysis",
            "coding_report": "qualitative_analysis",
        }.get(key),
        "reference": {name: str(args[name]) for name in ("project_id", "analysis_id") if args.get(name)},
    }


def assistant_context_for_request(
    endpoint: str | None,
    view_args: dict | None,
    user: object,
    *,
    query_project_id: object = None,
) -> dict[str, Any]:
    """Inclui apenas uma referência segura do projeto para a interface.

    A rota do chat volta a resolver o contexto completo. Assim, um payload
    antigo, uma troca de Base ou a troca de projeto não autorizam dados.
    """
    base = assistant_context_for_endpoint(endpoint, view_args)
    # Todas as telas autenticadas usam o mesmo ciclo: cache server-side de
    # sugestões fundamentadas quando houver contexto suficiente, ou os três
    # prompts estáticos como fallback. Nenhum dado adicional vai ao browser.
    base["dynamic_suggestions"] = bool(getattr(user, "is_authenticated", False))
    reference = dict(base["reference"])
    if "project_id" not in reference and query_project_id:
        reference["project_id"] = str(query_project_id)
    project_context = resolve_project_context(user, reference)
    if project_context is None:
        return base
    actual_tool = project_context["tool"]["id"]
    if base["tool"] and base["tool"] != actual_tool:
        return base
    if base["key"] == "fallback":
        base["key"] = TOOL_TO_CONTEXT.get(actual_tool, base["key"])
        base["tool"] = actual_tool
        base["onboarding_prompts"] = list(assistant_contextual_prompts(base["key"]))
    selected = project_context["analysis"].get("selected")
    base["reference"] = {
        "project_id": project_context["project"]["id"],
        **({"analysis_id": selected["id"]} if selected else {}),
    }
    base["project"] = {
        "id": project_context["project"]["id"],
        "name": project_context["project"]["name"],
    }
    base["context_indicator"] = (
        f"Contexto: {project_context['project']['name']} · {project_context['tool']['label']}"
    )
    return base


__all__ = [
    "ASSISTANT_CONTEXTS",
    "ASSISTANT_GREETING",
    "CONTEXTUAL_PROMPT_SCOPES",
    "CONTEXTUAL_PROMPT_TOOL_HINTS",
    "CONTEXTUAL_PROMPTS",
    "SUGGESTION_SCOPES",
    "assistant_contextual_prompt_scope",
    "assistant_contextual_prompts",
    "assistant_context_for_endpoint",
    "assistant_context_for_request",
]
