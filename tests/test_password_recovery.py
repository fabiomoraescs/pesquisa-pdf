"""Recuperação por e-mail: resposta neutra, token efêmero e senha definitiva."""

import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from flask_migrate import upgrade
from sqlalchemy import inspect

from app import create_app
from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AuditLog, PasswordRecoveryToken, User, utcnow
from platform_core.services import current_grant


class PasswordRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def _request_link(self, email):
        return self.client.post("/esqueci-senha", data={
            "csrf_token": csrf_from(self.client.get("/esqueci-senha")), "email": email,
        })

    def _token(self):
        outbox = self.app.extensions["password_recovery_outbox"]
        match = re.search(r"/redefinir-senha/([A-Za-z0-9_-]+)", outbox[-1].get_content())
        self.assertIsNotNone(match)
        return match.group(1)

    def test_public_auth_cards_have_no_large_identity_intro(self):
        for path, heading in (
            ("/login", "Entrar"),
            ("/cadastro", "Criar conta"),
            ("/esqueci-senha", "Esqueci minha senha"),
            ("/redefinir-senha/token-invalido", "Definir nova senha"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 400 if "token-invalido" in path else 200)
                html = response.get_data(as_text=True)
                self.assertNotIn('class="platform-auth-intro"', html)
                self.assertIn('class="platform-auth-card', html)
                self.assertIn(heading, html)
                self.assertIn('<strong>Análysis</strong><small>ferramentas para pesquisa</small>', html)
        self.assertIn("Esqueci minha senha", self.client.get("/login").get_data(as_text=True))
        user = create_user("Admin", "admin-intro@example.org", role="admin")
        login(self.client, user.email)
        self.assertIn(
            '<strong>Análysis</strong><small>ferramentas para pesquisa</small>',
            self.client.get("/perfil").get_data(as_text=True),
        )

    def test_login_link_neutral_response_and_csrf(self):
        user = create_user(email="conta@example.org")
        self.assertIn("Esqueci minha senha", self.client.get("/login").get_data(as_text=True))
        self.assertEqual(self.client.get("/esqueci-senha").status_code, 200)
        self.assertEqual(self.client.post("/esqueci-senha", data={"email": user.email}).status_code, 400)
        known = self._request_link(user.email)
        unknown = self._request_link("desconhecido@example.org")
        self.assertEqual((known.status_code, unknown.status_code), (200, 200))
        self.assertEqual(known.get_data(as_text=True), unknown.get_data(as_text=True))
        self.assertIn("Se houver uma conta associada a esse e-mail", known.get_data(as_text=True))
        self.assertEqual(len(self.app.extensions["password_recovery_outbox"]), 1)

    def test_secure_single_use_token_and_password_change(self):
        user = create_user(email="recuperacao@example.org")
        original_hash = user.password_hash
        original_state = (user.status, user.deleted_at)
        self._request_link(user.email)
        token = self._token()
        record = db.session.query(PasswordRecoveryToken).one()
        self.assertNotEqual(record.token_digest, token)
        self.assertNotIn(token, record.token_digest)
        self.assertEqual(len(record.token_digest), 64)
        url = f"/redefinir-senha/{token}"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn("Definir nova senha", page.get_data(as_text=True))
        self.assertEqual(self.client.post(url, data={"password": "nova-senha-segura-123"}).status_code, 400)

        csrf = csrf_from(self.client.get(url))
        for password, confirm in (("raspagemdedados", "raspagemdedados"),
                                  ("curta", "curta"),
                                  ("nova-senha-segura-123", "outra")):
            with self.subTest(password=password, confirm=confirm):
                self.assertEqual(self.client.post(url, data={
                    "csrf_token": csrf, "password": password, "confirm": confirm,
                }).status_code, 200)
                self.assertIsNone(record.consumed_at)
        response = self.client.post(url, data={
            "csrf_token": csrf, "password": "nova-senha-segura-123",
            "confirm": "nova-senha-segura-123",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        db.session.refresh(user)
        self.assertNotEqual(user.password_hash, original_hash)
        self.assertFalse(user.check_password("senha-de-teste-segura-123"))
        self.assertTrue(user.check_password("nova-senha-segura-123"))
        self.assertEqual((user.status, user.deleted_at), original_state)
        self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(self.client.post(url, data={
            "csrf_token": csrf, "password": "outra-senha-segura-123",
            "confirm": "outra-senha-segura-123",
        }).status_code, 400)
        self.assertEqual(self.client.get("/redefinir-senha/token-invalido").status_code, 400)
        old_login = self.client.post("/login", data={
            "csrf_token": csrf_from(self.client.get("/login")), "email": user.email,
            "password": "senha-de-teste-segura-123",
        })
        self.assertEqual(old_login.status_code, 200)
        new_login = self.client.post("/login", data={
            "csrf_token": csrf_from(self.client.get("/login")), "email": user.email,
            "password": "nova-senha-segura-123",
        })
        self.assertEqual(new_login.status_code, 302)
        self.assertEqual(new_login.headers["Location"], "/")
        audits = db.session.query(AuditLog).filter_by(target_type="user", target_id=user.id).all()
        self.assertEqual({item.action for item in audits if item.action.startswith("password_recover")},
                         {"password_recovery_requested", "password_recovered"})
        for item in audits:
            serialized = f"{item.before_json} {item.after_json}"
            for secret in (token, record.token_digest, user.password_hash, "nova-senha-segura-123"):
                self.assertNotIn(secret, serialized)

    def test_expired_and_ineligible_accounts_never_receive_usable_link(self):
        user = create_user(email="estado@example.org")
        self._request_link(user.email)
        token = self._token()
        row = db.session.query(PasswordRecoveryToken).one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(self.client.get(f"/redefinir-senha/{token}").status_code, 400)
        for status, deleted in (("blocked", None), ("suspended", None), ("active", utcnow())):
            with self.subTest(status=status, deleted=bool(deleted)):
                user.status = status
                user.deleted_at = deleted
                db.session.commit()
                stored_deleted_at = user.deleted_at
                before = len(self.app.extensions["password_recovery_outbox"])
                response = self._request_link(user.email)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(self.app.extensions["password_recovery_outbox"]), before)
                self.assertEqual((user.status, user.deleted_at), (status, stored_deleted_at))
        user.status = "active"
        user.deleted_at = None
        current_grant(user).status = "canceled"
        db.session.commit()
        before = len(self.app.extensions["password_recovery_outbox"])
        self._request_link(user.email)
        self.assertEqual(len(self.app.extensions["password_recovery_outbox"]), before)

    def test_recovery_clears_administrative_change_requirement_without_reactivating(self):
        user = create_user(email="obrigatoria@example.org")
        user.must_change_password = True
        db.session.commit()
        self._request_link(user.email)
        token = self._token()
        url = f"/redefinir-senha/{token}"
        response = self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get(url)),
            "password": "senha-definitiva-nova-123", "confirm": "senha-definitiva-nova-123",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(user.must_change_password)
        self.assertEqual(user.status, "active")
        self.assertIsNone(user.deleted_at)

    def test_missing_email_configuration_stays_neutral_and_revokes_token(self):
        user = create_user(email="sem-smtp@example.org")
        self.app.config["TESTING"] = False
        try:
            known = self._request_link(user.email)
            unknown = self._request_link("ninguem@example.org")
        finally:
            self.app.config["TESTING"] = True
        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.get_data(as_text=True), unknown.get_data(as_text=True))
        self.assertEqual(db.session.query(PasswordRecoveryToken).count(), 0)

    def test_first_success_invalidates_other_open_links(self):
        user = create_user(email="dois-links@example.org")
        self._request_link(user.email)
        first = self._token()
        self._request_link(user.email)
        second = self._token()
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.post(f"/redefinir-senha/{first}", data={
            "csrf_token": csrf_from(self.client.get(f"/redefinir-senha/{first}")),
            "password": "senha-definitiva-456", "confirm": "senha-definitiva-456",
        }).status_code, 302)
        self.assertEqual(self.client.get(f"/redefinir-senha/{second}").status_code, 400)

    def test_permanent_user_deletion_removes_pending_recovery_tokens(self):
        target = create_user(email="excluir-token@example.org")
        target_id = target.id
        self._request_link(target.email)
        token = self._token()
        admin = create_user("Admin", "admin@example.org", role="admin", plan="researcher")
        admin_client = self.app.test_client()
        with self.app.app_context():
            login(admin_client, admin.email)
            url = f"/admin/usuarios/{target_id}/excluir-permanentemente"
            self.assertEqual(admin_client.post(url, data={
                "csrf_token": csrf_from(admin_client.get(url)), "confirmation": "excluir",
            }).status_code, 302)
        db.session.expire_all()
        self.assertIsNone(db.session.get(User, target_id))
        self.assertEqual(db.session.query(PasswordRecoveryToken).count(), 0)
        self.assertEqual(self.client.get(f"/redefinir-senha/{token}").status_code, 400)


class PasswordRecoveryMigrationTests(unittest.TestCase):
    def test_upgrade_creates_recovery_table_and_keeps_existing_schema(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-recovery-migration-") as root:
            database = Path(root) / "migration.sqlite3"
            app = create_app({
                "TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root, "SECRET_KEY": "migration-test-only",
            })
            migrations = str(Path(__file__).resolve().parent.parent / "migrations")
            with app.app_context():
                upgrade(directory=migrations, revision="head")
                inspector = inspect(db.engine)
                self.assertIn("password_recovery_tokens", inspector.get_table_names())
                self.assertIn("users", inspector.get_table_names())
                self.assertEqual({"user_id", "token_digest", "expires_at", "consumed_at"} -
                                 {column["name"] for column in inspector.get_columns("password_recovery_tokens")}, set())
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
