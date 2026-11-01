"""Persistência transacional das coletas normalizadas do OAI-PMH.

Este módulo não executa HTTP, não conhece Flask e não faz ``commit``. A camada
que o chama fornece uma ``Session`` já autorizada e decide quando confirmar a
transação externa. Cada chamada usa um *savepoint*: se a reconciliação falhar,
as alterações desta coleta são desfeitas sem deixar a auditoria finalizada pela
metade.

O estado atual de :class:`OjsArticle` é ordenado por ``observed_at``. Uma
observação com instante anterior nunca substitui o estado atual; em empate, o
estado já conhecido é preservado. As observações históricas são registradas de
qualquer forma. Assim, reprocessar uma auditoria antiga não regride dados mais
novos e a ordem de chegada não decide um conflito temporal ambíguo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from typing import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import OjsArticle, OjsArticleObservation, OjsJournal, OjsSeoAuditRun, utcnow
from .ojs_oai_client import DC_FIELDS, OaiCollectionResult, OaiDeletedRecord, OaiIssue, OaiRecord


PERSISTENCE_VERSION = "ojs-seo-persistence-v1"
FINAL_RUN_STATUSES = frozenset({"completed", "partial", "failed", "canceled"})
COLLECTION_STATUSES = frozenset({"completed", "partial", "failed"})
_FIELD_NAMES = tuple(sorted(DC_FIELDS))


class OjsPersistenceError(ValueError):
    """Erro seguro de integração entre resultado OAI normalizado e banco."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


class OjsPersistenceConflictError(OjsPersistenceError):
    """Uma execução finalizada foi reutilizada com conteúdo incompatível."""


class OjsPersistenceValidationError(OjsPersistenceError):
    """O resultado normalizado não respeita o contrato persistível."""


@dataclass(frozen=True, slots=True)
class OjsPersistenceSummary:
    """Resumo verificável, sem metadados bibliográficos ou conteúdo bruto."""

    journal_id: str
    audit_run_id: str
    status: str
    trace_hash: str
    observed_identifier_count: int
    article_count_created: int
    article_count_updated: int
    observation_count_created: int
    current_state_updates: int
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class _NormalizedObservation:
    identifier: str
    record_status: str
    fields: dict[str, list[str]]
    dois: list[str]
    datestamp: str | None
    metadata_hash: str

    @property
    def title(self) -> str | None:
        return self.fields["title"][0] if self.fields["title"] else None

    @property
    def canonical_doi(self) -> str | None:
        return self.dois[0] if self.dois else None


