"""Armazenamento e autorização de execuções, sem participar da análise textual."""

from __future__ import annotations

import json
import errno
import logging
import math
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from uuid import UUID, uuid4

from flask import abort, current_app
from sqlalchemy import delete, select
from werkzeug.utils import secure_filename

from .extensions import db
from .models import Analysis, AnalysisDocument, Project, User, utcnow


def analysis_root() -> Path:
    """Dados mutáveis ficam sob o volume persistente PLATFORM_DATA_DIR."""
    base = Path(current_app.config["PLATFORM_DATA_DIR"]).resolve()
    root = base / "analyses"
    if root.is_symlink() or root.resolve().parent != base:
        raise ValueError("Diretório de análises inválido.")
    root.mkdir(parents=True, exist_ok=True)
    return root


def analysis_dir(analysis_id: str) -> Path:
    identifier = str(UUID(analysis_id))
    root = analysis_root()
    target = root / identifier
    if target.is_symlink() or target.resolve().parent != root:
        raise ValueError("Diretório da análise inválido.")
    return target


def get_analysis(analysis_id: str, user: User) -> Analysis:
    try:
        identifier = str(UUID(analysis_id))
    except ValueError:
        abort(404)
    analysis = db.session.get(Analysis, identifier)
    project = db.session.get(Project, analysis.project_id) if analysis is not None and analysis.project_id else None
    if analysis is None or (analysis.user_id != user.id and user.role != "admin"
                            and (project is None or project.owner_user_id != user.id)):
        abort(404)
    return analysis


def create_analysis(*, user_id: str, project_id: str | None, tool_id: str,
                    tool_version: str, parameters: dict, name: str | None = None,
                    analysis_id: str | None = None) -> Analysis:
    analysis = Analysis(
        id=str(UUID(analysis_id)) if analysis_id else str(uuid4()),
        user_id=user_id, project_id=project_id,
        source_type="project" if project_id else "standard",
        name=name or "",
        name_confirmed=bool(name and name.strip()),
        tool_id=tool_id, tool_version=tool_version,
        status="processando", parameters_json=parameters,
        document_count=0, result_count=0, excel_files_json=[],
    )
    directory = analysis_dir(analysis.id)
    directory.mkdir(exist_ok=False)
    try:
        db.session.add(analysis)
        db.session.commit()
    except Exception:
        db.session.rollback()
        directory.rmdir()
        raise
    return analysis


def preserve_documents(analysis: Analysis, files: list[tuple[Path, str]]) -> None:
    destination = analysis_dir(analysis.id) / "documents"
    destination.mkdir(exist_ok=True)
    for number, (source, original_name) in enumerate(files, start=1):
        safe = secure_filename(original_name) or "documento.pdf"
        stored = f"{number:03d}-{safe}"
        shutil.copy2(source, destination / stored)
        db.session.add(AnalysisDocument(
            analysis_id=analysis.id, original_name=original_name[:255], stored_name=stored,
        ))
    analysis.document_count = len(files)
    db.session.commit()


