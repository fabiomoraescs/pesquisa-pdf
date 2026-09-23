"""Ambiente isolado para testes de rotas: nunca toca o banco do usuário."""

import re
import tempfile
from contextlib import contextmanager

from app import create_app
from platform_core.extensions import db
from platform_core.models import User
from platform_core.services import replace_grant, seed_platform


@contextmanager
def isolated_platform():
    with tempfile.TemporaryDirectory(prefix="pesquisapdf-platform-test-") as data_dir:
        app = create_app({
            "TESTING": True, "WTF_CSRF_ENABLED": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "PLATFORM_DATA_DIR": data_dir,
            "SECRET_KEY": "test-only-secret-not-for-production",
        })
        with app.app_context():
            db.create_all()
            seed_platform()
            yield app
            db.session.remove()
            db.drop_all()


def csrf_from(response) -> str:
    html = response.get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match, "token CSRF ausente no formulário"
    return match.group(1)


def create_user(name="Pesquisador", email="pesquisador@example.org", role="user", plan="student") -> User:
    user = User(name=name, email=email, role=role, status="active")
    user.set_password("senha-de-teste-segura-123")
    db.session.add(user)
    db.session.flush()
    replace_grant(user, plan, "student_free" if plan == "student" else "admin_courtesy", "active")
    db.session.commit()
    return user


def login(client, email="pesquisador@example.org") -> None:
    token = csrf_from(client.get("/login"))
    response = client.post("/login", data={
        "csrf_token": token, "email": email,
        "password": "senha-de-teste-segura-123",
    })
    assert response.status_code == 302, response.get_data(as_text=True)


def create_project(client, name="Pesquisa de teste", libraries=("relacoes_raciais",)) -> str:
    token = csrf_from(client.get("/projetos/novo"))
    response = client.post("/projetos/novo", data={
        "csrf_token": token, "name": name, "description": "",
        "libraries": list(libraries),
    })
    assert response.status_code == 302, response.get_data(as_text=True)
    return response.headers["Location"].split("/projetos/")[1]
