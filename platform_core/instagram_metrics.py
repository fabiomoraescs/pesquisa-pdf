"""Métricas derivadas reproduzíveis do Instagram.

Os valores deste módulo são calculados sob demanda a partir da última
observação selecionada pelo dashboard. Nenhum valor derivado é persistido.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median
import re
from typing import Iterable, Mapping


DERIVED_METRICS_VERSION = "instagram-derived-v1"


@dataclass(frozen=True)
class DerivedMetricDefinition:
    """Contrato estável e auditável de uma métrica derivada."""

    identifier: str
    name: str
    version: str
    formula: str
    numerator_field: str
    denominator_field: str
    unit: str
    null_rule: str
    zero_denominator_rule: str

    @property
    def fields(self) -> tuple[str, str]:
        return self.numerator_field, self.denominator_field


_RATE_NULL_RULE = "NULL quando o numerador ou o denominador não foi informado."
_RATE_ZERO_DENOMINATOR_RULE = "NULL quando o denominador é zero; não há divisão por zero."

RATE_METRIC_DEFINITIONS = (
    DerivedMetricDefinition(
        "interaction_rate_by_reach", "Taxa de interação por alcance", DERIVED_METRICS_VERSION,
        "interactions / reach * 100", "interactions", "reach", "%",
        _RATE_NULL_RULE, _RATE_ZERO_DENOMINATOR_RULE,
    ),
    DerivedMetricDefinition(
        "likes_rate_by_reach", "Curtidas por alcance", DERIVED_METRICS_VERSION,
        "likes / reach * 100", "likes", "reach", "%",
        _RATE_NULL_RULE, _RATE_ZERO_DENOMINATOR_RULE,
    ),
    DerivedMetricDefinition(
        "comments_rate_by_reach", "Comentários por alcance", DERIVED_METRICS_VERSION,
        "comments_count / reach * 100", "comments_count", "reach", "%",
        _RATE_NULL_RULE, _RATE_ZERO_DENOMINATOR_RULE,
    ),
    DerivedMetricDefinition(
        "shares_rate_by_reach", "Compartilhamentos por alcance", DERIVED_METRICS_VERSION,
        "shares / reach * 100", "shares", "reach", "%",
        _RATE_NULL_RULE, _RATE_ZERO_DENOMINATOR_RULE,
    ),
    DerivedMetricDefinition(
        "saves_rate_by_reach", "Salvamentos por alcance", DERIVED_METRICS_VERSION,
        "saves / reach * 100", "saves", "reach", "%",
        _RATE_NULL_RULE, _RATE_ZERO_DENOMINATOR_RULE,
    ),
    DerivedMetricDefinition(
        "follows_generated_rate_by_reach", "Seguidores gerados por alcance", DERIVED_METRICS_VERSION,
        "follows_generated / reach * 100", "follows_generated", "reach", "%",
        _RATE_NULL_RULE, _RATE_ZERO_DENOMINATOR_RULE,
    ),
    DerivedMetricDefinition(
        "interaction_rate_by_impressions", "Taxa de interação por impressões", DERIVED_METRICS_VERSION,
        "interactions / impressions * 100", "interactions", "impressions", "%",
        _RATE_NULL_RULE, _RATE_ZERO_DENOMINATOR_RULE,
    ),
)
FOLLOWER_VARIATION_DEFINITIONS = (
    DerivedMetricDefinition(
        "followers_variation_absolute", "Variação absoluta de seguidores", DERIVED_METRICS_VERSION,
        "latest_followers_count - previous_followers_count", "latest_followers_count",
        "previous_followers_count", "seguidores",
        "NULL quando não há dois snapshots válidos.", "Não se aplica.",
    ),
    DerivedMetricDefinition(
        "followers_variation_percentage", "Variação percentual de seguidores", DERIVED_METRICS_VERSION,
        "(latest_followers_count - previous_followers_count) / previous_followers_count * 100",
        "latest_followers_count", "previous_followers_count", "%",
        "NULL quando não há dois snapshots válidos.",
        "NULL quando o snapshot anterior é zero; não há divisão por zero.",
    ),
)
DERIVED_METRIC_DEFINITIONS = RATE_METRIC_DEFINITIONS + FOLLOWER_VARIATION_DEFINITIONS
DERIVED_METRICS_BY_ID = {definition.identifier: definition for definition in DERIVED_METRIC_DEFINITIONS}


@dataclass(frozen=True)
class MetricStatistics:
    """Estatísticas simples por publicação, com cobertura explícita."""

    identifier: str
    label: str
    mean_value: float | None
    median_value: float | None
    known: int
    publications: int

    @property
    def partial(self) -> bool:
        return self.known < self.publications


@dataclass(frozen=True)
class FollowerVariation:
    """Comparação entre os dois snapshots válidos mais recentes."""

    previous: int | None
    current: int | None
    absolute: int | None
    percentage: float | None


@dataclass(frozen=True)
class ContentTypeInput:
    media_id: str
    media_type_raw: str | None
    raw_values: Mapping[str, int | None]
    derived_values: Mapping[str, float | None]


@dataclass(frozen=True)
class ContentTypeComparison:
    key: str
    label: str
    raw_types: tuple[str, ...]
    publications: int
    reach: MetricStatistics
    interactions: MetricStatistics
    interaction_rate_by_reach: MetricStatistics


def ratio_percentage(numerator: int | None, denominator: int | None) -> float | None:
    """Calcula uma taxa sem converter ausência em zero ou limitar o resultado."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator * 100


