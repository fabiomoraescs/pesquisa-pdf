"""Edição opcional do perfil, sem efeitos em permissões ou análise."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .extensions import db
from .models import UserProfile
from .presentation import EDUCATION, GENDER, RACE_COLOR

profile_bp = Blueprint("profile", __name__)

TEXT_LIMITS = {"formation_area": 160, "occupation": 160, "institutional_affiliation": 200}
CHOICES = {"education_level": EDUCATION, "gender": GENDER, "race_color": RACE_COLOR}


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
