"""Classificação automática local, rastreável e substituível de comentários.

Este módulo não interpreta comentários com IA e não chama serviços externos.
Ele define o contrato para classificadores futuros e fornece uma implementação
determinística pequena, baseada em regras explícitas, para validar o fluxo de
histórico do domínio.
"""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
import re
import unicodedata
from typing import Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .extensions import db
from .instagram_comments import (
    HUMAN_ASSESSMENT_SOURCES,
    INTENSITY_LABELS,
    INTENT_LABELS,
    RISK_LABELS,
    SENTIMENT_LABELS,
    current_text_version,
    effective_assessment,
)
from .models import (
    AnalyticsProject,
    InstagramAccount,
    InstagramComment,
    InstagramCommentAssessment,
    InstagramCommentTextVersion,
    InstagramMedia,
    utcnow,
)


LOCAL_COMMENT_CLASSIFIER_KEY = "local-rule-based"
LOCAL_COMMENT_CLASSIFIER_VERSION = "comment-local-rules-v1"
LOCAL_COMMENT_CLASSIFIER_LABEL = "Classificador local baseado em regras"
COMMENT_CLASSIFIER_LABELS = {LOCAL_COMMENT_CLASSIFIER_KEY: LOCAL_COMMENT_CLASSIFIER_LABEL}


class CommentClassificationError(ValueError):
    """Erro conhecido de uma entrada que pode ser informado sem abortar o lote."""


class CommentClassificationPersistenceError(RuntimeError):
    """A persistência falhou e a transação inteira foi revertida."""


@dataclass(frozen=True)
class CommentClassifierInput:
    """Evidência mínima e imutável entregue a um classificador."""

    comment_id: str
    text_version_id: str
    text: str


@dataclass(frozen=True)
class CommentClassificationResult:
    """Resultado estruturado validado antes de criar um assessment automático."""

    classifier_key: str
    classifier_version: str
    sentiment: str | None = None
    intensity: str | None = None
    intent: str | None = None
    topic: str | None = None
    topic_normalized: str | None = None
    needs_response: bool | None = None
    risk: str | None = None
    confidence: float | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class CommentClassificationSkip:
    """Representa uma entrada sem evidência textual suficiente para classificar."""

    classifier_key: str
    classifier_version: str
    rationale: str


class CommentClassifier(Protocol):
    """Contrato intercambiável para providers locais ou futuros providers externos."""

    classifier_key: str
    classifier_version: str

    def classify(self, item: CommentClassifierInput) -> CommentClassificationResult | CommentClassificationSkip:
        """Classifica uma versão textual, sem conhecer Flask, rota ou banco."""


@dataclass(frozen=True)
class CommentClassificationBatchSummary:
    total_comments: int
    eligible: int
    classified: int
    skipped: int
    already_classified: int
    human_reviewed: int
    errors: int
    error_messages: tuple[str, ...] = ()


def _normalized_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
    return without_marks.casefold()


