"""Bibliotecas oficiais imutáveis e snapshots independentes por projeto."""

from __future__ import annotations

import copy
from pathlib import Path
from threading import RLock

from flask import current_app

from historico_racial.occurrences import normalizar_com_mapa
from historico_racial.vocabulary import VocabularyStore, VocabularioError, validar_vocabulario

from .extensions import db
from .models import Project, ProjectLibrary, ProjectVocabularyVersion, VocabularyLibrary

_stores: dict[str, VocabularyStore] = {}
_stores_lock = RLock()


def project_store(project_id: str, initial_vocabulary: dict | None = None) -> VocabularyStore:
    root = Path(current_app.config["PLATFORM_DATA_DIR"]) / "projects" / project_id
    key = str(root.resolve())
    with _stores_lock:
        if key not in _stores:
            _stores[key] = VocabularyStore(root, initial_vocabulary=initial_vocabulary)
        return _stores[key]


def forget_project_store(project_id: str) -> None:
    root = Path(current_app.config["PLATFORM_DATA_DIR"]) / "projects" / project_id
    with _stores_lock:
        _stores.pop(str(root.resolve()), None)


def _variant_key(text: str) -> str:
    return normalizar_com_mapa(text.strip())[0]


def merge_libraries(libraries: list[VocabularyLibrary]) -> dict:
    """Une entity_key compatíveis; conflitos nunca são decididos silenciosamente."""
    if not libraries:
        raise VocabularioError("Selecione ao menos uma biblioteca ativa.")
    groups: dict = {}
    entities: dict[str, dict] = {}
    variants: dict[str, str] = {}
    for index, library in enumerate(libraries):
        if not library.active or library.status != "published":
            raise VocabularioError(f"Biblioteca indisponível: {library.name}")
        source = copy.deepcopy(library.snapshot_json)
        group_map: dict[str, str] = {}
        for group_id, group in source["grupos"].items():
            new_id = group_id if group_id not in groups else f"{library.id}_{group_id}"
            if new_id in groups:
                raise VocabularioError(f"ID de grupo em conflito: {new_id}")
            group_map[group_id] = new_id
            group["id_grupo"] = new_id
            if any(existing["nome"].casefold() == group["nome"].casefold() for existing in groups.values()):
                group["nome"] = f"{group['nome']} ({library.name})"
            groups[new_id] = group
        for entity in source["entidades"]:
            key = entity.get("entity_key") or entity["id_entidade"]
            mapped_groups = [group_map[g] for g in entity["grupo"]]
            if key in entities:
                existing = entities[key]
                if (_variant_key(existing["forma_canonica"]) != _variant_key(entity["forma_canonica"])
                        or existing["tipo_entidade"] != entity["tipo_entidade"]):
                    raise VocabularioError(f"entity_key incompatível entre bibliotecas: {key}")
                existing["grupo"] = list(dict.fromkeys(existing["grupo"] + mapped_groups))
                existing["source_libraries"].append(library.id)
                known = {_variant_key(v["texto"]) for v in existing["variantes"]}
                for variant in entity["variantes"]:
                    if _variant_key(variant["texto"]) not in known:
                        existing["variantes"].append(variant)
                        known.add(_variant_key(variant["texto"]))
            else:
                entity["grupo"] = mapped_groups
                entity["entity_key"] = key
                entity["source_libraries"] = [library.id]
                entities[key] = entity
    for key, entity in entities.items():
        for variant in entity["variantes"]:
            normalized = _variant_key(variant["texto"])
            previous = variants.get(normalized)
            if previous and previous != key:
                raise VocabularioError(
                    f"Variante '{variant['texto']}' conflita entre {previous} e {key}."
                )
            variants[normalized] = key
    return validar_vocabulario({"grupos": groups, "entidades": list(entities.values())})


def register_version(project: Project, record: dict) -> None:
    db.session.query(ProjectVocabularyVersion).filter_by(project_id=project.id, active=True).update({"active": False})
    store = project_store(project.id)
    relative_snapshot = (store.versions / f"{record['version']}.json").relative_to(Path(current_app.config["PLATFORM_DATA_DIR"]))
    db.session.add(ProjectVocabularyVersion(
        project_id=project.id, version=record["version"], parent_version=record["parent"],
        content_hash=record["hash"], counts_json=record["counts"],
        snapshot_path=relative_snapshot.as_posix(),
        note=record["note"], active=True,
    ))


def create_project_vocabulary(project: Project, libraries: list[VocabularyLibrary]) -> dict:
    merged = merge_libraries(libraries)
    store = project_store(project.id, initial_vocabulary=merged)
    record = store.capturar_ativa()
    for library in libraries:
        db.session.add(ProjectLibrary(
            project_id=project.id, library_id=library.id, source_hash=library.content_hash,
            source_version=library.version,
        ))
    register_version(project, record)
    return record
