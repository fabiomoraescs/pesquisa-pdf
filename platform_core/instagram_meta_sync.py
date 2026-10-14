"""Sincronização Meta para os modelos canônicos do Analytics Instagram.

CSV/XLSX e Meta API convergem em ``AnalyticsRun``, ``InstagramMedia``,
``InstagramMediaObservation`` e ``InstagramAccountSnapshot``. Este módulo não
persiste comentários: essa coleta pertence deliberadamente à Fase 10.2.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from calendar import monthrange
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from .extensions import db
from .instagram_meta import (
    INSTAGRAM_LOGIN_INSIGHTS_SCOPE,
    InstagramMetaError,
    InstagramMetaSettings,
    InstagramTokenCipher,
    log_meta_capability_failure,
    profile_identity,
)
from .models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccountSnapshot,
    InstagramApiConnection,
    InstagramMedia,
    InstagramMediaObservation,
    utcnow,
)


META_SYNC_VERSION = "instagram-meta-sync-v1"
META_SYNC_SOURCE_KIND = "meta_api"
SYNC_PERIOD_OPTIONS = (
    ("last_7_days", "Últimos 7 dias"),
    ("last_30_days", "Últimos 30 dias"),
    ("last_60_days", "Últimos 60 dias"),
    ("last_90_days", "Últimos 90 dias"),
    ("last_6_months", "Últimos 6 meses"),
    ("last_12_months", "Últimos 12 meses"),
    ("custom", "Período personalizado"),
    ("all_history", "Todo o histórico disponível"),
)
SYNC_PERIOD_PRESETS = frozenset(key for key, _label in SYNC_PERIOD_OPTIONS)
# O modelo atual não possui um campo canônico para ``views``. A métrica não é
# convertida para ``plays`` porque as duas semânticas não são equivalentes.
FACEBOOK_MEDIA_METRICS = {
    "reach": "reach",
    "saved": "saves",
    "shares": "shares",
    "total_interactions": "interactions",
    "follows": "follows_generated",
}
MEDIA_FIELDS = (
    "reach", "impressions", "plays", "likes", "comments_count", "shares", "saves",
    "interactions", "follows_generated",
)
ACCOUNT_FIELDS = (
    "followers_count", "new_followers", "account_reach", "profile_views", "website_clicks",
)
_PARTIAL_META_CATEGORIES = frozenset({
    "permission_denied", "unsupported_metric", "not_applicable", "rate_limited", "temporary", "temporary_error",
})


class InstagramMetaSyncError(ValueError):
    """Falha segura do motor, sem dados de resposta, URL ou credenciais."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class PublicationWindow:
    """Janela de ``published_at`` congelada na criação da sincronização.

    A janela não é o período semântico dos insights Meta nem dos snapshots da
    conta. Ela só define quais publicações entram nesta execução canônica.
    """

    preset: str
    published_from: datetime | None
    published_to: datetime | None
    timezone_name: str
    end_clamped_to_now: bool = False

    def includes(self, published_at: datetime | None) -> bool:
        if self.published_from is None and self.published_to is None:
            return True
        if published_at is None:
            return False
        return (
            (self.published_from is None or published_at >= self.published_from)
            and (self.published_to is None or published_at <= self.published_to)
        )

    def as_parameters(self) -> dict[str, object]:
        return {
            "sync_mode": "full",
            "period_preset": self.preset,
            "published_from": self.published_from.isoformat() if self.published_from else None,
            "published_to": self.published_to.isoformat() if self.published_to else None,
            "presentation_timezone": self.timezone_name,
            "published_to_clamped_to_now": self.end_clamped_to_now,
        }


