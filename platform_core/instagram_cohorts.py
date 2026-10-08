"""Coortes estruturadas e reproduzíveis para Insights do Instagram.

Uma coorte nunca é inferida a partir de títulos ou descrições.  O snapshot
persistido contém apenas identificadores estáveis e pode ser reaplicado a um
``AnalyticsRun`` posterior para comparar o mesmo universo analítico.
"""

from __future__ import annotations

from hashlib import sha256
from json import dumps
from typing import Mapping

from sqlalchemy import select

from .extensions import db
from .instagram_metrics import normalized_content_type
from .instagram_taxonomy import current_classifications
from .models import AnalyticsProject, InstagramMedia


INSTAGRAM_COHORT_VERSION = "instagram-cohort-v1"


def media_type_cohort(media_type_key: str, label: str) -> dict[str, str]:
    """Cria o snapshot canônico de uma coorte de tipo de mídia."""
    return {
        "version": INSTAGRAM_COHORT_VERSION,
        "kind": "media_type",
        "media_type_key": media_type_key,
        "label": f"Tipo de mídia: {label}",
    }


def taxonomy_category_cohort(
    dimension_id: str, category_id: str, dimension_name: str, category_name: str,
) -> dict[str, str]:
    """Cria o snapshot canônico de uma categoria taxonômica."""
    return {
        "version": INSTAGRAM_COHORT_VERSION,
        "kind": "taxonomy_category",
        "dimension_id": dimension_id,
        "category_id": category_id,
        "label": f"{dimension_name}: {category_name}",
    }


def normalize_cohort_snapshot(value: object) -> dict[str, str] | None:
    """Valida e normaliza um snapshot já persistido, sem tentar adivinhar dados."""
    if not isinstance(value, Mapping) or value.get("version") != INSTAGRAM_COHORT_VERSION:
        return None
    kind = value.get("kind")
    label = value.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    if kind == "media_type":
        media_type_key = value.get("media_type_key")
        if not isinstance(media_type_key, str) or not media_type_key.strip():
            return None
        return {
            "version": INSTAGRAM_COHORT_VERSION,
            "kind": "media_type",
            "media_type_key": media_type_key.strip(),
            "label": label.strip(),
        }
    if kind == "taxonomy_category":
        dimension_id, category_id = value.get("dimension_id"), value.get("category_id")
        if not all(isinstance(item, str) and item.strip() for item in (dimension_id, category_id)):
            return None
        return {
            "version": INSTAGRAM_COHORT_VERSION,
            "kind": "taxonomy_category",
            "dimension_id": dimension_id.strip(),
            "category_id": category_id.strip(),
            "label": label.strip(),
        }
    return None


def cohort_signature(snapshot: Mapping[str, str] | None) -> str | None:
    """Retorna uma assinatura estável para comparabilidade entre snapshots."""
    normalized = normalize_cohort_snapshot(snapshot)
    if normalized is None:
        return None
    canonical = dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def cohort_label(snapshot: Mapping[str, str] | None) -> str | None:
    normalized = normalize_cohort_snapshot(snapshot)
    return normalized["label"] if normalized is not None else None


def cohort_media_ids(project: AnalyticsProject, snapshot: Mapping[str, str] | None) -> tuple[str, ...] | None:
    """Resolve apenas uma coorte explícita; ``None`` significa não reconstruível.

    Uma coorte válida, porém sem mídia atual, devolve a tupla vazia.  Assim a
    ausência de observações não é confundida com a ausência de definição.
    """
    normalized = normalize_cohort_snapshot(snapshot)
    if normalized is None:
        return None
    media = db.session.scalars(
        select(InstagramMedia)
        .join(InstagramMedia.instagram_account)
        .where(InstagramMedia.instagram_account.has(analytics_project_id=project.id))
        .order_by(InstagramMedia.id)
    ).all()
    if normalized["kind"] == "media_type":
        key = normalized["media_type_key"]
        return tuple(item.id for item in media if normalized_content_type(item.media_type_raw)[0] == key)

    classifications = current_classifications(project.id, media_ids=tuple(item.id for item in media))
    selected = {
        item.instagram_media_id
        for item in classifications
        if item.instagram_taxonomy_dimension_id == normalized["dimension_id"]
        and item.instagram_taxonomy_category_id == normalized["category_id"]
    }
    return tuple(item.id for item in media if item.id in selected)


__all__ = [
    "INSTAGRAM_COHORT_VERSION",
    "cohort_label",
    "cohort_media_ids",
    "cohort_signature",
    "media_type_cohort",
    "normalize_cohort_snapshot",
    "taxonomy_category_cohort",
]