def persist_oai_collection(
    session: Session,
    journal: OjsJournal,
    audit_run: OjsSeoAuditRun,
    collection: OaiCollectionResult,
    *,
    observed_at: datetime | None = None,
) -> OjsPersistenceSummary:
    """Persiste uma coleta já normalizada, sem confirmar a transação externa.

    ``journal`` e ``audit_run`` precisam estar persistidos e pertencer ao mesmo
    periódico OJS. Para uma execução pendente ou em andamento, o serviço cria
    artigos/observações e a finaliza como ``completed``, ``partial`` ou
    ``failed`` de acordo com ``collection.status``. Para uma execução já
    finalizada, a mesma impressão de rastreabilidade retorna um resumo
    idempotente; qualquer outro conteúdo é recusado.

    A chamada usa ``begin_nested`` para isolar a unidade de persistência, mas
    nunca chama ``commit``. Logo, o chamador deve confirmar ou desfazer a
    transação externa depois de receber o resumo.
    """
    managed_journal, managed_run = _managed_scope(session, journal, audit_run)
    normalized, duplicate_identifiers = _normalize_collection(collection)
    trace_hash = _collection_trace_hash(collection, normalized, duplicate_identifiers)

    if managed_run.status in FINAL_RUN_STATUSES:
        if managed_run.trace_hash == trace_hash:
            return _summary_from_run(managed_run, idempotent=True)
        raise OjsPersistenceConflictError(
            "finalized_run_reused",
            "A auditoria já foi finalizada com um resultado diferente e não pode ser reutilizada.",
        )
    if managed_run.status not in {"pending", "running"}:
        raise OjsPersistenceValidationError(
            "invalid_audit_run_status",
            "A auditoria não está em um estado que aceite persistência.",
        )

    finished_at = _normalize_time(observed_at or utcnow())
    if managed_run.started_at is None:
        # A coleta recebida já terminou; o melhor instante disponível é o fim
        # menos seu tempo relatado, sem fabricar uma data de negócio separada.
        elapsed = float(collection.elapsed_seconds)
        started_at = finished_at - timedelta(seconds=elapsed)
    else:
        started_at = _normalize_time(managed_run.started_at)
    if started_at > finished_at:
        finished_at = started_at

    if collection.status == "failed" and normalized:
        raise OjsPersistenceValidationError(
            "failed_collection_has_records",
            "Uma coleta com falha não pode persistir registros como se tivesse sido concluída.",
        )

    created = updated = observations_created = current_updates = 0
    try:
        with session.begin_nested():
            managed_run.started_at = started_at
            if collection.status != "failed":
                for item in normalized:
                    article = session.scalar(select(OjsArticle).where(
                        OjsArticle.journal_id == managed_journal.id,
                        OjsArticle.oai_identifier == item.identifier,
                    ))
                    if article is None:
                        article = _new_article(managed_journal.id, item, finished_at)
                        session.add(article)
                        session.flush()
                        created += 1
                        current_updates += 1
                    else:
                        if _should_update_current(article, finished_at):
                            _apply_current_observation(article, item, finished_at)
                            updated += 1
                            current_updates += 1
                        elif article.first_observed_at is None or finished_at < _normalize_time(article.first_observed_at):
                            article.first_observed_at = finished_at

                    existing_observation = session.scalar(select(OjsArticleObservation).where(
                        OjsArticleObservation.article_id == article.id,
                        OjsArticleObservation.audit_run_id == managed_run.id,
                    ))
                    if existing_observation is not None:
                        if not _observation_matches(existing_observation, item):
                            raise OjsPersistenceConflictError(
                                "duplicate_observation_conflict",
                                "A auditoria já contém uma observação diferente para este identificador OAI.",
                            )
                        continue
                    session.add(_new_observation(managed_journal.id, article.id, managed_run.id, item, finished_at))
                    observations_created += 1

            _apply_run_result(
                managed_run,
                collection,
                trace_hash,
                normalized,
                duplicate_identifiers,
                finished_at,
            )
            session.flush()
    except IntegrityError as exc:
        raise OjsPersistenceError(
            "persistence_integrity_error",
            "Não foi possível registrar esta coleta OAI-PMH de forma consistente.",
        ) from exc

    return OjsPersistenceSummary(
        journal_id=managed_journal.id,
        audit_run_id=managed_run.id,
        status=managed_run.status,
        trace_hash=trace_hash,
        observed_identifier_count=len(normalized),
        article_count_created=created,
        article_count_updated=updated,
        observation_count_created=observations_created,
        current_state_updates=current_updates,
    )


def _managed_scope(
    session: Session, journal: OjsJournal, audit_run: OjsSeoAuditRun
) -> tuple[OjsJournal, OjsSeoAuditRun]:
    if not journal.id or not audit_run.id:
        raise OjsPersistenceValidationError(
            "unpersisted_scope", "O periódico e a auditoria precisam existir antes de receber uma coleta."
        )
    managed_journal = session.get(OjsJournal, journal.id)
    managed_run = session.get(OjsSeoAuditRun, audit_run.id)
    if managed_journal is None or managed_run is None:
        raise OjsPersistenceValidationError(
            "unknown_scope", "O periódico ou a auditoria informada não existe mais."
        )
    if managed_run.journal_id != managed_journal.id:
        raise OjsPersistenceValidationError(
            "audit_journal_mismatch", "A auditoria informada não pertence ao periódico indicado."
        )
    project = managed_journal.analytics_project
    if project is None or project.module_key != "ojs_seo":
        raise OjsPersistenceValidationError(
            "invalid_ojs_project", "O periódico não está vinculado a um projeto OJS SEO válido."
        )
    return managed_journal, managed_run


