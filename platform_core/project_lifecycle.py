"""Arquivamento e exclusão explícita dos dados exclusivos de projetos."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from flask import current_app
from sqlalchemy import delete, select

from .extensions import db
from .models import Analysis, AnalysisDocument, Project, ProjectLibrary, ProjectVocabularyVersion, User, utcnow
from .qualitative import delete_qualitative_dependents
from .scraping_types import QUALITATIVE_TOOL
from .services import record_audit
from .vocabularies import forget_project_store


class ProjectActionError(ValueError):
    """A ação não satisfaz o estado ou a confirmação exigida."""


def archive(project: Project, actor: User) -> None:
    if project.status != "active" or project.deleted_at is not None:
        raise ProjectActionError("Somente projetos ativos podem ser arquivados.")
    project.status = "archived"
    project.archived_at = utcnow()
    record_audit(actor, "project_archived", "project", project.id,
                 {"status": "active", "owner_user_id": project.owner_user_id},
                 {"status": "archived", "owner_user_id": project.owner_user_id})
    db.session.commit()


def restore(project: Project, actor: User) -> None:
    if project.status != "archived" or project.deleted_at is not None:
        raise ProjectActionError("Somente projetos arquivados podem ser desarquivados.")
    project.status = "active"
    project.archived_at = None
    record_audit(actor, "project_restored", "project", project.id,
                 {"status": "archived", "owner_user_id": project.owner_user_id},
                 {"status": "active", "owner_user_id": project.owner_user_id})
    db.session.commit()


def _project_directory(project_id: str) -> Path:
    # UUID validado antes de formar qualquer caminho de remoção.
    UUID(project_id)
    root = (Path(current_app.config["PLATFORM_DATA_DIR"]) / "projects").resolve()
    target = root / project_id
    if target.parent != root or target.is_symlink() or target.resolve().parent != root:
        raise ProjectActionError("Diretório do projeto inválido; exclusão cancelada.")
    return target


def delete_archived(projects: list[Project], actor: User, confirmation: str) -> int:
    return _delete_projects(projects, actor, confirmation, legacy_cleanup=False)


def purge_legacy_deleted(projects: list[Project], actor: User | None, confirmation: str) -> int:
    """Remove registros legados já excluídos, sem atribuir manutenção local a uma conta."""
    return _delete_projects(projects, actor, confirmation, legacy_cleanup=True)


def _delete_projects(projects: list[Project], actor: User | None, confirmation: str, *, legacy_cleanup: bool) -> int:
    """Valida o lote inteiro; snapshots são isolados antes do commit SQL.

    Se o commit falhar, os diretórios são restaurados. Após o commit, uma falha
    excepcional na limpeza deixa apenas uma quarentena recuperável e é logada.
    """
    if confirmation.strip() != "deletar":
        raise ProjectActionError('Digite exatamente "deletar" para confirmar a exclusão.')
    if not projects or len({project.id for project in projects}) != len(projects):
        raise ProjectActionError("Selecione ao menos um projeto arquivado válido.")
    if legacy_cleanup:
        if (actor is not None and actor.role != "admin") or any(
            project.status != "deleted" or project.deleted_at is None for project in projects
        ):
            raise ProjectActionError("Somente administradores podem limpar projetos legados já excluídos.")
    elif any(project.status != "archived" or project.deleted_at is not None for project in projects):
        raise ProjectActionError("Todos os projetos selecionados precisam estar arquivados.")
    if not legacy_cleanup and actor is None:
        raise ProjectActionError("A exclusão exige uma conta autenticada.")
    if actor is not None and actor.role != "admin" and any(project.owner_user_id != actor.id for project in projects):
        raise ProjectActionError("Um dos projetos não pertence à sua conta.")
    from historico_racial.routes import JOBS_LOCK, PROGRESSOS_HR, RESULTADOS_HR

    root = Path(current_app.config["PLATFORM_DATA_DIR"]).resolve()
    deletion_root = root / "deletions"
    if deletion_root.is_symlink() or deletion_root.resolve().parent != root:
        raise ProjectActionError("Diretório de quarentena inválido; exclusão cancelada.")
    quarantine = deletion_root / str(uuid4())
    if quarantine.exists() or quarantine.is_symlink() or quarantine.resolve().parent != deletion_root:
        raise ProjectActionError("Diretório de quarentena inválido; exclusão cancelada.")
    staged: list[tuple[Path, Path]] = []
    selected = {project.id for project in projects}
    analyses = db.session.scalars(select(Analysis).where(Analysis.project_id.in_(selected))).all()
    if any(item.status == "processando" for item in analyses):
        raise ProjectActionError("Aguarde o término das análises antes de excluir o projeto.")
    with JOBS_LOCK:
        if any(data.get("project_id") in selected and data.get("status") == "processando"
               for data in PROGRESSOS_HR.values()):
            raise ProjectActionError("Aguarde o término do processamento antes de excluir o projeto.")
        try:
            from .analyses import analysis_dir
            for analysis in analyses:
                source = analysis_dir(analysis.id)
                if source.exists():
                    quarantine.mkdir(parents=True, exist_ok=True)
                    destination = quarantine / f"analysis-{analysis.id}"
                    os.replace(source, destination)
                    staged.append((source, destination))
                if analysis.tool_id == "pdf_scraper":
                    application_root = Path(current_app.root_path).resolve()
                    for label, folder in (("upload", "uploads"), ("legacy-output", "outputs")):
                        root_for_job = (application_root / folder).resolve()
                        old_source = root_for_job / analysis.id
                        if old_source.is_symlink() or old_source.resolve().parent != root_for_job:
                            raise ProjectActionError("Artefato antigo inválido; exclusão cancelada.")
                        if old_source.exists():
                            quarantine.mkdir(parents=True, exist_ok=True)
                            old_destination = quarantine / f"{label}-{analysis.id}"
                            os.replace(old_source, old_destination)
                            staged.append((old_source, old_destination))
            for project in projects:
                source = _project_directory(project.id)
                if source.exists():
                    quarantine.mkdir(parents=True, exist_ok=True)
                    destination = quarantine / project.id
                    os.replace(source, destination)
                    staged.append((source, destination))
            for project in projects:
                for analysis in [item for item in analyses if item.project_id == project.id]:
                    if analysis.tool_id == QUALITATIVE_TOOL:
                        delete_qualitative_dependents((analysis.id,))
                    db.session.execute(delete(AnalysisDocument).where(AnalysisDocument.analysis_id == analysis.id))
                    db.session.delete(analysis)
                db.session.execute(delete(ProjectVocabularyVersion).where(ProjectVocabularyVersion.project_id == project.id))
                db.session.execute(delete(ProjectLibrary).where(ProjectLibrary.project_id == project.id))
                record_audit(actor, "project_permanently_deleted", "project", project.id,
                             {"status": project.status, "owner_user_id": project.owner_user_id, "name": project.name},
                             {"deleted": True})
                db.session.delete(project)
            db.session.commit()
        except Exception:
            db.session.rollback()
            for source, destination in reversed(staged):
                if destination.exists():
                    os.replace(destination, source)
            if quarantine.exists():
                quarantine.rmdir()
            raise
        for project_id in selected:
            forget_project_store(project_id)
        for job_id, data in list(PROGRESSOS_HR.items()):
            if data.get("project_id") in selected:
                PROGRESSOS_HR.pop(job_id, None)
                RESULTADOS_HR.pop(job_id, None)
        from app import ANALISES, ANALISES_LOCK, PROGRESSOS, PROGRESSOS_LOCK
        with ANALISES_LOCK, PROGRESSOS_LOCK:
            for analysis in analyses:
                ANALISES.pop(analysis.id, None)
                PROGRESSOS.pop(analysis.id, None)
                RESULTADOS_HR.pop(analysis.id, None)
                PROGRESSOS_HR.pop(analysis.id, None)
    if quarantine.exists():
        if quarantine.is_symlink() or quarantine.resolve().parent != deletion_root:
            logging.getLogger(__name__).error("Quarentena fora do diretório previsto: %s", quarantine)
            return len(projects)
        try:
            shutil.rmtree(quarantine)
        except OSError:
            logging.getLogger(__name__).exception("Quarentena de projeto não pôde ser limpa: %s", quarantine)
            # Os dados já não são acessíveis pela plataforma; a quarentena pode
            # ser inspecionada e removida manualmente, sem tocar em outros projetos.
    return len(projects)
