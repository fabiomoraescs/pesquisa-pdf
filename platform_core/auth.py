"""Cadastro e sessão local da plataforma."""

from __future__ import annotations

import re

from flask import Blueprint, current_app, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import select

from .extensions import db
from .mail import MailDeliveryError, send_password_recovery
from .models import Plan, User, UserProfile
from .password_policy import RETIRED_TEMPORARY_PASSWORD, TEMPORARY_PASSWORD
from .password_recovery import consume_token, issue_token, revoke_token, valid_token
from .profile import validated_profile
from .services import access_is_active, record_audit, replace_grant

auth_bp = Blueprint("auth", __name__)


def _next_url() -> str:
    target = request.args.get("next", "")
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("home")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated and current_user.is_active:
        if current_user.must_change_password:
            return redirect(url_for("auth.change_password"))
        return redirect(_next_url())
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        user = db.session.scalar(select(User).where(User.email == email))
        if user and user.is_active and not (
            user.must_change_password and password == RETIRED_TEMPORARY_PASSWORD
        ) and user.check_password(password):
            login_user(user)
            if user.must_change_password:
                return redirect(url_for("auth.change_password"))
            return redirect(_next_url())
        flash("E-mail ou senha inválidos, ou conta indisponível.", "danger")
    return render_template("platform/auth.html", mode="login")


@auth_bp.route("/esqueci-senha", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        if len(email) > 320:
            email = ""
        user = db.session.scalar(select(User).where(User.email == email)) if email else None
        if user is not None and access_is_active(user):
            ttl_seconds = int(current_app.config["PASSWORD_RECOVERY_TTL_SECONDS"])
            token = issue_token(user, ttl_seconds)
            try:
                send_password_recovery(user.email, token, ttl_seconds // 60)
            except MailDeliveryError:
                revoke_token(token)
                current_app.logger.warning("Envio de recuperação de senha indisponível; confira a configuração SMTP.")
            else:
                record_audit(None, "password_recovery_requested", "user", user.id)
                db.session.commit()
        return render_template("platform/forgot_password.html", submitted=True)
    return render_template("platform/forgot_password.html", submitted=False)


@auth_bp.route("/redefinir-senha/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    pair = valid_token(token)
    if request.method == "POST" and pair is not None:
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if password == TEMPORARY_PASSWORD or len(password) < 12 or len(password) > 256:
            flash("Escolha uma senha definitiva de 12 a 256 caracteres, diferente da senha temporária.", "danger")
        elif password != confirm:
            flash("A confirmação da nova senha não confere.", "danger")
        elif consume_token(token, password):
            logout_user()
            flash("Senha redefinida. Entre com sua nova senha.", "success")
            return redirect(url_for("auth.login"))
        else:
            pair = None
    response = make_response(render_template("platform/reset_password.html", valid=pair is not None),
                             200 if pair is not None else 400)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex"
    return response


@auth_bp.route("/cadastro", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated and current_user.is_active:
        if current_user.must_change_password:
            return redirect(url_for("auth.change_password"))
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
        elif password == TEMPORARY_PASSWORD:
            error = "Escolha uma senha diferente da senha temporária administrativa."
        elif password != confirm:
            error = "A confirmação da senha não confere."
        elif db.session.scalar(select(User.id).where(User.email == email)):
            error = "Este e-mail já está cadastrado."
        elif not (student_plan := db.session.get(Plan, "student")) or not student_plan.active:
            error = "O Plano Estudante está indisponível para novos cadastros no momento."
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


@auth_bp.route("/alterar-senha", methods=["GET", "POST"])
def change_password():
    if not current_user.is_authenticated or not current_user.is_active:
        return redirect(url_for("auth.login"))
    if not current_user.must_change_password:
        return redirect(_next_url())
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if current_password == RETIRED_TEMPORARY_PASSWORD:
            flash("A senha temporária anterior não é mais válida. Solicite uma nova redefinição ao administrador.", "danger")
        elif not current_user.check_password(current_password):
            flash("A senha temporária não confere.", "danger")
        elif password == TEMPORARY_PASSWORD or len(password) < 12 or len(password) > 256:
            flash("Escolha uma senha definitiva de 12 a 256 caracteres, diferente da senha temporária.", "danger")
        elif password != confirm:
            flash("A confirmação da nova senha não confere.", "danger")
        else:
            current_user.set_password(password)
            current_user.must_change_password = False
            record_audit(current_user, "password_changed_after_reset", "user", current_user.id,
                         {"must_change_password": True}, {"must_change_password": False})
            db.session.commit()
            flash("Senha alterada. Você já pode continuar.", "success")
            return redirect(_next_url())
    return render_template("platform/change_password.html")


@auth_bp.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
