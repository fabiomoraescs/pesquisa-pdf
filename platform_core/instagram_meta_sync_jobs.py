"""Orquestração durável da sincronização Meta fora da requisição web.

``AnalyticsRun`` é a fonte única de estado do job: um run Meta pendente é
reivindicado por um worker Flask separado e só se torna analiticamente
elegível quando o motor canônico o marca como ``completed``. Não há thread no
processo web nem uma segunda tabela de fila.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import click
from flask import Flask, current_app
from sqlalchemy import select, update

from .extensions import db
from .instagram_meta import InstagramMetaError, InstagramMetaSettings, log_meta_operation, safe_meta_error_message
from .instagram_meta_sync import (
    META_SYNC_SOURCE_KIND,
    META_SYNC_VERSION,
    InstagramMetaSyncError,
    PublicationWindow,
    safe_meta_sync_error_message,
    sync_meta_connection,
)
from .models import AnalyticsProject, AnalyticsRun, utcnow


META_SYNC_PENDING = "pending"
META_SYNC_RUNNING = "running"
META_SYNC_COMPLETED = "completed"
META_SYNC_FAILED = "failed"
META_SYNC_ACTIVE_STATUSES = (META_SYNC_PENDING, META_SYNC_RUNNING)
DEFAULT_META_SYNC_STALE_SECONDS = 2 * 60 * 60


def _connection_id_from_run(run: AnalyticsRun) -> str | None:
    parameters = run.parameters_json if isinstance(run.parameters_json, dict) else {}
    connection_id = parameters.get("connection_id")
    return connection_id if isinstance(connection_id, str) and connection_id else None


def _provider_from_run(run: AnalyticsRun) -> str | None:
    parameters = run.parameters_json if isinstance(run.parameters_json, dict) else {}
    provider = parameters.get("provider")
    return provider if provider in {"facebook_login", "instagram_login"} else None


def active_meta_sync_run(project_id: str) -> AnalyticsRun | None:
    """Retorna somente a sincronização Meta ainda em fila ou execução."""
    return db.session.scalar(
        select(AnalyticsRun).where(
            AnalyticsRun.analytics_project_id == project_id,
            AnalyticsRun.source_kind == META_SYNC_SOURCE_KIND,
            AnalyticsRun.processor_version == META_SYNC_VERSION,
            AnalyticsRun.status.in_(META_SYNC_ACTIVE_STATUSES),
        ).order_by(AnalyticsRun.created_at.asc(), AnalyticsRun.id.asc())
    )


def enqueue_meta_sync_run(
    project: AnalyticsProject,
    connection_id: str,
    provider: str,
    publication_window: PublicationWindow,
) -> tuple[AnalyticsRun, bool]:
    """Cria uma execução pendente, ou reutiliza a ativa do mesmo projeto.

    O lock da linha de projeto serializa o enqueue em bancos que suportam
    ``FOR UPDATE``. No SQLite padrão da aplicação, o único escritor é
    serializado pelo próprio banco; o worker usa ainda um update condicional
    para que dois processos nunca reivindiquem o mesmo run.
    """
    locked_project = db.session.scalar(
        select(AnalyticsProject).where(AnalyticsProject.id == project.id).with_for_update()
    )
    if locked_project is None:
        raise InstagramMetaSyncError("connection_unavailable", "O projeto não está disponível para sincronização.")

    active = active_meta_sync_run(locked_project.id)
    if active is not None:
        return active, False

    run = AnalyticsRun(
        analytics_project=locked_project,
        source_kind=META_SYNC_SOURCE_KIND,
        status=META_SYNC_PENDING,
        processor_version=META_SYNC_VERSION,
        parameters_json={
            "schema_version": META_SYNC_VERSION,
            "execution_mode": "durable_worker",
            "connection_id": connection_id,
            "provider": provider,
            **publication_window.as_parameters(),
        },
        record_count=0,
    )
    db.session.add(run)
    db.session.commit()
    return run, True


def recover_stale_meta_sync_runs(*, stale_seconds: int) -> int:
    """Libera jobs cujo worker morreu antes de concluir a transação canônica."""
    if stale_seconds <= 0:
        raise ValueError("O prazo de recuperação de sincronização precisa ser positivo.")
    now = utcnow()
    cutoff = now - timedelta(seconds=stale_seconds)
    result = db.session.execute(
        update(AnalyticsRun)
        .where(
            AnalyticsRun.source_kind == META_SYNC_SOURCE_KIND,
            AnalyticsRun.processor_version == META_SYNC_VERSION,
            AnalyticsRun.status == META_SYNC_RUNNING,
            AnalyticsRun.updated_at < cutoff,
        )
        .values(
            status=META_SYNC_FAILED,
            completed_at=now,
            error_message="A sincronização foi interrompida antes da conclusão. Tente novamente.",
            updated_at=now,
        )
    )
    if result.rowcount:
        db.session.commit()
    return int(result.rowcount or 0)


def claim_next_meta_sync_run() -> AnalyticsRun | None:
    """Reivindica no máximo um job pendente com compare-and-set atômico."""
    candidate_id = db.session.scalar(
        select(AnalyticsRun.id).where(
            AnalyticsRun.source_kind == META_SYNC_SOURCE_KIND,
            AnalyticsRun.processor_version == META_SYNC_VERSION,
            AnalyticsRun.status == META_SYNC_PENDING,
        ).order_by(AnalyticsRun.created_at.asc(), AnalyticsRun.id.asc()).limit(1)
    )
    if candidate_id is None:
        return None

    now = utcnow()
    claim = db.session.execute(
        update(AnalyticsRun)
        .where(AnalyticsRun.id == candidate_id, AnalyticsRun.status == META_SYNC_PENDING)
        .values(status=META_SYNC_RUNNING, started_at=now, updated_at=now, error_message=None)
    )
    db.session.commit()
    if claim.rowcount != 1:
        return None
    return db.session.get(AnalyticsRun, candidate_id)


def _mark_meta_sync_failed(run_id: str, message: str) -> None:
    """Marca o job já reivindicado sem expor detalhes da API ao usuário."""
    now = utcnow()
    db.session.execute(
        update(AnalyticsRun)
        .where(AnalyticsRun.id == run_id, AnalyticsRun.status == META_SYNC_RUNNING)
        .values(status=META_SYNC_FAILED, completed_at=now, error_message=message[:300], updated_at=now)
    )
    db.session.commit()


def _current_connection_for_run(project: AnalyticsProject, run: AnalyticsRun):
    if project.status != "active" or project.archived_at is not None:
        raise InstagramMetaSyncError("connection_unavailable", "O projeto foi arquivado antes da sincronização.")
    account = project.instagram_account
    connection = account.api_connection if account is not None else None
    expected_connection_id = _connection_id_from_run(run)
    expected_provider = _provider_from_run(run)
    if (
        connection is None
        or connection.status != "connected"
        or expected_connection_id is None
        or expected_provider is None
        or connection.id != expected_connection_id
        or connection.provider != expected_provider
    ):
        raise InstagramMetaSyncError("connection_unavailable", "A conexão foi alterada antes da sincronização.")
    return connection


def process_next_meta_sync_run(
    *,
    settings_factory: Callable[[], InstagramMetaSettings] | None = None,
    client_factory: Callable[[InstagramMetaSettings, str], Any] | None = None,
) -> AnalyticsRun | None:
    """Executa um job reivindicado e devolve seu estado final.

    O motor de persistência continua transacional. Como o ``running`` foi
    confirmado antes da chamada do motor, uma falha faz rollback apenas dos
    dados canônicos e é então registrada no run em uma transação separada.
    """
    run = claim_next_meta_sync_run()
    if run is None:
        return None

    try:
        project = db.session.get(AnalyticsProject, run.analytics_project_id)
        if project is None:
            raise InstagramMetaSyncError("connection_unavailable", "O projeto não está mais disponível.")
        connection = _current_connection_for_run(project, run)
        settings = settings_factory() if settings_factory is not None else InstagramMetaSettings.from_mapping(current_app.config)
        if client_factory is not None:
            client = client_factory(settings, connection.provider)
        else:
            configured_factory = current_app.extensions.get("instagram_meta_client_factory")
            client = configured_factory(settings, connection.provider) if configured_factory is not None else None
        if client is None:
            from .instagram_meta import connection_provider

            client = connection_provider(settings, connection.provider)
        result = sync_meta_connection(project, connection, settings, client, run=run)
    except InstagramMetaError as error:
        db.session.rollback()
        message = safe_meta_error_message(error)
        _mark_meta_sync_failed(run.id, message)
        log_meta_operation(project_id=run.analytics_project_id, operation="sync", status=error.category, http_status=error.http_status)
    except InstagramMetaSyncError as error:
        db.session.rollback()
        message = safe_meta_sync_error_message(error)
        _mark_meta_sync_failed(run.id, message)
        log_meta_operation(project_id=run.analytics_project_id, operation="sync", status=error.category)
    except Exception:
        db.session.rollback()
        message = "Não foi possível concluir a sincronização. Nenhum dado foi salvo."
        _mark_meta_sync_failed(run.id, message)
        current_app.logger.error(
            "instagram_meta project=%s operation=sync status=unexpected_error",
            run.analytics_project_id,
        )
    else:
        log_meta_operation(project_id=run.analytics_project_id, operation="sync", status="completed")
        current_app.logger.info(
            "instagram_meta project=%s operation=sync status=completed records=%s partial_metrics=%s",
            run.analytics_project_id,
            result.observations_created,
            result.partial_metric_count,
        )
    return db.session.get(AnalyticsRun, run.id)


def run_meta_sync_worker(app: Flask, *, once: bool, poll_interval: float, stale_seconds: int) -> None:
    """Loop operacional simples para processo separado, interrompível por Ctrl+C."""
    if poll_interval <= 0:
        raise click.ClickException("O intervalo de polling precisa ser positivo.")
    if stale_seconds <= 0:
        raise click.ClickException("O prazo de recuperação precisa ser positivo.")

    with app.app_context():
        while True:
            recovered = recover_stale_meta_sync_runs(stale_seconds=stale_seconds)
            if recovered:
                current_app.logger.warning("instagram_meta worker recovered_stale_jobs=%s", recovered)
            run = process_next_meta_sync_run()
            db.session.remove()
            if once:
                return
            if run is None:
                time.sleep(poll_interval)
