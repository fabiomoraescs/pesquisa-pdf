"""Representação imutável das entidades configuradas, sem mecanismo de busca."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .dictionaries import carregar_entidades


@dataclass(frozen=True, slots=True)
class Entidade:
    id_entidade: str
    forma_canonica: str
    variantes: tuple[str, ...]
    tipo_entidade: str
    grupo: tuple[str, ...]
    tradicao_intelectual: str | None = None
    pais_regiao: str | None = None
    observacoes: str | None = None


def listar_entidades(base_dir: Path | str | None = None) -> tuple[Entidade, ...]:
    """Converte registros YAML validados em objetos somente de leitura."""
    registros = carregar_entidades(base_dir)["entidades"]
    entidades = []
    for registro in registros:
        grupo = registro["grupo"]
        entidades.append(
            Entidade(
                id_entidade=registro["id_entidade"],
                forma_canonica=registro["forma_canonica"],
                variantes=tuple(registro["variantes"]),
                tipo_entidade=registro["tipo_entidade"],
                grupo=(grupo,) if isinstance(grupo, str) else tuple(grupo),
                tradicao_intelectual=registro.get("tradicao_intelectual"),
                pais_regiao=registro.get("pais_regiao"),
                observacoes=registro.get("observacoes"),
            )
        )
    return tuple(entidades)
