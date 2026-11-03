"""Validação integral de importações CSV/XLSX de comentários Instagram.

O módulo não toca no banco: ele transforma o arquivo em DTOs imutáveis para
que a prévia e a persistência transacional compartilhem exatamente os mesmos
dados validados.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .instagram_import import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_COLUMNS,
    MAX_IMPORT_ROWS,
    MAX_COUNT_VALUE,
    InstagramImportError,
    InstagramImportField,
    _csv_rows,
    _iso_datetime,
    _optional_count,
    _optional_text,
    _required_text,
    _safe_xlsx_rows,
    sanitized_import_filename,
)


COMMENT_IMPORT_SCHEMA_VERSION = "instagram-comments-import-v1"
COMMENT_IMPORT_PROCESSOR_VERSION = "instagram-comments-import-v1"
MAX_COMMENT_TEXT_CHARS = 10_000
MAX_COMMENT_AUTHOR_CHARS = 100

# Uma única definição sustenta parser, tela de orientação e modelo XLSX.
COMMENT_IMPORT_FIELDS = (
    InstagramImportField("media_external_id", True, "text", "ID estável da publicação a que o comentário pertence.", "post_123"),
    InstagramImportField("comment_external_id", True, "text", "ID estável do comentário na origem.", "comment_456"),
    InstagramImportField("comment_text", True, "text", "Texto do comentário exatamente como foi observado.", "Gostei muito deste conteúdo!"),
    InstagramImportField("published_at", True, "datetime", "Data e hora de publicação do comentário.", "2026-10-02T14:30:00-03:00"),
    InstagramImportField("observed_at", True, "datetime", "Data e hora em que comentário e métricas foram observados.", "2026-10-03T09:00:00-03:00"),
    InstagramImportField("parent_comment_external_id", False, "text", "ID externo do comentário pai, quando for uma resposta.", "comment_400"),
    InstagramImportField("author_username", False, "text", "Username visível do autor, sem @.", "leitora_exemplo"),
    InstagramImportField("author_external_id", False, "text", "ID externo do autor, quando disponível.", "user_789"),
    InstagramImportField("like_count", False, "count", "Curtidas observadas no comentário.", "12"),
    InstagramImportField("replies_count", False, "count", "Número de respostas observadas no comentário.", "2"),
)
COMMENT_CANONICAL_HEADERS = tuple(field.name for field in COMMENT_IMPORT_FIELDS)
COMMENT_REQUIRED_HEADERS = tuple(field.name for field in COMMENT_IMPORT_FIELDS if field.required)


def comment_text_hash(text: str) -> str:
    """Hash conservador: só normaliza terminadores de linha para detectar edição."""
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _comment_text(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise InstagramImportError(f"{location}: informe um texto válido.")
    # Espaços e pontuação integram a evidência textual; apenas a ausência real
    # de conteúdo é inválida. A normalização dos terminadores é documentada no
    # hash e evita versões artificiais entre CSV e XLSX.
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise InstagramImportError(f"{location}: campo obrigatório vazio.")
    if len(text) > MAX_COMMENT_TEXT_CHARS:
        raise InstagramImportError(f"{location}: excede o limite de {MAX_COMMENT_TEXT_CHARS} caracteres.")
    return text


def _validate_comment_headers(headers: list[object], location: str) -> tuple[str, ...]:
    if not headers:
        raise InstagramImportError(f"{location}: cabeçalhos ausentes.")
    if len(headers) > MAX_IMPORT_COLUMNS:
        raise InstagramImportError(f"{location}: o arquivo excede o limite de colunas.")
    if any(not isinstance(header, str) for header in headers):
        raise InstagramImportError(f"{location}: todos os cabeçalhos devem ser texto.")
    normalized = tuple(header.strip() for header in headers)
    if len(set(normalized)) != len(normalized):
        raise InstagramImportError(f"{location}: há cabeçalhos duplicados.")
    missing = [header for header in COMMENT_REQUIRED_HEADERS if header not in normalized]
    if missing:
        raise InstagramImportError(f"Coluna obrigatória ausente: {missing[0]}.")
    unexpected = [header for header in normalized if header not in COMMENT_CANONICAL_HEADERS]
    if unexpected:
        raise InstagramImportError(f"Coluna não reconhecida neste formato: {unexpected[0]}.")
    return normalized


@dataclass(frozen=True)
class InstagramCommentImportRow:
    line_number: int
    media_external_id: str
    comment_external_id: str
    comment_text: str
    comment_text_hash: str
    published_at: datetime
    observed_at: datetime
    parent_comment_external_id: str | None
    author_username: str | None
    author_external_id: str | None
    like_count: int | None
    replies_count: int | None


@dataclass(frozen=True)
class InstagramCommentImportDataset:
    schema_version: str
    processor_version: str
    source_kind: str
    original_filename: str
    source_hash: str
    source_row_count: int
    rows: tuple[InstagramCommentImportRow, ...]
    warnings: tuple[str, ...]

    @property
    def observed_at_min(self) -> datetime:
        return min(row.observed_at for row in self.rows)

    @property
    def observed_at_max(self) -> datetime:
        return max(row.observed_at for row in self.rows)


@dataclass(frozen=True)
class InstagramCommentImportPreview:
    dataset: InstagramCommentImportDataset
    known_comment_count: int
    new_comment_count: int
    edited_text_count: int
    repeated_source_hash: bool
    warnings: tuple[str, ...]


def _same_comment_observation(
    left: InstagramCommentImportRow, right: InstagramCommentImportRow,
) -> bool:
    return (
        left.media_external_id,
        left.comment_external_id,
        left.comment_text,
        left.published_at,
        left.observed_at,
        left.parent_comment_external_id,
        left.author_username,
        left.author_external_id,
        left.like_count,
        left.replies_count,
    ) == (
        right.media_external_id,
        right.comment_external_id,
        right.comment_text,
        right.published_at,
        right.observed_at,
        right.parent_comment_external_id,
        right.author_username,
        right.author_external_id,
        right.like_count,
        right.replies_count,
    )


def _normalized_dataset(
    source_rows: list[tuple[int, dict[str, object]]], *, source_kind: str, original_filename: str, source_hash: str,
) -> InstagramCommentImportDataset:
    if not source_rows:
        raise InstagramImportError("O arquivo não possui linhas de dados.")
    normalized_rows: list[InstagramCommentImportRow] = []
    for line_number, raw in source_rows:
        media_external_id = _required_text(
            raw.get("media_external_id"), f"Linha {line_number}: media_external_id", limit=128
        )
        comment_external_id = _required_text(
            raw.get("comment_external_id"), f"Linha {line_number}: comment_external_id", limit=128
        )
        parent = _optional_text(
            raw.get("parent_comment_external_id"), f"Linha {line_number}: parent_comment_external_id", limit=128
        )
        if parent == comment_external_id:
            raise InstagramImportError(f"Linha {line_number}: um comentário não pode ser resposta de si mesmo.")
        text = _comment_text(raw.get("comment_text"), f"Linha {line_number}: comment_text")
        normalized_rows.append(InstagramCommentImportRow(
            line_number=line_number,
            media_external_id=media_external_id,
            comment_external_id=comment_external_id,
            comment_text=text,
            comment_text_hash=comment_text_hash(text),
            published_at=_iso_datetime(raw.get("published_at"), f"Linha {line_number}: published_at", required=True),
            observed_at=_iso_datetime(raw.get("observed_at"), f"Linha {line_number}: observed_at", required=True),
            parent_comment_external_id=parent,
            author_username=_optional_text(
                raw.get("author_username"), f"Linha {line_number}: author_username", limit=MAX_COMMENT_AUTHOR_CHARS
            ),
            author_external_id=_optional_text(
                raw.get("author_external_id"), f"Linha {line_number}: author_external_id", limit=128
            ),
            like_count=_optional_count(raw.get("like_count"), f"Linha {line_number}: like_count"),
            replies_count=_optional_count(raw.get("replies_count"), f"Linha {line_number}: replies_count"),
        ))
    rows: list[InstagramCommentImportRow] = []
    seen: dict[tuple[str, str], InstagramCommentImportRow] = {}
    warnings: list[str] = []
    external_media: dict[str, str] = {}
    for row in normalized_rows:
        previous_media = external_media.setdefault(row.comment_external_id, row.media_external_id)
        if previous_media != row.media_external_id:
            raise InstagramImportError(
                f"Linha {row.line_number}: comment_external_id '{row.comment_external_id}' aparece em publicações diferentes."
            )
        identity = (row.media_external_id, row.comment_external_id)
        previous = seen.get(identity)
        if previous is None:
            seen[identity] = row
            rows.append(row)
        elif _same_comment_observation(previous, row):
            warnings.append(f"Linha {row.line_number} duplicada da linha {previous.line_number}; será consolidada.")
        else:
            raise InstagramImportError(
                f"Linha {row.line_number}: comentário '{row.comment_external_id}' conflita com a linha {previous.line_number}."
            )
    # Relações de resposta no arquivo já precisam apontar para comentário da
    # mesma publicação; a cadeia completa (incluindo existentes) é validada na
    # persistência, dentro da transação.
    media_by_comment = {row.comment_external_id: row.media_external_id for row in rows}
    for row in rows:
        if row.parent_comment_external_id and row.parent_comment_external_id in media_by_comment:
            if media_by_comment[row.parent_comment_external_id] != row.media_external_id:
                raise InstagramImportError(
                    f"Linha {row.line_number}: o comentário pai pertence a outra publicação."
                )
    return InstagramCommentImportDataset(
        schema_version=COMMENT_IMPORT_SCHEMA_VERSION,
        processor_version=COMMENT_IMPORT_PROCESSOR_VERSION,
        source_kind=source_kind,
        original_filename=original_filename,
        source_hash=source_hash,
        source_row_count=len(source_rows),
        rows=tuple(rows),
        warnings=tuple(warnings),
    )


def parse_instagram_comment_import(filename: str, data: bytes) -> InstagramCommentImportDataset:
    """Lê CSV/XLSX de comentários com as mesmas defesas usadas nos posts."""
    original_filename = sanitized_import_filename(filename)
    if not data or len(data) > MAX_IMPORT_BYTES:
        raise InstagramImportError("Envie um arquivo CSV ou XLSX de até 2 MB.")
    suffix = original_filename.rsplit(".", 1)[-1].casefold() if "." in original_filename else ""
    if suffix == "csv":
        rows = _csv_rows(data, _validate_comment_headers)
        source_kind = "csv"
    elif suffix == "xlsx":
        rows = _safe_xlsx_rows(data, _validate_comment_headers)
        source_kind = "xlsx"
    else:
        raise InstagramImportError("Envie somente arquivos com extensão .csv ou .xlsx.")
    return _normalized_dataset(
        rows,
        source_kind=source_kind,
        original_filename=original_filename,
        source_hash=hashlib.sha256(data).hexdigest(),
    )


def build_comment_preview(
    dataset: InstagramCommentImportDataset,
    *, known_comment_hashes: dict[tuple[str, str], set[str]],
    repeated_source_hash: bool,
) -> InstagramCommentImportPreview:
    known = 0
    edited = 0
    for row in dataset.rows:
        hashes = known_comment_hashes.get((row.media_external_id, row.comment_external_id))
        if hashes is not None:
            known += 1
            if row.comment_text_hash not in hashes:
                edited += 1
    warnings = list(dataset.warnings)
    if repeated_source_hash:
        warnings.append("Este arquivo parece já ter sido importado anteriormente neste projeto.")
    if edited:
        warnings.append(f"{edited} comentário(s) têm texto novo e gerarão uma versão histórica adicional.")
    return InstagramCommentImportPreview(
        dataset=dataset,
        known_comment_count=known,
        new_comment_count=len(dataset.rows) - known,
        edited_text_count=edited,
        repeated_source_hash=repeated_source_hash,
        warnings=tuple(warnings),
    )


__all__ = [
    "COMMENT_CANONICAL_HEADERS", "COMMENT_IMPORT_FIELDS", "COMMENT_IMPORT_PROCESSOR_VERSION",
    "COMMENT_IMPORT_SCHEMA_VERSION", "InstagramCommentImportDataset", "InstagramCommentImportPreview",
    "InstagramCommentImportRow", "build_comment_preview", "comment_text_hash", "parse_instagram_comment_import",
]