class LocalRuleBasedCommentClassifier:
    """Pequeno classificador V1, deliberadamente limitado e auditável.

    As regras não tentam resolver semântica em português. Elas somente usam
    sinais explícitos: interrogação/perguntas, agradecimento, elogio, crítica e
    urgência. A confidence é uma força heurística da regra, não uma medida
    estatística de acurácia.
    """

    classifier_key = LOCAL_COMMENT_CLASSIFIER_KEY

    def __init__(self, *, classifier_version: str = LOCAL_COMMENT_CLASSIFIER_VERSION) -> None:
        self.classifier_version = classifier_version

    def classify(self, item: CommentClassifierInput) -> CommentClassificationResult | CommentClassificationSkip:
        raw = item.text.strip()
        normalized = _normalized_text(raw)
        if not raw or not any(character.isalnum() for character in raw):
            return self._skip("Comentário ignorado: não há texto classificável além de espaço ou símbolos.")
        if re.fullmatch(r"https?://\S+", raw, flags=re.IGNORECASE):
            return self._skip("Comentário ignorado: o texto contém somente uma URL.")

        question = "?" in raw or bool(re.search(r"\b(como|quando|onde|qual|quais|porque|por que)\b", normalized))
        urgent = bool(re.search(r"\b(urgente|socorro|ajuda imediatamente)\b", normalized))
        thanks = bool(re.search(r"\b(obrigad[oa]|agradeco|valeu)\b", normalized))
        praise = bool(re.search(r"\b(parabens|adorei|excelente|maravilhoso|otimo|incrivel)\b", normalized))
        criticism = bool(re.search(r"\b(pessimo|horrivel|absurdo|decepcionante|ruim|odio)\b", normalized))

        if urgent:
            return self._result(
                sentiment="negative", intensity="high", intent="request", topic="Pedido urgente",
                topic_normalized="pedido_urgente", needs_response=True, risk="high", confidence=0.85,
                rationale="Pedido de ajuda ou urgência detectado por marcador explícito da regra local.",
            )
        if question:
            return self._result(
                sentiment="neutral", intensity="low", intent="question", topic="Pergunta",
                topic_normalized="pergunta", needs_response=True, risk=None, confidence=0.78,
                rationale="Pergunta detectada por interrogação ou marcador interrogativo explícito da regra local.",
            )
        if criticism:
            return self._result(
                sentiment="negative", intensity="medium", intent="criticism", topic="Crítica",
                topic_normalized="critica", needs_response=None, risk="low", confidence=0.76,
                rationale="Crítica detectada por expressão negativa explícita da regra local.",
            )
        if thanks:
            return self._result(
                sentiment="positive", intensity="low", intent="praise", topic="Agradecimento",
                topic_normalized="agradecimento", needs_response=False, risk="none", confidence=0.8,
                rationale="Agradecimento detectado por expressão explícita da regra local.",
            )
        if praise:
            return self._result(
                sentiment="positive", intensity="medium", intent="praise", topic="Elogio",
                topic_normalized="elogio", needs_response=False, risk="none", confidence=0.74,
                rationale="Elogio detectado por expressão positiva explícita da regra local.",
            )
        return self._result(
            sentiment="neutral", intensity=None, intent="other", topic=None, topic_normalized=None,
            needs_response=None, risk=None, confidence=0.4,
            rationale="Nenhum marcador determinístico da versão local foi encontrado; classificação neutra de baixa confiança heurística.",
        )

    def _result(self, **values: object) -> CommentClassificationResult:
        return CommentClassificationResult(
            classifier_key=self.classifier_key,
            classifier_version=self.classifier_version,
            **values,
        )

    def _skip(self, rationale: str) -> CommentClassificationSkip:
        return CommentClassificationSkip(
            classifier_key=self.classifier_key,
            classifier_version=self.classifier_version,
            rationale=rationale,
        )


COMMENT_CLASSIFIER_REGISTRY = {
    LOCAL_COMMENT_CLASSIFIER_KEY: LocalRuleBasedCommentClassifier,
}


def get_comment_classifier(key: str = LOCAL_COMMENT_CLASSIFIER_KEY) -> CommentClassifier:
    """Retorna o provider atual por uma registry pequena e explícita."""
    try:
        classifier_factory = COMMENT_CLASSIFIER_REGISTRY[key]
    except KeyError as error:
        raise CommentClassificationError("Classificador de comentários não reconhecido.") from error
    return classifier_factory()


def _comments_for_project(project_id: str) -> Iterable[InstagramComment]:
    return db.session.scalars(
        select(InstagramComment)
        .join(InstagramMedia, InstagramMedia.id == InstagramComment.instagram_media_id)
        .join(InstagramAccount, InstagramAccount.id == InstagramMedia.instagram_account_id)
        .where(InstagramAccount.analytics_project_id == project_id)
        .order_by(InstagramComment.id)
        .options(
            selectinload(InstagramComment.text_versions),
            selectinload(InstagramComment.assessments),
        )
    ).yield_per(100)