def calculate_derived_metrics(values: Mapping[str, int | None]) -> dict[str, float | None]:
    """Retorna todas as taxas v1 para uma única observação de mídia."""
    return {
        definition.identifier: ratio_percentage(
            values.get(definition.numerator_field), values.get(definition.denominator_field)
        )
        for definition in RATE_METRIC_DEFINITIONS
    }


def summarize_values(identifier: str, label: str, values: Iterable[int | float | None],
                     publications: int) -> MetricStatistics:
    """Média e mediana calculadas apenas sobre valores conhecidos."""
    known_values = [value for value in values if value is not None]
    return MetricStatistics(
        identifier=identifier,
        label=label,
        mean_value=mean(known_values) if known_values else None,
        median_value=median(known_values) if known_values else None,
        known=len(known_values),
        publications=publications,
    )


def follower_variation(values: Iterable[int]) -> FollowerVariation:
    """Usa os dois últimos valores válidos de estoque, sem somá-los."""
    known_values = tuple(values)
    if len(known_values) < 2:
        return FollowerVariation(None, known_values[-1] if known_values else None, None, None)
    previous, current = known_values[-2:]
    absolute = current - previous
    return FollowerVariation(
        previous=previous,
        current=current,
        absolute=absolute,
        percentage=(absolute / previous * 100) if previous != 0 else None,
    )


def normalized_content_type(raw_value: str | None) -> tuple[str, str]:
    """Normaliza somente o rótulo de apresentação; o valor bruto é preservado."""
    if not raw_value or not raw_value.strip():
        return "unknown", "Não informado"
    normalized = re.sub(r"[^a-z0-9]+", "_", raw_value.casefold()).strip("_")
    if normalized in {"reel", "reels"}:
        return "reel", "Reels"
    if normalized in {"carousel", "carrossel", "carousel_album", "album"}:
        return "carousel", "Carrosséis"
    if normalized in {"image", "images", "imagem", "imagens", "photo", "photos"}:
        return "image", "Imagens"
    return "other", "Outro"


def compare_content_types(items: Iterable[ContentTypeInput]) -> tuple[ContentTypeComparison, ...]:
    """Agrupa publicações por tipo sem inferir nem alterar o tipo armazenado."""
    groups: dict[tuple[str, str], list[ContentTypeInput]] = {}
    for item in items:
        groups.setdefault(normalized_content_type(item.media_type_raw), []).append(item)

    comparisons = []
    for (key, label), group in groups.items():
        publications = len(group)
        comparisons.append(ContentTypeComparison(
            key=key,
            label=label,
            raw_types=tuple(sorted({item.media_type_raw for item in group if item.media_type_raw})),
            publications=publications,
            reach=summarize_values("reach", "Alcance", (item.raw_values.get("reach") for item in group), publications),
            interactions=summarize_values(
                "interactions", "Interações", (item.raw_values.get("interactions") for item in group), publications
            ),
            interaction_rate_by_reach=summarize_values(
                "interaction_rate_by_reach", "Taxa de interação por alcance",
                (item.derived_values.get("interaction_rate_by_reach") for item in group), publications,
            ),
        ))
    return tuple(sorted(comparisons, key=lambda comparison: (comparison.label, comparison.key)))
