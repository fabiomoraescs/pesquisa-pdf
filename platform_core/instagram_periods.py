"""Vocabulário e resolução compartilhados dos recortes temporais Instagram.

O mesmo seletor é usado para consultas locais (Dashboard, Comentários e
Insights), relatórios e sincronização. Cada consumidor mantém sua própria
semântica — por exemplo, sincronização congela a janela no enqueue —, mas as
opções, labels e validações de datas não divergem entre telas.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TEMPORAL_PERIOD_OPTIONS = (
    ("7d", "Últimos 7 dias"),
    ("30d", "Últimos 30 dias"),
    ("60d", "Últimos 60 dias"),
    ("90d", "Últimos 90 dias"),
    ("6m", "Últimos 6 meses"),
    ("12m", "Últimos 12 meses"),
    ("custom", "Período personalizado"),
    ("all", "Todo o histórico disponível"),
)
TEMPORAL_PERIOD_KEYS = frozenset(key for key, _label in TEMPORAL_PERIOD_OPTIONS)
TEMPORAL_PERIOD_LABELS = dict(TEMPORAL_PERIOD_OPTIONS)


class InstagramPeriodError(ValueError):
    """Erro de formulário sem expor detalhes técnicos."""


@dataclass(frozen=True)
class LocalPeriod:
    key: str
    start: datetime | None
    end: datetime | None
    timezone_name: str


@dataclass(frozen=True)
class EffectiveRunPeriod:
    """Janela analítica já registrada por uma execução, sem reescrever o run.

    Execuções tradicionais guardam a janela nas colunas do ``AnalyticsRun``.
    A sincronização Meta durável a congela em ``parameters_json`` para não
    inventar ou alterar o período depois do processamento.  Esta estrutura
    oferece aos consumidores uma única leitura desses dois contratos e mantém
    explícitos tanto todo o histórico quanto legados sem período conhecido.
    """

    kind: Literal["bounded", "all_history", "unknown"]
    start: datetime | None = None
    end: datetime | None = None
    source: Literal["columns", "meta_parameters", "meta_all_history", "unknown"] = "unknown"
    preset: str | None = None
    timezone_name: str | None = None

    @property
    def is_bounded(self) -> bool:
        return self.kind == "bounded" and self.start is not None and self.end is not None


def _as_utc_datetime(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def effective_run_period(run: object) -> EffectiveRunPeriod:
    """Resolve a semântica temporal de um ``AnalyticsRun`` sem persistir nada.

    Colunas válidas são canônicas para importações tradicionais.  Runs Meta
    usam a janela publicada congelada; ``all_history`` é deliberadamente
    ilimitado e um legado sem informação permanece desconhecido.
    """

    start = _as_utc_datetime(getattr(run, "period_start", None))
    end = _as_utc_datetime(getattr(run, "period_end", None))
    if start is not None and end is not None and start <= end:
        return EffectiveRunPeriod("bounded", start, end, "columns")

    parameters = getattr(run, "parameters_json", None)
    if getattr(run, "source_kind", None) != "meta_api" or not isinstance(parameters, dict):
        return EffectiveRunPeriod("unknown")
    preset = parameters.get("period_preset")
    timezone_name = parameters.get("presentation_timezone")
    timezone_value = timezone_name if isinstance(timezone_name, str) else None
    if preset == "all_history":
        return EffectiveRunPeriod("all_history", None, None, "meta_all_history", preset, timezone_value)
    start = _as_utc_datetime(parameters.get("published_from"))
    end = _as_utc_datetime(parameters.get("published_to"))
    if start is not None and end is not None and start <= end:
        return EffectiveRunPeriod("bounded", start, end, "meta_parameters", preset if isinstance(preset, str) else None, timezone_value)
    return EffectiveRunPeriod("unknown", preset=preset if isinstance(preset, str) else None, timezone_name=timezone_value)


def _subtract_months(day: date, months: int) -> date:
    absolute_month = day.year * 12 + day.month - 1 - months
    year, month_index = divmod(absolute_month, 12)
    month = month_index + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _parse_date(value: object, label: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise InstagramPeriodError(f"Informe {label} para o período personalizado.")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise InstagramPeriodError(f"{label.capitalize()} inválida.") from error


def form_period(values: dict | None = None, *, default: str = "30d") -> dict[str, str]:
    """Normaliza o estado do formulário sem transformar entradas em datas."""
    values = values or {}
    key = values.get("period", default)
    key = key if isinstance(key, str) and key in TEMPORAL_PERIOD_KEYS else default
    return {
        "key": key,
        "start": str(values.get("period_start", "") or ""),
        "end": str(values.get("period_end", "") or ""),
    }


def resolve_local_period(
    period: str,
    timezone_name: str,
    *,
    reference_at: datetime | None,
    period_start: object = None,
    period_end: object = None,
) -> LocalPeriod:
    """Converte um preset em limites UTC inclusivos para dados já armazenados.

    ``reference_at`` é a referência factual da tela, não o relógio do navegador.
    Assim Dashboard/Comentários não passam a mostrar dados futuros apenas porque
    a página foi aberta em outro momento.
    """
    if period not in TEMPORAL_PERIOD_KEYS:
        raise InstagramPeriodError("Selecione um período válido.")
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise InstagramPeriodError("O timezone configurado é inválido.") from error

    if period == "all":
        return LocalPeriod(period, None, reference_at, timezone_name)
    if period == "custom":
        start_day = _parse_date(period_start, "a data inicial")
        end_day = _parse_date(period_end, "a data final")
        if start_day > end_day:
            raise InstagramPeriodError("A data inicial não pode ser posterior à data final.")
        return LocalPeriod(
            period,
            datetime.combine(start_day, time.min, tzinfo=local_timezone).astimezone(timezone.utc),
            datetime.combine(end_day, time.max, tzinfo=local_timezone).astimezone(timezone.utc),
            timezone_name,
        )
    if reference_at is None:
        return LocalPeriod(period, None, None, timezone_name)
    if reference_at.tzinfo is None:
        reference_at = reference_at.replace(tzinfo=timezone.utc)
    end = reference_at.astimezone(timezone.utc)
    local_end = end.astimezone(local_timezone)
    if period in {"7d", "30d", "60d", "90d"}:
        # Presets de dias são datas-calendário locais inclusivas.  A mesma
        # semântica é congelada pela sincronização Meta, evitando que o
        # Dashboard/Insights incluam uma tarde extra do dia anterior.
        days = int(period.removesuffix("d"))
        start_local = datetime.combine(local_end.date() - timedelta(days=days - 1), time.min, tzinfo=local_timezone)
    else:
        months = 6 if period == "6m" else 12
        start_day = _subtract_months(local_end.date(), months)
        start_local = datetime.combine(start_day, time.min, tzinfo=local_timezone)
    return LocalPeriod(period, start_local.astimezone(timezone.utc), end, timezone_name)


def period_label(period: str) -> str:
    try:
        return TEMPORAL_PERIOD_LABELS[period]
    except KeyError as error:
        raise InstagramPeriodError("Selecione um período válido.") from error


def resolve_calendar_dates(
    period: str,
    timezone_name: str,
    *,
    period_start: object = None,
    period_end: object = None,
    now: datetime | None = None,
) -> tuple[date, date] | None:
    """Resolve presets para um intervalo de datas congelável, como Relatórios.

    ``None`` para todo o histórico permite que o consumidor use somente o
    intervalo efetivamente elegível, em vez de inventar uma data inicial.
    """
    if period not in TEMPORAL_PERIOD_KEYS:
        raise InstagramPeriodError("Selecione um período válido.")
    if period == "all":
        return None
    if period == "custom":
        start_day = _parse_date(period_start, "a data inicial")
        end_day = _parse_date(period_end, "a data final")
        if start_day > end_day:
            raise InstagramPeriodError("A data inicial não pode ser posterior à data final.")
        return start_day, end_day
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise InstagramPeriodError("O timezone configurado é inválido.") from error
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    end_day = reference.astimezone(local_timezone).date()
    if period.endswith("d"):
        start_day = end_day - timedelta(days=int(period.removesuffix("d")) - 1)
    else:
        start_day = _subtract_months(end_day, 6 if period == "6m" else 12)
    return start_day, end_day


__all__ = [
    "EffectiveRunPeriod", "InstagramPeriodError", "LocalPeriod", "TEMPORAL_PERIOD_KEYS",
    "TEMPORAL_PERIOD_LABELS", "TEMPORAL_PERIOD_OPTIONS", "form_period",
    "effective_run_period", "period_label", "resolve_calendar_dates", "resolve_local_period",
]