def _normalize_collection(
    collection: OaiCollectionResult,
) -> tuple[tuple[_NormalizedObservation, ...], frozenset[str]]:
    if not isinstance(collection, OaiCollectionResult):
        raise OjsPersistenceValidationError(
            "invalid_collection", "O resultado da coleta OAI-PMH não possui o formato esperado."
        )
    if collection.status not in COLLECTION_STATUSES:
        raise OjsPersistenceValidationError(
            "invalid_collection_status", "O resultado da coleta possui um estado não suportado."
        )
    for value in (collection.pages_fetched, collection.total_response_bytes):
        if not isinstance(value, int) or value < 0:
            raise OjsPersistenceValidationError(
                "invalid_collection_counts", "O resultado da coleta contém contagens inválidas."
            )
    if (
        isinstance(collection.elapsed_seconds, bool)
        or not isinstance(collection.elapsed_seconds, (int, float))
        or not math.isfinite(collection.elapsed_seconds)
        or collection.elapsed_seconds < 0
    ):
        raise OjsPersistenceValidationError(
            "invalid_collection_duration", "O resultado da coleta contém uma duração inválida."
        )
    _safe_reason(collection.completion_reason)
    _safe_issues(collection.warnings)
    _safe_issues(collection.errors)

    observations: dict[str, _NormalizedObservation] = {}
    duplicate_identifiers: set[str] = set()
    for record in collection.records:
        item = _normalize_active_record(record)
        _add_normalized_observation(observations, item, duplicate_identifiers)
    for record in collection.deleted_records:
        item = _normalize_deleted_record(record)
        _add_normalized_observation(observations, item, duplicate_identifiers)

    reported_identifiers = _valid_identifier_set(collection.unique_identifiers, "unique_identifiers")
    observed_identifiers = set(observations)
    if reported_identifiers and reported_identifiers != observed_identifiers:
        raise OjsPersistenceValidationError(
            "inconsistent_unique_identifiers",
            "O resultado da coleta possui identificadores inconsistentes."
        )
    duplicate_identifiers.update(_valid_identifier_set(collection.duplicate_identifiers, "duplicate_identifiers"))
    if not duplicate_identifiers.issubset(observed_identifiers):
        raise OjsPersistenceValidationError(
            "invalid_duplicate_identifier", "O resultado da coleta contém duplicidades inconsistentes."
        )
    return tuple(observations[key] for key in sorted(observations)), frozenset(duplicate_identifiers)


def _add_normalized_observation(
    observations: dict[str, _NormalizedObservation],
    item: _NormalizedObservation,
    duplicate_identifiers: set[str],
) -> None:
    existing = observations.get(item.identifier)
    if existing is None:
        observations[item.identifier] = item
        return
    duplicate_identifiers.add(item.identifier)
    if existing != item:
        raise OjsPersistenceValidationError(
            "conflicting_duplicate_identifier",
            "A coleta contém versões conflitantes do mesmo identificador OAI."
        )


def _normalize_active_record(record: OaiRecord) -> _NormalizedObservation:
    if not isinstance(record, OaiRecord):
        raise OjsPersistenceValidationError(
            "invalid_active_record", "A coleta contém um registro ativo inválido."
        )
    fields = _normalize_fields(record.fields)
    dois = _normalize_text_values(record.dois, "doi", maximum_length=500)
    return _normalized_observation(record.identifier, "active", fields, dois, record.datestamp)


def _normalize_deleted_record(record: OaiDeletedRecord) -> _NormalizedObservation:
    if not isinstance(record, OaiDeletedRecord):
        raise OjsPersistenceValidationError(
            "invalid_deleted_record", "A coleta contém um registro excluído inválido."
        )
    return _normalized_observation(record.identifier, "deleted", {field: [] for field in _FIELD_NAMES}, [], record.datestamp)


def _normalized_observation(
    identifier: object,
    status: str,
    fields: dict[str, list[str]],
    dois: list[str],
    datestamp: object,
) -> _NormalizedObservation:
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 1024:
        raise OjsPersistenceValidationError(
            "invalid_oai_identifier", "A coleta contém um identificador OAI inválido."
        )
    if datestamp is not None and (not isinstance(datestamp, str) or len(datestamp) > 64):
        raise OjsPersistenceValidationError(
            "invalid_oai_datestamp", "A coleta contém uma data OAI inválida."
        )
    payload = {
        "datestamp": datestamp,
        "dois": dois,
        "fields": fields,
        "record_status": status,
    }
    return _NormalizedObservation(
        identifier=identifier,
        record_status=status,
        fields=fields,
        dois=dois,
        datestamp=datestamp,
        metadata_hash=_hash_payload(payload),
    )


