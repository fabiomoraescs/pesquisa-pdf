"""Carregamento e validação estrutural dos dicionários histórico-raciais.

Este módulo não procura termos em documentos nem atribui classificações.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "historico_racial"
ARQUIVOS = ("schema.yml", "entities.yml", "categories.yml", "suggestions.yml", "ibr.yml")
ESTRUTURAS = frozenset({"DOCUMENTOS", "OCORRENCIAS", "CODIFICACAO", "COOCORRENCIAS"})


class ConfiguracaoInvalidaError(ValueError):
    """Indica configuração ausente ou estruturalmente inválida."""


def _diretorio(base_dir: Path | str | None) -> Path:
    return Path(base_dir) if base_dir is not None else CONFIG_DIR


def _carregar_yaml(nome: str, base_dir: Path | str | None = None) -> dict[str, Any]:
    if nome not in ARQUIVOS:
        raise ConfiguracaoInvalidaError(f"Arquivo de configuração desconhecido: {nome}")
    caminho = _diretorio(base_dir) / nome
    if not caminho.is_file():
        raise ConfiguracaoInvalidaError(f"Arquivo de configuração ausente: {caminho}")
    try:
        with caminho.open("r", encoding="utf-8") as arquivo:
            dados = yaml.safe_load(arquivo)
    except (OSError, UnicodeError, yaml.YAMLError) as erro:
        raise ConfiguracaoInvalidaError(f"Não foi possível carregar {nome}: {erro}") from erro
    if not isinstance(dados, dict):
        raise ConfiguracaoInvalidaError(f"{nome} deve conter um mapeamento YAML")
    return dados


def _texto(valor: Any, campo: str) -> str:
    if not isinstance(valor, str) or not valor.strip():
        raise ConfiguracaoInvalidaError(f"{campo} deve ser um texto não vazio")
    return valor


def _lista_textos(valor: Any, campo: str) -> list[str]:
    if not isinstance(valor, list) or not valor:
        raise ConfiguracaoInvalidaError(f"{campo} deve ser uma lista não vazia")
    textos = [_texto(item, campo) for item in valor]
    if len({item.strip().casefold() for item in textos}) != len(textos):
        raise ConfiguracaoInvalidaError(f"{campo} contém valores repetidos")
    return textos


def carregar_schema(base_dir: Path | str | None = None) -> dict[str, Any]:
    """Carrega a referência versionada das quatro estruturas previstas."""
    dados = _carregar_yaml("schema.yml", base_dir)
    _texto(dados.get("schema_version"), "schema_version")
    estruturas = dados.get("estruturas")
    if not isinstance(estruturas, dict) or not ESTRUTURAS.issubset(estruturas):
        raise ConfiguracaoInvalidaError("schema.yml deve definir as quatro estruturas previstas")
    for nome, definicao in estruturas.items():
        _texto(nome, "nome da estrutura")
        if not isinstance(definicao, dict):
            raise ConfiguracaoInvalidaError(f"Estrutura {nome} deve ser um mapeamento")
        _texto(definicao.get("descricao"), f"descrição de {nome}")
    return dados


def carregar_categorias(base_dir: Path | str | None = None) -> dict[str, Any]:
    """Carrega os vocabulários editáveis de codificação e metadados."""
    dados = _carregar_yaml("categories.yml", base_dir)
    for chave in (
        "tipos_entidade", "tradicoes_intelectuais", "formas_referencia",
        "operacoes_repertorio", "status_validacao",
    ):
        _lista_textos(dados.get(chave), chave)
    return dados


def carregar_entidades(base_dir: Path | str | None = None) -> dict[str, Any]:
    """Carrega entidades únicas, suas variantes e grupos declarados."""
    dados = _carregar_yaml("entities.yml", base_dir)
    categorias = carregar_categorias(base_dir)
    grupos = dados.get("grupos")
    entidades = dados.get("entidades")
    if not isinstance(grupos, dict) or not grupos:
        raise ConfiguracaoInvalidaError("entities.yml deve definir grupos")
    for codigo, descricao in grupos.items():
        _texto(codigo, "código de grupo")
        _texto(descricao, f"descrição do grupo {codigo}")
    if not isinstance(entidades, list) or not entidades:
        raise ConfiguracaoInvalidaError("entities.yml deve definir entidades")

    ids: set[str] = set()
    formas: set[str] = set()
    variantes_por_entidade: dict[str, str] = {}
    for indice, entidade in enumerate(entidades, start=1):
        if not isinstance(entidade, dict):
            raise ConfiguracaoInvalidaError(f"Entidade {indice} deve ser um mapeamento")
        identificador = _texto(entidade.get("id_entidade"), f"ID da entidade {indice}")
        canonica = _texto(entidade.get("forma_canonica"), f"forma canônica de {identificador}")
        variantes = _lista_textos(entidade.get("variantes"), f"variantes de {identificador}")
        tipo = _texto(entidade.get("tipo_entidade"), f"tipo de {identificador}")
        if identificador in ids:
            raise ConfiguracaoInvalidaError(f"ID de entidade repetido: {identificador}")
        ids.add(identificador)
        forma_normalizada = canonica.strip().casefold()
        if forma_normalizada in formas:
            raise ConfiguracaoInvalidaError(f"Forma canônica repetida: {canonica}")
        formas.add(forma_normalizada)
        if forma_normalizada not in {variante.strip().casefold() for variante in variantes}:
            raise ConfiguracaoInvalidaError(f"A forma canônica de {identificador} deve constar nas variantes")
        if tipo not in categorias["tipos_entidade"]:
            raise ConfiguracaoInvalidaError(f"Tipo não cadastrado em {identificador}: {tipo}")
        grupo = entidade.get("grupo")
        codigos = [grupo] if isinstance(grupo, str) else grupo
        if not isinstance(codigos, list) or not codigos:
            raise ConfiguracaoInvalidaError(f"Grupo inválido em {identificador}")
        codigos = [_texto(codigo, f"grupo de {identificador}") for codigo in codigos]
        if len(set(codigos)) != len(codigos):
            raise ConfiguracaoInvalidaError(f"Grupo repetido em {identificador}")
        for codigo in codigos:
            if codigo not in grupos:
                raise ConfiguracaoInvalidaError(f"Grupo não cadastrado em {identificador}: {codigo}")
        tradicao = entidade.get("tradicao_intelectual")
        if tradicao is not None and tradicao not in categorias["tradicoes_intelectuais"]:
            raise ConfiguracaoInvalidaError(f"Tradição não cadastrada em {identificador}: {tradicao}")
        for campo in ("pais_regiao", "observacoes"):
            if campo in entidade:
                _texto(entidade[campo], f"{campo} de {identificador}")
        for variante in variantes:
            chave = variante.strip().casefold()
            outro_id = variantes_por_entidade.get(chave)
            if outro_id is not None:
                raise ConfiguracaoInvalidaError(
                    f"Variante repetida entre entidades {outro_id} e {identificador}: {variante}"
                )
            variantes_por_entidade[chave] = identificador
    return dados


def carregar_sugestoes(base_dir: Path | str | None = None) -> dict[str, Any]:
    """Carrega nomes de sugestões futuras, sem criar regras de inferência."""
    dados = _carregar_yaml("suggestions.yml", base_dir)
    _lista_textos(dados.get("classificacoes_sugeridas"), "classificacoes_sugeridas")
    return dados


def carregar_ibr(base_dir: Path | str | None = None) -> dict[str, Any]:
    """Carrega descritores B1–B5, sem calcular pontuação."""
    dados = _carregar_yaml("ibr.yml", base_dir)
    criterios = dados.get("criterios")
    if not isinstance(criterios, dict) or set(criterios) != {f"B{indice}" for indice in range(1, 6)}:
        raise ConfiguracaoInvalidaError("ibr.yml deve definir exatamente B1–B5")
    for codigo, descricao in criterios.items():
        _texto(descricao, codigo)
    minimo, maximo = dados.get("minimo"), dados.get("maximo")
    if type(minimo) is not int or type(maximo) is not int or minimo != 0 or maximo != 5:
        raise ConfiguracaoInvalidaError("ibr.yml deve definir minimo=0 e maximo=5")
    _texto(dados.get("observacao"), "observacao do IBR")
    return dados


def carregar_configuracoes(base_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Retorna as cinco configurações validadas em memória."""
    return {
        "schema": carregar_schema(base_dir),
        "entities": carregar_entidades(base_dir),
        "categories": carregar_categorias(base_dir),
        "suggestions": carregar_sugestoes(base_dir),
        "ibr": carregar_ibr(base_dir),
    }