def _has_matching_automatic_assessment(
    comment: InstagramComment,
    text_version: InstagramCommentTextVersion,
    classifier: CommentClassifier,
) -> bool:
    return any(
        assessment.source == "automatic"
        and assessment.instagram_comment_text_version_id == text_version.id
        and assessment.classifier_key == classifier.classifier_key
        and assessment.classifier_version == classifier.classifier_version
        for assessment in comment.assessments
    )


def _input_for(comment: InstagramComment, text_version: InstagramCommentTextVersion) -> CommentClassifierInput:
    return CommentClassifierInput(comment_id=comment.id, text_version_id=text_version.id, text=text_version.text)


def _validate_choice(value: str | None, allowed: dict[str, str], label: str) -> None:
    if value is not None and value not in allowed:
        raise CommentClassificationError(f"O classificador retornou {label} inválido.")


def _validate_text(value: str | None, label: str, *, limit: int, required: bool = False) -> None:
    if value is None:
        if required:
            raise CommentClassificationError(f"O classificador não informou {label}.")
        return
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise CommentClassificationError(f"O classificador retornou {label} inválido.")


def validate_comment_classification_result(
    result: CommentClassificationResult,
    classifier: CommentClassifier,
) -> None:
    """Valida um resultado contra as mesmas categorias aceitas pelo model."""
    if result.classifier_key != classifier.classifier_key or result.classifier_version != classifier.classifier_version:
        raise CommentClassificationError("O resultado não corresponde ao classificador selecionado.")
    _validate_choice(result.sentiment, SENTIMENT_LABELS, "sentimento")
    _validate_choice(result.intensity, INTENSITY_LABELS, "intensidade")
    _validate_choice(result.intent, INTENT_LABELS, "intenção")
    _validate_choice(result.risk, RISK_LABELS, "risco")
    if result.needs_response is not None and not isinstance(result.needs_response, bool):
        raise CommentClassificationError("O classificador retornou necessidade de resposta inválida.")
    if result.confidence is not None:
        if isinstance(result.confidence, bool) or not isinstance(result.confidence, Real) or not 0 <= result.confidence <= 1:
            raise CommentClassificationError("O classificador retornou confidence inválida.")
    _validate_text(result.topic, "tema", limit=200)
    _validate_text(result.topic_normalized, "tema normalizado", limit=200)
    _validate_text(result.rationale, "justificativa", limit=2_000, required=True)
    if all(value is None for value in (
        result.sentiment, result.intensity, result.intent, result.topic, result.topic_normalized,
        result.needs_response, result.risk,
    )):
        raise CommentClassificationError("O classificador não retornou uma classificação utilizável.")


def _summary(
    *, total_comments: int, eligible: int, classified: int, skipped: int,
    already_classified: int, human_reviewed: int, errors: int, messages: list[str],
) -> CommentClassificationBatchSummary:
    return CommentClassificationBatchSummary(
        total_comments=total_comments,
        eligible=eligible,
        classified=classified,
        skipped=skipped,
        already_classified=already_classified,
        human_reviewed=human_reviewed,
        errors=errors,
        error_messages=tuple(messages),
    )


