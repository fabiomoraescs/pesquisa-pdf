"""Prévia temporária compartilhada entre workers, sem guardar o upload bruto."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from time import time as now_timestamp
from uuid import UUID

from flask import current_app

from .instagram_import import (
    IMPORT_PROCESSOR_VERSION,
    IMPORT_SCHEMA_VERSION,
    PREVIEW_TTL_SECONDS,
    InstagramAccountSnapshotData,
    InstagramImportDataset,
    InstagramImportPreview,
    InstagramImportRow,
)


_STORE_VERSION = 1
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class InstagramPreviewExpired(ValueError):
    """O artefato existia, mas ultrapassou o TTL persistido."""


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _decode_preview(value: dict) -> InstagramImportPreview:
    data = value["dataset"]
    if data["schema_version"] != IMPORT_SCHEMA_VERSION or data["processor_version"] != IMPORT_PROCESSOR_VERSION:
        raise ValueError("Versão da prévia incompatível")
    rows = tuple(InstagramImportRow(
        line_number=row["line_number"],
        media_external_id=row["media_external_id"],
        published_at=_datetime(row["published_at"]),
        observed_at=_datetime(row["observed_at"]),
        period_start=_datetime(row["period_start"]),
        period_end=_datetime(row["period_end"]),
        media_type=row["media_type"],
        permalink=row["permalink"],
        caption=row["caption"],
        media_metrics=row["media_metrics"],
    ) for row in data["rows"])
    snapshot_data = data["account_snapshot"]
    snapshot = None if snapshot_data is None else InstagramAccountSnapshotData(
        observed_at=_datetime(snapshot_data["observed_at"]),
        period_start=_datetime(snapshot_data["period_start"]),
        period_end=_datetime(snapshot_data["period_end"]),
        metrics=snapshot_data["metrics"],
    )
    dataset = InstagramImportDataset(
        schema_version=data["schema_version"],
        processor_version=data["processor_version"],
        source_kind=data["source_kind"],
        original_filename=data["original_filename"],
        source_hash=data["source_hash"],
        source_row_count=data["source_row_count"],
        rows=rows,
        account_username=data["account_username"],
        account_username_normalized=data["account_username_normalized"],
        account_display_name=data["account_display_name"],
        account_external_id=data["account_external_id"],
        account_snapshot=snapshot,
        warnings=tuple(data["warnings"]),
    )
    return InstagramImportPreview(
        dataset=dataset,
        known_media_count=value["known_media_count"],
        new_media_count=value["new_media_count"],
        repeated_source_hash=value["repeated_source_hash"],
        warnings=tuple(value["warnings"]),
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Tipo não serializável: {type(value).__name__}")


@dataclass
class PreviewClaim:
    """Posse exclusiva do token até commit ou rollback da importação."""

    preview: InstagramImportPreview
    path: Path
    expires_at: float

    def complete(self) -> None:
        self.path.unlink(missing_ok=True)

    def release(self) -> None:
        if now_timestamp() >= self.expires_at:
            self.complete()
        elif self.path.exists():
            os.replace(self.path, self.path.with_suffix(".json"))


class InstagramImportPreviews:
    """Arquivos JSON por projeto e usuário; rename atômico impede confirmação dupla."""

    def __init__(self, ttl_seconds: int = PREVIEW_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _directory(user_id: str, project_id: str, *, create: bool = False) -> Path:
        # IDs e token nunca são tratados como fragmentos de caminho arbitrários.
        user_key, project_key = str(UUID(user_id)), str(UUID(project_id))
        root = Path(current_app.config["PLATFORM_DATA_DIR"]).resolve()
        directory = root / "analytics" / project_key / "import_previews" / user_key
        if create:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(root):
            raise ValueError("Diretório de prévias fora da área de dados")
        return directory

    @staticmethod
    def _path(directory: Path, token: str) -> Path | None:
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            return None
        path = directory / f"{token}.json"
        return None if path.is_symlink() else path

    @staticmethod
    def _read(path: Path, user_id: str, project_id: str) -> tuple[InstagramImportPreview, float] | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            if (
                envelope["format_version"] != _STORE_VERSION
                or envelope["user_id"] != str(UUID(user_id))
                or envelope["project_id"] != str(UUID(project_id))
            ):
                return None
            expires_at = float(envelope["expires_at"])
            if now_timestamp() >= expires_at:
                path.unlink(missing_ok=True)
                raise InstagramPreviewExpired()
            return _decode_preview(envelope["preview"]), expires_at
        except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, InstagramPreviewExpired):
                raise
            return None

    def _purge(self, directory: Path, *, exclude: Path | None = None) -> None:
        if not directory.exists():
            return
        for path in directory.iterdir():
            if path == exclude or path.suffix not in {".json", ".claim"} or path.is_symlink():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    expires_at = float(json.load(handle)["expires_at"])
                if now_timestamp() >= expires_at:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                # Arquivos incompletos/desconhecidos não são consumidos como prévias.
                continue

    def put(self, user_id: str, project_id: str, preview: InstagramImportPreview) -> str:
        directory = self._directory(user_id, project_id, create=True)
        self._purge(directory)
        token = secrets.token_urlsafe(32)
        path = self._path(directory, token)
        assert path is not None
        created_at = now_timestamp()
        envelope = {
            "format_version": _STORE_VERSION,
            "created_at": created_at,
            "expires_at": created_at + self.ttl_seconds,
            "user_id": str(UUID(user_id)),
            "project_id": str(UUID(project_id)),
            "preview": asdict(preview),
        }
        payload = json.dumps(envelope, default=_json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        temporary = directory / f".{token}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return token

    def get(self, token: str, user_id: str, project_id: str) -> InstagramImportPreview | None:
        directory = self._directory(user_id, project_id)
        path = self._path(directory, token)
        if path is None:
            return None
        try:
            loaded = self._read(path, user_id, project_id)
        finally:
            self._purge(directory, exclude=path)
        return loaded[0] if loaded else None

    def claim(self, token: str, user_id: str, project_id: str) -> PreviewClaim | None:
        directory = self._directory(user_id, project_id)
        path = self._path(directory, token)
        if path is None:
            return None
        claimed = path.with_suffix(".claim")
        if claimed.is_symlink() or claimed.exists():
            return None
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            return None
        try:
            loaded = self._read(claimed, user_id, project_id)
            self._purge(directory, exclude=claimed)
            if loaded is None:
                claimed.unlink(missing_ok=True)
                return None
            return PreviewClaim(loaded[0], claimed, loaded[1])
        except Exception:
            claimed.unlink(missing_ok=True)
            raise

    def discard(self, token: str, user_id: str, project_id: str) -> None:
        directory = self._directory(user_id, project_id)
        path = self._path(directory, token)
        if path is not None:
            path.unlink(missing_ok=True)
        self._purge(directory)


previews = InstagramImportPreviews()
