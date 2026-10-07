"""Consultas e classificações manuais para Comentários e recepção.

Todas as decisões de estado efetivo ficam aqui para que listas, detalhe e
resumos usem a mesma regra determinística de histórico.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .extensions import db
from .models import (
    AnalyticsProject,
    InstagramAccount,
    InstagramComment,
    InstagramCommentAssessment,
    InstagramCommentObservation,
    InstagramCommentTextVersion,
    InstagramMedia,
    utcnow,
)


COMMENT_PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "all": None}
COMMENT_STATUSES = ("all", "unclassified", "classified")
COMMENT_SENTIMENTS = ("all", "positive", "neutral", "negative", "mixed")
COMMENT_NEEDS_RESPONSE = ("all", "yes", "no", "unknown")
COMMENT_RISKS = ("all", "none", "low", "medium", "high")
COMMENT_SORTS = ("recent", "oldest", "likes", "risk")

SENTIMENT_LABELS = {"positive": "Positivo", "neutral": "Neutro", "negative": "Negativo", "mixed": "Misto"}
INTENT_LABELS = {
    "praise": "Elogio", "question": "Pergunta", "request": "Pedido", "complaint": "Reclamação",
    "criticism": "Crítica", "information": "Informação", "spam": "Spam", "other": "Outro",
}
INTENSITY_LABELS = {"low": "Baixa", "medium": "Média", "high": "Alta"}
RISK_LABELS = {"none": "Nenhum", "low": "Baixo", "medium": "Médio", "high": "Alto"}
ASSESSMENT_SOURCE_LABELS = {
    "automatic": "Classificação automática",
    "manual": "Classificação humana",
    "human_review": "Revisão humana",
    "imported": "Avaliação importada",
}
HUMAN_ASSESSMENT_SOURCES = frozenset({"manual", "human_review"})


class CommentAssessmentValidationError(ValueError):
    """Entrada humana inválida para uma avaliação manual de comentário."""


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _ranked(values: Iterable, *, key) -> object | None:
    values = tuple(values)
    if not values:
        return None
    return max(values, key=key)


def effective_assessment(assessments: Iterable[InstagramCommentAssessment]) -> InstagramCommentAssessment | None:
    """Retorna a avaliação atual, preservando a prioridade da revisão humana.

    Avaliações automáticas são evidência histórica útil, mas uma reexecução do
    classificador não pode substituir silenciosamente uma decisão humana que
    ainda esteja vigente. Entre avaliações da mesma natureza, a mais recente
    vence de forma determinística.
    """
    current = [item for item in assessments if item.superseded_at is None]
    human = [item for item in current if item.source in HUMAN_ASSESSMENT_SOURCES]
    return _ranked(human or current, key=lambda item: (_utc(item.created_at), item.id))


def current_text_version(versions: Iterable[InstagramCommentTextVersion]) -> InstagramCommentTextVersion | None:
    return _ranked(versions, key=lambda item: (_utc(item.first_observed_at), _utc(item.created_at), item.id))


def current_comment_observation(observations: Iterable[InstagramCommentObservation]) -> InstagramCommentObservation | None:
    return _ranked(observations, key=lambda item: (_utc(item.observed_at), item.id))


def _choice(value: str | None, allowed: dict[str, str], field: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if value not in allowed:
        raise CommentAssessmentValidationError(f"Informe um valor válido para {field}.")
    return value


def _optional_text(value: str | None, field: str, *, limit: int) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > limit:
        raise CommentAssessmentValidationError(f"{field} excede o limite de {limit} caracteres.")
    return text


def _needs_response(value: str | None) -> bool | None:
    normalized = (value or "").strip()
    if normalized in {"", "unknown"}:
        return None
    if normalized == "yes":
        return True
    if normalized == "no":
        return False
    raise CommentAssessmentValidationError("Informe uma decisão válida sobre necessidade de resposta.")


def classify_comment(
    comment: InstagramComment,
    reviewer_user_id: str,
    values: dict[str, str | None],
) -> InstagramCommentAssessment:
    """Registra revisão humana sem perder a avaliação efetiva anterior."""
    version = current_text_version(comment.text_versions)
    if version is None:
        raise CommentAssessmentValidationError("O comentário não possui uma versão textual classificável.")
    current = effective_assessment(comment.assessments)
    sentiment = _choice(values.get("sentiment"), SENTIMENT_LABELS, "sentimento")
    intensity = _choice(values.get("intensity"), INTENSITY_LABELS, "intensidade")
    intent = _choice(values.get("intent"), INTENT_LABELS, "intenção")
    risk = _choice(values.get("risk"), RISK_LABELS, "risco")
    topic = _optional_text(values.get("topic"), "Tema", limit=200)
    topic_normalized = _optional_text(values.get("topic_normalized"), "Tema normalizado", limit=200)
    rationale = _optional_text(values.get("rationale"), "Justificativa", limit=2_000)
    needs_response = _needs_response(values.get("needs_response"))
    if all(value is None for value in (
        sentiment, intensity, intent, topic, topic_normalized, needs_response, risk, rationale,
    )):
        raise CommentAssessmentValidationError("Informe ao menos uma classificação ou justificativa para o comentário.")
    if current is not None:
        current.superseded_at = utcnow()
    assessment = InstagramCommentAssessment(
        instagram_comment=comment,
        text_version=version,
        supersedes=current,
        source="human_review" if current is not None else "manual",
        reviewer_user_id=reviewer_user_id,
        sentiment=sentiment,
        intensity=intensity,
        intent=intent,
        topic=topic,
        topic_normalized=topic_normalized,
        needs_response=needs_response,
        risk=risk,
        rationale=rationale,
    )
    db.session.add(assessment)
    return assessment


def _safe_permalink(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


@dataclass(frozen=True)
class CommentRow:
    comment: InstagramComment
    media: InstagramMedia
    text_version: InstagramCommentTextVersion | None
    observation: InstagramCommentObservation | None
    assessment: InstagramCommentAssessment | None

    @property
    def text(self) -> str:
        return (self.text_version.text if self.text_version else self.comment.original_text)

    @property
    def short_text(self) -> str:
        return self.text if len(self.text) <= 160 else f"{self.text[:157]}…"

    @property
    def permalink(self) -> str | None:
        return _safe_permalink(self.media.permalink)

    @property
    def status(self) -> str:
        return "classified" if self.assessment else "unclassified"


@dataclass(frozen=True)
class ReceptionMetrics:
    imported: int
    classified: int
    automatically_classified: int
    needs_response: int
    medium_or_high_risk: int
    known_likes: int
    known_replies: int

    @property
    def coverage_percentage(self) -> float | None:
        return None if self.imported == 0 else self.classified / self.imported * 100


@dataclass(frozen=True)
class InstagramCommentsDashboard:
    reference_at: datetime | None
    period: str
    sort: str
    rows: tuple[CommentRow, ...]
    all_rows: tuple[CommentRow, ...]
    metrics: ReceptionMetrics
    chart_data: dict


def _reference_at(project_id: str) -> datetime | None:
    return db.session.scalar(
        select(func.max(InstagramCommentObservation.observed_at))
        .join(InstagramComment, InstagramComment.id == InstagramCommentObservation.instagram_comment_id)
        .join(InstagramMedia, InstagramMedia.id == InstagramComment.instagram_media_id)
        .join(InstagramAccount, InstagramAccount.id == InstagramMedia.instagram_account_id)
        .where(InstagramAccount.analytics_project_id == project_id)
    )


def _all_rows(project_id: str) -> tuple[CommentRow, ...]:
    comments = db.session.scalars(
        select(InstagramComment)
        .join(InstagramMedia, InstagramMedia.id == InstagramComment.instagram_media_id)
        .join(InstagramAccount, InstagramAccount.id == InstagramMedia.instagram_account_id)
        .where(InstagramAccount.analytics_project_id == project_id)
        .options(
            selectinload(InstagramComment.instagram_media),
            selectinload(InstagramComment.text_versions),
            selectinload(InstagramComment.observations),
            selectinload(InstagramComment.assessments),
        )
    ).all()
    return tuple(CommentRow(
        comment=comment,
        media=comment.instagram_media,
        text_version=current_text_version(comment.text_versions),
        observation=current_comment_observation(comment.observations),
        assessment=effective_assessment(comment.assessments),
    ) for comment in comments)


def _counter_chart(rows: Iterable[CommentRow], field: str, labels: dict[str, str], *, include_unassessed: bool = True) -> dict:
    counter = Counter(
        getattr(row.assessment, field)
        for row in rows
        if row.assessment is not None and getattr(row.assessment, field) is not None
    )
    ordered = [key for key in labels if counter[key]]
    chart_labels = [labels[key] for key in ordered]
    chart_values = [counter[key] for key in ordered]
    if include_unassessed:
        missing = sum(row.assessment is None or getattr(row.assessment, field) is None for row in rows)
        if missing:
            chart_labels.append("Não avaliado")
            chart_values.append(missing)
    return {"labels": chart_labels, "values": chart_values}


def _needs_response_chart(rows: Iterable[CommentRow]) -> dict:
    counter = Counter(
        "Sim" if row.assessment.needs_response else "Não"
        for row in rows
        if row.assessment is not None and row.assessment.needs_response is not None
    )
    unknown = sum(row.assessment is None or row.assessment.needs_response is None for row in rows)
    labels = ["Sim", "Não"]
    values = [counter["Sim"], counter["Não"]]
    if unknown:
        labels.append("Não avaliado")
        values.append(unknown)
    return {"labels": labels, "values": values}


def _sort_rows(rows: list[CommentRow], sort: str) -> list[CommentRow]:
    risk_rank = {None: -1, "none": 0, "low": 1, "medium": 2, "high": 3}
    if sort == "oldest":
        return sorted(rows, key=lambda row: (_utc(row.comment.published_at), row.comment.id))
    if sort == "likes":
        return sorted(rows, key=lambda row: (row.observation.like_count if row.observation and row.observation.like_count is not None else -1,
                                              _utc(row.comment.published_at), row.comment.id), reverse=True)
    if sort == "risk":
        return sorted(rows, key=lambda row: (risk_rank[row.assessment.risk] if row.assessment else -1,
                                              _utc(row.comment.published_at), row.comment.id), reverse=True)
    return sorted(rows, key=lambda row: (_utc(row.comment.published_at), row.comment.id), reverse=True)


def build_instagram_comments_dashboard(
    project: AnalyticsProject,
    *, period: str, sort: str, status: str = "all", sentiment: str = "all",
    needs_response: str = "all", risk: str = "all",
) -> InstagramCommentsDashboard:
    if (period not in COMMENT_PERIOD_DAYS or sort not in COMMENT_SORTS or status not in COMMENT_STATUSES
            or sentiment not in COMMENT_SENTIMENTS or needs_response not in COMMENT_NEEDS_RESPONSE
            or risk not in COMMENT_RISKS):
        raise ValueError("Filtro de comentários inválido.")
    reference_at = _utc(_reference_at(project.id))
    rows = list(_all_rows(project.id))
    if reference_at is not None and COMMENT_PERIOD_DAYS[period] is not None:
        lower = reference_at - timedelta(days=COMMENT_PERIOD_DAYS[period])
        rows = [row for row in rows if _utc(row.comment.published_at) >= lower]
    unfiltered = tuple(rows)
    if status != "all":
        rows = [row for row in rows if row.status == status]
    if sentiment != "all":
        rows = [row for row in rows if row.assessment and row.assessment.sentiment == sentiment]
    if needs_response == "yes":
        rows = [row for row in rows if row.assessment and row.assessment.needs_response is True]
    elif needs_response == "no":
        rows = [row for row in rows if row.assessment and row.assessment.needs_response is False]
    elif needs_response == "unknown":
        rows = [row for row in rows if row.assessment is None or row.assessment.needs_response is None]
    if risk != "all":
        rows = [row for row in rows if row.assessment and row.assessment.risk == risk]
    metric_source = unfiltered
    metrics = ReceptionMetrics(
        imported=len(metric_source),
        classified=sum(row.assessment is not None for row in metric_source),
        automatically_classified=sum(
            any(item.source == "automatic" for item in row.comment.assessments)
            for row in metric_source
        ),
        needs_response=sum(row.assessment is not None and row.assessment.needs_response is True for row in metric_source),
        medium_or_high_risk=sum(
            row.assessment is not None and row.assessment.risk in {"medium", "high"} for row in metric_source
        ),
        known_likes=sum(row.observation is not None and row.observation.like_count is not None for row in metric_source),
        known_replies=sum(row.observation is not None and row.observation.replies_count is not None for row in metric_source),
    )
    chart_rows = tuple(rows)
    return InstagramCommentsDashboard(
        reference_at=reference_at,
        period=period,
        sort=sort,
        rows=tuple(_sort_rows(rows, sort)),
        all_rows=unfiltered,
        metrics=metrics,
        chart_data={
            "sentiment": _counter_chart(chart_rows, "sentiment", SENTIMENT_LABELS),
            "intent": _counter_chart(chart_rows, "intent", INTENT_LABELS),
            "risk": _counter_chart(chart_rows, "risk", RISK_LABELS),
            "needs_response": _needs_response_chart(chart_rows),
        },
    )


__all__ = [
    "ASSESSMENT_SOURCE_LABELS",
    "COMMENT_NEEDS_RESPONSE", "COMMENT_PERIOD_DAYS", "COMMENT_RISKS", "COMMENT_SENTIMENTS", "COMMENT_SORTS",
    "COMMENT_STATUSES", "CommentAssessmentValidationError", "HUMAN_ASSESSMENT_SOURCES", "INTENSITY_LABELS", "INTENT_LABELS", "RISK_LABELS",
    "SENTIMENT_LABELS", "build_instagram_comments_dashboard", "classify_comment", "current_comment_observation",
    "current_text_version", "effective_assessment",
]
