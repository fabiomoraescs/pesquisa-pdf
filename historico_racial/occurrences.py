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
    termo_encontrado: str
    forma_original_no_texto: str
    inicio: int
    fim: int


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
    """Compila padrões por job; flexões automáticas são opt-in para novos jobs."""

    def __init__(self, entidades: tuple[Entidade, ...], *, incluir_morfologia: bool = False):
        # Reutiliza as mesmas flexões simples da Análise por termos. O padrão
        # histórico, baseado somente nas variantes explícitas, não muda.
        if incluir_morfologia:
            from analyzer.v1 import gerar_variacoes_termo

        self._padroes: list[tuple[Entidade, tuple[str, ...], re.Pattern[str]]] = []
        for entidade in entidades:
            variantes_por_forma: dict[str, list[str]] = {}
            for variante in entidade.variantes:
                formas = (gerar_variacoes_termo(variante) if incluir_morfologia
                          else {normalizar_com_mapa(variante.strip())[0]})
                for forma in sorted(formas):
                    variantes_por_forma.setdefault(forma, []).append(variante)
            for forma, variantes in variantes_por_forma.items():
                self._padroes.append((entidade, tuple(variantes), _padrao_variante(forma)))

    def localizar(self, texto: str) -> list[Correspondencia]:
        """Retorna matches únicos com recortes originais e limites de palavra."""
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
                    Correspondencia(
                        entidade.id_entidade, entidade, variante, original, inicio, fim
                    )
                )
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
