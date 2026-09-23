"""Visibilidade de senha compartilhada e integridade da Auditoria isolada."""

import unittest

from sqlalchemy import delete, func, select

from platform_helpers import create_project, create_user, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import (
    AccessGrant, AuditLog, PasswordRecoveryToken, Project, User, VocabularyLibrary,
)
from platform_core.password_recovery import issue_token
from platform_core.services import record_audit


class PasswordEyeAndAuditCleanupTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_all_password_pages_use_hidden_shared_eye_off_component(self):
        user = create_user()
        recovery_token = issue_token(user, 1800)
        pages = (
            ("/login", 1),
            ("/cadastro", 2),
            (f"/redefinir-senha/{recovery_token}", 2),
        )
        for path, fields in pages:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                for marker in ('type="password"', 'data-password-toggle=',
                               'class="platform-password-eye-slash"',
                               'aria-label="Mostrar senha"', 'title="Mostrar senha"',
                               'aria-pressed="false"'):
                    self.assertEqual(html.count(marker), fields, (path, marker))

        login(self.client, user.email)
        user.must_change_password = True
        db.session.commit()
        response = self.client.get("/alterar-senha")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for marker in ('type="password"', 'data-password-toggle=',
                       'class="platform-password-eye-slash"',
                       'aria-label="Mostrar senha"', 'title="Mostrar senha"',
                       'aria-pressed="false"'):
            self.assertEqual(html.count(marker), 3, marker)

    def test_isolated_audit_cleanup_leaves_other_data_and_allows_new_events(self):
        admin = create_user("Admin", "admin-audit-clean@example.org", role="admin")
        user = create_user("Pesquisadora", "researcher-audit-clean@example.org")
        issue_token(user, 1800)
        login(self.client, admin.email)
        create_project(self.client)
        record_audit(admin, "tool_state_changed", "tool", "before-clean",
                     {"active": True}, {"active": False})
        db.session.commit()
        models = (User, Project, VocabularyLibrary, AccessGrant, PasswordRecoveryToken)
        counts_before = {model.__tablename__: db.session.scalar(select(func.count()).select_from(model))
                         for model in models}
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AuditLog)), 1)

        db.session.execute(delete(AuditLog))
        db.session.commit()
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AuditLog)), 0)
        self.assertEqual(
            {model.__tablename__: db.session.scalar(select(func.count()).select_from(model))
             for model in models}, counts_before,
        )
        self.assertIn("Nenhuma ação registrada.",
                      self.client.get("/admin/auditoria").get_data(as_text=True))

        record_audit(admin, "tool_state_changed", "tool", "after-clean",
                     {"active": False}, {"active": True})
        db.session.commit()
        log = db.session.scalar(select(AuditLog))
        self.assertEqual(log.target_id, "after-clean")
        self.assertEqual(log.before_json, {"active": False})
        self.assertEqual(log.after_json, {"active": True})
        self.assertIn("after-clean",
                      self.client.get("/admin/auditoria").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
