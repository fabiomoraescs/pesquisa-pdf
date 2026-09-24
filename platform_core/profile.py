"""Edição opcional do perfil, sem efeitos em permissões ou análise."""

from __future__ import annotations

import math
import os
from io import BytesIO
from pathlib import Path
from uuid import UUID

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from PIL import Image, ImageOps, UnidentifiedImageError

from .extensions import db
from .models import UserProfile
from .presentation import EDUCATION, GENDER, RACE_COLOR

profile_bp = Blueprint("profile", __name__)

TEXT_LIMITS = {"formation_area": 160, "occupation": 160, "institutional_affiliation": 200}
CHOICES = {"education_level": EDUCATION, "gender": GENDER, "race_color": RACE_COLOR}
PHOTO_MAX_BYTES = 5 * 1024 * 1024
PHOTO_MAX_PIXELS = 20_000_000
PHOTO_SIZE = 512


def profile_photo_path(user_id: str) -> Path:
    """Caminho fixo por UUID, sem usar o nome enviado pelo navegador."""
    base = Path(current_app.config["PLATFORM_DATA_DIR"]).resolve()
    root = base / "profile_photos"
    folder = root / str(UUID(user_id))
    photo = folder / "avatar.webp"
    if (root.is_symlink() or root.resolve().parent != base
            or folder.is_symlink() or folder.resolve().parent != root
            or photo.is_symlink()):
        raise ValueError("Diretório de foto inválido.")
    return photo


def remove_profile_photo(user_id: str) -> None:
    photo = profile_photo_path(user_id)
    photo.unlink(missing_ok=True)
    if photo.parent.is_dir():
        photo.parent.rmdir()


@profile_bp.app_context_processor
def _profile_avatar_context():
    if not current_user.is_authenticated:
        return {"profile_avatar_url": None}
    photo = profile_photo_path(current_user.id)
    try:
        version = photo.stat().st_mtime_ns if photo.is_file() else None
    except OSError:
        version = None
    return {"profile_avatar_url": url_for("profile.photo", v=version) if version else None}


def _normalized_photo(upload, crop_values) -> BytesIO:
    content = upload.stream.read(PHOTO_MAX_BYTES + 1)
    if len(content) > PHOTO_MAX_BYTES:
        raise OverflowError("A foto deve ter no máximo 5 MB.")
    try:
        with Image.open(BytesIO(content)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Envie uma imagem JPEG, PNG ou WEBP válida.")
            if min(source.size) < 64 or source.width * source.height > PHOTO_MAX_PIXELS:
                raise ValueError("A foto precisa ter ao menos 64 px por lado e até 20 megapixels.")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            width, height = oriented.size
            if all(crop_values):
                try:
                    x, y, size = (float(value) for value in crop_values)
                except ValueError as error:
                    raise ValueError("A área de recorte é inválida. Selecione a foto novamente.") from error
                if (not all(math.isfinite(value) for value in (x, y, size))
                        or size < 1 or x < 0 or y < 0
                        or x + size > width + 0.01 or y + size > height + 0.01):
                    raise ValueError("A área de recorte é inválida. Selecione a foto novamente.")
            elif any(crop_values):
                raise ValueError("A área de recorte é inválida. Selecione a foto novamente.")
            else:
                size = min(width, height)
                x, y = (width - size) / 2, (height - size) / 2
            box = (round(x), round(y), round(x + size), round(y + size))
            square = oriented.crop(box).convert("RGB").resize((PHOTO_SIZE, PHOTO_SIZE), Image.Resampling.LANCZOS)
            clean = Image.new("RGB", (PHOTO_SIZE, PHOTO_SIZE))
            clean.paste(square)
            output = BytesIO()
            clean.save(output, format="WEBP", quality=84, method=4)
            output.seek(0)
            return output
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("Envie uma imagem JPEG, PNG ou WEBP válida.") from error


@profile_bp.get("/perfil/foto")
@login_required
def photo():
    path = profile_photo_path(current_user.id)
    if not path.is_file():
        abort(404)
    # A miniatura é pequena; servir em memória evita bloquear a substituição
    # do arquivo no Windows enquanto outra requisição lê o avatar.
    response = send_file(BytesIO(path.read_bytes()), mimetype="image/webp", max_age=3600)
    response.cache_control.private = True
    return response


@profile_bp.post("/perfil/foto")
@login_required
def upload_photo():
    if request.content_length is not None and request.content_length > PHOTO_MAX_BYTES + 64 * 1024:
        flash("A foto deve ter no máximo 5 MB.", "danger")
        return render_template("platform/profile.html", profile=current_user.profile), 413
    upload = request.files.get("photo")
    if upload is None or not upload.filename:
        flash("Selecione uma foto para enviar.", "danger")
        return render_template("platform/profile.html", profile=current_user.profile), 400
    try:
        normalized = _normalized_photo(upload, (
            request.form.get("crop_x", ""), request.form.get("crop_y", ""), request.form.get("crop_size", ""),
        ))
    except (ValueError, OverflowError) as error:
        flash(str(error), "danger")
        return render_template("platform/profile.html", profile=current_user.profile), 413 if isinstance(error, OverflowError) else 400
    path = profile_photo_path(current_user.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("avatar-upload-" + os.urandom(8).hex() + ".webp")
    try:
        with temporary.open("xb") as output:
            output.write(normalized.getbuffer())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    flash("Foto do perfil atualizada.", "success")
    return redirect(url_for("profile.my_profile"))


@profile_bp.post("/perfil/foto/remover")
@login_required
def remove_photo():
    remove_profile_photo(current_user.id)
    flash("Foto removida. O avatar padrão voltou a ser exibido.", "success")
    return redirect(url_for("profile.my_profile"))


def validated_profile(form) -> tuple[dict, str | None]:
    values = {}
    for field, options in CHOICES.items():
        value = form.get(field, "").strip()
        if value and value not in options:
            return {}, "Selecione uma opção válida para os campos de perfil."
        values[field] = value or None
    for field, limit in TEXT_LIMITS.items():
        value = form.get(field, "").strip()
        if len(value) > limit:
            return {}, "Um dos campos de perfil excede o limite de caracteres."
        values[field] = value or None
    return values, None


@profile_bp.route("/perfil", methods=["GET", "POST"])
@login_required
def my_profile():
    profile = current_user.profile
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        values, error = validated_profile(request.form)
        if not name or len(name) > 160:
            error = "Informe seu nome completo (até 160 caracteres)."
        if error:
            flash(error, "danger")
        else:
            if profile is None:
                profile = UserProfile(user_id=current_user.id)
                db.session.add(profile)
            current_user.name = name
            for field, value in values.items():
                setattr(profile, field, value)
            db.session.commit()
            flash("Perfil atualizado com sucesso.", "success")
            return redirect(url_for("profile.my_profile"))
    return render_template("platform/profile.html", profile=profile)
