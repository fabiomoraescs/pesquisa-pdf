"""Matching textual puro compartilhado pelas ferramentas de busca.

Este módulo concentra a política já usada pelo leitor Quali-dados para
Literal e Regex.  Ele não conhece PDF, banco ou estruturas de resultado;
cada ferramenta mantém os próprios contextos e exportações.
"""

from __future__ import annotations

import regex


MAX_QUERY_LENGTH = 200
SEARCH_TIMEOUT_SECONDS = 2.0


class SearchPatternError(ValueError):
    """Consulta textual inválida ou custosa demais para avaliação segura."""


def compile_search_pattern(
    query: str,
    *,
    use_regex: bool = False,
    case_sensitive: bool = False,
    longest_regex: bool = False,
):
    """Compila Literal/Regex com a mesma semântica do Quali-dados.

    Literal é uma correspondência textual escapada, insensível a caixa por
    padrão e sem dobramento de acentos. Regex recebe limite por página no
    iterador abaixo, para impedir backtracking excessivo.
    """
    query = str(query or "").strip()
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise SearchPatternError(f"Informe uma busca de até {MAX_QUERY_LENGTH} caracteres.")
    flags = regex.VERSION1 | (0 if case_sensitive else regex.IGNORECASE | regex.FULLCASE)
    if use_regex and longest_regex:
        flags |= regex.POSIX
    try:
        return regex.compile(query if use_regex else regex.escape(query), flags)
    except regex.error as error:
        raise SearchPatternError("Expressão Regex inválida.") from error


def find_search_spans(
    text: str,
    query: str,
    *,
    use_regex: bool = False,
    case_sensitive: bool = False,
    longest_regex: bool = False,
):
    """Retorna spans não vazios com a proteção Regex do Quali-dados."""
    pattern = compile_search_pattern(
        query,
        use_regex=use_regex,
        case_sensitive=case_sensitive,
        longest_regex=longest_regex,
    )
    try:
        matches = pattern.finditer(text, timeout=SEARCH_TIMEOUT_SECONDS) if use_regex else pattern.finditer(text)
        return [(match.start(), match.end()) for match in matches if match.end() > match.start()]
    except TimeoutError as error:
        raise SearchPatternError(
            "A busca Regex não foi concluída: a avaliação de uma página excedeu o tempo de segurança. "
            "Nenhum resultado parcial foi aplicado. Revise a expressão."
        ) from error
