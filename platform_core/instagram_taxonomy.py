"""Taxonomias editoriais manuais e históricas do Análysis Instagram.

Este módulo mantém o domínio de classificação separado das métricas brutas.
Classificações nunca são apagadas: uma alteração marca a atribuição anterior
como substituída e registra uma nova escolha manual.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import re
import unicodedata
from typing import Iterable, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from .extensions import db
from .instagram_metrics import MetricStatistics, summarize_values
from .models import (
    AnalyticsProject,
    InstagramAccount,
    InstagramMedia,
    InstagramMediaClassification,
    InstagramTaxonomyCategory,
    InstagramTaxonomyDimension,
    User,
    utcnow,
)


DEFAULT_TAXONOMY_VERSION = "instagram-taxonomy-default-v1"
SELECTION_MODES = ("single", "multi")
CLASSIFICATION_STATUSES = ("all", "unclassified", "partial", "classified")

# As chaves não dependem do rótulo de apresentação e permanecem estáveis em
# importações, relatórios e futuras versões do módulo.
DEFAULT_TAXONOMY = (
    {
        "key": "communicational-objective",
        "name": "Objetivo comunicacional",
        "selection_mode": "single",
        "categories": (
            ("inform", "Informar"),
            ("educate-orient", "Educar / Orientar"),
            ("promote", "Divulgar / Promover"),
            ("mobilize-invite", "Mobilizar / Convidar"),
            ("relate-engage", "Relacionar / Engajar"),
            ("institutional", "Institucional"),
            ("other", "Outro"),
        ),
    },
    {
        "key": "editorial-type",
        "name": "Tipo editorial",
        "selection_mode": "single",
        "categories": (
            ("news-notice", "Notícia / Aviso"),
            ("tip-tutorial", "Dica / Tutorial"),
            ("curiosity", "Curiosidade"),
            ("content-promotion", "Divulgação de conteúdo / publicação"),
            ("event", "Evento"),
            ("behind-the-scenes", "Bastidores"),
            ("interview-testimonial", "Entrevista / Depoimento"),
            ("institutional", "Institucional"),
            ("commemorative-date", "Data comemorativa"),
            ("other", "Outro"),
        ),
    },
    {
        "key": "call-to-action",
        "name": "CTA",
        "selection_mode": "multi",
        "categories": (
            ("comment", "Comentar"),
            ("share", "Compartilhar"),
            ("save", "Salvar"),
            ("access-link", "Acessar link"),
            ("subscribe-participate", "Inscrever-se / Participar"),
            ("follow-profile", "Seguir perfil"),
            ("contact", "Entrar em contato"),
            ("other", "Outro"),
        ),
    },
    {
        "key": "main-theme",
        "name": "Tema principal",
        "selection_mode": "single",
        "categories": (),
    },
)


class TaxonomyValidationError(ValueError):
    """Erro de domínio seguro para apresentação em formulário."""


@dataclass(frozen=True)
class MediaClassificationState:
    media_id: str
    status: str
    classifications: tuple[InstagramMediaClassification, ...]

    @property
    def label(self) -> str:
        return {
            "unclassified": "Não classificado",
            "partial": "Parcialmente classificado",
            "classified": "Classificado",
        }[self.status]


@dataclass(frozen=True)
class TaxonomyMetricInput:
    media_id: str
    raw_values: Mapping[str, int | None]
    derived_values: Mapping[str, float | None]


@dataclass(frozen=True)
class TaxonomyCategoryComparison:
    category_id: str | None
    key: str
    name: str
    publications: int
    reach: MetricStatistics
    interactions: MetricStatistics
    interaction_rate_by_reach: MetricStatistics
    is_unclassified: bool = False


@dataclass(frozen=True)
class TaxonomyDashboardData:
    dimensions: tuple[InstagramTaxonomyDimension, ...]
    selected_dimension: InstagramTaxonomyDimension | None
    comparisons: tuple[TaxonomyCategoryComparison, ...]
    has_current_classifications: bool

    @property
    def is_multi(self) -> bool:
        return bool(self.selected_dimension and self.selected_dimension.selection_mode == "multi")


def normalize_taxonomy_key(value: str) -> str:
    """Converte um rótulo de usuário em chave curta, estável e previsível."""
    normalized = unicodedata.normalize("NFKD", value.strip()).encode("ascii", "ignore").decode("ascii")
    key = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    if not key or len(key) > 80:
        raise TaxonomyValidationError("Informe uma chave válida de até 80 caracteres.")
    return key


def _text(value: str | None, *, label: str, maximum: int, required: bool = True) -> str | None:
    cleaned = (value or "").strip()
    if not cleaned:
        if required:
            raise TaxonomyValidationError(f"Informe {label}.")
        return None
    if len(cleaned) > maximum:
        raise TaxonomyValidationError(f"{label.capitalize()} excede o limite de {maximum} caracteres.")
    return cleaned


def _position(value: int | str | None, *, fallback: int) -> int:
    if value in (None, ""):
        return fallback
    try:
        position = int(value)
    except (TypeError, ValueError) as error:
        raise TaxonomyValidationError("A posição deve ser um número inteiro não negativo.") from error
    if position < 0:
        raise TaxonomyValidationError("A posição deve ser um número inteiro não negativo.")
    return position


def taxonomy_dimensions(project_id: str, *, active_only: bool = False) -> tuple[InstagramTaxonomyDimension, ...]:
    statement = (
        select(InstagramTaxonomyDimension)
        .where(InstagramTaxonomyDimension.analytics_project_id == project_id)
        .options(selectinload(InstagramTaxonomyDimension.categories))
        .order_by(InstagramTaxonomyDimension.position, InstagramTaxonomyDimension.name, InstagramTaxonomyDimension.id)
    )
    if active_only:
        statement = statement.where(InstagramTaxonomyDimension.active.is_(True))
    return tuple(db.session.scalars(statement).unique().all())


def create_initial_taxonomy(project: AnalyticsProject) -> tuple[InstagramTaxonomyDimension, ...]:
    """Cria somente as dimensões padrão ausentes, sem reativar escolhas arquivadas."""
    existing = {
        dimension.key: dimension
        for dimension in taxonomy_dimensions(project.id)
    }
    for position, specification in enumerate(DEFAULT_TAXONOMY):
        if specification["key"] in existing:
            continue
        dimension = InstagramTaxonomyDimension(
            analytics_project_id=project.id,
            key=specification["key"],
            name=specification["name"],
            selection_mode=specification["selection_mode"],
            position=position,
        )
        db.session.add(dimension)
        db.session.flush()
        for category_position, (key, name) in enumerate(specification["categories"]):
            db.session.add(InstagramTaxonomyCategory(
                instagram_taxonomy_dimension_id=dimension.id,
                key=key,
                name=name,
                position=category_position,
            ))
    db.session.flush()
    return taxonomy_dimensions(project.id)


def create_dimension(project: AnalyticsProject, *, name: str, key: str | None,
                     description: str | None, selection_mode: str, position: int | str | None) -> InstagramTaxonomyDimension:
    if selection_mode not in SELECTION_MODES:
        raise TaxonomyValidationError("Selecione um modo de escolha válido.")
    clean_name = _text(name, label="o nome da dimensão", maximum=160)
    clean_key = normalize_taxonomy_key(key or clean_name)
    clean_description = _text(description, label="a descrição", maximum=4_000, required=False)
    next_position = db.session.scalar(
        select(func.coalesce(func.max(InstagramTaxonomyDimension.position), -1) + 1)
        .where(InstagramTaxonomyDimension.analytics_project_id == project.id)
    )
    dimension = InstagramTaxonomyDimension(
        analytics_project_id=project.id,
        key=clean_key,
        name=clean_name,
        description=clean_description,
        selection_mode=selection_mode,
        position=_position(position, fallback=int(next_position or 0)),
    )
    db.session.add(dimension)
    db.session.flush()
    return dimension


def _dimension_for_project(project: AnalyticsProject, dimension_id: str) -> InstagramTaxonomyDimension:
    dimension = db.session.scalar(select(InstagramTaxonomyDimension).where(
        InstagramTaxonomyDimension.id == dimension_id,
        InstagramTaxonomyDimension.analytics_project_id == project.id,
    ))
    if dimension is None:
        raise TaxonomyValidationError("A dimensão não pertence a este projeto.")
    return dimension


def create_category(project: AnalyticsProject, dimension_id: str, *, name: str, key: str | None,
                    description: str | None, position: int | str | None) -> InstagramTaxonomyCategory:
    dimension = _dimension_for_project(project, dimension_id)
    clean_name = _text(name, label="o nome da categoria", maximum=160)
    clean_key = normalize_taxonomy_key(key or clean_name)
    clean_description = _text(description, label="a descrição", maximum=4_000, required=False)
    next_position = db.session.scalar(
        select(func.coalesce(func.max(InstagramTaxonomyCategory.position), -1) + 1)
        .where(InstagramTaxonomyCategory.instagram_taxonomy_dimension_id == dimension.id)
    )
    category = InstagramTaxonomyCategory(
        instagram_taxonomy_dimension_id=dimension.id,
        key=clean_key,
        name=clean_name,
        description=clean_description,
        position=_position(position, fallback=int(next_position or 0)),
    )
    db.session.add(category)
    db.session.flush()
    return category


def set_dimension_active(project: AnalyticsProject, dimension_id: str, *, active: bool) -> InstagramTaxonomyDimension:
    dimension = _dimension_for_project(project, dimension_id)
    dimension.active = active
    return dimension


def set_category_active(project: AnalyticsProject, category_id: str, *, active: bool) -> InstagramTaxonomyCategory:
    category = db.session.scalar(
        select(InstagramTaxonomyCategory)
        .join(InstagramTaxonomyDimension)
        .where(
            InstagramTaxonomyCategory.id == category_id,
            InstagramTaxonomyDimension.analytics_project_id == project.id,
        )
    )
    if category is None:
        raise TaxonomyValidationError("A categoria não pertence a este projeto.")
    category.active = active
    return category


def _media_for_project(project: AnalyticsProject, media_id: str) -> InstagramMedia:
    media = db.session.scalar(
        select(InstagramMedia)
        .join(InstagramAccount)
        .where(
            InstagramMedia.id == media_id,
            InstagramAccount.analytics_project_id == project.id,
        )
    )
    if media is None:
        raise TaxonomyValidationError("A publicação não pertence a este projeto.")
    return media


def current_classifications(project_id: str, *, media_ids: Sequence[str] | None = None,
                            dimension_id: str | None = None) -> tuple[InstagramMediaClassification, ...]:
    """Obtém somente o estado atual, com dimensão e categoria carregadas em lote."""
    if media_ids is not None and not media_ids:
        return ()
    statement = (
        select(InstagramMediaClassification)
        .join(InstagramMedia, InstagramMedia.id == InstagramMediaClassification.instagram_media_id)
        .join(InstagramAccount, InstagramAccount.id == InstagramMedia.instagram_account_id)
        .where(
            InstagramAccount.analytics_project_id == project_id,
            InstagramMediaClassification.superseded_at.is_(None),
        )
        .options(
            joinedload(InstagramMediaClassification.dimension),
            joinedload(InstagramMediaClassification.category),
        )
        .order_by(
            InstagramMediaClassification.instagram_media_id,
            InstagramMediaClassification.instagram_taxonomy_dimension_id,
            InstagramMediaClassification.assigned_at.desc(),
            InstagramMediaClassification.id.desc(),
        )
    )
    if media_ids is not None:
        statement = statement.where(InstagramMediaClassification.instagram_media_id.in_(tuple(media_ids)))
    if dimension_id is not None:
        statement = statement.where(InstagramMediaClassification.instagram_taxonomy_dimension_id == dimension_id)
    return tuple(db.session.scalars(statement).all())


def classify_media(project: AnalyticsProject, media_id: str, selections: Mapping[str, Iterable[str]],
                   assigned_by: User | None) -> None:
    """Substitui escolhas atuais de dimensões ativas sem apagar seu histórico."""
    media = _media_for_project(project, media_id)
    dimensions = taxonomy_dimensions(project.id, active_only=True)
    current = current_classifications(project.id, media_ids=(media.id,))
    current_by_dimension: dict[str, list[InstagramMediaClassification]] = defaultdict(list)
    for item in current:
        if item.dimension.active:
            current_by_dimension[item.instagram_taxonomy_dimension_id].append(item)

    now = utcnow()
    for dimension in dimensions:
        requested = tuple(dict.fromkeys(str(category_id) for category_id in selections.get(dimension.id, ()) if category_id))
        if dimension.selection_mode == "single" and len(requested) > 1:
            raise TaxonomyValidationError(f"{dimension.name}: escolha somente uma categoria.")
        categories = tuple(db.session.scalars(select(InstagramTaxonomyCategory).where(
            InstagramTaxonomyCategory.id.in_(requested),
            InstagramTaxonomyCategory.instagram_taxonomy_dimension_id == dimension.id,
            InstagramTaxonomyCategory.active.is_(True),
        )).all()) if requested else ()
        category_ids = {category.id for category in categories}
        if len(category_ids) != len(requested):
            raise TaxonomyValidationError(f"{dimension.name}: a categoria selecionada não está disponível.")

        existing = current_by_dimension.get(dimension.id, [])
        if dimension.selection_mode == "single":
            desired_id = requested[0] if requested else None
            if len(existing) == 1 and existing[0].instagram_taxonomy_category_id == desired_id:
                continue
            for item in existing:
                item.superseded_at = now
            if desired_id is not None:
                db.session.add(InstagramMediaClassification(
                    instagram_media_id=media.id,
                    instagram_taxonomy_dimension_id=dimension.id,
                    instagram_taxonomy_category_id=desired_id,
                    assigned_by_user_id=assigned_by.id if assigned_by is not None else None,
                    assigned_at=now,
                ))
            continue

        retained: dict[str, InstagramMediaClassification] = {}
        for item in existing:
            previous = retained.get(item.instagram_taxonomy_category_id)
            if previous is not None or item.instagram_taxonomy_category_id not in category_ids:
                item.superseded_at = now
            else:
                retained[item.instagram_taxonomy_category_id] = item
        for category_id in category_ids - retained.keys():
            db.session.add(InstagramMediaClassification(
                instagram_media_id=media.id,
                instagram_taxonomy_dimension_id=dimension.id,
                instagram_taxonomy_category_id=category_id,
                assigned_by_user_id=assigned_by.id if assigned_by is not None else None,
                assigned_at=now,
            ))
    db.session.flush()


def classification_states(project: AnalyticsProject, media_ids: Sequence[str]) -> dict[str, MediaClassificationState]:
    """Calcula os três estados de preenchimento sem consultas por publicação."""
    dimensions = taxonomy_dimensions(project.id, active_only=True)
    active_ids = {dimension.id for dimension in dimensions}
    # Uma dimensão criada sem categorias (como o Tema principal padrão) ainda
    # não oferece uma escolha possível e, portanto, não impede a conclusão.
    required_single_ids = {
        dimension.id for dimension in dimensions
        if dimension.selection_mode == "single" and any(category.active for category in dimension.categories)
    }
    grouped: dict[str, list[InstagramMediaClassification]] = defaultdict(list)
    for item in current_classifications(project.id, media_ids=media_ids):
        if item.instagram_taxonomy_dimension_id in active_ids:
            grouped[item.instagram_media_id].append(item)
    states = {}
    for media_id in media_ids:
        current = tuple(grouped.get(media_id, ()))
        dimensions_with_value = {item.instagram_taxonomy_dimension_id for item in current}
        if not current:
            status = "unclassified"
        elif required_single_ids.issubset(dimensions_with_value):
            status = "classified"
        else:
            status = "partial"
        states[media_id] = MediaClassificationState(media_id, status, current)
    return states


def _comparison(category_id: str | None, key: str, name: str,
                items: Sequence[TaxonomyMetricInput], *, unclassified: bool = False) -> TaxonomyCategoryComparison:
    publications = len(items)
    return TaxonomyCategoryComparison(
        category_id=category_id,
        key=key,
        name=name,
        publications=publications,
        reach=summarize_values("reach", "Alcance", (item.raw_values.get("reach") for item in items), publications),
        interactions=summarize_values(
            "interactions", "Interações", (item.raw_values.get("interactions") for item in items), publications
        ),
        interaction_rate_by_reach=summarize_values(
            "interaction_rate_by_reach", "Taxa de interação por alcance",
            (item.derived_values.get("interaction_rate_by_reach") for item in items), publications,
        ),
        is_unclassified=unclassified,
    )


def taxonomy_dashboard_data(project: AnalyticsProject, items: Iterable[TaxonomyMetricInput], *,
                            selected_dimension_id: str | None = None) -> TaxonomyDashboardData:
    """Agrupa métricas existentes sem recalcular fórmulas nem duplicar mídias."""
    dimensions = taxonomy_dimensions(project.id, active_only=True)
    if not dimensions:
        return TaxonomyDashboardData((), None, (), False)
    dimensions_by_id = {dimension.id: dimension for dimension in dimensions}
    selected = dimensions_by_id.get(selected_dimension_id) if selected_dimension_id else dimensions[0]
    if selected is None:
        raise TaxonomyValidationError("A dimensão editorial selecionada não está disponível.")

    rows = tuple(items)
    inputs_by_media = {item.media_id: item for item in rows}
    classifications = current_classifications(
        project.id, media_ids=tuple(inputs_by_media), dimension_id=selected.id
    )
    grouped: dict[str, list[InstagramMediaClassification]] = defaultdict(list)
    for classification in classifications:
        grouped[classification.instagram_media_id].append(classification)

    category_items: dict[tuple[str | None, str, str, bool], list[TaxonomyMetricInput]] = defaultdict(list)
    if selected.selection_mode == "single":
        for media_id, item in inputs_by_media.items():
            classifications_for_media = grouped.get(media_id, ())
            if classifications_for_media:
                chosen = classifications_for_media[0]
                category_items[(chosen.category.id, chosen.category.key, chosen.category.name, False)].append(item)
            else:
                category_items[(None, "unclassified", "Não classificado", True)].append(item)
    else:
        seen: set[tuple[str, str]] = set()
        for media_id, classifications_for_media in grouped.items():
            for classification in classifications_for_media:
                unique_pair = (media_id, classification.category.id)
                if unique_pair in seen:
                    continue
                seen.add(unique_pair)
                category_items[(
                    classification.category.id, classification.category.key, classification.category.name, False
                )].append(inputs_by_media[media_id])

    comparisons = tuple(sorted(
        (_comparison(category_id, key, name, values, unclassified=unclassified)
         for (category_id, key, name, unclassified), values in category_items.items()),
        key=lambda item: (item.is_unclassified, item.name.casefold(), item.key),
    ))
    return TaxonomyDashboardData(
        dimensions=dimensions,
        selected_dimension=selected,
        comparisons=comparisons,
        has_current_classifications=bool(classifications),
    )
