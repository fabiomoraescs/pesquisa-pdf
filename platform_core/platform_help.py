"""Tutoriais oficiais da interface, carregados de um registro versionado.

O conteúdo é propositalmente externo às rotas e aos serviços: a mesma fonte
alimenta o diálogo de ajuda e uma recuperação curta, factual e limitada para o
Assistente. Não contém dados de projeto, permissões ou corpus.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


REGISTRY_PATH = Path(__file__).resolve().parents[1] / "resources" / "platform_tutorials.json"
FALLBACK_KEY = "fallback"
MAX_ASSISTANT_SECTIONS = 4
MAX_ASSISTANT_SECTION_CHARS = 1_800
_RETRIEVAL_STOP_WORDS = frozenset({
    "aqui", "como", "com", "das", "dos", "ela", "ele", "entre", "esta", "este",
    "foi", "mais", "menos", "nao", "onde", "para", "pode", "podem", "por", "qual",
    "quais", "que", "sem", "ser", "sua", "seu", "sobre", "uma", "umas", "uns",
})


class HelpRegistryError(ValueError):
    """O registro de tutoriais não tem a forma segura esperada."""


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _searchable(value: object) -> str:
    folded = unicodedata.normalize("NFKD", _text(value).casefold())
    return "".join(char for char in folded if not unicodedata.combining(char))


def _words(value: object) -> set[str]:
    words = {
        word for word in re.findall(r"[a-z0-9]{3,}", _searchable(value))
        if word not in _RETRIEVAL_STOP_WORDS
    }
    # A recuperação continua deliberadamente simples e local, mas a forma
    # singular evita perder capítulos por uma diferença editorial comum entre
    # a pergunta e o manual, como ocorrência/ocorrências ou entidade/entidades.
    for word in tuple(words):
        if len(word) > 4 and word.endswith("s"):
            words.add(word[:-1])
    return words


def _validate_tutorial(item: object) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise HelpRegistryError("Cada tutorial deve ser um objeto.")
    key, title, summary, sections = (item.get(name) for name in ("key", "title", "summary", "sections"))
    if not all(isinstance(value, str) and value.strip() for value in (key, title, summary)):
        raise HelpRegistryError("Tutorial sem key, title ou summary textual.")
    if not isinstance(sections, list) or len(sections) < 3:
        raise HelpRegistryError(f"Tutorial {key!r} precisa de pelo menos três seções.")
    section_ids: set[str] = set()
    for section in sections:
        if not isinstance(section, dict):
            raise HelpRegistryError(f"Tutorial {key!r} contém seção inválida.")
        section_id, section_title, body = (section.get(name) for name in ("id", "title", "body"))
        if not isinstance(section_id, str) or not re.fullmatch(r"[a-z0-9-]+", section_id):
            raise HelpRegistryError(f"Tutorial {key!r} contém id de seção inválido.")
        if section_id in section_ids:
            raise HelpRegistryError(f"Tutorial {key!r} repete a seção {section_id!r}.")
        section_ids.add(section_id)
        if not isinstance(section_title, str) or not section_title.strip() or not isinstance(body, list) or not body:
            raise HelpRegistryError(f"Seção {section_id!r} de {key!r} está incompleta.")
        if not all(isinstance(paragraph, str) and paragraph.strip() for paragraph in body):
            raise HelpRegistryError(f"Seção {section_id!r} de {key!r} contém texto inválido.")
        items = section.get("items", [])
        if not isinstance(items, list):
            raise HelpRegistryError(f"Seção {section_id!r} de {key!r} possui itens inválidos.")
        for entry in items:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("description"), str):
                raise HelpRegistryError(f"Seção {section_id!r} de {key!r} possui item incompleto.")
        keywords = section.get("keywords", [])
        if (not isinstance(keywords, list)
                or not all(isinstance(keyword, str) and keyword.strip() for keyword in keywords)):
            raise HelpRegistryError(f"Seção {section_id!r} de {key!r} possui palavras-chave inválidas.")
    return item


@lru_cache(maxsize=1)
def load_help_registry() -> Mapping[str, Any]:
    """Lê, valida e indexa o JSON distribuído com a plataforma."""
    try:
        payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HelpRegistryError("Não foi possível carregar os tutoriais oficiais.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        raise HelpRegistryError("Registro de tutoriais sem versão.")
    tutorials = payload.get("tutorials")
    if not isinstance(tutorials, list):
        raise HelpRegistryError("Registro de tutoriais sem lista.")
    indexed: dict[str, dict[str, Any]] = {}
    for raw in tutorials:
        tutorial = _validate_tutorial(raw)
        if tutorial["key"] in indexed:
            raise HelpRegistryError(f"Tutorial duplicado: {tutorial['key']!r}.")
        indexed[tutorial["key"]] = tutorial
    if FALLBACK_KEY not in indexed:
        raise HelpRegistryError("O tutorial fallback é obrigatório.")
    return {"version": payload["version"], "tutorials": indexed}


def tutorial_for(context_key: object) -> Mapping[str, Any]:
    """Escolhe um tutorial conhecido; a chave da rota nunca vira conteúdo livre."""
    registry = load_help_registry()
    key = context_key if isinstance(context_key, str) else FALLBACK_KEY
    tutorial = registry["tutorials"].get(key, registry["tutorials"][FALLBACK_KEY])
    # O leitor contém também os controles de busca. O apêndice reaproveita a
    # mesma seção do registro, sem duplicar texto em template ou JavaScript.
    if tutorial["key"] == "qualitative_reader":
        search = registry["tutorials"].get("qualitative_search")
        if search is not None:
            tutorial = dict(tutorial)
            tutorial["sections"] = [*tutorial["sections"], search["sections"][1]]
    return tutorial


def _section_text(section: Mapping[str, Any]) -> str:
    # ``keywords`` é uma taxonomia interna: melhora a recuperação de nomes de
    # controles e sinônimos sem poluir o manual que a pessoa lê no diálogo.
    parts = [section.get("title", ""), *section.get("keywords", []), *section.get("body", [])]
    for item in section.get("items", []):
        if isinstance(item, Mapping):
            parts.extend((item.get("name", ""), item.get("description", "")))
    return _text(" ".join(str(part) for part in parts))


def assistant_help(topic: object, context_key: object, *, max_sections: int = MAX_ASSISTANT_SECTIONS) -> dict[str, Any]:
    """Retorna poucas seções oficiais relevantes, sem transferir o manual inteiro.

    A recuperação é determinística, local e limitada. Ela complementa fatos de
    acesso já apurados pelo executor; jamais substitui leitura de documentos ou
    consulta ao corpus.
    """
    registry = load_help_registry()
    key = context_key if isinstance(context_key, str) else FALLBACK_KEY
    # A composição de capítulos é exclusiva do diálogo visual. Na recuperação,
    # cada capítulo conserva sua origem para não duplicar uma seção anexada ao
    # manual do leitor nem ocultar uma correspondência explícita em outro guia.
    tutorial = registry["tutorials"].get(key, registry["tutorials"][FALLBACK_KEY])
    query_words = _words(topic)
    ranked: list[tuple[int, int, Mapping[str, Any], Mapping[str, Any]]] = []
    candidates = [tutorial]
    if query_words:
        candidates.extend(item for key, item in registry["tutorials"].items() if key != tutorial["key"])
    for tutorial_position, candidate in enumerate(candidates):
        for position, section in enumerate(candidate["sections"]):
            text = _section_text(section)
            score = len(query_words & _words(text))
            if query_words:
                # Um título é a taxonomia editorial do capítulo. Dar-lhe peso
                # adicional evita que uma menção incidental em outro texto
                # esconda, por exemplo, a seção específica da planilha.
                score += 2 * len(query_words & _words(section.get("title", "")))
                # Os nomes dos itens correspondem a controles, abas e campos
                # visíveis. Eles são sinais mais precisos que uma ocorrência
                # incidental da mesma palavra dentro de um parágrafo longo.
                item_names = " ".join(
                    str(item.get("name", "")) for item in section.get("items", [])
                    if isinstance(item, Mapping)
                )
                score += 3 * len(query_words & _words(item_names))
                # A página atual é preferida somente entre seções que de fato
                # respondem à pergunta; capítulos de outras ferramentas podem
                # conter a explicação específica solicitada.
                if score and candidate["key"] == tutorial["key"]:
                    score += 2
                if score and position == 0:
                    score += 1
            else:
                if candidate["key"] == tutorial["key"]:
                    score += 2
                if position == 0:
                    score += 1
            ranked.append((score, -(tutorial_position * 100 + position), candidate, section))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    selected = ranked[:max(1, min(max_sections, MAX_ASSISTANT_SECTIONS))]
    sections = []
    for _, _, source_tutorial, section in selected:
        content = _section_text(section)[:MAX_ASSISTANT_SECTION_CHARS].rstrip()
        sections.append({"id": section["id"], "title": section["title"], "content": content,
                         "tutorial_key": source_tutorial["key"]})
    return {
        "documentation_version": registry["version"],
        "page": {"key": tutorial["key"], "title": tutorial["title"], "summary": tutorial["summary"]},
        "sections": sections,
        "limited": len(tutorial["sections"]) > len(sections),
        "official_platform_documentation": True,
    }


__all__ = [
    "FALLBACK_KEY", "HelpRegistryError", "REGISTRY_PATH", "assistant_help",
    "load_help_registry", "tutorial_for",
]
