"""Exclusão física de contas sem cascade de projetos ou auditoria."""

import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from flask_migrate import upgrade
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

import app as legacy
from historico_racial import routes as documentary
from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AccessGrant, AuditLog, Project, User, UserProfile, UserToolOverride
from platform_core.services import record_audit, replace_grant


class PermanentUserDeletionTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.admin = create_user("Admin principal", "admin1@example.org", role="admin")
        login(self.client, self.admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    @staticmethod
    def url(user_id):
        return f"/admin/usuarios/{user_id}/excluir-permanentemente"

    def submit(self, user_id, word="excluir"):
        page = self.client.get(self.url(user_id))
        return self.client.post(self.url(user_id), data={
            "csrf_token": csrf_from(page), "confirmation": word,
        })

    def test_eligible_account_is_physically_removed_with_profile_grants_and_override(self):
        user = create_user("Pessoa elegível", "elegivel@example.org")
        target_id, email, secret_hash = user.id, user.email, user.password_hash
        db.session.add(UserProfile(user_id=target_id, education_level="graduacao",
                                   formation_area="Ciências Sociais", gender="mulher"))
        db.session.add(UserToolOverride(user_id=target_id, tool_id="pdf_scraper", decision="deny"))
        db.session.commit()
        old_session = self.app.test_client()
        with self.app.app_context():
            login(old_session, email)

        page = self.client.get(self.url(target_id))
        self.assertEqual(page.status_code, 200)
        self.assertIn("Pessoa elegível", page.get_data(as_text=True))
        self.assertIn(email, page.get_data(as_text=True))
        self.assertIsNotNone(db.session.get(User, target_id))  # GET nunca exclui
        self.assertEqual(self.submit(target_id).status_code, 302)

        self.assertIsNone(db.session.get(User, target_id))
        self.assertIsNone(db.session.get(UserProfile, target_id))
        self.assertIsNone(db.session.scalar(select(AccessGrant.id).where(AccessGrant.user_id == target_id)))
        self.assertIsNone(db.session.get(UserToolOverride, (target_id, "pdf_scraper")))
        self.assertNotIn(email, self.client.get("/admin/usuarios").get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(old_session.get("/perfil").status_code, 302)
            self.assertIn("/login", old_session.get("/perfil").headers["Location"])
            response = old_session.post("/login", data={
                "csrf_token": csrf_from(old_session.get("/login")), "email": email,
                "password": "senha-de-teste-segura-123",
            })
            self.assertIn("E-mail ou senha inválidos", response.get_data(as_text=True))
        audit = db.session.scalar(select(AuditLog).where(
            AuditLog.action == "user_permanently_deleted", AuditLog.target_id == target_id))
        self.assertIsNotNone(audit)
        self.assertEqual(audit.admin_user_id, self.admin.id)
        self.assertEqual(audit.before_json["name"], "Pessoa elegível")
        self.assertNotIn("password", str(audit.before_json).lower())
        self.assertNotIn(secret_hash, str(audit.before_json) + str(audit.after_json))

    def test_common_user_get_and_post_are_forbidden(self):
        target = create_user("Alvo", "alvo@example.org")
        ordinary = create_user("Comum", "comum@example.org")
        client = self.app.test_client()
        with self.app.app_context():
            login(client, ordinary.email)
            self.assertEqual(client.get(self.url(target.id)).status_code, 403)
            self.assertEqual(client.post(self.url(target.id), data={
                "csrf_token": csrf_from(client.get("/perfil")), "confirmation": "excluir",
            }).status_code, 403)
        self.assertIsNotNone(db.session.get(User, target.id))

    def test_csrf_and_confirmation_are_mandatory(self):
        user = create_user("Alvo", "alvo@example.org")
        self.assertEqual(self.client.post(self.url(user.id), data={"confirmation": "excluir"}).status_code, 400)
        self.assertEqual(self.submit(user.id, word="sim").status_code, 400)
        self.assertIsNotNone(db.session.get(User, user.id))

    def test_self_and_last_active_admin_are_protected(self):
        only_admin = self.client.get(self.url(self.admin.id))
        self.assertIn("O último administrador do sistema não pode ser excluído", only_admin.get_data(as_text=True))
        self.assertEqual(self.submit(self.admin.id).status_code, 409)
        second = create_user("Segundo admin", "admin2@example.org", role="admin")
        own_page = self.client.get(self.url(self.admin.id))
        self.assertIn("Você não pode excluir sua própria conta", own_page.get_data(as_text=True))
        self.assertEqual(self.submit(self.admin.id).status_code, 409)
        self.assertIsNotNone(db.session.get(User, second.id))

    def test_other_admin_can_be_removed_and_old_authorship_survives(self):
        other_admin = create_user("Admin histórico", "historico@example.org", role="admin")
        other_id = other_admin.id
        beneficiary = create_user("Beneficiário", "beneficiario@example.org")
        record_audit(other_admin, "tool_state_changed", "tool", "pdf_scraper", {"active": True}, {"active": False})
        replace_grant(beneficiary, "researcher", "admin_courtesy", "active", other_admin)
        db.session.commit()
        log_id = db.session.scalar(select(AuditLog.id).where(
            AuditLog.admin_user_id == other_id, AuditLog.action == "tool_state_changed"))
        grant_id = db.session.scalar(select(AccessGrant.id).where(
            AccessGrant.user_id == beneficiary.id, AccessGrant.granted_by_id == other_id))

        self.assertEqual(self.submit(other_id).status_code, 302)
        self.assertIsNone(db.session.get(User, other_id))
        self.assertIsNotNone(db.session.get(User, self.admin.id))
        historical_log = db.session.get(AuditLog, log_id)
        historical_grant = db.session.get(AccessGrant, grant_id)
        self.assertIsNone(historical_log.admin_user_id)
        self.assertEqual(historical_log.admin_name_snapshot, "Admin histórico")
        self.assertIsNone(historical_grant.granted_by_id)
        self.assertEqual(historical_grant.granted_by_name_snapshot, "Admin histórico")
        self.assertIn("Admin histórico", self.client.get("/admin/auditoria").get_data(as_text=True))

    def test_any_project_including_archived_blocks_without_cascade(self):
        for status in ("active", "archived"):
            with self.subTest(status=status):
                user = create_user(f"Alvo {status}", f"{status}@example.org")
                project = Project(owner_user_id=user.id, name="Pesquisa preservada", status=status)
                db.session.add(project)
                db.session.commit()
                user_id, project_id = user.id, project.id
                page = self.client.get(self.url(user_id))
                self.assertIn("possui projetos vinculados", page.get_data(as_text=True))
                self.assertEqual(self.submit(user_id).status_code, 409)
                self.assertIsNotNone(db.session.get(User, user_id))
                self.assertIsNotNone(db.session.get(Project, project_id))

    def test_legacy_and_documentary_active_jobs_block_deletion(self):
        user = create_user("Com job", "job@example.org")
        legacy_id, documentary_id = str(uuid4()), str(uuid4())
        try:
            with legacy.PROGRESSOS_LOCK:
                legacy.PROGRESSOS[legacy_id] = {"owner_user_id": user.id, "status": "processando"}
            self.assertIn("processamento ativo", self.client.get(self.url(user.id)).get_data(as_text=True))
            self.assertEqual(self.submit(user.id).status_code, 409)
            with legacy.PROGRESSOS_LOCK:
                legacy.PROGRESSOS[legacy_id]["status"] = "concluido"
            with documentary.JOBS_LOCK:
                documentary.PROGRESSOS_HR[documentary_id] = {"owner_user_id": user.id, "status": "processando"}
            self.assertEqual(self.submit(user.id).status_code, 409)
            self.assertIsNotNone(db.session.get(User, user.id))
        finally:
            with legacy.PROGRESSOS_LOCK:
                legacy.PROGRESSOS.pop(legacy_id, None)
            with documentary.JOBS_LOCK:
                documentary.PROGRESSOS_HR.pop(documentary_id, None)

    def test_soft_delete_remains_separate_from_permanent_delete_and_icons_are_distinct(self):
        user = create_user("Alvo", "alvo@example.org")
        listing = self.client.get("/admin/usuarios").get_data(as_text=True)
        self.assertIn('title="Excluir usuário"', listing)
        self.assertNotIn('title="Excluir usuário permanentemente"', listing)
        self.assertIn("platform-open-record", listing)
        detail = self.client.get(f"/admin/usuarios/{user.id}").get_data(as_text=True)
        self.assertIn('title="Excluir usuário"', detail)
        self.assertNotIn('title="Excluir usuário permanentemente"', detail)
        soft = self.client.post(f"/admin/usuarios/{user.id}/excluir", data={
            "csrf_token": csrf_from(self.client.get(f"/admin/usuarios/{user.id}/excluir")), "confirm": "yes",
        })
        self.assertEqual(soft.status_code, 302)
        self.assertIsNotNone(db.session.get(User, user.id))
        self.assertEqual(db.session.get(User, user.id).status, "blocked")
        excluded = self.client.get("/admin/usuarios/excluidos").get_data(as_text=True)
        self.assertIn('title="Restaurar usuário"', excluded)
        self.assertIn('title="Excluir usuário permanentemente"', excluded)
        self.assertIn('aria-label="Excluir usuário permanentemente"', excluded)
        self.assertEqual(self.submit(user.id).status_code, 302)
        self.assertIsNone(db.session.get(User, user.id))

    def test_failed_commit_rolls_back_profile_and_user_deletion(self):
        user = create_user("Alvo", "alvo@example.org")
        db.session.add(UserProfile(user_id=user.id, occupation="Pesquisador"))
        db.session.commit()
        target_id = user.id
        with patch.object(db.session, "commit", side_effect=IntegrityError("DELETE", {}, Exception("FK"))):
            response = self.submit(target_id)
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(db.session.get(User, target_id))
        self.assertIsNotNone(db.session.get(UserProfile, target_id))
        self.assertIsNone(db.session.scalar(select(AuditLog.id).where(
            AuditLog.action == "user_permanently_deleted", AuditLog.target_id == target_id)))


class PermanentUserMigrationTests(unittest.TestCase):
    def test_migration_backfills_existing_audit_and_grant_authorship(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-user-migration-") as root:
            database = Path(root) / "migration.sqlite3"
            app = legacy.create_app({
                "TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root, "SECRET_KEY": "migration-test-only",
            })
            migrations = str(Path(__file__).resolve().parent.parent / "migrations")
            with app.app_context():
                upgrade(directory=migrations, revision="b49c62e8a113")
                moment = datetime.now(timezone.utc)
                actor_id = str(uuid4())
                with db.engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO users (id,name,email,password_hash,role,status,student_verification_status,created_at,updated_at) "
                        "VALUES (:id,:name,:email,'teste','admin','active','not_required',:created,:updated)"
                    ), {"id": actor_id, "name": "Admin anterior", "email": "anterior@example.org",
                        "created": moment, "updated": moment})
                    connection.execute(text(
                        "INSERT INTO plans (id,name,description,active,limits_json) "
                        "VALUES ('student','Estudante','',1,'{}')"
                    ))
                    connection.execute(text(
                        "INSERT INTO audit_logs (id,admin_user_id,action,target_type,target_id,timestamp,before_json,after_json) "
                        "VALUES (:id,:admin,'tool_state_changed','tool','pdf_scraper',:moment,'{}','{}')"
                    ), {"id": str(uuid4()), "admin": actor_id, "moment": moment})
                    connection.execute(text(
                        "INSERT INTO access_grants (id,user_id,plan_id,access_mode,status,created_at,granted_by_id) "
                        "VALUES (:id,:user,'student','admin_courtesy','active',:moment,:admin)"
                    ), {"id": str(uuid4()), "user": actor_id, "moment": moment, "admin": actor_id})
                upgrade(directory=migrations, revision="head")
                with db.engine.connect() as connection:
                    self.assertEqual(connection.scalar(text(
                        "SELECT admin_name_snapshot FROM audit_logs WHERE admin_user_id = :id"
                    ), {"id": actor_id}), "Admin anterior")
                    self.assertEqual(connection.scalar(text(
                        "SELECT granted_by_name_snapshot FROM access_grants WHERE granted_by_id = :id"
                    ), {"id": actor_id}), "Admin anterior")
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
