"""Núcleo linguístico compartilhado das buscas morfológica e lexical.

Flexões conservam as regras V1/Snowball. A modalidade lexical acrescenta
somente famílias formadas por sufixos derivacionais produtivos do português;
não há comparação por prefixo livre nem por distância entre palavras.
"""

from functools import lru_cache
import unicodedata

import regex
import snowballstemmer

from .search_matching import find_search_spans


WORDS = regex.compile(r"[\p{L}\p{M}]+")
_WORD_CHAR = regex.compile(r"[\p{L}\p{M}\p{N}_]")
_STEMMER = snowballstemmer.stemmer("portuguese")
# Formas flexionadas dos sufixos também são explícitas: a fronteira da palavra
# é sempre verificada pelo tokenizador, nunca por startswith no texto bruto.
_DERIVATIONAL_SUFFIXES = tuple(sorted({
    "ializacoes", "ializacao", "ializados", "ializadas", "ializado", "ializada",
    "ialmente", "ialistas", "ialista", "ialismos", "ialismo", "iais", "ial",
    "acionais", "acionalmente", "acional", "acoes", "acao",
    "izacoes", "izacao", "izados", "izadas", "izado", "izada",
    "istas", "ista", "ismos", "ismo",
}, key=len, reverse=True))


def normalize(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def lexical_code_name(found_text: str) -> str:
    """Nome estável do código para uma forma concreta encontrada no corpus.

    A forma visual original continua disponível no trecho. O código usa NFKC e
    casefold, a mesma convenção de identidade dos códigos qualitativos, para
    não criar códigos distintos apenas por caixa ou composição Unicode.
    """
    return unicodedata.normalize("NFKC", " ".join(found_text.split())).casefold()


@lru_cache(maxsize=8192)
def _inflections(word: str) -> frozenset[str]:
    from .v1 import gerar_variacoes_palavra
    return frozenset(gerar_variacoes_palavra(word))


@lru_cache(maxsize=16384)
def _stem(word: str) -> str:
    return _STEMMER.stemWord(normalize(word))


@lru_cache(maxsize=16384)
def derivational_roots(word: str) -> frozenset[str]:
    """Raízes obtidas por regras sufixais fechadas, não prefix matching.

    A alternância ortográfica ç/c antes de -a/-as é aplicável também à
    consulta sem cedilha (raça/raca/raças -> rac).
    """
    normalized = normalize(word)
    roots = set()
    for ending in ("cas", "ca"):
        if normalized.endswith(ending):
            root = normalized[:-(len(ending) - 1)]
            if len(root) >= 3:
                roots.add(root)
            break
    for suffix in _DERIVATIONAL_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 3:
            roots.add(normalized[:-len(suffix)])
            break  # o sufixo mais específico evita uma raiz curta acidental
    return frozenset(roots)


@lru_cache(maxsize=32768)
def word_relation(query: str, candidate: str) -> str | None:
    """literal, morphological, derivational ou None para palavras completas."""
    wanted, found = normalize(query), normalize(candidate)
    if wanted == found:
        return "literal"
    if found in _inflections(query.casefold()) or wanted in _inflections(candidate.casefold()):
        return "morphological"
    if derivational_roots(query) & derivational_roots(candidate):
        return "derivational"
    if _stem(query) == _stem(candidate):
        return "morphological"
    return None


def morphological_relation(query: str, candidate: str) -> str | None:
    """Modo estrito: forma literal e flexões, nunca derivações identificadas."""
    relation = word_relation(query, candidate)
    return relation if relation != "derivational" else None


def find_lexical_spans(text: str, query: str, *, case_sensitive: bool = False):
    """Encontra expressões adjacentes na ordem original, com offsets Unicode."""
    wanted = [match.group() for match in WORDS.finditer(query)]
    if not wanted or not query.strip() or any(not char.isspace() and not char.isalpha()
                                               for char in query):
        return
    words = list(WORDS.finditer(text))
    for index in range(len(words) - len(wanted) + 1):
        window = words[index:index + len(wanted)]
        if any(not text[left.end():right.start()].isspace()
               for left, right in zip(window, window[1:])):
            continue
        relations = []
        for source, target in zip(wanted, window):
            actual = target.group()
            if case_sensitive and _case_shape(source) != _case_shape(actual):
                break
            relation = word_relation(source, actual)
            if relation is None:
                break
            relations.append(relation)
        else:
            kind = "derivational" if "derivational" in relations else (
                "morphological" if "morphological" in relations else "literal")
            yield window[0].start(), window[-1].end(), kind


def find_lexical_matches(text: str, query: str, *, case_sensitive: bool = False):
    """Une Literal integral e família lexical como a busca Lexical do Quali.

    A parte Literal é filtrada para não recuperar subcadeias de outra palavra;
    a expansão seguinte permanece baseada em tokens e regras sufixais fechadas.
    """
    if not WORDS.search(query):
        for start, end in find_search_spans(text, query, case_sensitive=case_sensitive):
            yield start, end, "literal"
        return
    matches: dict[tuple[int, int], str] = {}
    for start, end in find_search_spans(text, query, case_sensitive=case_sensitive):
        if ((start and _WORD_CHAR.fullmatch(text[start - 1]))
                or (end < len(text) and _WORD_CHAR.fullmatch(text[end]))):
            continue
        matches[start, end] = "literal"
    for start, end, kind in find_lexical_spans(text, query, case_sensitive=case_sensitive):
        matches.setdefault((start, end), kind)
    for (start, end), kind in sorted(matches.items()):
        yield start, end, kind


def count_lexical_occurrences(text: str, query: str, legacy_pattern) -> int:
    """Compatibilidade das versões legadas, agora pelo motor do Quali-dados."""
    del legacy_pattern
    return sum(1 for _ in find_lexical_matches(text, query))


def _case_shape(word: str) -> str:
    if word.isupper():
        return "upper"
    if word.istitle():
        return "title"
    if word.islower():
        return "lower"
    return word
