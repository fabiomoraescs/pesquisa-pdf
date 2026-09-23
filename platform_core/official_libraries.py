"""Edição manual de bibliotecas oficiais enquanto permanecem em rascunho."""

from __future__ import annotations

import copy
import re
import unicodedata

from sqlalchemy import select

from historico_racial.dictionaries import carregar_categorias
from historico_racial.vocabulary import _contagens, hash_vocabulario, validar_vocabulario, VocabularioError

from .extensions import db
from .models import VocabularyLibrary


class LibraryError(ValueError):
    pass


def slug(text: str, limit: int = 60) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    result = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")[:limit].rstrip("_")
    if not result or not result[0].isalpha():
        raise LibraryError("Informe um nome que permita gerar um identificador seguro.")
    return result


def create_draft(name: str, description: str) -> VocabularyLibrary:
    name, description = name.strip(), description.strip()
    if not name or len(name) > 160 or len(description) > 4000:
        raise LibraryError("Informe um nome válido e uma descrição de até 4.000 caracteres.")
    key = slug(name)
    if db.session.get(VocabularyLibrary, key) is not None or db.session.scalar(
        select(VocabularyLibrary.id).where(db.func.lower(VocabularyLibrary.name) == name.lower())
    ):
        raise LibraryError("Já existe uma biblioteca com esse nome ou identificador.")
    snapshot = {"grupos": {}, "entidades": []}
    library = VocabularyLibrary(id=key, name=name, description=description,
                                status="draft", version="v1", active=False,
                                snapshot_json=snapshot, counts_json=_contagens(snapshot),
                                content_hash=hash_vocabulario(snapshot))
    db.session.add(library)
    return library


def create_imported_draft(name: str, description: str, snapshot: dict) -> VocabularyLibrary:
    """Cria a biblioteca inteira em uma única transação controlada pela rota."""
    library = create_draft(name, description)
    _save(library, snapshot)
    return library


def _draft(library: VocabularyLibrary, base_hash: str) -> dict:
    if library.status != "draft":
        raise LibraryError("Bibliotecas publicadas ou inativas não podem ser editadas diretamente.")
    if base_hash != library.content_hash:
        raise LibraryError("O rascunho mudou. Recarregue a página antes de continuar.")
    return copy.deepcopy(library.snapshot_json)


def _save(library: VocabularyLibrary, snapshot: dict) -> None:
    try:
        validated = validar_vocabulario(snapshot)
    except VocabularioError as error:
        raise LibraryError(str(error)) from error
    library.snapshot_json = validated
    library.counts_json = _contagens(validated)
    library.content_hash = hash_vocabulario(validated)


def add_group(library: VocabularyLibrary, base_hash: str, name: str, description: str, active: bool) -> str:
    snapshot = _draft(library, base_hash)
    name, description = name.strip(), description.strip()
    if not name or len(name) > 160 or len(description) > 1000:
        raise LibraryError("Informe um nome de grupo válido e uma descrição de até 1.000 caracteres.")
    key = slug(name, 80)
    if key in snapshot["grupos"]:
        raise LibraryError("Já existe um grupo com esse identificador.")
    snapshot["grupos"][key] = {"id_grupo": key, "nome": name, "descricao": description, "ativo": active}
    _save(library, snapshot)
    return key


def add_entity(library: VocabularyLibrary, base_hash: str, canonical: str, entity_key: str,
               groups: list[str], entity_type: str, tradition: str, region: str, active: bool) -> str:
    snapshot = _draft(library, base_hash)
    canonical, tradition, region = canonical.strip(), tradition.strip(), region.strip()
    if not canonical or len(canonical) > 200 or len(region) > 160:
        raise LibraryError("Informe uma forma canônica válida; país/região deve ter até 160 caracteres.")
    key = slug(entity_key.strip() or canonical, 80)
    if any(item["id_entidade"] == key or item.get("entity_key") == key for item in snapshot["entidades"]):
        raise LibraryError("Já existe uma entidade com esse identificador.")
    if not groups or len(set(groups)) != len(groups) or any(group not in snapshot["grupos"] for group in groups):
        raise LibraryError("Selecione ao menos um grupo válido.")
    categories = carregar_categorias()
    if entity_type not in categories["tipos_entidade"] or (tradition and tradition not in categories["tradicoes_intelectuais"]):
        raise LibraryError("Tipo ou tradição intelectual inválido.")
    snapshot["entidades"].append({
        "id_entidade": key, "entity_key": key, "forma_canonica": canonical,
        "variantes": [{"texto": canonical, "ativo": True}], "tipo_entidade": entity_type,
        "grupo": groups, "tradicao_intelectual": tradition, "pais_regiao": region,
        "observacoes": "", "ativo": active,
    })
    _save(library, snapshot)
    return key


def add_variant(library: VocabularyLibrary, base_hash: str, entity_key: str, text: str, active: bool) -> None:
    snapshot = _draft(library, base_hash)
    text = text.strip()
    if not text or len(text) > 200:
        raise LibraryError("Informe uma variante de até 200 caracteres.")
    entity = next((item for item in snapshot["entidades"] if item["id_entidade"] == entity_key), None)
    if entity is None:
        raise LibraryError("Entidade não encontrada.")
    entity["variantes"].append({"texto": text, "ativo": active})
    _save(library, snapshot)


def set_item_active(library: VocabularyLibrary, base_hash: str, kind: str,
                    key: str, active: bool, variant_index: str = "") -> None:
    snapshot = _draft(library, base_hash)
    if kind == "group":
        item = snapshot["grupos"].get(key)
    elif kind in {"entity", "variant"}:
        entity = next((item for item in snapshot["entidades"] if item["id_entidade"] == key), None)
        if kind == "entity":
            item = entity
        else:
            try:
                index = int(variant_index)
            except ValueError as error:
                raise LibraryError("Variante inválida.") from error
            item = entity["variantes"][index] if entity is not None and 0 <= index < len(entity["variantes"]) else None
    else:
        raise LibraryError("Tipo de registro inválido.")
    if item is None:
        raise LibraryError("Registro não encontrado.")
    item["ativo"] = active
    _save(library, snapshot)


def change_publication(library: VocabularyLibrary, target: str) -> None:
    if target == "published" and library.status == "draft":
        if not library.snapshot_json["entidades"]:
            raise LibraryError("Cadastre ao menos uma entidade antes de publicar.")
        try:
            validar_vocabulario(library.snapshot_json)
        except VocabularioError as error:
            raise LibraryError(str(error)) from error
    elif not (library.status == "published" and target == "inactive"
              or library.status == "inactive" and target == "published"):
        raise LibraryError("Mudança de estado indisponível.")
    library.status = target
    library.active = target == "published"
