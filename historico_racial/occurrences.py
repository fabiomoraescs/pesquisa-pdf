"""Localização lexical observacional das variantes cadastradas no YAML."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .entities import Entidade


@dataclass(frozen=True, slots=True)
class Correspondencia:
    id_entidade: str
    entidade: Entidade
    variante_configurada: str
    termo_encontrado: str
    forma_original_no_texto: str
    inicio: int
    fim: int


def _correspondencia(entidade: Entidade, variante: str, texto: str, inicio: int, fim: int,
                     *, codigo_da_forma_encontrada: bool = False) -> Correspondencia:
    original = texto[inicio:fim]
    if codigo_da_forma_encontrada:
        from analyzer.lexical_family import lexical_code_name
        termo_encontrado = lexical_code_name(original)
    else:
        termo_encontrado = variante
    return Correspondencia(
        entidade.id_entidade, entidade, variante, termo_encontrado, original, inicio, fim,
    )


def normalizar_com_mapa(texto: str) -> tuple[str, list[int]]:
    """Normaliza somente a cópia de busca e mapeia cada caractere ao original."""
    caracteres: list[str] = []
    posicoes: list[int] = []
    for indice, caractere in enumerate(texto):
        for parte in unicodedata.normalize("NFKD", caractere):
            if unicodedata.category(parte) == "Mn":
                continue
            for letra in parte.casefold():
                caracteres.append(letra)
                posicoes.append(indice)
    return "".join(caracteres), posicoes


def _padrao_variante(variante: str) -> re.Pattern[str]:
    normalizada, _ = normalizar_com_mapa(variante.strip())
    partes = re.split(r"\s+", normalizada)
    corpo = r"\s+".join(re.escape(parte) for parte in partes)
    return re.compile(rf"(?<!\w){corpo}(?!\w)")


class BuscadorLexical:
    """Localiza variantes preservando o contrato histórico e os novos modos."""

    def __init__(self, entidades: tuple[Entidade, ...], *, incluir_morfologia: bool = False,
                 incluir_familia_lexical: bool = False, metodo: str | None = None,
                 usar_regex: bool = False):
        if metodo not in {None, "literal", "lexical"}:
            raise ValueError("Método textual inválido.")
        if usar_regex and metodo != "literal":
            raise ValueError("Regex só pode ser usada com o método Literal.")
        self._metodo = metodo
        self._usar_regex = usar_regex
        self._shared_variants: list[tuple[Entidade, str]] = []
        self._literal_variants: list[tuple[Entidade, str]] = []
        if metodo == "lexical":
            self._shared_variants = [
                (entidade, variante)
                for entidade in entidades
                for variante in entidade.variantes
            ]
            self._lexical_variants = []
            self._padroes = []
            return
        if metodo == "literal":
            self._literal_variants = [
                (entidade, variante)
                for entidade in entidades
                for variante in entidade.variantes
            ]
            self._lexical_variants = []
            self._padroes = []
            return
        # Reutiliza as mesmas flexões simples da Análise por termos. O padrão
        # histórico, baseado somente nas variantes explícitas, não muda.
        if incluir_morfologia or incluir_familia_lexical:
            from analyzer.v1 import gerar_variacoes_termo
        self._lexical_variants: list[tuple[Entidade, str]] = []

        self._padroes: list[tuple[Entidade, tuple[str, ...], re.Pattern[str]]] = []
        for entidade in entidades:
            variantes_por_forma: dict[str, list[str]] = {}
            for variante in entidade.variantes:
                if incluir_familia_lexical:
                    self._lexical_variants.append((entidade, variante))
                formas = (gerar_variacoes_termo(variante) if incluir_morfologia or incluir_familia_lexical
                          else {normalizar_com_mapa(variante.strip())[0]})
                for forma in sorted(formas):
                    variantes_por_forma.setdefault(forma, []).append(variante)
            for forma, variantes in variantes_por_forma.items():
                self._padroes.append((entidade, tuple(variantes), _padrao_variante(forma)))

    def localizar(self, texto: str) -> list[Correspondencia]:
        """Retorna matches únicos com recortes originais e limites de palavra."""
        if self._metodo == "literal":
            from analyzer.search_matching import find_search_spans

            candidatos_por_entidade: dict[str, list[Correspondencia]] = {}
            for entidade, variante in self._literal_variants:
                for inicio, fim in find_search_spans(texto, variante, use_regex=self._usar_regex):
                    candidatos_por_entidade.setdefault(entidade.id_entidade, []).append(
                        _correspondencia(entidade, variante, texto, inicio, fim)
                    )
            return self._deduplicar(candidatos_por_entidade)
        if self._metodo == "lexical":
            from analyzer.lexical_family import find_lexical_matches

            candidatos_por_entidade: dict[str, list[Correspondencia]] = {}
            for entidade, variante in self._shared_variants:
                for inicio, fim, _ in find_lexical_matches(texto, variante):
                    candidatos_por_entidade.setdefault(entidade.id_entidade, []).append(
                        _correspondencia(entidade, variante, texto, inicio, fim,
                                         codigo_da_forma_encontrada=True)
                    )
            return self._deduplicar(candidatos_por_entidade)
        normalizado, mapa = normalizar_com_mapa(texto)
        if not normalizado:
            return []
        candidatos_por_entidade: dict[str, list[Correspondencia]] = {}
        for entidade, variantes, padrao in self._padroes:
            for encontrado in padrao.finditer(normalizado):
                inicio = mapa[encontrado.start()]
                fim = mapa[encontrado.end() - 1] + 1
                original = texto[inicio:fim]
                variante = next(
                    (item for item in variantes if item.casefold() == original.casefold()),
                    variantes[0],
                )
                candidatos_por_entidade.setdefault(entidade.id_entidade, []).append(
                    _correspondencia(entidade, variante, texto, inicio, fim)
                )
        if self._lexical_variants:
            from analyzer.lexical_family import find_lexical_spans
            for entidade, variante in self._lexical_variants:
                for inicio, fim, _ in find_lexical_spans(texto, variante):
                    candidatos_por_entidade.setdefault(entidade.id_entidade, []).append(
                        _correspondencia(entidade, variante, texto, inicio, fim)
                    )
        return self._deduplicar(candidatos_por_entidade)

    @staticmethod
    def _deduplicar(candidatos_por_entidade: dict[str, list[Correspondencia]]) -> list[Correspondencia]:
        resultados: list[Correspondencia] = []
        for candidatos in candidatos_por_entidade.values():
            usados: list[tuple[int, int]] = []
            for candidato in sorted(candidatos, key=lambda item: (-(item.fim - item.inicio), item.inicio)):
                if any(candidato.inicio < fim and candidato.fim > inicio for inicio, fim in usados):
                    continue
                usados.append((candidato.inicio, candidato.fim))
                resultados.append(candidato)
        return sorted(resultados, key=lambda item: (item.inicio, item.fim, item.id_entidade))


def localizar_variantes(texto: str, entidades: tuple[Entidade, ...]) -> list[Correspondencia]:
    """Atalho para localização pontual; jobs usam BuscadorLexical reutilizável."""
    return BuscadorLexical(entidades).localizar(texto)
