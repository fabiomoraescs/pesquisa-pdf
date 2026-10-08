"""Arquivamento e exclusão definitiva explícita de projetos Instagram."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from flask import current_app
from sqlalchemy import delete, or_, select

from .extensions import db
from .models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramAccountSnapshot,
    InstagramComment,
    InstagramCommentAssessment,
    InstagramCommentObservation,
    InstagramCommentTextVersion,
    InstagramInsightEvidence,
    InstagramInsightFinding,
    InstagramInsightRun,
    InstagramRecommendation,
    InstagramRecommendationFinding,
    InstagramRecommendationRun,
    InstagramMedia,
    InstagramMediaClassification,
    InstagramMediaObservation,
    InstagramTaxonomyCategory,
    InstagramTaxonomyDimension,
    User,
)
from .services import record_audit


class InstagramProjectDeletionError(ValueError):
    """A exclusão permanente não satisfaz as condições de segurança."""


def _data_root() -> Path:
    return Path(current_app.config["PLATFORM_DATA_DIR"]).resolve()


def _project_directory(project_id: str) -> Path:
    """Retorna somente o diretório exclusivo e validado do projeto Instagram."""
    project_key = str(UUID(project_id))
    root = _data_root()
    analytics_root = root / "analytics"
    if analytics_root.is_symlink():
        raise InstagramProjectDeletionError("Diretório de dados Instagram inválido; exclusão cancelada.")
    if analytics_root.exists() and (
        not analytics_root.is_dir() or analytics_root.resolve().parent != root
    ):
        raise InstagramProjectDeletionError("Diretório de dados Instagram inválido; exclusão cancelada.")
    target = analytics_root / project_key
    if target.is_symlink() or (
        target.exists() and (not target.is_dir() or target.resolve().parent != analytics_root.resolve())
    ):
        raise InstagramProjectDeletionError("Diretório do projeto inválido; exclusão cancelada.")
    return target


def _quarantine_directory() -> Path:
    root = _data_root()
    deletion_root = root / "deletions"
    if deletion_root.is_symlink():
        raise InstagramProjectDeletionError("Diretório de quarentena inválido; exclusão cancelada.")
    deletion_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not deletion_root.is_dir() or deletion_root.resolve().parent != root:
        raise InstagramProjectDeletionError("Diretório de quarentena inválido; exclusão cancelada.")
    quarantine = deletion_root / str(uuid4())
    if quarantine.exists() or quarantine.is_symlink() or quarantine.resolve().parent != deletion_root:
        raise InstagramProjectDeletionError("Diretório de quarentena inválido; exclusão cancelada.")
    return quarantine


def _delete_project_records(project_id: str) -> None:
    """Remove os dependentes do projeto na ordem das chaves estrangeiras reais."""
    account_ids = select(InstagramAccount.id).where(
        InstagramAccount.analytics_project_id == project_id
    )
    media_ids = select(InstagramMedia.id).where(
        InstagramMedia.instagram_account_id.in_(account_ids)
    )
    comment_ids = select(InstagramComment.id).where(
        InstagramComment.instagram_media_id.in_(media_ids)
    )
    run_ids = select(AnalyticsRun.id).where(AnalyticsRun.analytics_project_id == project_id)
    insight_run_ids = select(InstagramInsightRun.id).where(
        InstagramInsightRun.analytics_project_id == project_id
    )
    insight_finding_ids = select(InstagramInsightFinding.id).where(
        InstagramInsightFinding.instagram_insight_run_id.in_(insight_run_ids)
    )
    recommendation_run_ids = select(InstagramRecommendationRun.id).where(
        InstagramRecommendationRun.analytics_project_id == project_id
    )
    recommendation_ids = select(InstagramRecommendation.id).where(
        InstagramRecommendation.instagram_recommendation_run_id.in_(recommendation_run_ids)
    )
    dimension_ids = select(InstagramTaxonomyDimension.id).where(
        InstagramTaxonomyDimension.analytics_project_id == project_id
    )

    # A cadeia Recommendation -> Finding -> Evidence e os demais dependentes
    # precisam ser apagados antes dos runs e, por fim, do projeto.
    db.session.execute(delete(InstagramRecommendationFinding).where(
        InstagramRecommendationFinding.instagram_recommendation_id.in_(recommendation_ids)
    ))
    db.session.execute(delete(InstagramRecommendation).where(
        InstagramRecommendation.instagram_recommendation_run_id.in_(recommendation_run_ids)
    ))
    db.session.execute(delete(InstagramRecommendationRun).where(
        InstagramRecommendationRun.analytics_project_id == project_id
    ))
    # Evidências polimórficas não têm FK para as fontes, mas os achados e runs
    # têm FK real entre si; apagamos nessa ordem antes do projeto.
    db.session.execute(delete(InstagramInsightEvidence).where(
        InstagramInsightEvidence.instagram_insight_finding_id.in_(insight_finding_ids)
    ))
    db.session.execute(delete(InstagramInsightFinding).where(
        InstagramInsightFinding.instagram_insight_run_id.in_(insight_run_ids)
    ))
    db.session.execute(delete(InstagramInsightRun).where(
        InstagramInsightRun.analytics_project_id == project_id
    ))
    # Inclui classificações atuais e históricas. A segunda condição protege a
    # limpeza mesmo se um registro legado apontar para uma dimensão do projeto.
    db.session.execute(delete(InstagramMediaClassification).where(or_(
        InstagramMediaClassification.instagram_media_id.in_(media_ids),
        InstagramMediaClassification.instagram_taxonomy_dimension_id.in_(dimension_ids),
    )))
    # Comentários são dependentes das mídias, mas suas versões, avaliações e
    # observações precisam desaparecer antes da auto-FK de respostas.
    db.session.execute(delete(InstagramCommentAssessment).where(
        InstagramCommentAssessment.instagram_comment_id.in_(comment_ids)
    ))
    db.session.execute(delete(InstagramCommentObservation).where(or_(
        InstagramCommentObservation.instagram_comment_id.in_(comment_ids),
        InstagramCommentObservation.analytics_run_id.in_(run_ids),
    )))
    db.session.execute(delete(InstagramCommentTextVersion).where(
        InstagramCommentTextVersion.instagram_comment_id.in_(comment_ids)
    ))
    db.session.execute(delete(InstagramComment).where(
        InstagramComment.instagram_media_id.in_(media_ids)
    ))
    db.session.execute(delete(InstagramMediaObservation).where(or_(
        InstagramMediaObservation.instagram_media_id.in_(media_ids),
        InstagramMediaObservation.analytics_run_id.in_(run_ids),
    )))
    db.session.execute(delete(InstagramAccountSnapshot).where(or_(
        InstagramAccountSnapshot.instagram_account_id.in_(account_ids),
        InstagramAccountSnapshot.analytics_run_id.in_(run_ids),
    )))
    db.session.execute(delete(InstagramTaxonomyCategory).where(
        InstagramTaxonomyCategory.instagram_taxonomy_dimension_id.in_(dimension_ids)
    ))
    db.session.execute(delete(InstagramTaxonomyDimension).where(
        InstagramTaxonomyDimension.analytics_project_id == project_id
    ))
    db.session.execute(delete(InstagramMedia).where(
        InstagramMedia.instagram_account_id.in_(account_ids)
    ))
    db.session.execute(delete(AnalyticsRun).where(AnalyticsRun.analytics_project_id == project_id))
    db.session.execute(delete(InstagramAccount).where(
        InstagramAccount.analytics_project_id == project_id
    ))
    db.session.execute(delete(AnalyticsProject).where(AnalyticsProject.id == project_id))


def _restore_staged_directory(staged: tuple[Path, Path] | None) -> None:
    if staged is None:
        return
    source, destination = staged
    if destination.exists() and not source.exists():
        os.replace(destination, source)
    quarantine = destination.parent
    try:
        if quarantine.exists() and not any(quarantine.iterdir()):
            quarantine.rmdir()
    except OSError:
        logging.getLogger(__name__).exception(
            "Quarentena Instagram não pôde ser limpa após rollback: %s", quarantine
        )


def delete_instagram_project_permanently(
    project: AnalyticsProject, actor: User, confirmation: str,
) -> None:
    """Remove um projeto Instagram já arquivado, seus dependentes e prévias.

    O banco é transacional. Como o filesystem não participa dessa transação, o
    diretório exclusivo do projeto é movido para uma quarentena validada antes
    do commit e restaurado se qualquer operação de banco falhar.
    """
    if confirmation.strip() != "deletar":
        raise InstagramProjectDeletionError('Digite exatamente "deletar" para confirmar a exclusão.')
    if project.module_key != "instagram":
        raise InstagramProjectDeletionError("Projeto Instagram inválido.")
    if project.status != "archived" or project.archived_at is None:
        raise InstagramProjectDeletionError("Somente projetos arquivados podem ser excluídos permanentemente.")
    if project.owner_user_id != actor.id:
        raise InstagramProjectDeletionError("O projeto não pertence à sua conta.")

    source = _project_directory(project.id)
    staged: tuple[Path, Path] | None = None
    try:
        if source.exists():
            quarantine = _quarantine_directory()
            quarantine.mkdir(mode=0o700)
            if quarantine.is_symlink() or quarantine.resolve().parent != quarantine.parent:
                raise InstagramProjectDeletionError("Diretório de quarentena inválido; exclusão cancelada.")
            destination = quarantine / f"instagram-project-{project.id}"
            os.replace(source, destination)
            staged = (source, destination)

        _delete_project_records(project.id)
        record_audit(
            actor,
            "analytics_instagram_project_permanently_deleted",
            "analytics_project",
            project.id,
            {
                "status": project.status,
                "owner_user_id": project.owner_user_id,
                "name": project.name,
            },
            {"deleted": True},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        _restore_staged_directory(staged)
        raise

    if staged is not None:
        quarantine = staged[1].parent
        try:
            shutil.rmtree(quarantine)
        except OSError:
            # Depois do commit, a quarentena é isolada e não fica acessível
            # pela plataforma. O erro é visível no log para remoção segura.
            logging.getLogger(__name__).exception(
                "Quarentena de projeto Instagram não pôde ser limpa: %s", quarantine
            )


__all__ = ["InstagramProjectDeletionError", "delete_instagram_project_permanently"]
