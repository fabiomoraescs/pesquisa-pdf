"""Cadastro e sessão local da plataforma."""

from __future__ import annotations

import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import select

from .extensions import db
from .models import User, UserProfile
from .profile import validated_profile
from .services import can_use_tool, record_audit, replace_grant

auth_bp = Blueprint("auth", __name__)


def _next_url() -> str:
    target = request.args.get("next", "")
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    if can_use_tool(current_user, "document_analysis"):
        return url_for("projects.list_projects")
    if can_use_tool(current_user, "pdf_scraper"):
        return url_for("inicio")
    if current_user.role == "admin":
        return url_for("admin.home")
    abort(403)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated and current_user.is_active:
        return redirect(_next_url())
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        user = db.session.scalar(select(User).where(User.email == email))
        if user and user.is_active and user.check_password(password):
            login_user(user)
            return redirect(_next_url())
        flash("E-mail ou senha inválidos, ou conta indisponível.", "danger")
    return render_template("platform/auth.html", mode="login")


@auth_bp.route("/cadastro", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated and current_user.is_active:
        return redirect(_next_url())
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        profile_values, profile_error = validated_profile(request.form)
        error = None
        if not name or len(name) > 160:
            error = "Informe seu nome (até 160 caracteres)."
        elif not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 320:
            error = "Informe um e-mail válido."
        elif len(password) < 12 or len(password) > 256:
            error = "A senha deve ter entre 12 e 256 caracteres."
        elif password != confirm:
            error = "A confirmação da senha não confere."
        elif db.session.scalar(select(User.id).where(User.email == email)):
            error = "Este e-mail já está cadastrado."
        elif profile_error:
            error = profile_error
        if error:
            flash(error, "danger")
        else:
            user = User(name=name, email=email, role="user", status="active")
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            db.session.add(UserProfile(user_id=user.id, **profile_values))
            replace_grant(user, "student", "student_free", "active")
            record_audit(None, "user_registered", "user", user.id, after={"role": "user", "status": "active"})
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            login_user(user)
            return redirect(_next_url())
    return render_template("platform/auth.html", mode="register")


@auth_bp.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