def _classify_batch(
    project: AnalyticsProject,
    classifier: CommentClassifier,
    *, reprocess: bool,
    persist: bool,
) -> CommentClassificationBatchSummary:
    if project.status != "active" or project.archived_at is not None:
        raise CommentClassificationError("Projetos arquivados não podem receber nova classificação automática.")

    total_comments = eligible = classified = skipped = already_classified = human_reviewed = errors = 0
    messages: list[str] = []
    for comment in _comments_for_project(project.id):
        total_comments += 1
        text_version = current_text_version(comment.text_versions)
        if text_version is None:
            skipped += 1
            continue
        effective = effective_assessment(comment.assessments)
        if effective is not None and effective.source in HUMAN_ASSESSMENT_SOURCES:
            human_reviewed += 1
        if not reprocess and _has_matching_automatic_assessment(comment, text_version, classifier):
            already_classified += 1
            continue
        eligible += 1
        try:
            classification = classifier.classify(_input_for(comment, text_version))
            if isinstance(classification, CommentClassificationSkip):
                if (
                    classification.classifier_key != classifier.classifier_key
                    or classification.classifier_version != classifier.classifier_version
                    or not classification.rationale.strip()
                ):
                    raise CommentClassificationError("O classificador retornou um skip inválido.")
                skipped += 1
                continue
            validate_comment_classification_result(classification, classifier)
        except CommentClassificationError as error:
            errors += 1
            messages.append(f"Comentário {comment.external_id}: {error}")
            continue

        if not persist:
            classified += 1
            continue

        previous_automatic = max(
            (
                assessment for assessment in comment.assessments
                if assessment.source == "automatic" and assessment.superseded_at is None
            ),
            key=lambda item: (item.created_at, item.id),
            default=None,
        )
        if previous_automatic is not None:
            previous_automatic.superseded_at = utcnow()
        assessment = InstagramCommentAssessment(
            instagram_comment=comment,
            text_version=text_version,
            supersedes=previous_automatic,
            source="automatic",
            classifier_key=classification.classifier_key,
            classifier_version=classification.classifier_version,
            sentiment=classification.sentiment,
            intensity=classification.intensity,
            intent=classification.intent,
            topic=classification.topic.strip() if classification.topic else None,
            topic_normalized=classification.topic_normalized.strip() if classification.topic_normalized else None,
            needs_response=classification.needs_response,
            risk=classification.risk,
            confidence=float(classification.confidence) if classification.confidence is not None else None,
            rationale=classification.rationale.strip() if classification.rationale else None,
        )
        db.session.add(assessment)
        classified += 1
    return _summary(
        total_comments=total_comments,
        eligible=eligible,
        classified=classified,
        skipped=skipped,
        already_classified=already_classified,
        human_reviewed=human_reviewed,
        errors=errors,
        messages=messages,
    )


def plan_comment_classification(
    project: AnalyticsProject,
    classifier: CommentClassifier | None = None,
    *, reprocess: bool = False,
) -> CommentClassificationBatchSummary:
    """Calcula a prévia de um lote sem criar avaliações no banco."""
    return _classify_batch(project, classifier or get_comment_classifier(), reprocess=reprocess, persist=False)


def classify_project_comments(
    project: AnalyticsProject,
    classifier: CommentClassifier | None = None,
    *, reprocess: bool = False,
) -> CommentClassificationBatchSummary:
    """Persiste um lote completo ou reverte tudo se a infraestrutura falhar."""
    try:
        summary = _classify_batch(project, classifier or get_comment_classifier(), reprocess=reprocess, persist=True)
        db.session.commit()
        return summary
    except CommentClassificationError:
        db.session.rollback()
        raise
    except Exception as error:
        db.session.rollback()
        raise CommentClassificationPersistenceError(
            "Não foi possível concluir a classificação automática; nenhuma avaliação foi gravada."
        ) from error


__all__ = [
    "COMMENT_CLASSIFIER_LABELS", "COMMENT_CLASSIFIER_REGISTRY", "CommentClassificationBatchSummary", "CommentClassificationError",
    "CommentClassificationPersistenceError", "CommentClassificationResult", "CommentClassificationSkip",
    "CommentClassifier", "CommentClassifierInput", "LOCAL_COMMENT_CLASSIFIER_KEY",
    "LOCAL_COMMENT_CLASSIFIER_LABEL", "LOCAL_COMMENT_CLASSIFIER_VERSION", "LocalRuleBasedCommentClassifier",
    "classify_project_comments", "get_comment_classifier", "plan_comment_classification",
    "validate_comment_classification_result",
]