def _normalize_fields(value: object) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        raise OjsPersistenceValidationError(
            "invalid_dublin_core", "A coleta contém metadados Dublin Core inválidos."
        )
    unknown = set(value) - set(_FIELD_NAMES)
    if unknown:
        raise OjsPersistenceValidationError(
            "unknown_dublin_core_field", "A coleta contém um campo Dublin Core não suportado."
        )
    return {
        field: _normalize_text_values(value.get(field, ()), field)
        for field in _FIELD_NAMES
    }


def _normalize_text_values(value: object, field: str, *, maximum_length: int | None = None) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise OjsPersistenceValidationError(
            "invalid_dublin_core_value", "A coleta contém valores bibliográficos inválidos."
        )
    values: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise OjsPersistenceValidationError(
                "invalid_dublin_core_value", "A coleta contém valores bibliográficos inválidos."
            )
        if maximum_length is not None and len(item) > maximum_length:
            raise OjsPersistenceValidationError(
                "bibliographic_value_too_long", "A coleta contém um valor bibliográfico acima do limite suportado."
            )
        values.append(item)
    return values


def _valid_identifier_set(values: object, _field: str) -> set[str]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise OjsPersistenceValidationError(
            "invalid_identifier_collection", "O resultado da coleta contém identificadores inválidos."
        )
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise OjsPersistenceValidationError(
                "invalid_identifier_collection", "O resultado da coleta contém identificadores inválidos."
            )
        result.add(value)
    return result


def _collection_trace_hash(
    collection: OaiCollectionResult,
    observations: Sequence[_NormalizedObservation],
    duplicate_identifiers: frozenset[str],
) -> str:
    payload = {
        "completion_reason": collection.completion_reason,
        "duplicate_identifiers": sorted(duplicate_identifiers),
        "errors": _safe_issues(collection.errors),
        "observations": [
            {
                "datestamp": item.datestamp,
                "dois": item.dois,
                "fields": item.fields,
                "identifier": item.identifier,
                "record_status": item.record_status,
            }
            for item in observations
        ],
        "pages_fetched": collection.pages_fetched,
        "status": collection.status,
        "total_response_bytes": collection.total_response_bytes,
        "warnings": _safe_issues(collection.warnings),
    }
    return _hash_payload(payload)


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _new_article(journal_id: str, item: _NormalizedObservation, observed_at: datetime) -> OjsArticle:
    article = OjsArticle(journal_id=journal_id, oai_identifier=item.identifier)
    _apply_current_observation(article, item, observed_at)
    article.first_observed_at = observed_at
    return article


def _should_update_current(article: OjsArticle, observed_at: datetime) -> bool:
    return article.last_observed_at is None or observed_at > _normalize_time(article.last_observed_at)


def _apply_current_observation(article: OjsArticle, item: _NormalizedObservation, observed_at: datetime) -> None:
    article.record_status = item.record_status
    article.title = item.title
    article.titles_json = item.fields["title"]
    article.creators_json = item.fields["creator"]
    article.subjects_json = item.fields["subject"]
    article.descriptions_json = item.fields["description"]
    article.identifiers_json = item.fields["identifier"]
    article.dates_json = item.fields["date"]
    article.languages_json = item.fields["language"]
    article.types_json = item.fields["type"]
    article.doi = item.canonical_doi
    article.dois_json = item.dois
    article.oai_datestamp = item.datestamp
    article.metadata_hash = item.metadata_hash
    article.last_observed_at = observed_at
    if article.first_observed_at is None or observed_at < _normalize_time(article.first_observed_at):
        article.first_observed_at = observed_at