def _subtract_months(day: date, months: int) -> date:
    absolute_month = day.year * 12 + (day.month - 1) - months
    year, month_index = divmod(absolute_month, 12)
    month = month_index + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _date_from_form(value: object, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise InstagramMetaSyncError("invalid_period", f"Informe {field} para o período personalizado.")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise InstagramMetaSyncError("invalid_period", f"{field.capitalize()} inválida.") from error


def _as_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise InstagramMetaSyncError("invalid_period", f"{field}: data sem timezone.")
    return value.astimezone(timezone.utc)


def _window_from_dates(
    *,
    preset: str,
    start_date: date,
    end_date: date,
    timezone_name: str,
    now: datetime,
) -> PublicationWindow:
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise InstagramMetaSyncError("invalid_period", "O timezone configurado para apresentação é inválido.") from error
    now_utc = _as_utc(now, "Agora")
    local_now = now_utc.astimezone(local_timezone)
    if start_date > end_date:
        raise InstagramMetaSyncError("invalid_period", "A data inicial não pode ser posterior à data final.")

    start = datetime.combine(start_date, datetime_time.min, tzinfo=local_timezone).astimezone(timezone.utc)
    end_of_requested_day = datetime.combine(end_date, datetime_time.max, tzinfo=local_timezone).astimezone(timezone.utc)
    end = min(end_of_requested_day, now_utc)
    if start > end:
        raise InstagramMetaSyncError("invalid_period", "O período selecionado ainda não possui um intervalo disponível.")
    return PublicationWindow(
        preset=preset,
        published_from=start,
        published_to=end,
        timezone_name=timezone_name,
        end_clamped_to_now=end_of_requested_day > now_utc,
    )


def resolve_publication_window(
    preset: object,
    published_from: object,
    published_to: object,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> PublicationWindow:
    """Resolve presets no enqueue, em limites UTC inclusivos e estáveis."""
    if not isinstance(preset, str) or preset not in SYNC_PERIOD_PRESETS:
        raise InstagramMetaSyncError("invalid_period", "Selecione um período de sincronização válido.")
    instant = now or utcnow()
    try:
        local_today = _as_utc(instant, "Agora").astimezone(ZoneInfo(timezone_name)).date()
    except ZoneInfoNotFoundError as error:
        raise InstagramMetaSyncError("invalid_period", "O timezone configurado para apresentação é inválido.") from error
    if preset == "all_history":
        return PublicationWindow(preset, None, None, timezone_name)
    if preset == "custom":
        return _window_from_dates(
            preset=preset,
            start_date=_date_from_form(published_from, "a data inicial"),
            end_date=_date_from_form(published_to, "a data final"),
            timezone_name=timezone_name,
            now=instant,
        )
    if preset == "last_7_days":
        start_date = local_today - timedelta(days=6)
    elif preset == "last_30_days":
        start_date = local_today - timedelta(days=29)
    elif preset == "last_60_days":
        start_date = local_today - timedelta(days=59)
    elif preset == "last_90_days":
        start_date = local_today - timedelta(days=89)
    elif preset == "last_6_months":
        start_date = _subtract_months(local_today, 6)
    else:
        start_date = _subtract_months(local_today, 12)
    return _window_from_dates(
        preset=preset,
        start_date=start_date,
        end_date=local_today,
        timezone_name=timezone_name,
        now=instant,
    )


def publication_window_from_run(run: AnalyticsRun) -> PublicationWindow | None:
    """Lê somente a janela já congelada; runs legados mantêm coleta integral."""
    parameters = run.parameters_json if isinstance(run.parameters_json, dict) else {}
    preset = parameters.get("period_preset")
    if preset is None:
        return None
    timezone_name = parameters.get("presentation_timezone")
    if not isinstance(preset, str) or preset not in SYNC_PERIOD_PRESETS or not isinstance(timezone_name, str):
        raise InstagramMetaSyncError("invalid_period", "A janela de publicações desta sincronização é inválida.")
    if preset == "all_history":
        return PublicationWindow(preset, None, None, timezone_name)
    raw_from, raw_to = parameters.get("published_from"), parameters.get("published_to")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        raise InstagramMetaSyncError("invalid_period", "A janela de publicações desta sincronização é inválida.")
    try:
        start = _as_utc(datetime.fromisoformat(raw_from.replace("Z", "+00:00")), "Data inicial")
        end = _as_utc(datetime.fromisoformat(raw_to.replace("Z", "+00:00")), "Data final")
    except ValueError as error:
        raise InstagramMetaSyncError("invalid_period", "A janela de publicações desta sincronização é inválida.") from error
    if start > end:
        raise InstagramMetaSyncError("invalid_period", "A janela de publicações desta sincronização é inválida.")
    return PublicationWindow(
        preset=preset,
        published_from=start,
        published_to=end,
        timezone_name=timezone_name,
        end_clamped_to_now=parameters.get("published_to_clamped_to_now") is True,
    )


def safe_meta_sync_error_message(error: InstagramMetaSyncError) -> str:
    messages = {
        "connection_unavailable": "Conecte uma conta profissional válida antes de sincronizar dados.",
        "identity_mismatch": "A conta conectada não corresponde à identidade oficial da conexão.",
        "invalid_period": "O período solicitado para esta sincronização é inválido.",
        "payload_invalid": "A API retornou dados de sincronização em formato inválido. Nenhum dado foi salvo.",
        "pagination_limit": "A sincronização excedeu o limite seguro de páginas. Nenhum dado foi salvo.",
    }
    return messages.get(error.category, "Não foi possível concluir a sincronização com a API do Instagram. Nenhum dado foi salvo.")


@dataclass(frozen=True)
class CanonicalMetaMedia:
    external_id: str
    permalink: str | None
    caption: str | None
    published_at: datetime | None
    media_type_raw: str | None
    metrics: dict[str, int | None]
    metric_statuses: dict[str, str]


@dataclass(frozen=True)
class MetaSyncResult:
    run_id: str
    account_id: str
    media_created: int
    media_reused: int
    observations_created: int
    snapshot_created: bool
    partial_metric_count: int


@dataclass
class _MetricCapabilityCache:
    """Conhecimento efêmero de métricas por provider e tipo de mídia.

    O cache nunca sai desta chamada de sincronização. ``unsupported`` só é
    aceito depois de uma resposta explicitamente classificada dessa forma; os
    demais erros continuam ``unknown`` e podem ser tentados numa mídia futura.
    """

    states: dict[tuple[str, str, str], str]

    def __init__(self) -> None:
        self.states = {}

    @staticmethod
    def _key(provider: str, media_type: str | None, metric: str) -> tuple[str, str, str]:
        return provider, (media_type or "unknown").upper(), metric

    def get(self, provider: str, media_type: str | None, metric: str) -> str:
        return self.states.get(self._key(provider, media_type, metric), "unknown")

    def set(self, provider: str, media_type: str | None, metric: str, status: str) -> None:
        self.states[self._key(provider, media_type, metric)] = status


def _optional_text(value: object, field: str, *, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InstagramMetaSyncError("payload_invalid", f"{field}: valor textual inválido.")
    value = value.strip()
    if not value:
        return None
    if len(value) > limit:
        raise InstagramMetaSyncError("payload_invalid", f"{field}: excede o limite permitido.")
    return value


def _required_external_id(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise InstagramMetaSyncError("payload_invalid", "A API não retornou o ID estável da mídia.")
    external_id = str(value).strip()
    if not external_id or len(external_id) > 128:
        raise InstagramMetaSyncError("payload_invalid", "A API não retornou o ID estável da mídia.")
    return external_id


def _utc_datetime(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InstagramMetaSyncError("payload_invalid", f"{field}: data inválida retornada pela API.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise InstagramMetaSyncError("payload_invalid", f"{field}: data inválida retornada pela API.") from error
    if parsed.tzinfo is None:
        raise InstagramMetaSyncError("payload_invalid", f"{field}: data sem timezone retornada pela API.")
    return parsed.astimezone(timezone.utc)


def _stored_utc(value: datetime | None) -> datetime | None:
    """SQLite pode devolver timestamps sem tzinfo, embora tenham sido gravados em UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _count(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InstagramMetaSyncError("payload_invalid", f"{field}: métrica inválida retornada pela API.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise InstagramMetaSyncError("payload_invalid", f"{field}: métrica inválida retornada pela API.")
    if number < 0:
        raise InstagramMetaSyncError("payload_invalid", f"{field}: métrica negativa retornada pela API.")
    return number


def _metric_values(
    payload: Mapping[str, Any], requested_metrics: tuple[str, ...]
) -> tuple[dict[str, int | None], set[str]]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise InstagramMetaSyncError("payload_invalid", "A API retornou uma métrica em formato inesperado.")
    requested = set(requested_metrics)
    results = {metric: None for metric in requested_metrics}
    observed: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise InstagramMetaSyncError("payload_invalid", "A API retornou uma métrica em formato inesperado.")
        metric = row.get("name")
        if not isinstance(metric, str) or metric not in requested:
            continue
        observed.add(metric)
        values = row.get("values")
        if not isinstance(values, list) or not values:
            continue
        value = values[-1]
        if not isinstance(value, Mapping):
            raise InstagramMetaSyncError("payload_invalid", "A API retornou o valor de métrica em formato inesperado.")
        results[metric] = _count(value.get("value"), metric)
    return results, observed


def _metric_value(payload: Mapping[str, Any], requested_metric: str) -> int | None:
    return _metric_values(payload, (requested_metric,))[0][requested_metric]


def _profile_count(profile: Mapping[str, Any], field: str) -> int | None:
    return _count(profile.get(field), field) if profile.get(field) is not None else None


def _optional_profile_enrichment(
    *,
    connection: InstagramApiConnection,
    client: Any,
    access_token: str,
) -> tuple[int | None, str]:
    """Lê somente atributos analíticos que não pertencem à identidade.

    ``profile_identity`` continua mínimo para não transformar uma contagem
    opcional em falha estrutural. O enriquecimento é uma capacidade separada:
    uma recusa da Meta mantém o snapshot válido e deixa o campo como NULL.
    """
    reader = getattr(client, "instagram_profile_enrichment", None)
    if not callable(reader):
        return None, "not_available_for_provider"
    try:
        payload = reader(connection.provider_account_external_id, access_token)
    except InstagramMetaError as error:
        log_meta_capability_failure(
            provider=connection.provider,
            probe="sync_profile_enrichment",
            metric="followers_count",
            error=error,
        )
        return None, error.category if _is_partial_metric_error(error) else "unknown"
    if not isinstance(payload, Mapping):
        raise InstagramMetaSyncError("payload_invalid", "A API retornou o perfil analítico em formato inesperado.")
    value = _profile_count(payload, "followers_count")
    return value, "supported" if value is not None else "not_observed"


def _is_partial_metric_error(error: InstagramMetaError) -> bool:
    return error.category in _PARTIAL_META_CATEGORIES


def _media_metadata(row: Mapping[str, Any]) -> tuple[str, str | None, str | None, datetime | None, str | None]:
    external_id = _required_external_id(row.get("id"))
    caption = _optional_text(row.get("caption"), "caption", limit=1_000_000)
    permalink = _optional_text(row.get("permalink"), "permalink", limit=512)
    published_at = _utc_datetime(row.get("timestamp"), "timestamp")
    media_type_raw = _canonical_media_type(row)
    return external_id, permalink, caption, published_at, media_type_raw


def _canonical_media_type(row: Mapping[str, Any]) -> str | None:
    """Traduz os dois campos Meta para o vocabulário existente do Analytics.

    ``media_product_type=FEED`` descreve o produto, não o tipo editorial da
    publicação. Por isso IMAGE/CAROUSEL_ALBUM/VIDEO continuam prevalecendo
    quando presentes, enquanto REELS mantém a classificação de Reel.
    """
    media_type = _optional_text(row.get("media_type"), "media_type", limit=40)
    product_type = _optional_text(row.get("media_product_type"), "media_product_type", limit=40)
    candidates = tuple(value.upper() for value in (media_type, product_type) if value)
    if any("REEL" in value for value in candidates):
        return "REEL"
    if any("CAROUSEL" in value for value in candidates):
        return "CAROUSEL_ALBUM"
    if any("IMAGE" in value for value in candidates):
        return "IMAGE"
    if any("VIDEO" in value for value in candidates):
        return "VIDEO"
    return product_type or media_type


def _record_metric_error(
    *, provider: str, cache: _MetricCapabilityCache, metric: str, media_type: str | None, error: InstagramMetaError
) -> str:
    """Registra uma falha parcial sem transformar falhas transitórias em cache."""
    if not _is_partial_metric_error(error):
        raise error
    log_meta_capability_failure(
        provider=provider, probe="sync_media_metric", metric=metric,
        media_type=media_type, error=error,
    )
    if error.category == "unsupported_metric":
        cache.set(provider, media_type, metric, "unsupported")
    return error.category


def _read_individual_metric(
    client: Any,
    media_id: str,
    access_token: str,
    media_type: str | None,
    metric: str,
    cache: _MetricCapabilityCache,
    *,
    provider: str,
) -> tuple[int | None, str]:
    try:
        payload = client.media_metric(media_id, metric, access_token)
        value = _metric_value(payload, metric)
    except InstagramMetaError as error:
        return None, _record_metric_error(
            provider=provider, cache=cache, metric=metric, media_type=media_type, error=error,
        )
    cache.set(provider, media_type, metric, "supported")
    return value, "supported" if value is not None else "not_observed"


def _read_supported_metric_batch(
    client: Any,
    media_id: str,
    access_token: str,
    media_type: str | None,
    metrics: tuple[str, ...],
    cache: _MetricCapabilityCache,
    *,
    provider: str,
) -> dict[str, tuple[int | None, str]]:
    """Lê em lote somente capacidades antes comprovadas para aquele tipo.

    Uma falha de lote por métrica inválida volta a sondar individualmente apenas
    esta mídia, para descobrir a responsável sem marcar as demais como
    incompatíveis. Falhas temporárias continuam parciais e não contaminam o
    cache da execução.
    """
    if not metrics:
        return {}
    try:
        payload = client.media_metrics(media_id, metrics, access_token)
        values, observed = _metric_values(payload, metrics)
    except AttributeError:
        # Clientes de teste/integrações antigas continuam corretos: só deixam de
        # aproveitar o lote, sem alterar semântica ou persistência.
        return {
            metric: _read_individual_metric(
                client, media_id, access_token, media_type, metric, cache, provider=provider,
            )
            for metric in metrics
        }
    except InstagramMetaError as error:
        if error.category != "unsupported_metric":
            status = _record_metric_error(
                provider=provider, cache=cache, metric=",".join(metrics), media_type=media_type, error=error,
            )
            return {metric: (None, status) for metric in metrics}
        # A resposta do lote não identifica qual métrica foi recusada. Sondar
        # cada uma uma única vez mantém as válidas e aprende a incompatível.
        return {
            metric: _read_individual_metric(
                client, media_id, access_token, media_type, metric, cache, provider=provider,
            )
            for metric in metrics
        }

    results: dict[str, tuple[int | None, str]] = {}
    for metric in metrics:
        if metric in observed:
            cache.set(provider, media_type, metric, "supported")
            value = values[metric]
            results[metric] = value, "supported" if value is not None else "not_observed"
        else:
            # Resposta parcial não prova incompatibilidade. A tentativa pontual
            # abaixo preserva métricas válidas e só aprende unsupported quando a
            # Meta o afirmar explicitamente.
            results[metric] = _read_individual_metric(
                client, media_id, access_token, media_type, metric, cache, provider=provider,
            )
    return results


def _provider_media_metrics(
    client: Any,
    media_id: str,
    access_token: str,
    media_type: str | None,
    cache: _MetricCapabilityCache,
    *,
    provider: str,
) -> tuple[dict[str, int | None], dict[str, str]]:
    values = {field: None for field in MEDIA_FIELDS}
    statuses: dict[str, str] = {}
    known_supported: list[str] = []
    unknown: list[str] = []
    for metric in FACEBOOK_MEDIA_METRICS:
        capability = cache.get(provider, media_type, metric)
        if capability == "unsupported":
            statuses[metric] = "unsupported_metric"
        elif capability == "supported":
            known_supported.append(metric)
        else:
            unknown.append(metric)

    # A primeira mídia de cada tipo descobre capacidades individualmente. Nas
    # seguintes, as métricas já confirmadas viajam em uma única chamada.
    batched = _read_supported_metric_batch(
        client, media_id, access_token, media_type, tuple(known_supported), cache, provider=provider,
    )
    individual = {
        metric: _read_individual_metric(
            client, media_id, access_token, media_type, metric, cache, provider=provider,
        )
        for metric in unknown
    }
    for metric, (value, status) in {**batched, **individual}.items():
        values[FACEBOOK_MEDIA_METRICS[metric]] = value
        statuses[metric] = status
    return values, statuses


def _account_snapshot_values(
    *, profile: Mapping[str, Any], connection: InstagramApiConnection, client: Any, access_token: str
) -> tuple[dict[str, int | None], dict[str, str]]:
    values = {field: None for field in ACCOUNT_FIELDS}
    statuses: dict[str, str] = {}
    # Instagram Login pode devolver seguidores junto ao perfil; o dado continua
    # opcional e nunca é inferido quando o provider não o retorna.
    values["followers_count"] = _profile_count(profile, "followers_count")
    if values["followers_count"] is not None:
        statuses["followers_count"] = "supported"
    else:
        values["followers_count"], statuses["followers_count"] = _optional_profile_enrichment(
            connection=connection, client=client, access_token=access_token,
        )

    if connection.provider not in {"facebook_login", "instagram_login"}:
        return values, statuses
    if (
        connection.provider == "instagram_login"
        and INSTAGRAM_LOGIN_INSIGHTS_SCOPE not in set(connection.scopes_json or [])
    ):
        # Credenciais anteriores continuam aptas à sincronização básica. Sem o
        # escopo novo, não há chamada de Insights nem falso unsupported.
        statuses["account_reach"] = "not_requested"
        return values, statuses
    try:
        payload = client.account_insights(connection.provider_account_external_id, access_token)
        values["account_reach"] = _metric_value(payload, "reach")
        statuses["account_reach"] = "supported" if values["account_reach"] is not None else "not_observed"
    except InstagramMetaError as error:
        if not _is_partial_metric_error(error):
            raise
        log_meta_capability_failure(
            provider=connection.provider, probe="sync_account_insights", metric="reach", error=error,
        )
        statuses["account_reach"] = error.category
    return values, statuses


def _prepare_media(
    connection: InstagramApiConnection,
    client: Any,
    access_token: str,
    metric_capabilities: _MetricCapabilityCache,
    publication_window: PublicationWindow | None,
) -> tuple[Mapping[str, Any], list[CanonicalMetaMedia], dict[str, int | None], dict[str, str]]:
    provider_account_id = str(connection.provider_account_external_id or "").strip()
    if not provider_account_id:
        raise InstagramMetaSyncError("connection_unavailable", "A conexão não possui a identidade oficial Meta.")
    try:
        profile = client.instagram_profile(provider_account_id, access_token)
        returned_id, _, _, _ = profile_identity(profile)
    except InstagramMetaError:
        raise
    if returned_id != provider_account_id:
        raise InstagramMetaSyncError("identity_mismatch", "A identidade retornada não corresponde à conexão.")

    try:
        raw_media = client.list_media(provider_account_id, access_token)
    except AttributeError as error:
        raise InstagramMetaSyncError("connection_unavailable", "O provider não suporta sincronização de mídias.") from error
    if not isinstance(raw_media, list):
        raise InstagramMetaSyncError("payload_invalid", "A API retornou mídias em formato inesperado.")

    prepared: list[CanonicalMetaMedia] = []
    seen_ids: set[str] = set()
    for raw_row in raw_media:
        if not isinstance(raw_row, Mapping):
            raise InstagramMetaSyncError("payload_invalid", "A API retornou uma mídia em formato inesperado.")
        media_id, permalink, caption, published_at, media_type = _media_metadata(raw_row)
        if media_id in seen_ids:
            raise InstagramMetaSyncError("payload_invalid", "A API retornou uma mídia repetida na mesma sincronização.")
        seen_ids.add(media_id)
        if publication_window is not None and not publication_window.includes(published_at):
            continue
        if connection.provider in {"facebook_login", "instagram_login"}:
            if (
                connection.provider == "instagram_login"
                and INSTAGRAM_LOGIN_INSIGHTS_SCOPE not in set(connection.scopes_json or [])
            ):
                metrics = {field: None for field in MEDIA_FIELDS}
                statuses = {metric: "not_requested" for metric in FACEBOOK_MEDIA_METRICS}
            else:
                metrics, statuses = _provider_media_metrics(
                    client, media_id, access_token, media_type, metric_capabilities,
                    provider=connection.provider,
                )
            # Estes campos são fornecidos diretamente no objeto de mídia quando
            # presentes. Ausência continua NULL; zero é preservado.
            metrics["likes"] = _count(raw_row.get("like_count"), "like_count") if raw_row.get("like_count") is not None else None
            metrics["comments_count"] = _count(raw_row.get("comments_count"), "comments_count") if raw_row.get("comments_count") is not None else None
        else:
            # Providers futuros continuam podendo fornecer somente os
            # contadores básicos que tenham equivalência canônica direta.
            metrics = {field: None for field in MEDIA_FIELDS}
            metrics["likes"] = _count(raw_row.get("like_count"), "like_count") if raw_row.get("like_count") is not None else None
            metrics["comments_count"] = _count(raw_row.get("comments_count"), "comments_count") if raw_row.get("comments_count") is not None else None
            statuses = {
                "likes": "supported" if metrics["likes"] is not None else "not_observed",
                "comments_count": "supported" if metrics["comments_count"] is not None else "not_observed",
            }
        prepared.append(CanonicalMetaMedia(
            external_id=media_id,
            permalink=permalink,
            caption=caption,
            published_at=published_at,
            media_type_raw=media_type,
            metrics=metrics,
            metric_statuses=statuses,
        ))
    snapshot_values, snapshot_statuses = _account_snapshot_values(
        profile=profile, connection=connection, client=client, access_token=access_token,
    )
    return profile, prepared, snapshot_values, snapshot_statuses


def _update_media_from_meta(media: InstagramMedia, item: CanonicalMetaMedia, observed_at: datetime) -> None:
    """Atualiza somente metadados objetivos oficiais, sem apagar informação.

    ``caption_original`` preserva a evidência inicial: a API apenas completa um
    campo vazio. Publicação, tipo e permalink são metadados objetivos e podem
    ser atualizados pela fonte oficial quando ela os fornece.
    """
    if item.permalink is not None:
        media.permalink = item.permalink
    if item.published_at is not None:
        media.published_at = item.published_at
    if item.media_type_raw is not None:
        media.media_type_raw = item.media_type_raw
    if not media.caption_original and item.caption:
        media.caption_original = item.caption
    first_seen_at = _stored_utc(media.first_seen_at)
    last_seen_at = _stored_utc(media.last_seen_at)
    if first_seen_at is None or observed_at < first_seen_at:
        media.first_seen_at = observed_at
    if last_seen_at is None or observed_at > last_seen_at:
        media.last_seen_at = observed_at


def sync_meta_connection(
    project: AnalyticsProject,
    connection: InstagramApiConnection,
    settings: InstagramMetaSettings,
    client: Any,
    *,
    run: AnalyticsRun | None = None,
) -> MetaSyncResult:
    """Lê a API e persiste uma execução canônica de modo transacional.

    ``observed_at`` representa o momento do snapshot da sincronização. Os
    insights Meta misturam valores lifetime e periodizados; sem um intervalo
    comum verificável, ``period_start``/``period_end`` permanecem NULL em vez
    de inventar um período que os dados não afirmam.
    """
    is_durable_run = run is not None
    account = project.instagram_account
    if account is None or account.api_connection is None or account.api_connection.id != connection.id:
        raise InstagramMetaSyncError("connection_unavailable", "O projeto não possui a conexão solicitada.")
    if connection.status != "connected":
        raise InstagramMetaSyncError("connection_unavailable", "A conexão não está ativa.")
    if connection.provider not in {"facebook_login", "instagram_login"}:
        raise InstagramMetaSyncError("connection_unavailable", "O método de conexão não é suportado.")

    try:
        access_token = InstagramTokenCipher(settings.token_encryption_key).decrypt(connection.encrypted_access_token)
        observed_at = utcnow()
        publication_window = publication_window_from_run(run) if run is not None else None
        _, media_items, snapshot_values, snapshot_statuses = _prepare_media(
            connection, client, access_token, _MetricCapabilityCache(), publication_window,
        )

        existing_media = {
            media.external_id: media
            for media in db.session.scalars(
                select(InstagramMedia).where(InstagramMedia.instagram_account_id == account.id)
            )
            if media.external_id is not None
        }
        partial_metric_count = sum(
            1
            for item in media_items
            for status in item.metric_statuses.values()
            if status not in {"supported", "not_observed"}
        ) + sum(
            1 for status in snapshot_statuses.values() if status not in {"supported", "not_observed"}
        )
        if run is None:
            run = AnalyticsRun(
                analytics_project=project,
                source_kind=META_SYNC_SOURCE_KIND,
                status="completed",
                period_start=None,
                period_end=None,
                started_at=observed_at,
                completed_at=utcnow(),
                processor_version=META_SYNC_VERSION,
                record_count=len(media_items),
            )
            db.session.add(run)
        elif (
            run.analytics_project_id != project.id
            or run.source_kind != META_SYNC_SOURCE_KIND
            or run.processor_version != META_SYNC_VERSION
            or run.status != "running"
        ):
            raise InstagramMetaSyncError("connection_unavailable", "A execução de sincronização não está disponível.")

        # Em execução durável, o run já foi confirmado como ``running`` pelo
        # worker. A transação canônica abaixo é a única que pode promovê-lo a
        # ``completed`` junto com mídias, observações e snapshot.
        run.status = "completed"
        run.period_start = None
        run.period_end = None
        run.completed_at = utcnow()
        existing_parameters = dict(run.parameters_json) if isinstance(run.parameters_json, dict) else {}
        existing_parameters.update({
            "schema_version": META_SYNC_VERSION,
            "provider": connection.provider,
            "execution_mode": "durable_worker" if is_durable_run else "inline",
            "media_count": len(media_items),
            "metric_statuses": {
                "media": {item.external_id: item.metric_statuses for item in media_items if item.metric_statuses},
                "account": snapshot_statuses,
            },
            "unmapped_provider_metrics": ["views"] if connection.provider == "facebook_login" else [],
        })
        run.parameters_json = existing_parameters
        run.record_count = len(media_items)
        db.session.flush()

        created = reused = 0
        for item in media_items:
            media = existing_media.get(item.external_id)
            if media is None:
                media = InstagramMedia(
                    instagram_account=account,
                    external_id=item.external_id,
                    permalink=item.permalink,
                    caption_original=item.caption or "",
                    published_at=item.published_at,
                    media_type_raw=item.media_type_raw,
                    first_seen_at=observed_at,
                    last_seen_at=observed_at,
                )
                db.session.add(media)
                existing_media[item.external_id] = media
                created += 1
            else:
                _update_media_from_meta(media, item, observed_at)
                reused += 1
            db.session.add(InstagramMediaObservation(
                instagram_media=media,
                analytics_run=run,
                observed_at=observed_at,
                period_start=None,
                period_end=None,
                **item.metrics,
            ))

        db.session.add(InstagramAccountSnapshot(
            instagram_account=account,
            analytics_run=run,
            observed_at=observed_at,
            period_start=None,
            period_end=None,
            **snapshot_values,
        ))
        db.session.flush()
        result = MetaSyncResult(
            run_id=run.id,
            account_id=account.id,
            media_created=created,
            media_reused=reused,
            observations_created=len(media_items),
            snapshot_created=True,
            partial_metric_count=partial_metric_count,
        )
        db.session.commit()
        return result
    except Exception:
        db.session.rollback()
        raise
