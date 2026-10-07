"""Persistência atômica dos comentários e de sua evidência histórica."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from .extensions import db
from .instagram_comment_import import InstagramCommentImportDataset
from .models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramComment,
    InstagramCommentObservation,
    InstagramCommentTextVersion,
    InstagramMedia,
    utcnow,
)


class InstagramCommentImportPersistenceError(ValueError):
    """Conflito de domínio descoberto ao confirmar a prévia de comentários."""


@dataclass(frozen=True)
class InstagramCommentImportResult:
    run_id: str
    comments_created: int
    comments_reused: int
    text_versions_created: int
    observations_created: int


def _project_account(project: AnalyticsProject) -> InstagramAccount:
    account = project.instagram_account
    if account is None:
        raise InstagramCommentImportPersistenceError(
            "Importe primeiro as publicações da conta antes de importar comentários."
        )
    return account


def _media_by_external_id(account: InstagramAccount) -> dict[str, InstagramMedia]:
    return {
        media.external_id: media
        for media in db.session.scalars(
            select(InstagramMedia).where(InstagramMedia.instagram_account_id == account.id)
        )
    }


def comment_import_conflict(project: AnalyticsProject, dataset: InstagramCommentImportDataset) -> str | None:
    """Verifica somente condições que dependem do estado atual do projeto."""
    try:
        account = _project_account(project)
    except InstagramCommentImportPersistenceError as error:
        return str(error)
    media = _media_by_external_id(account)
    for row in dataset.rows:
        if row.media_external_id not in media:
            return (
                f"A publicação '{row.media_external_id}' da linha {row.line_number} não pertence "
                "a este projeto."
            )
    return None


def _validate_reply_graph(
    *, comments_by_media_external: dict[tuple[str, str], InstagramComment],
    rows,
    media_by_external: dict[str, InstagramMedia],
) -> dict[tuple[str, str], InstagramComment | None]:
    """Resolve pais sem permitir troca de vínculo, auto-referência ou ciclos."""
    rows_by_key = {(row.media_external_id, row.comment_external_id): row for row in rows}
    all_by_id = {comment.id: comment for comment in comments_by_media_external.values()}
    parent_key_by_key: dict[tuple[str, str], tuple[str, str] | None] = {}
    parent_object_by_key: dict[tuple[str, str], InstagramComment | None] = {}

    for key, comment in comments_by_media_external.items():
        if comment.parent_comment_id is None:
            parent_key_by_key[key] = None
            parent_object_by_key[key] = None
            continue
        parent = all_by_id.get(comment.parent_comment_id)
        if parent is None:
            raise InstagramCommentImportPersistenceError("Um comentário existente possui uma resposta pai inválida.")
        parent_key = (next(
            media.external_id for media in media_by_external.values() if media.id == parent.instagram_media_id
        ), parent.external_id)
        parent_key_by_key[key] = parent_key
        parent_object_by_key[key] = parent

    for row in rows:
        key = (row.media_external_id, row.comment_external_id)
        existing = comments_by_media_external.get(key)
        # A ausência do pai numa coleta posterior não é uma ordem para apagar
        # uma relação histórica já conhecida; apenas não traz essa evidência.
        desired_key = ((row.media_external_id, row.parent_comment_external_id)
                       if row.parent_comment_external_id else parent_key_by_key.get(key))
        if desired_key is not None:
            parent = comments_by_media_external.get(desired_key)
            if parent is None and desired_key not in rows_by_key:
                raise InstagramCommentImportPersistenceError(
                    f"Linha {row.line_number}: comentário pai '{row.parent_comment_external_id}' não foi encontrado na mesma publicação."
                )
            if parent is not None and parent.instagram_media_id != media_by_external[row.media_external_id].id:
                raise InstagramCommentImportPersistenceError(
                    f"Linha {row.line_number}: o comentário pai pertence a outra publicação."
                )
        if existing is not None and existing.parent_comment_id is not None:
            current_key = parent_key_by_key[key]
            if desired_key != current_key:
                raise InstagramCommentImportPersistenceError(
                    f"Linha {row.line_number}: o comentário já possui uma relação de resposta diferente."
                )
        parent_key_by_key[key] = desired_key

    # Uma cadeia somente pode caminhar por comentários do mesmo media. A busca
    # por ciclo inclui os itens já persistidos e os que serão criados nesta run.
    for initial_key in parent_key_by_key:
        visited: set[tuple[str, str]] = set()
        current = initial_key
        while current is not None:
            if current in visited:
                raise InstagramCommentImportPersistenceError("A hierarquia de respostas contém um ciclo.")
            visited.add(current)
            current = parent_key_by_key.get(current)

    for row in rows:
        key = (row.media_external_id, row.comment_external_id)
        parent_key = parent_key_by_key[key]
        parent_object_by_key[key] = comments_by_media_external.get(parent_key) if parent_key else None
    return parent_object_by_key


def persist_instagram_comment_import(
    project: AnalyticsProject, dataset: InstagramCommentImportDataset,
) -> InstagramCommentImportResult:
    """Cria uma run e todas as observações de comentário em uma transação única."""
    conflict = comment_import_conflict(project, dataset)
    if conflict:
        raise InstagramCommentImportPersistenceError(conflict)
    try:
        account = _project_account(project)
        media_by_external = _media_by_external_id(account)
        media_ids = [media.id for media in media_by_external.values()]
        existing_comments = list(db.session.scalars(
            select(InstagramComment).where(InstagramComment.instagram_media_id.in_(media_ids))
        ))
        comments_by_key = {
            (next(media.external_id for media in media_by_external.values() if media.id == comment.instagram_media_id),
             comment.external_id): comment
            for comment in existing_comments
        }
        parent_objects = _validate_reply_graph(
            comments_by_media_external=comments_by_key,
            rows=dataset.rows,
            media_by_external=media_by_external,
        )

        run = AnalyticsRun(
            analytics_project=project,
            source_kind=dataset.source_kind,
            status="completed",
            period_start=None,
            period_end=None,
            started_at=utcnow(),
            completed_at=utcnow(),
            processor_version=dataset.processor_version,
            source_hash=dataset.source_hash,
            parameters_json={
                "dataset_kind": "comments",
                "schema_version": dataset.schema_version,
                "source_filename": dataset.original_filename,
                "source_rows": dataset.source_row_count,
                "comments": len(dataset.rows),
            },
            record_count=len(dataset.rows),
        )
        db.session.add(run)
        db.session.flush()

        version_hashes: dict[str, set[str]] = {}
        if existing_comments:
            for version in db.session.scalars(select(InstagramCommentTextVersion).where(
                InstagramCommentTextVersion.instagram_comment_id.in_([comment.id for comment in existing_comments])
            )):
                version_hashes.setdefault(version.instagram_comment_id, set()).add(version.content_hash)

        created = reused = versions_created = 0
        comments_for_rows: dict[tuple[str, str], InstagramComment] = {}
        for row in dataset.rows:
            key = (row.media_external_id, row.comment_external_id)
            comment = comments_by_key.get(key)
            if comment is None:
                comment = InstagramComment(
                    instagram_media=media_by_external[row.media_external_id],
                    external_id=row.comment_external_id,
                    parent=parent_objects[key],
                    author_username=row.author_username,
                    author_external_id=row.author_external_id,
                    published_at=row.published_at,
                    original_text=row.comment_text,
                )
                db.session.add(comment)
                comments_by_key[key] = comment
                created += 1
            else:
                if comment.author_username is None and row.author_username:
                    comment.author_username = row.author_username
                if comment.author_external_id is None and row.author_external_id:
                    comment.author_external_id = row.author_external_id
                reused += 1
            comments_for_rows[key] = comment

        db.session.flush()
        # Filhos que chegaram antes dos pais podem agora ser ligados com objetos
        # persistidos, sem depender da ordem física das linhas do arquivo.
        for row in dataset.rows:
            comment = comments_for_rows[(row.media_external_id, row.comment_external_id)]
            if row.parent_comment_external_id:
                parent = comments_for_rows.get((row.media_external_id, row.parent_comment_external_id))
                if parent is None:
                    parent = comments_by_key[(row.media_external_id, row.parent_comment_external_id)]
                comment.parent = parent

            hashes = version_hashes.setdefault(comment.id, set())
            if row.comment_text_hash not in hashes:
                db.session.add(InstagramCommentTextVersion(
                    instagram_comment=comment,
                    text=row.comment_text,
                    content_hash=row.comment_text_hash,
                    first_observed_at=row.observed_at,
                    is_original=(len(hashes) == 0),
                ))
                hashes.add(row.comment_text_hash)
                versions_created += 1
            db.session.add(InstagramCommentObservation(
                instagram_comment=comment,
                analytics_run=run,
                observed_at=row.observed_at,
                like_count=row.like_count,
                replies_count=row.replies_count,
            ))
        db.session.flush()
        result = InstagramCommentImportResult(
            run_id=run.id,
            comments_created=created,
            comments_reused=reused,
            text_versions_created=versions_created,
            observations_created=len(dataset.rows),
        )
        db.session.commit()
        return result
    except Exception:
        db.session.rollback()
        raise


__all__ = [
    "InstagramCommentImportPersistenceError", "InstagramCommentImportResult", "comment_import_conflict",
    "persist_instagram_comment_import",
]