def _new_observation(
    journal_id: str,
    article_id: str,
    audit_run_id: str,
    item: _NormalizedObservation,
    observed_at: datetime,
) -> OjsArticleObservation:
    return OjsArticleObservation(
        journal_id=journal_id,
        article_id=article_id,
        audit_run_id=audit_run_id,
        record_status=item.record_status,
        dublin_core_json=item.fields,
        doi_observed=item.canonical_doi,
        dois_observed_json=item.dois,
        oai_datestamp=item.datestamp,
        metadata_hash=item.metadata_hash,
        observed_at=observed_at,
    )


def _observation_matches(observation: OjsArticleObservation, item: _NormalizedObservation) -> bool:
    return (
        observation.record_status == item.record_status
        and observation.dublin_core_json == item.fields
        and observation.doi_observed == item.canonical_doi
        and observation.dois_observed_json == item.dois
        and observation.oai_datestamp == item.datestamp
        and observation.metadata_hash == item.metadata_hash
    )


def _apply_run_result(
    run: OjsSeoAuditRun,
    collection: OaiCollectionResult,
    trace_hash: str,
    observations: Sequence[_NormalizedObservation],
    duplicate_identifiers: frozenset[str],
    completed_at: datetime,
) -> None:
    collection_metrics = dict(collection.metrics)
    field_coverage = dict(collection.field_coverage())
    run.status = collection.status
    # A partir da revisão de claims duráveis, uma auditoria final não pode
    # reter a posse do worker. Esta atribuição acontece no mesmo flush que a
    # transição de estado, preservando a constraint inclusive dentro do
    # savepoint usado pela persistência.
    run.claim_token = None
    run.lease_expires_at = None
    run.completed_at = completed_at
    run.completion_reason = _safe_reason(collection.completion_reason)
    run.trace_hash = trace_hash
    run.page_count = collection.pages_fetched
    run.record_count = len(observations)
    run.deleted_record_count = sum(1 for item in observations if item.record_status == "deleted")
    run.duplicate_identifier_count = len(duplicate_identifiers)
    run.doi_count = sum(1 for item in observations if item.record_status == "active" and item.dois)
    run.response_bytes = collection.total_response_bytes
    run.warnings_json = _safe_issues(collection.warnings)
    run.errors_json = _safe_issues(collection.errors)
    run.metrics_json = {
        "collection": collection_metrics,
        "coverage": field_coverage,
        "observed_identifier_count": len(observations),
        "persistence_version": PERSISTENCE_VERSION,
    }


def _safe_issues(issues: object) -> list[dict[str, str]]:
    if isinstance(issues, str) or not isinstance(issues, Sequence):
        raise OjsPersistenceValidationError(
            "invalid_collection_issues", "O resultado da coleta contém avisos ou erros inválidos."
        )
    safe: list[dict[str, str]] = []
    for issue in issues:
        if not isinstance(issue, OaiIssue):
            raise OjsPersistenceValidationError(
                "invalid_collection_issues", "O resultado da coleta contém avisos ou erros inválidos."
            )
        safe.append({"code": _safe_text(issue.code, 80), "message": _safe_text(issue.message, 300)})
    return sorted(safe, key=lambda item: (item["code"], item["message"]))


def _safe_reason(value: object) -> str | None:
    if value is None:
        return None
    return _safe_text(value, 80)


def _safe_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise OjsPersistenceValidationError(
            "invalid_collection_issue", "O resultado da coleta contém uma mensagem inválida."
        )
    return " ".join(value.split())[:limit]


def _normalize_time(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise OjsPersistenceValidationError("invalid_observation_time", "O instante de observação é inválido.")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _summary_from_run(run: OjsSeoAuditRun, *, idempotent: bool) -> OjsPersistenceSummary:
    metrics = run.metrics_json if isinstance(run.metrics_json, Mapping) else {}
    return OjsPersistenceSummary(
        journal_id=run.journal_id,
        audit_run_id=run.id,
        status=run.status,
        trace_hash=run.trace_hash or "",
        observed_identifier_count=int(metrics.get("observed_identifier_count", run.record_count)),
        article_count_created=0,
        article_count_updated=0,
        observation_count_created=0,
        current_state_updates=0,
        idempotent=idempotent,
    )


__all__ = [
    "OjsPersistenceConflictError",
    "OjsPersistenceError",
    "OjsPersistenceSummary",
    "OjsPersistenceValidationError",
    "PERSISTENCE_VERSION",
    "persist_oai_collection",
]
