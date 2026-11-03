"""Consulta descritiva do Instagram: uma mídia, sua última observação válida."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlsplit

from sqlalchemy import and_, func, select

from .extensions import db
from .instagram_metrics import (
    DERIVED_METRICS_VERSION,
    ContentTypeComparison,
    ContentTypeInput,
    FollowerVariation,
    MetricStatistics,
    calculate_derived_metrics,
    compare_content_types,
    follower_variation,
    summarize_values,
)
from .instagram_periods import InstagramPeriodError, TEMPORAL_PERIOD_KEYS, resolve_local_period
from .pagination import PlatformPage, paginate_sequence
from .instagram_taxonomy import (
    TaxonomyDashboardData,
    TaxonomyMetricInput,
    TaxonomyValidationError,
    taxonomy_dashboard_data,
)
from .models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramAccountSnapshot,
    InstagramMedia,
    InstagramMediaObservation,
)
from .presentation import format_local_datetime, to_local_datetime


# Compatibilidade pública para módulos anteriores. A resolução efetiva fica em
# ``instagram_periods`` para que Dashboard, Comentários, Insights e Relatórios
# não mantenham listas de períodos diferentes.
PERIOD_DAYS = {"7d": 7, "30d": 30, "60d": 60, "90d": 90, "6m": None, "12m": None, "custom": None, "all": None}
TABLE_SORTS = ("recent", "reach", "interactions", "interaction_rate")
METRICS = (
    ("reach", "Alcance"),
    ("impressions", "Impressões"),
    ("plays", "Reproduções"),
    ("likes", "Curtidas"),
    ("comments_count", "Comentários"),
    ("shares", "Compartilhamentos"),
    ("saves", "Salvamentos"),
    ("interactions", "Interações"),
    ("follows_generated", "Seguidores gerados"),
)


def _number(value: int | None) -> str:
    return "—" if value is None else f"{value:,}".replace(",", ".")


@dataclass(frozen=True)
class MetricSummary:
    field: str
    label: str
    value: int | None
    known: int
    publications: int

    @property
    def display(self) -> str:
        return _number(self.value)

    @property
    def availability(self) -> str:
        if self.known == 0:
            return "unavailable"
        if self.known < self.publications:
            return "partial"
        return "complete"

    @property
    def partial(self) -> bool:
        return self.availability == "partial"

    @property
    def unavailable(self) -> bool:
        return self.availability == "unavailable"


@dataclass(frozen=True)
class MediaRow:
    media: InstagramMedia
    observation: InstagramMediaObservation | None
    ordinal: int
    derived_metrics: Mapping[str, float | None]

    @property
    def caption(self) -> str:
        return self.media.caption_original.strip() or self.media.external_id or "Publicação sem legenda"

    @property
    def short_caption(self) -> str:
        caption = self.caption
        return caption if len(caption) <= 90 else f"{caption[:87]}…"

    @property
    def permalink(self) -> str | None:
        value = self.media.permalink
        if not value:
            return None
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


@dataclass(frozen=True)
class FollowerPoint:
    observed_at: datetime
    value: int


@dataclass(frozen=True)
class InstagramDashboard:
    reference_at: datetime | None
    period: str
    rows: tuple[MediaRow, ...]
    metrics: tuple[MetricSummary, ...]
    ranking: tuple[MediaRow, ...]
    followers_count: int | None
    followers_observed_at: datetime | None
    followers_series: tuple[FollowerPoint, ...]
    followers_variation: FollowerVariation
    performance_statistics: Mapping[str, MetricStatistics]
    type_comparisons: tuple[ContentTypeComparison, ...]
    taxonomy: TaxonomyDashboardData
    table_rows: tuple[MediaRow, ...]
    table_pagination: PlatformPage[MediaRow]
    sort: str
    derived_metrics_version: str
    has_data: bool
    chart_data: dict

    @property
    def publication_count(self) -> int:
        return len(self.rows)

    @property
    def followers_display(self) -> str:
        return _number(self.followers_count)


def _latest_project_observation(project_id: str) -> datetime | None:
    media_at = db.session.scalar(
        select(func.max(InstagramMediaObservation.observed_at))
        .join(AnalyticsRun, AnalyticsRun.id == InstagramMediaObservation.analytics_run_id)
        .where(AnalyticsRun.analytics_project_id == project_id, AnalyticsRun.status == "completed")
    )
    account_at = db.session.scalar(
        select(func.max(InstagramAccountSnapshot.observed_at))
        .join(AnalyticsRun, AnalyticsRun.id == InstagramAccountSnapshot.analytics_run_id)
        .where(AnalyticsRun.analytics_project_id == project_id, AnalyticsRun.status == "completed")
    )
    return max((value for value in (media_at, account_at) if value is not None), default=None)


def dashboard_period_bounds(
    period: str,
    reference_at: datetime | None,
    timezone_name: str,
    *,
    period_start: object = None,
    period_end: object = None,
) -> tuple[datetime | None, datetime | None]:
    """Retorna o intervalo UTC do recorte já usado pelo Dashboard.

    A data de fim continua sendo a última observação do projeto; o filtro de
    publicações não usa o relógio atual. Outros módulos analíticos podem
    reutilizar esta regra sem criar uma semântica temporal concorrente.
    """
    try:
        resolved = resolve_local_period(
            period,
            timezone_name,
            reference_at=reference_at,
            period_start=period_start,
            period_end=period_end,
        )
    except InstagramPeriodError as error:
        raise ValueError(str(error)) from error
    return resolved.start, resolved.end


def _media_rows(
    project_id: str,
    period: str,
    reference_at: datetime | None,
    timezone_name: str,
    *,
    period_start: object = None,
    period_end: object = None,
) -> tuple[MediaRow, ...]:
    ranked_observations = (
        select(
            InstagramMediaObservation.id.label("observation_id"),
            InstagramMediaObservation.instagram_media_id.label("media_id"),
            func.row_number().over(
                partition_by=InstagramMediaObservation.instagram_media_id,
                order_by=(InstagramMediaObservation.observed_at.desc(), InstagramMediaObservation.id.desc()),
            ).label("position"),
        )
        .join(AnalyticsRun, AnalyticsRun.id == InstagramMediaObservation.analytics_run_id)
        .where(AnalyticsRun.analytics_project_id == project_id, AnalyticsRun.status == "completed")
        .subquery()
    )
    query = (
        select(InstagramMedia, InstagramMediaObservation)
        .join(InstagramAccount, InstagramAccount.id == InstagramMedia.instagram_account_id)
        .outerjoin(
            ranked_observations,
            and_(ranked_observations.c.media_id == InstagramMedia.id,
                 ranked_observations.c.position == 1),
        )
        .outerjoin(InstagramMediaObservation,
                   InstagramMediaObservation.id == ranked_observations.c.observation_id)
        .where(InstagramAccount.analytics_project_id == project_id)
        .order_by(InstagramMedia.published_at.desc(), InstagramMedia.id.desc())
    )
    start, end = dashboard_period_bounds(
        period, reference_at, timezone_name, period_start=period_start, period_end=period_end,
    )
    if period != "all":
        if start is None or end is None:
            return ()
        query = query.where(
            InstagramMedia.published_at >= start,
            InstagramMedia.published_at <= end,
        )
    return tuple(
        MediaRow(media, observation, ordinal, {})
        for ordinal, (media, observation) in enumerate(db.session.execute(query), start=1)
    )


def _followers(project_id: str) -> tuple[int | None, datetime | None, tuple[FollowerPoint, ...]]:
    snapshots = db.session.scalars(
        select(InstagramAccountSnapshot)
        .join(AnalyticsRun, AnalyticsRun.id == InstagramAccountSnapshot.analytics_run_id)
        .where(AnalyticsRun.analytics_project_id == project_id, AnalyticsRun.status == "completed")
        .order_by(InstagramAccountSnapshot.observed_at, InstagramAccountSnapshot.id)
    ).all()
    if not snapshots:
        return None, None, ()
    # Em um empate temporal, o maior ID prevalece; NULL no último não herda valor antigo.
    by_instant = {snapshot.observed_at: snapshot for snapshot in snapshots}
    series = tuple(
        FollowerPoint(moment, snapshot.followers_count)
        for moment, snapshot in by_instant.items()
        if snapshot.followers_count is not None
    )
    latest = snapshots[-1]
    return latest.followers_count, latest.observed_at, series


def _raw_values(observation: InstagramMediaObservation | None) -> dict[str, int | None]:
    return {field: getattr(observation, field) if observation is not None else None for field, _label in METRICS}


def _with_derived_metrics(rows: tuple[MediaRow, ...]) -> tuple[MediaRow, ...]:
    return tuple(
        MediaRow(
            media=row.media,
            observation=row.observation,
            ordinal=row.ordinal,
            derived_metrics=calculate_derived_metrics(_raw_values(row.observation)),
        )
        for row in rows
    )


def selected_media_rows(
    project: AnalyticsProject,
    period: str,
    timezone_name: str,
    *,
    period_start: object = None,
    period_end: object = None,
) -> tuple[datetime | None, tuple[MediaRow, ...]]:
    """Seleciona uma publicação por mídia e aplica as métricas derivadas v1.

    Esta é a interface reutilizável para camadas analíticas: mantém a regra
    canônica de "última observação por publicação" sem somar coletas passadas.
    """
    if period not in TEMPORAL_PERIOD_KEYS:
        raise ValueError("Período inválido")
    reference_at = _latest_project_observation(project.id)
    return reference_at, _with_derived_metrics(
        _media_rows(
            project.id, period, reference_at, timezone_name,
            period_start=period_start, period_end=period_end,
        )
    )


def _performance_statistics(rows: tuple[MediaRow, ...]) -> dict[str, MetricStatistics]:
    definitions = (
        ("reach", "Alcance", lambda row: _raw_values(row.observation)["reach"]),
        ("impressions", "Impressões", lambda row: _raw_values(row.observation)["impressions"]),
        ("interactions", "Interações", lambda row: _raw_values(row.observation)["interactions"]),
        ("interaction_rate_by_reach", "Taxa de interação por alcance",
         lambda row: row.derived_metrics["interaction_rate_by_reach"]),
    )
    return {
        identifier: summarize_values(identifier, label, (getter(row) for row in rows), len(rows))
        for identifier, label, getter in definitions
    }


def _type_comparisons(rows: tuple[MediaRow, ...]) -> tuple[ContentTypeComparison, ...]:
    return compare_content_types(
        ContentTypeInput(
            media_id=row.media.id,
            media_type_raw=row.media.media_type_raw,
            raw_values=_raw_values(row.observation),
            derived_values=row.derived_metrics,
        )
        for row in rows
    )


def _table_rows(rows: tuple[MediaRow, ...], sort: str) -> tuple[MediaRow, ...]:
    if sort == "recent":
        return rows
    if sort == "reach":
        value_for = lambda row: _raw_values(row.observation)["reach"]
    elif sort == "interactions":
        value_for = lambda row: _raw_values(row.observation)["interactions"]
    else:
        value_for = lambda row: row.derived_metrics["interaction_rate_by_reach"]
    # ``rows`` já oferece desempate determinístico por publicação e ID.
    return tuple(sorted(rows, key=lambda row: (
        value_for(row) is None,
        -(value_for(row) if value_for(row) is not None else 0),
    )))


def build_instagram_dashboard(
    project: AnalyticsProject,
    period: str,
    timezone_name: str,
    *,
    sort: str = "recent",
    taxonomy_dimension_id: str | None = None,
    period_start: object = None,
    period_end: object = None,
    table_page: object = 1,
) -> InstagramDashboard:
    """Carrega agregados sem N+1 e nunca soma observações de uma mesma mídia."""
    if period not in TEMPORAL_PERIOD_KEYS:
        raise ValueError("Período inválido")
    if sort not in TABLE_SORTS:
        raise ValueError("Ordenação inválida")
    reference_at, rows = selected_media_rows(
        project, period, timezone_name, period_start=period_start, period_end=period_end,
    )
    metrics = []
    for field, label in METRICS:
        values = [getattr(row.observation, field) for row in rows if row.observation is not None]
        known_values = [value for value in values if value is not None]
        metrics.append(MetricSummary(
            field=field, label=label,
            value=sum(known_values) if known_values else None,
            known=len(known_values), publications=len(rows),
        ))
    # rows já está ordenado por publicação mais recente e ID; sort estável preserva o desempate.
    ranking = tuple(sorted(
        (row for row in rows if row.observation is not None and row.observation.reach is not None),
        key=lambda row: row.observation.reach, reverse=True,
    )[:5])
    followers_count, followers_at, followers_series = _followers(project.id)
    followers_delta = follower_variation(point.value for point in followers_series)
    performance_statistics = _performance_statistics(rows)
    type_comparisons = _type_comparisons(rows)
    try:
        taxonomy = taxonomy_dashboard_data(
            project,
            (
                TaxonomyMetricInput(
                    media_id=row.media.id,
                    raw_values=_raw_values(row.observation),
                    derived_values=row.derived_metrics,
                )
                for row in rows
            ),
            selected_dimension_id=taxonomy_dimension_id,
        )
    except TaxonomyValidationError as error:
        raise ValueError(str(error)) from error
    reach_rows = [row for row in rows if row.observation is not None and row.observation.reach is not None]
    interaction_rows = [row for row in rows if row.observation is not None
                        and row.observation.interactions is not None]
    chart_data = {
        "followers": {
            "labels": [format_local_datetime(point.observed_at, timezone_name) for point in followers_series],
            "values": [point.value for point in followers_series],
        },
        "reach": {
            "labels": [f"Publicação {row.ordinal}" for row in reach_rows],
            "dates": [format_local_datetime(row.media.published_at, timezone_name) for row in reach_rows],
            "values": [row.observation.reach for row in reach_rows],
        },
        "interactions": {
            "labels": [f"Publicação {row.ordinal}" for row in interaction_rows],
            "dates": [format_local_datetime(row.media.published_at, timezone_name) for row in interaction_rows],
            "values": [row.observation.interactions for row in interaction_rows],
        },
        "type_interaction_rate": {
            "labels": [f"{comparison.label} (n = {comparison.publications})" for comparison in type_comparisons
                       if comparison.interaction_rate_by_reach.median_value is not None],
            "values": [comparison.interaction_rate_by_reach.median_value for comparison in type_comparisons
                       if comparison.interaction_rate_by_reach.median_value is not None],
        },
        "taxonomy_interaction_rate": {
            "labels": [f"{comparison.name} (n = {comparison.publications})" for comparison in taxonomy.comparisons
                       if comparison.interaction_rate_by_reach.median_value is not None],
            "values": [comparison.interaction_rate_by_reach.median_value for comparison in taxonomy.comparisons
                       if comparison.interaction_rate_by_reach.median_value is not None],
            "horizontal": len(taxonomy.comparisons) > 5,
        },
    }
    table_pagination = paginate_sequence(_table_rows(rows, sort), table_page)
    return InstagramDashboard(
        reference_at=reference_at, period=period, rows=rows, metrics=tuple(metrics), ranking=ranking,
        followers_count=followers_count, followers_observed_at=followers_at,
        followers_series=followers_series, followers_variation=followers_delta,
        performance_statistics=performance_statistics, type_comparisons=type_comparisons,
        taxonomy=taxonomy,
        table_rows=table_pagination.items, table_pagination=table_pagination, sort=sort,
        derived_metrics_version=DERIVED_METRICS_VERSION, has_data=reference_at is not None,
        chart_data=chart_data,
    )
