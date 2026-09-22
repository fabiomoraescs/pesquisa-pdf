"""Nomes estáveis e versão do esquema de pesquisa declarado no YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .dictionaries import carregar_schema


DOCUMENTOS = "DOCUMENTOS"
OCORRENCIAS = "OCORRENCIAS"
CODIFICACAO = "CODIFICACAO"
COOCORRENCIAS = "COOCORRENCIAS"


def obter_esquema(base_dir: Path | str | None = None) -> dict[str, Any]:
    """Disponibiliza a versão e as estruturas previstas a futuros exportadores."""
    return carregar_schema(base_dir)


SCHEMA_VERSION = obter_esquema()["schema_version"]
