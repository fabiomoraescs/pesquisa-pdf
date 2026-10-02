"""Análise determinística de perguntas para a Análysis IA.

Esta camada não é um modelo de linguagem nem uma segunda autorização. Ela
classifica intenções observáveis e escolhe uma operação local que continua
submetida à política de tools autorizadas pelo servidor.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


NativeIntentName = Literal[
    "LOCATE", "COUNT", "DEFINE", "CHARACTERIZE", "EXPLAIN", "RELATE",
    "COMPARE", "SUMMARIZE", "ARGUMENT", "EXEMPLIFY", "CONTEXTUALIZE",
    "DISTRIBUTE", "COOCCURRENCE", "PLATFORM_HELP", "UNSUPPORTED",
]
NativeScope = Literal[
    "current_excerpt", "current_page", "current_document", "corpus", "project", "platform",
]


@dataclass(frozen=True)
class NativeQuestionPlan:
    """Plano fechado para uma única consulta autorizável da Análysis IA."""

    intent: NativeIntentName
    operation: str
    tool_name: str | None
    scope: NativeScope
    query: str = ""
    corpus_only: bool = False


def _normal(value: object) -> str:
    compact = " ".join(value.split()) if isinstance(value, str) else ""
    return "".join(
        item for item in unicodedata.normalize("NFD", compact.casefold())
        if unicodedata.category(item) != "Mn"
    )


def _clean_topic(value: str) -> str:
    return " ".join(value.split()).strip(" ?!.,:;—–")[:200]


def _query(question: str) -> str:
    """Extrai o assunto explicitamente pedido, sem criar termo de busca."""
    compact = " ".join(question.split())[:400]
    quoted = re.search(r'["“]([^"”]{1,200})["”]', compact)
    if quoted:
        return _clean_topic(quoted.group(1)) or "conteúdo solicitado"
    patterns = (
        r"\b(?:como|o que|qual)\s+(.+?)\s+(?:e|é|foi|são|está|vem\s+sendo)?\s*(?:definid|compreendid|caracteriz|apresentad|explicad)",
        r"\b(?:o que|qual|como)\s+(?:o texto|o autor|os documentos?|o corpus)\s+(?:diz|afirma|argumenta|sustenta|mostra|explica)\s+(?:sobre\s+)?(.+?)(?:\?|$)",
        r"\b(?:onde aparece|localize|em quais documentos? aparece|compare|relacione|contextualize|exemplifique)\s+(.+?)(?:\s+(?:no|na)\s+corpus)?(?:\?|$)",
        r"\b(?:conceito|definicao|definição|argumento|exemplo|caracteristicas?|características?)\s+(?:de|do|da|sobre)\s+(.+?)(?:\?|$)",
        r"\bsobre\s+(.+?)(?:\s+(?:no|na)\s+(?:pagina|página|documento|corpus))?(?:\?|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, compact, flags=re.IGNORECASE)
        if match:
            topic = _clean_topic(match.group(1))
            if topic:
                return topic
    trimmed = re.sub(
        r"^(?:onde aparece|localize|resuma|explique|compare|conte\s+|quantos?\s+|qual\s+(?:é|a)\s+diferença\s+entre)\s+",
        "", compact, flags=re.IGNORECASE,
    )
    return _clean_topic(trimmed) or "conteúdo solicitado"


def _document_operation(text: str) -> tuple[NativeIntentName, str]:
    """Mantém a intenção analítica quando a policy já escolheu a fonte."""
    if re.search(r"\b(defin\w*|conceito|compreendid\w*|entende-se)\b", text):
        return "DEFINE", "EXTRACT_DEFINITIONS"
    if re.search(r"\b(caracter\w*|propriedad\w*|tracos?|traços?|aspectos?)\b", text):
        return "CHARACTERIZE", "CHARACTERIZE"
    if re.search(r"\b(argument\w*|defende|sustenta|tese|posicionamento)\b", text):
        return "ARGUMENT", "ARGUMENT"
    if re.search(r"\b(exemplo|exemplifique|por exemplo|caso)\b", text):
        return "EXEMPLIFY", "EXEMPLIFY"
    if re.search(r"\b(relacion\w*|articula\w*|vincula\w*|conecta\w*|dialoga\w*)\b", text):
        return "RELATE", "RELATE"
    if re.search(r"\b(compare\w*|diferenc\w*|contraste\w*|convergenc\w*|divergenc\w*)\b", text):
        return "COMPARE", "COMPARE"
    if re.search(r"\b(coocorr\w*|junt[oa]s?|proximidade|associad\w*)\b", text):
        return "COOCCURRENCE", "COOCCURRENCE"
    if re.search(r"\b(distribu\w*|frequenc\w*|quantos|quantas|ocorrenc\w*)\b", text):
        return "DISTRIBUTE", "DISTRIBUTE"
    if re.search(r"\b(contextualiz\w*|contexto|ao longo|demais documentos|outros textos)\b", text):
        return "CONTEXTUALIZE", "CONTEXTUALIZE"
    if re.search(r"\b(onde aparece|localize|em quais documentos|em que pagina|em qual pagina)\b", text):
        return "LOCATE", "LOCATE"
    return "EXPLAIN", "READ_CURRENT_PAGE"


def _required_plan(required: str, question: str) -> NativeQuestionPlan:
    mapping: dict[str, tuple[NativeIntentName, str, NativeScope, bool]] = {
        "get_platform_help": ("PLATFORM_HELP", "PLATFORM_HELP", "platform", False),
        "get_current_ui_context": ("CONTEXTUALIZE", "CONTEXTUALIZE", "platform", False),
        "get_project_context": ("COUNT", "PROJECT_STATUS", "project", False),
        "read_current_page": ("EXPLAIN", "READ_CURRENT_PAGE", "current_page", True),
        "read_document_pages": ("EXPLAIN", "READ_DOCUMENT_PAGES", "current_document", True),
        "search_corpus": ("LOCATE", "LOCATE", "corpus", True),
        "inspect_corpus": ("COUNT", "COUNT", "corpus", True),
        "summarize_document": ("SUMMARIZE", "SUMMARIZE", "current_document", True),
        "summarize_corpus": ("SUMMARIZE", "SUMMARIZE", "corpus", True),
    }
    intent, operation, scope, corpus_only = mapping.get(required, ("UNSUPPORTED", "UNSUPPORTED", "platform", False))
    if required in {"read_current_page", "read_document_pages", "search_corpus"}:
        intent, operation = _document_operation(_normal(question))
    return NativeQuestionPlan(intent, operation, required if intent != "UNSUPPORTED" else None,
                              scope, _query(question), corpus_only)


class QuestionAnalyzer:
    """Classificador local baseado em padrões explícitos e ordem conservadora."""

    def analyze(self, question: object, *, allowed_tool_names: tuple[str, ...] = ()) -> NativeQuestionPlan:
        raw = question if isinstance(question, str) else ""
        text = _normal(raw)
        # Uma seleção/política do servidor é sempre mais forte que a heurística.
        if allowed_tool_names:
            return _required_plan(allowed_tool_names[0], raw)

        query = _query(raw)
        if re.search(r"\b(resuma|resumo|sintese|sintetize)\b", text):
            scope: NativeScope = "corpus" if "corpus" in text else "current_document"
            return NativeQuestionPlan("SUMMARIZE", "SUMMARIZE", "summarize_corpus" if scope == "corpus" else "summarize_document",
                                      scope, query, True)
        if re.search(r"\b(coocorr\w*|junt[oa]s?|proximidade|associad\w*)\b", text):
            return NativeQuestionPlan("COOCCURRENCE", "COOCCURRENCE", "search_corpus", "corpus", query, True)
        if re.search(r"\b(quantos|quantas|numero|total)\b.*\b(documentos?|paginas?|p[aá]ginas?)\b", text):
            return NativeQuestionPlan("COUNT", "COUNT", "inspect_corpus", "corpus", query, True)
        if re.search(r"\b(distribu\w*|frequenc\w*|quantos|quantas|ocorrenc\w*)\b", text):
            return NativeQuestionPlan("DISTRIBUTE", "DISTRIBUTE", "search_corpus", "corpus", query, True)
        if re.search(r"\b(compare\w*|diferenc\w*|contraste\w*|convergenc\w*|divergenc\w*)\b", text):
            return NativeQuestionPlan("COMPARE", "COMPARE", "search_corpus", "corpus", query, True)
        if re.search(r"\b(defin\w*|conceito|compreendid\w*|entende-se)\b", text):
            return NativeQuestionPlan("DEFINE", "EXTRACT_DEFINITIONS", "search_corpus", "corpus", query, True)
        if re.search(r"\b(caracter\w*|propriedad\w*|tracos?|traços?|aspectos?)\b", text):
            return NativeQuestionPlan("CHARACTERIZE", "CHARACTERIZE", "search_corpus", "corpus", query, True)
        if re.search(r"\b(argument\w*|defende|sustenta|tese|posicionamento)\b", text):
            return NativeQuestionPlan("ARGUMENT", "ARGUMENT", "search_corpus", "corpus", query, True)
        if re.search(r"\b(exemplo|exemplifique|por exemplo|caso)\b", text):
            return NativeQuestionPlan("EXEMPLIFY", "EXEMPLIFY", "search_corpus", "corpus", query, True)
        if re.search(r"\b(relacion\w*|articula\w*|vincula\w*|conecta\w*|dialoga\w*)\b", text):
            return NativeQuestionPlan("RELATE", "RELATE", "search_corpus", "corpus", query, True)
        if re.search(r"\b(contextualiz\w*|contexto|ao longo|demais documentos|outros textos)\b", text):
            return NativeQuestionPlan("CONTEXTUALIZE", "CONTEXTUALIZE", "search_corpus", "corpus", query, True)
        if re.search(r"\b(onde aparece|localize|em quais documentos|em que pagina|em qual pagina)\b", text):
            return NativeQuestionPlan("LOCATE", "LOCATE", "search_corpus", "corpus", query, True)
        if re.search(r"\b(esta pagina|nesta pagina|este trecho|neste trecho)\b", text):
            return NativeQuestionPlan("EXPLAIN", "READ_CURRENT_PAGE", "read_current_page", "current_page", query, True)
        if re.search(r"\b(este documento|neste documento|capitulo)\b", text):
            return NativeQuestionPlan("EXPLAIN", "READ_DOCUMENT_PAGES", "read_document_pages", "current_document", query, True)
        if re.search(r"\b(projeto|base|codigo|memo)\b", text):
            return NativeQuestionPlan("COUNT", "PROJECT_STATUS", "get_project_context", "project", query)
        if re.search(r"\b(tela|pagina|leitor|onde estou|seguir)\b", text):
            return NativeQuestionPlan("CONTEXTUALIZE", "CONTEXTUALIZE", "get_current_ui_context", "platform", query)
        if re.search(r"\b(analysis|ferramenta|busca|literal|regex|lexical|semantica|plano|acesso)\b", text):
            return NativeQuestionPlan("PLATFORM_HELP", "PLATFORM_HELP", "get_platform_help", "platform", query)
        return NativeQuestionPlan("UNSUPPORTED", "UNSUPPORTED", None, "platform", query)


def native_question_plan(question: object, *, allowed_tool_names: tuple[str, ...] = ()) -> NativeQuestionPlan:
    """Compatibilidade simples para as chamadas existentes do provider."""
    return QuestionAnalyzer().analyze(question, allowed_tool_names=allowed_tool_names)


__all__ = ["NativeIntentName", "NativeQuestionPlan", "NativeScope", "QuestionAnalyzer", "native_question_plan"]
