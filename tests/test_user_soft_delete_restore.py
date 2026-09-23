"""Ciclo reversível de exclusão lógica de contas administrativas."""

import unittest
import tempfile
from pathlib import Path
from uuid import uuid4

from flask_migrate import upgrade
from sqlalchemy import select, text

from app import create_app
from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AccessGrant, AuditLog, Project, User, UserProfile, UserToolOverride, utcnow


class UserSoftDeleteRestoreTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.admin = create_user("Administrador", "admin@example.org", role="admin")
        login(self.client, self.admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    @staticmethod
    def soft_url(user):
        return f"/admin/usuarios/{user.id}/excluir"

    @staticmethod
    def restore_url(user):
        return f"/admin/usuarios/{user.id}/restaurar"

    def soft_delete(self, user):
        url = self.soft_url(user)
        return self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get(url)), "confirm": "yes",
        })

    def restore(self, user, **additional):
        url = self.restore_url(user)
        return self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get(url)), "confirm": "yes", **additional,
        })

    def test_soft_delete_preserves_account_profile_projects_access_and_audit(self):
        user = create_user("Pessoa", "pessoa@example.org")
        profile = UserProfile(user_id=user.id, occupation="Pesquisadora")
        project = Project(owner_user_id=user.id, name="Projeto preservado", status="archived")
        override = UserToolOverride(user_id=user.id, tool_id="pdf_scraper", decision="deny")
        db.session.add_all([profile, project, override])
        db.session.commit()
        user_id, project_id = user.id, project.id
        grant_id = db.session.scalar(select(AccessGrant.id).where(AccessGrant.user_id == user_id))

        self.assertIn("A conta deixará de acessar", self.client.get(self.soft_url(user)).get_data(as_text=True))
        self.assertIsNone(user.deleted_at)  # GET não altera dados
        self.assertEqual(self.soft_delete(user).status_code, 302)
        self.assertIsNotNone(user.deleted_at)
        self.assertEqual(user.status, "blocked")
        self.assertEqual(user.status_before_deletion, "active")
        self.assertNotIn(user.email, self.client.get("/admin/usuarios").get_data(as_text=True))
        excluded = self.client.get("/admin/usuarios/excluidos").get_data(as_text=True)
        self.assertIn(user.email, excluded)
        self.assertIn('title="Restaurar usuário"', excluded)
        self.assertIn('title="Excluir usuário permanentemente"', excluded)
        self.assertIn('data-view-mode="cardbox"', excluded)
        self.assertIsNotNone(db.session.get(User, user_id))
        self.assertEqual(db.session.get(UserProfile, user_id).occupation, "Pesquisadora")
        self.assertEqual(db.session.get(Project, project_id).owner_user_id, user_id)
        self.assertIsNotNone(db.session.get(AccessGrant, grant_id))
        self.assertIsNotNone(db.session.get(UserToolOverride, (user_id, "pdf_scraper")))
        audit = db.session.scalar(select(AuditLog).where(
            AuditLog.action == "user_soft_deleted", AuditLog.target_id == user_id))
        self.assertEqual(audit.admin_user_id, self.admin.id)
        self.assertEqual(audit.before_json["status"], "active")
        self.assertNotIn("password", str(audit.before_json) + str(audit.after_json))

        self.assertEqual(self.restore(user).status_code, 302)
        self.assertIsNone(user.deleted_at)
        self.assertIsNone(user.status_before_deletion)
        self.assertEqual(user.status, "active")
        self.assertIn(user.email, self.client.get("/admin/usuarios").get_data(as_text=True))
        self.assertNotIn(user.email, self.client.get("/admin/usuarios/excluidos").get_data(as_text=True))
        self.assertEqual(db.session.get(Project, project_id).owner_user_id, user_id)
        self.assertEqual(db.session.get(UserProfile, user_id).occupation, "Pesquisadora")
        self.assertIsNotNone(db.session.get(AccessGrant, grant_id))
        restored = db.session.scalar(select(AuditLog).where(
            AuditLog.action == "user_restored", AuditLog.target_id == user_id))
        self.assertEqual(restored.admin_user_id, self.admin.id)
        self.assertEqual(restored.after_json["status"], "active")

    def test_restores_exact_previous_status(self):
        for status in ("active", "suspended", "blocked"):
            with self.subTest(status=status):
                user = create_user(f"Pessoa {status}", f"{status}@example.org")
                user.status = status
                db.session.commit()
                self.assertEqual(self.soft_delete(user).status_code, 302)
                self.assertEqual(user.status_before_deletion, status)
                page = self.client.get(self.restore_url(user)).get_data(as_text=True)
                self.assertIn("Restaurar usuário", page)
                self.assertEqual(self.restore(user).status_code, 302)
                self.assertEqual(user.status, status)
                self.assertEqual(user.is_active, status == "active")

    def test_legacy_deleted_account_requires_explicit_status_choice(self):
        user = create_user("Legado", "legado@example.org")
        user.status = "blocked"
        user.deleted_at = utcnow()
        db.session.commit()
        page = self.client.get(self.restore_url(user)).get_data(as_text=True)
        self.assertIn("não é possível recuperá-lo", page)
        self.assertEqual(self.restore(user).status_code, 400)
        self.assertIsNotNone(user.deleted_at)
        self.assertEqual(self.restore(user, restore_status="invalid").status_code, 400)
        self.assertEqual(self.restore(user, restore_status="suspended").status_code, 302)
        self.assertEqual(user.status, "suspended")
        self.assertIsNone(user.deleted_at)

    def test_deleted_user_cannot_login_and_existing_session_is_rejected(self):
        user = create_user("Pessoa", "pessoa@example.org")
        existing = self.app.test_client()
        with self.app.app_context():
            login(existing, user.email)
        self.soft_delete(user)
        with self.app.app_context():
            response = existing.get("/perfil")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response.headers["Location"])
            retry = existing.post("/login", data={
                "csrf_token": csrf_from(existing.get("/login")), "email": user.email,
                "password": "senha-de-teste-segura-123",
            })
            self.assertIn("E-mail ou senha inválidos", retry.get_data(as_text=True))
        self.restore(user)
        restored_client = self.app.test_client()
        with self.app.app_context():
            login(restored_client, user.email)
            self.assertEqual(restored_client.get("/perfil").status_code, 200)

    def test_only_admin_and_csrf_confirmation_are_required(self):
        user = create_user("Pessoa", "pessoa@example.org")
        ordinary = create_user("Comum", "comum@example.org")
        other_client = self.app.test_client()
        with self.app.app_context():
            login(other_client, ordinary.email)
            self.assertEqual(other_client.get("/admin/usuarios/excluidos").status_code, 403)
            self.assertEqual(other_client.get(self.soft_url(user)).status_code, 403)
            self.assertEqual(other_client.post(self.soft_url(user), data={
                "csrf_token": csrf_from(other_client.get("/perfil")), "confirm": "yes",
            }).status_code, 403)
        self.assertEqual(self.client.post(self.soft_url(user), data={"confirm": "yes"}).status_code, 400)
        self.assertEqual(self.client.post(self.soft_url(user), data={
            "csrf_token": csrf_from(self.client.get(self.soft_url(user))), "confirm": "no",
        }).status_code, 400)
        self.assertEqual(self.client.get(self.restore_url(user)).status_code, 409)
        self.assertEqual(self.client.get(self.soft_url(self.admin)).status_code, 409)
        self.assertIsNone(user.deleted_at)
        self.soft_delete(user)
        self.assertEqual(self.client.post(self.restore_url(user), data={"confirm": "yes"}).status_code, 400)
        self.assertEqual(self.client.post(self.restore_url(user), data={
            "csrf_token": csrf_from(self.client.get(self.restore_url(user))), "confirm": "no",
        }).status_code, 400)
        self.assertEqual(self.client.post(f"/admin/usuarios/{user.id}/status", data={
            "csrf_token": csrf_from(self.client.get(self.restore_url(user))), "status": "active",
        }).status_code, 409)
        self.assertIsNotNone(user.deleted_at)

    def test_permanent_deletion_from_excluded_list_reuses_existing_guards(self):
        user = create_user("Pessoa", "pessoa@example.org")
        project = Project(owner_user_id=user.id, name="Projeto ativo", status="active")
        db.session.add(project)
        db.session.commit()
        self.soft_delete(user)
        excluded = self.client.get("/admin/usuarios/excluidos").get_data(as_text=True)
        permanent_url = f"/admin/usuarios/{user.id}/excluir-permanentemente"
        self.assertIn(permanent_url, excluded)
        self.assertIn("possui projetos vinculados", self.client.get(permanent_url).get_data(as_text=True))
        self.assertEqual(self.client.post(permanent_url, data={
            "csrf_token": csrf_from(self.client.get(permanent_url)), "confirmation": "excluir",
        }).status_code, 409)
        self.assertIsNotNone(db.session.get(User, user.id))
        db.session.delete(project)
        db.session.commit()
        self.assertEqual(self.client.post(permanent_url, data={
            "csrf_token": csrf_from(self.client.get(permanent_url)), "confirmation": "excluir",
        }).status_code, 302)
        self.assertIsNone(db.session.get(User, user.id))
        self.assertIsNotNone(db.session.scalar(select(AuditLog).where(
            AuditLog.action == "user_permanently_deleted", AuditLog.target_id == user.id)))


class UserSoftDeleteMigrationTests(unittest.TestCase):
    def test_existing_deleted_account_keeps_unknown_previous_status(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-restore-migration-") as root:
            database = Path(root) / "migration.sqlite3"
            app = create_app({
                "TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root, "SECRET_KEY": "migration-test-only",
            })
            migrations = str(Path(__file__).resolve().parent.parent / "migrations")
            user_id = str(uuid4())
            with app.app_context():
                upgrade(directory=migrations, revision="0d7a4e21c903")
                with db.engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO users (id,name,email,password_hash,role,status,student_verification_status,created_at,updated_at,deleted_at,must_change_password) "
                        "VALUES (:id,'Legado','legado@example.org','hash','user','blocked','not_required',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,0)"
                    ), {"id": user_id})
                upgrade(directory=migrations, revision="head")
                with db.engine.connect() as connection:
                    row = connection.execute(text(
                        "SELECT status,deleted_at,status_before_deletion FROM users WHERE id=:id"
                    ), {"id": user_id}).one()
                    self.assertEqual(row.status, "blocked")
                    self.assertIsNotNone(row.deleted_at)
                    self.assertIsNone(row.status_before_deletion)
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
