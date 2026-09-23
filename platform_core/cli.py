"""Provisionamento explícito; nunca grava credenciais no código."""

import click
from flask import Flask
from sqlalchemy import select

from .extensions import db
from .models import User
from .password_policy import TEMPORARY_PASSWORD
from .services import record_audit, replace_grant, seed_platform


def register_cli(app: Flask) -> None:
    @app.cli.command("seed-platform")
    def seed_command() -> None:
        seed_platform()
        click.echo("Planos, ferramentas e biblioteca oficial conferidos.")

    @app.cli.command("create-admin")
    @click.option("--name", prompt="Nome")
    @click.option("--email", prompt="E-mail")
    @click.password_option(confirmation_prompt=True)
    def create_admin(name: str, email: str, password: str) -> None:
        email = email.strip().casefold()
        if len(password) < 12:
            raise click.ClickException("A senha precisa ter pelo menos 12 caracteres.")
        if password == TEMPORARY_PASSWORD:
            raise click.ClickException("Escolha uma senha diferente da senha temporária administrativa.")
        if db.session.scalar(select(User.id).where(User.email == email)):
            raise click.ClickException("E-mail já cadastrado.")
        seed_platform()
        user = User(name=name.strip(), email=email, role="admin", status="active")
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        replace_grant(user, "institutional", "admin_courtesy", "active")
        record_audit(user, "admin_created", "user", user.id, after={"role": "admin", "status": "active"})
        db.session.commit()
        click.echo("Administrador criado. A senha não foi exibida nem gravada em arquivo.")