def _json_safe(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if type(value).__name__ == "NAType":
        return None
    if hasattr(value, "columns") and hasattr(value, "to_dict"):
        return _json_safe(value.to_dict("records"))
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    if hasattr(value, "item"):
        return _json_safe(value.item())
    raise TypeError(f"Tipo de resultado não serializável: {type(value).__name__}")


def save_success(analysis: Analysis, result: dict, excel_files: list[tuple[str, Path]],
                 result_count: int) -> None:
    """Publica JSON e XLSX antes de sinalizar a conclusão no banco."""
    directory = analysis_dir(analysis.id)
    saved = []
    for name, source in excel_files:
        if name != Path(name).name or not name.endswith(".xlsx"):
            raise ValueError("Nome de planilha inválido.")
        destination = directory / name
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        saved.append(name)
    payload = _json_safe(result)
    temporary = directory / "result.json.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / "result.json")
    analysis.excel_files_json = saved
    analysis.result_count = result_count
    analysis.completed_at = utcnow()
    analysis.status = "concluida"
    analysis.error_message = None
    db.session.commit()


def save_error(analysis_id: str, public_message: str) -> None:
    analysis = db.session.get(Analysis, analysis_id)
    if analysis is None:
        return
    analysis.status = "erro"
    analysis.error_message = public_message[:300]
    analysis.completed_at = utcnow()
    db.session.commit()


def load_result(analysis: Analysis) -> dict:
    if analysis.status != "concluida":
        abort(404)
    path = analysis_dir(analysis.id) / "result.json"
    if not path.is_file() or path.is_symlink():
        abort(404)
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def documents_for(analysis: Analysis) -> list[AnalysisDocument]:
    return db.session.scalars(
        select(AnalysisDocument).where(AnalysisDocument.analysis_id == analysis.id)
        .order_by(AnalysisDocument.stored_name)
    ).all()


def document_paths(analysis: Analysis) -> list[tuple[Path, str]]:
    folder = analysis_dir(analysis.id) / "documents"
    return [(folder / item.stored_name, item.original_name) for item in documents_for(analysis)]


def _move_artifact(source: Path, destination: Path) -> None:
    """Move um diretório sem depender de origem e destino no mesmo filesystem."""
    if destination.exists() or destination.is_symlink():
        raise OSError(f"Destino de quarentena já existe: {destination}")
    try:
        source.rename(destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise

    # Uma cópia completa antecede a remoção da origem. Assim, falhas durante
    # a cópia mantêm o original intacto, inclusive no caminho de rollback.
    try:
        shutil.copytree(source, destination, symlinks=True)
    except Exception as error:
        try:
            if destination.exists():
                shutil.rmtree(destination)
        except Exception as cleanup_error:
            raise ExceptionGroup("Falha ao limpar cópia parcial da análise.",
                                 [error, cleanup_error]) from error
        raise
    try:
        shutil.rmtree(source)
    except Exception as error:
        try:
            shutil.copytree(destination, source, dirs_exist_ok=True, symlinks=True)
            shutil.rmtree(destination)
        except Exception as restore_error:
            raise ExceptionGroup("Falha ao restaurar artefato após erro de I/O.",
                                 [error, restore_error]) from error
        raise


def delete_analysis(analysis: Analysis) -> None:
    """Exclui apenas os artefatos próprios; PDFs de projetos não são referenciados."""
    directory = analysis_dir(analysis.id)
    quarantine = directory.with_name(directory.name + ".removing")
    if quarantine.exists() or quarantine.is_symlink():
        raise OSError("Exclusão anterior incompleta; revisão necessária.")
    staged = []
    candidates = [(directory, "analysis")]
    if analysis.tool_id == "pdf_scraper":
        application_root = Path(current_app.root_path).resolve()
        for label, base in (("upload", application_root / "uploads"),
                            ("legacy-output", application_root / "outputs")):
            source = base / analysis.id
            if source.resolve().parent != base.resolve() or source.is_symlink():
                raise OSError("Diretório antigo da execução inválido.")
            candidates.append((source, label))
    quarantine.mkdir()
    try:
        for source, label in candidates:
            if source.exists():
                destination = quarantine / label
                _move_artifact(source, destination)
                staged.append((source, destination))
        db.session.execute(delete(AnalysisDocument).where(AnalysisDocument.analysis_id == analysis.id))
        db.session.delete(analysis)
        db.session.commit()
    except Exception as error:
        db.session.rollback()
        rollback_errors = []
        for source, destination in reversed(staged):
            if destination.exists():
                try:
                    _move_artifact(destination, source)
                except Exception as restore_error:
                    rollback_errors.append(restore_error)
        try:
            quarantine.rmdir()
        except OSError as cleanup_error:
            rollback_errors.append(cleanup_error)
        if rollback_errors:
            raise ExceptionGroup("Falha na exclusão e na restauração dos artefatos.",
                                 [error, *rollback_errors]) from error
        raise
    try:
        shutil.rmtree(quarantine)
    except OSError:
        logging.getLogger(__name__).exception("Quarentena da análise não pôde ser limpa: %s", quarantine)
