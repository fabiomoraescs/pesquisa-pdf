"""Persistência transacional das importações validadas do Análysis Instagram."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from .extensions import db
from .instagram_import import InstagramImportDataset
from .models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramAccountSnapshot,
    InstagramMedia,
    InstagramMediaObservation,
    utcnow,
)


class InstagramImportPersistenceError(ValueError):
    """Conflito de domínio detectado antes de confirmar uma prévia válida."""


@dataclass(frozen=True)
class InstagramImportResult:
    run_id: str
    account_id: str
    media_created: int
    media_reused: int
    observations_created: int
    snapshot_created: bool


def account_conflict(project: AnalyticsProject, dataset: InstagramImportDataset) -> str | None:
    """Retorna somente conflitos que dependem do estado persistido do projeto."""
    account = project.instagram_account
    if account is None:
        return None
    if account.username_normalized != dataset.account_username_normalized:
        return (
            f"O projeto está associado a @{account.username}, mas o arquivo contém "
            f"@{dataset.account_username}."
        )
    if (account.external_id and dataset.account_external_id
            and account.external_id != dataset.account_external_id):
        return "O identificador externo da conta no arquivo não corresponde à conta já vinculada ao projeto."
    return None


def _fill_missing_account_metadata(account: InstagramAccount, dataset: InstagramImportDataset) -> None:
    if account.external_id is None and dataset.account_external_id:
        account.external_id = dataset.account_external_id
    if not account.display_name and dataset.account_display_name:
        account.display_name = dataset.account_display_name


def _fill_missing_media_metadata(media: InstagramMedia, row) -> None:
    """Completa apenas lacunas estáveis; texto ou data divergentes nunca são sobrescritos."""
    if media.permalink is None and row.permalink:
        media.permalink = row.permalink
    if media.caption_original == "" and row.caption:
        media.caption_original = row.caption
    if media.published_at is None:
        media.published_at = row.published_at
    if media.media_type_raw is None and row.media_type:
        media.media_type_raw = row.media_type
    first_seen_at = _as_utc(media.first_seen_at)
    last_seen_at = _as_utc(media.last_seen_at)
    if first_seen_at is None or row.observed_at < first_seen_at:
        media.first_seen_at = row.observed_at
    if last_seen_at is None or row.observed_at > last_seen_at:
        media.last_seen_at = row.observed_at


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite não preserva tzinfo; timestamps persistidos seguem sendo UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def persist_instagram_import(project: AnalyticsProject, dataset: InstagramImportDataset) -> InstagramImportResult:
    """Grava conta, run e observações como uma única unidade transacional."""
    conflict = account_conflict(project, dataset)
    if conflict:
        raise InstagramImportPersistenceError(conflict)
    try:
        account = project.instagram_account
        if account is None:
            account = InstagramAccount(
                analytics_project=project,
                username=dataset.account_username,
                username_normalized=dataset.account_username_normalized,
                external_id=dataset.account_external_id,
                display_name=dataset.account_display_name or "",
            )
            db.session.add(account)
            db.session.flush()
        else:
            _fill_missing_account_metadata(account, dataset)

        existing_media = {
            media.external_id: media
            for media in db.session.scalars(
                select(InstagramMedia).where(InstagramMedia.instagram_account_id == account.id)
            )
        }
        run = AnalyticsRun(
            analytics_project=project,
            source_kind=dataset.source_kind,
            status="completed",
            period_start=dataset.run_period_start,
            period_end=dataset.run_period_end,
            started_at=utcnow(),
            completed_at=utcnow(),
            processor_version=dataset.processor_version,
            source_hash=dataset.source_hash,
            parameters_json={
                "schema_version": dataset.schema_version,
                "source_filename": dataset.original_filename,
                "source_rows": dataset.source_row_count,
                "observations": len(dataset.rows),
            },
            record_count=len(dataset.rows),
        )
        db.session.add(run)
        db.session.flush()

        created = 0
        reused = 0
        for row in dataset.rows:
            media = existing_media.get(row.media_external_id)
            if media is None:
                media = InstagramMedia(
                    instagram_account=account,
                    external_id=row.media_external_id,
                    permalink=row.permalink,
                    caption_original=row.caption or "",
                    published_at=row.published_at,
                    media_type_raw=row.media_type,
                    first_seen_at=row.observed_at,
                    last_seen_at=row.observed_at,
                )
                db.session.add(media)
                existing_media[row.media_external_id] = media
                created += 1
            else:
                _fill_missing_media_metadata(media, row)
                reused += 1
            db.session.add(InstagramMediaObservation(
                instagram_media=media,
                analytics_run=run,
                observed_at=row.observed_at,
                period_start=row.period_start,
                period_end=row.period_end,
                **row.media_metrics,
            ))

        snapshot_created = dataset.account_snapshot is not None
        if dataset.account_snapshot is not None:
            snapshot = dataset.account_snapshot
            db.session.add(InstagramAccountSnapshot(
                instagram_account=account,
                analytics_run=run,
                observed_at=snapshot.observed_at,
                period_start=snapshot.period_start,
                period_end=snapshot.period_end,
                **snapshot.metrics,
            ))
        db.session.flush()
        result = InstagramImportResult(
            run_id=run.id,
            account_id=account.id,
            media_created=created,
            media_reused=reused,
            observations_created=len(dataset.rows),
            snapshot_created=snapshot_created,
        )
        db.session.commit()
        return result
    except Exception:
        db.session.rollback()
        raise
