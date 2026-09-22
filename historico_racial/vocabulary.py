"""Snapshots imutáveis do vocabulário da pesquisa histórico-racial.

O YAML é apenas o seed. Os dados mutáveis ficam fora do código e podem ser
montados em um volume persistente por HISTORICO_RACIAL_DATA_DIR.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .dictionaries import ConfiguracaoInvalidaError, carregar_categorias, carregar_entidades
from .entities import Entidade


class VocabularioError(ValueError):
    """Erro controlado de validação ou integridade do vocabulário."""


def _diretorio_padrao() -> Path:
    return Path(os.environ.get(
        "HISTORICO_RACIAL_DATA_DIR",
        str(Path(__file__).resolve().parent.parent / "outputs" / "historico_racial"),
    ))


def _canonico(dados: Any) -> bytes:
    return json.dumps(dados, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def hash_vocabulario(vocabulario: dict[str, Any]) -> str:
    return hashlib.sha256(_canonico(vocabulario)).hexdigest()


def _texto(valor: Any, campo: str, obrigatorio: bool = True) -> str:
    if not isinstance(valor, str):
        raise VocabularioError(f"{campo} deve ser um texto.")
    valor = valor.strip()
    if obrigatorio and not valor:
        raise VocabularioError(f"{campo} é obrigatório.")
    return valor


def _booleano(valor: Any, campo: str) -> bool:
    if type(valor) is not bool:
        raise VocabularioError(f"{campo} deve ser ativo ou inativo.")
    return valor


def _contagens(vocabulario: dict[str, Any]) -> dict[str, int]:
    return {
        "grupos": len(vocabulario["grupos"]),
        "entidades": len(vocabulario["entidades"]),
        "variantes": sum(len(item["variantes"]) for item in vocabulario["entidades"]),
    }


def _seed() -> dict[str, Any]:
    dados = carregar_entidades()
    return {
        "grupos": {
            codigo: {"id_grupo": codigo, "nome": nome, "descricao": "", "ativo": True}
            for codigo, nome in dados["grupos"].items()
        },
        "entidades": [{
            "id_entidade": item["id_entidade"],
            "forma_canonica": item["forma_canonica"],
            "variantes": [{"texto": texto, "ativo": True} for texto in item["variantes"]],
            "tipo_entidade": item["tipo_entidade"],
            "grupo": [item["grupo"]] if isinstance(item["grupo"], str) else list(item["grupo"]),
            "tradicao_intelectual": item.get("tradicao_intelectual") or "",
            "pais_regiao": item.get("pais_regiao") or "",
            "observacoes": item.get("observacoes") or "",
            "ativo": True,
        } for item in dados["entidades"]],
    }


def validar_vocabulario(vocabulario: Any, anterior: dict | None = None) -> dict[str, Any]:
    """Valida o snapshot inteiro; desativar nunca remove registros anteriores."""
    if not isinstance(vocabulario, dict) or set(vocabulario) != {"grupos", "entidades"}:
        raise VocabularioError("O vocabulário deve conter grupos e entidades.")
    grupos = vocabulario["grupos"]
    entidades = vocabulario["entidades"]
    if not isinstance(grupos, dict) or not grupos or not isinstance(entidades, list):
        raise VocabularioError("Grupos ou entidades inválidos.")
    if len(grupos) > 1000 or len(entidades) > 20000:
        raise VocabularioError("O vocabulário excede o limite de registros.")
    nomes_grupos: set[str] = set()
    for codigo, grupo in grupos.items():
        if not isinstance(codigo, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,79}", codigo):
            raise VocabularioError("ID de grupo inválido.")
        if not isinstance(grupo, dict) or grupo.get("id_grupo") != codigo:
            raise VocabularioError(f"Grupo {codigo} inválido.")
        nome = _texto(grupo.get("nome"), "Nome do grupo")
        _texto(grupo.get("descricao", ""), "Descrição do grupo", False)
        _booleano(grupo.get("ativo"), f"Estado do grupo {codigo}")
        if nome.casefold() in nomes_grupos:
            raise VocabularioError(f"Nome de grupo repetido: {nome}")
        nomes_grupos.add(nome.casefold())
    categorias = carregar_categorias()
    ids: set[str] = set()
    canonicas: set[str] = set()
    variantes_globais: dict[str, str] = {}
    for entidade in entidades:
        if not isinstance(entidade, dict):
            raise VocabularioError("Registro de entidade inválido.")
        identificador = _texto(entidade.get("id_entidade"), "ID da entidade")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,79}", identificador):
            raise VocabularioError(f"ID de entidade inválido: {identificador}")
        if identificador in ids:
            raise VocabularioError(f"ID de entidade repetido: {identificador}")
        ids.add(identificador)
        canonica = _texto(entidade.get("forma_canonica"), f"Forma canônica de {identificador}")
        if canonica.casefold() in canonicas:
            raise VocabularioError(f"Forma canônica repetida: {canonica}")
        canonicas.add(canonica.casefold())
        _booleano(entidade.get("ativo"), f"Estado de {identificador}")
        if not isinstance(entidade.get("tipo_entidade"), str) or entidade["tipo_entidade"] not in categorias["tipos_entidade"]:
            raise VocabularioError(f"Tipo de entidade inválido: {identificador}")
        for campo in ("tradicao_intelectual", "pais_regiao", "observacoes"):
            _texto(entidade.get(campo, ""), campo, False)
        tradicao = entidade.get("tradicao_intelectual") or ""
        if tradicao and (not isinstance(tradicao, str) or tradicao not in categorias["tradicoes_intelectuais"]):
            raise VocabularioError(f"Tradição não cadastrada: {tradicao}")
        associados = entidade.get("grupo")
        if (not isinstance(associados, list) or not associados
                or any(not isinstance(codigo, str) for codigo in associados)
                or len(set(associados)) != len(associados)):
            raise VocabularioError(f"Grupos inválidos em {identificador}")
        if any(codigo not in grupos for codigo in associados):
            raise VocabularioError(f"Grupo desconhecido em {identificador}")
        variantes = entidade.get("variantes")
        if not isinstance(variantes, list) or not variantes or len(variantes) > 500:
            raise VocabularioError(f"Variantes inválidas em {identificador}")
        for variante in variantes:
            if not isinstance(variante, dict):
                raise VocabularioError(f"Variante inválida em {identificador}")
            texto = _texto(variante.get("texto"), f"Variante de {identificador}")
            _booleano(variante.get("ativo"), f"Estado da variante {texto}")
            chave = texto.casefold()
            if chave in variantes_globais:
                raise VocabularioError(
                    f"Variante repetida entre entidades {variantes_globais[chave]} e {identificador}: {texto}"
                )
            variantes_globais[chave] = identificador
        if canonica.casefold() not in {item["texto"].casefold() for item in variantes}:
            raise VocabularioError(f"A forma canônica deve constar nas variantes de {identificador}")
    if anterior is not None:
        if not set(anterior["grupos"]).issubset(grupos):
            raise VocabularioError("Grupos antigos não podem ser apagados; desative-os.")
        novos_por_id = {item["id_entidade"]: item for item in entidades}
        for antiga in anterior["entidades"]:
            atual = novos_por_id.get(antiga["id_entidade"])
            if atual is None:
                raise VocabularioError("Entidades antigas não podem ser apagadas; desative-as.")
            formas = {item["texto"].casefold() for item in atual["variantes"]}
            if any(item["texto"].casefold() not in formas for item in antiga["variantes"]):
                raise VocabularioError("Variantes antigas não podem ser apagadas; desative-as.")
    return copy.deepcopy(vocabulario)


def entidades_pesquisaveis(vocabulario: dict[str, Any]) -> tuple[Entidade, ...]:
    """Adapta o snapshot ao buscador existente sem alterar seu algoritmo."""
    grupos_ativos = {codigo for codigo, grupo in vocabulario["grupos"].items() if grupo["ativo"]}
    resultado = []
    for item in vocabulario["entidades"]:
        if not item["ativo"] or not grupos_ativos.intersection(item["grupo"]):
            continue
        variantes = tuple(variante["texto"] for variante in item["variantes"] if variante["ativo"])
        if variantes:
            resultado.append(Entidade(
                id_entidade=item["id_entidade"], forma_canonica=item["forma_canonica"],
                variantes=variantes, tipo_entidade=item["tipo_entidade"], grupo=tuple(item["grupo"]),
                tradicao_intelectual=item.get("tradicao_intelectual") or None,
                pais_regiao=item.get("pais_regiao") or None,
                observacoes=item.get("observacoes") or None,
            ))
    return tuple(resultado)


class VocabularyStore:
    def __init__(self, data_dir: Path | str | None = None):
        self.root = Path(data_dir) if data_dir is not None else _diretorio_padrao()
        self.versions = self.root / "vocabulario" / "versions"
        self.active = self.root / "vocabulario" / "active.json"
        self.lock = RLock()

    @staticmethod
    def _ler(caminho: Path) -> dict[str, Any]:
        try:
            with caminho.open("r", encoding="utf-8") as arquivo:
                dados = json.load(arquivo)
        except (OSError, UnicodeError, json.JSONDecodeError) as erro:
            raise VocabularioError(f"Não foi possível ler o vocabulário: {caminho.name}") from erro
        if not isinstance(dados, dict):
            raise VocabularioError("Arquivo de vocabulário inválido.")
        return dados

    @staticmethod
    def _escrever_atomico(caminho: Path, dados: dict, exclusivo: bool = False) -> None:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        temporario = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=caminho.parent, prefix=".vocab-", suffix=".tmp", delete=False
            ) as arquivo:
                temporario = Path(arquivo.name)
                arquivo.write(json.dumps(dados, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
                arquivo.flush()
                os.fsync(arquivo.fileno())
            if exclusivo:
                os.link(temporario, caminho)  # criação exclusiva: versão anterior nunca é substituída
            else:
                os.replace(temporario, caminho)
        finally:
            if temporario is not None:
                temporario.unlink(missing_ok=True)

    def _criar_versao(self, versao: str, pai: str | None, nota: str, vocabulario: dict) -> dict:
        vocabulario = validar_vocabulario(vocabulario)
        registro = {
            "version": versao,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "parent": pai,
            "note": nota,
            "counts": _contagens(vocabulario),
            "hash": hash_vocabulario(vocabulario),
            "vocabulario": vocabulario,
        }
        caminho = self.versions / f"{versao}.json"
        try:
            self._escrever_atomico(caminho, registro, exclusivo=True)
            self._escrever_atomico(self.active, {"version": versao, "hash": registro["hash"]})
        except (OSError, FileExistsError) as erro:
            raise VocabularioError("Não foi possível gravar a nova versão do vocabulário.") from erro
        return copy.deepcopy(registro)

    def _bootstrap(self) -> None:
        if self.active.is_file():
            return
        inicial = self.versions / "v1.0.json"
        if inicial.is_file():
            if any(caminho.name != "v1.0.json" for caminho in self.versions.glob("v*.json")):
                raise VocabularioError("A referência à versão ativa está ausente; verifique o armazenamento.")
            registro = self._ler(inicial)
            if registro.get("hash") != hash_vocabulario(registro.get("vocabulario", {})):
                raise VocabularioError("A versão inicial do vocabulário está corrompida.")
            self._escrever_atomico(self.active, {"version": "v1.0", "hash": registro["hash"]})
            return
        if self.versions.is_dir() and any(self.versions.glob("v*.json")):
            raise VocabularioError("A referência à versão ativa está ausente; verifique o armazenamento.")
        self._criar_versao("v1.0", None, "Vocabulário inicial importado de entities.yml", _seed())

    def carregar(self, versao: str) -> dict:
        if not re.fullmatch(r"v1\.\d+", versao):
            raise VocabularioError("Identificador de versão inválido.")
        with self.lock:
            self._bootstrap()
            registro = self._ler(self.versions / f"{versao}.json")
            if registro.get("version") != versao or registro.get("hash") != hash_vocabulario(registro.get("vocabulario", {})):
                raise VocabularioError(f"Integridade da versão {versao} inválida.")
            return copy.deepcopy(registro)

    def capturar_ativa(self) -> dict:
        with self.lock:
            self._bootstrap()
            ponteiro = self._ler(self.active)
            registro = self.carregar(ponteiro.get("version", ""))
            if ponteiro.get("hash") != registro["hash"]:
                raise VocabularioError("Hash da versão ativa divergente.")
            return registro

    def listar_versoes(self) -> list[dict]:
        with self.lock:
            ativa = self.capturar_ativa()["version"]
            registros = [self.carregar(caminho.stem) for caminho in self.versions.glob("v1.*.json")]
            return [{chave: item[chave] for chave in ("version", "created_at", "parent", "note", "counts", "hash")}
                    | {"active": item["version"] == ativa} for item in
                    sorted(registros, key=lambda item: int(item["version"].split(".")[1]), reverse=True)]

    def salvar(self, base_version: str, vocabulario: dict, nota: str = "") -> dict:
        with self.lock:
            atual = self.capturar_ativa()
            if base_version != atual["version"]:
                raise VocabularioError("O vocabulário mudou durante a edição. Recarregue a página antes de salvar.")
            novo = validar_vocabulario(vocabulario, atual["vocabulario"])
            if hash_vocabulario(novo) == atual["hash"]:
                raise VocabularioError("Nenhuma alteração foi feita no vocabulário.")
            proxima = f"v1.{int(base_version.split('.')[1]) + 1}"
            return self._criar_versao(proxima, base_version, _texto(nota, "Nota", False)[:500], novo)
