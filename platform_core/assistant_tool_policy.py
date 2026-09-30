"""Política local que obriga fontes autorizadas quando a pergunta depende delas."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from .assistant_context import CONTEXTUAL_PROMPTS, CONTEXTUAL_PROMPT_TOOL_HINTS, SUGGESTION_SCOPES


TOOL_MODE_AUTO = "auto"
TOOL_MODE_REQUIRED = "required"


@dataclass(frozen=True)
class ToolPolicy:
    """Escolha server-side para a primeira rodada de um turno do Assistente."""

    category: str = "general"
    tool_mode: str = TOOL_MODE_AUTO
    allowed_tool_names: tuple[str, ...] = ()
    corpus_only: bool = False

    @property
    def requires_tool(self) -> bool:
        return self.tool_mode == TOOL_MODE_REQUIRED and bool(self.allowed_tool_names)


AUTO_POLICY = ToolPolicy()


def _normalized(value: object) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return "".join(
        character for character in unicodedata.normalize("NFD", text.casefold())
        if unicodedata.category(character) != "Mn"
    )


def _required(category: str, *names: str, corpus_only: bool = False) -> ToolPolicy:
    return ToolPolicy(category=category, tool_mode=TOOL_MODE_REQUIRED,
                      allowed_tool_names=tuple(names), corpus_only=corpus_only)


def _onboarding_policy(question: str, context_key: str) -> ToolPolicy | None:
    prompts = CONTEXTUAL_PROMPTS.get(context_key, ())
    hints = CONTEXTUAL_PROMPT_TOOL_HINTS.get(context_key, ())
    for prompt, tool_name in zip(prompts, hints):
        if _normalized(prompt) == question:
            category = "platform_help" if tool_name == "get_platform_help" else "current_ui"
            if tool_name == "get_project_context":
                category = "project"
            return _required(category, tool_name)
    return None


def _has_current_page(project_context: Mapping[str, object] | None) -> bool:
    if not isinstance(project_context, Mapping):
        return False
    page = project_context.get("page")
    return isinstance(page, Mapping) and bool(page.get("current_page")) and isinstance(page.get("document"), Mapping)


def tool_policy_for_suggestion_scope(
    scope: object,
    *,
    project_context: Mapping[str, object] | None,
) -> ToolPolicy | None:
    """Converte somente scopes server-side em uma fonte obrigatória fechada."""
    if not isinstance(scope, str) or scope not in SUGGESTION_SCOPES:
        return None
    if scope == "platform":
        return _required("platform_help", "get_platform_help")
    if scope == "current_ui":
        return _required("current_ui", "get_current_ui_context")
    if scope == "project":
        return _required("project", "get_project_context")
    if scope == "report":
        return _required("report", "get_project_context")
    if scope == "current_page" and _has_current_page(project_context):
        return _required("current_page", "read_current_page", corpus_only=True)
    if scope == "current_document" and _has_current_page(project_context):
        return _required("current_document", "read_document_pages", corpus_only=True)
    if scope == "corpus":
        return _required("corpus", "search_corpus", corpus_only=True)
    return None


def tool_policy_for_question(
    question: object,
    *,
    context_key: object,
    project_context: Mapping[str, object] | None,
) -> ToolPolicy:
    """Classifica por padrões determinísticos, nunca por uma chamada de IA.

    A ordem é deliberada: perguntas documentais e de corpus prevalecem sobre
    menções genéricas à tela, impedindo uma resposta externa antes da leitura.
    """
    text = _normalized(question)
    key = context_key if isinstance(context_key, str) and context_key in CONTEXTUAL_PROMPTS else "fallback"
    onboarding = _onboarding_policy(text, key)
    if onboarding is not None:
        return onboarding

    is_summary = bool(re.search(r"\b(resuma|resumo|sintese|sintetize)\b", text))
    is_corpus = bool(re.search(
        r"\b(corpus|onde aparece|compare(?:\s+os)?\s+documentos|os documentos dizem|"
        r"restante do documento|demais documentos|outros documentos)\b", text,
    ))
    has_page = _has_current_page(project_context)
    is_current_page_content = bool(re.search(
        r"\b(explique|o que aparece|o autor afirma|o autor diz|trecho|passagem|pagina atual|nesta pagina|esta pagina)\b", text,
    ))
    is_document_content = bool(re.search(r"\b(este documento|neste documento|capitulo\s*\d+|capitulo)\b", text))

    if is_summary:
        if "corpus" in text:
            return _required("summary", "summarize_corpus", corpus_only=True)
        if "pagina" in text and has_page:
            return _required("current_page", "read_current_page", corpus_only=True)
        if is_document_content or has_page:
            return _required("summary", "summarize_document", corpus_only=True)
    if is_corpus:
        return _required("corpus", "search_corpus", corpus_only=True)
    if has_page and (is_current_page_content or is_document_content):
        return _required("current_page", "read_current_page", corpus_only=True)

    is_project = bool(re.search(r"\b(neste projeto|meu projeto|nesta base|minha base|meus documentos|organiz(?:o|ar|acao))\b", text))
    if is_project:
        return _required("project", "get_project_context")

    is_current_ui = bool(re.search(
        r"\b(esta tela|nesta tela|esta pagina|nesta pagina|para onde posso seguir|daqui|onde estou|este leitor)\b", text,
    ))
    if is_current_ui:
        return _required("current_ui", "get_current_ui_context")

    is_platform_help = bool(re.search(
        r"\b(analysis|ferramentas?|plano|permissoes?|acessos?|busca por termos|busca estruturada|"
        r"literal|regex|lexical|semantica|como funciona esta ferramenta)\b", text,
    ))
    if is_platform_help:
        return _required("platform_help", "get_platform_help")
    return AUTO_POLICY


__all__ = [
    "AUTO_POLICY", "TOOL_MODE_AUTO", "TOOL_MODE_REQUIRED", "ToolPolicy", "tool_policy_for_question",
    "tool_policy_for_suggestion_scope",
]
