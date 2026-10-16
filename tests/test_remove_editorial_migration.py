"""Migração de retirada do Editorial em SQLite temporário e isolado."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from flask_migrate import upgrade
from sqlalchemy import text

from app import create_app
from platform_core.extensions import db
from platform_core.models import User
from platform_core.services import seed_platform


class RemoveEditorialMigrationTests(unittest.TestCase):
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")
    before_removal = "a4b5c6d7e8f"
    removal_revision = "b5c6d7e8f90"

    def test_upgrade_removes_only_editorial_schema_data_and_docchat_columns(self):
        temporary_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="remove-editorial-migration-", dir=temporary_root
        ) as directory:
            database = Path(directory) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SECRET_KEY": "remove-editorial-migration-test-only",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": directory,
            })
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.before_removal)
                seed_platform()
                user = User(name="Usuário de teste", email="remove-editorial@example.org", role="admin")
                user.set_password("senha-de-teste-segura-123")
                db.session.add(user)
                db.session.commit()

                with db.engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO tools (id, name, description, route, active) "
                        "VALUES ('diagramador', 'Diagramador', '', '/editorial/diagramador', 1)"
                    ))
                    connection.execute(text(
                        "INSERT INTO plan_tools (plan_id, tool_id) VALUES ('researcher', 'diagramador')"
                    ))
                    connection.execute(text(
                        "INSERT INTO user_tool_overrides (user_id, tool_id, decision, changed_at) "
                        "VALUES (:user_id, 'diagramador', 'allow', CURRENT_TIMESTAMP)"
                    ), {"user_id": user.id})
                    connection.execute(text(
                        "INSERT INTO tool_visibility_settings "
                        "(id, scope_type, scope_id, visible, changed_at, changed_by_id, changed_by_name_snapshot) "
                        "VALUES ('visibility-editorial', 'group', 'editorial', 0, CURRENT_TIMESTAMP, :user_id, 'Usuário de teste'), "
                        "('visibility-diagramador', 'tool', 'diagramador', 0, CURRENT_TIMESTAMP, :user_id, 'Usuário de teste'), "
                        "('visibility-quali', 'group', 'research_quali', 0, CURRENT_TIMESTAMP, :user_id, 'Usuário de teste')"
                    ), {"user_id": user.id})
                    connection.execute(text(
                        "INSERT INTO editorial_projects "
                        "(id, owner_user_id, name, description, status, created_at, updated_at) "
                        "VALUES ('editorial-project', :user_id, 'Teste editorial', '', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ), {"user_id": user.id})
                    connection.execute(text(
                        "INSERT INTO editorial_documents "
                        "(id, editorial_project_id, original_name, storage_name, storage_key, byte_size, sha256, status, uploaded_at, updated_at) "
                        "VALUES ('editorial-document', 'editorial-project', 'teste.docx', 'teste.docx', 'editorial/teste.docx', 1, :sha, 'uploaded', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ), {"sha": "0" * 64})
                    connection.execute(text(
                        "INSERT INTO editorial_layout_runs "
                        "(id, editorial_project_id, source_document_id, configuration_json, configuration_version, engine_version, status, created_at, integrity_json, updated_at) "
                        "VALUES ('editorial-run', 'editorial-project', 'editorial-document', '{}', 'diagramador-v1', 'diagramador-v5-core', 'completed', CURRENT_TIMESTAMP, '{}', CURRENT_TIMESTAMP)"
                    ))
                    connection.execute(text(
                        "INSERT INTO audit_logs "
                        "(id, admin_user_id, admin_name_snapshot, action, target_type, target_id, timestamp, before_json, after_json) "
                        "VALUES ('editorial-audit', :user_id, 'Usuário de teste', 'editorial_project_created', 'editorial_project', 'editorial-project', CURRENT_TIMESTAMP, '{}', '{}'), "
                        "('quali-audit', :user_id, 'Usuário de teste', 'tool_visibility_changed', 'tool_visibility', 'group:research_quali', CURRENT_TIMESTAMP, '{}', '{}')"
                    ), {"user_id": user.id})

                upgrade(directory=self.migrations, revision=self.removal_revision)
                with db.engine.connect() as connection:
                    tables = {row[0] for row in connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )}
                    self.assertFalse({"editorial_projects", "editorial_documents", "editorial_layout_runs"} & tables)
                    assistant_columns = {
                        row[1] for row in connection.exec_driver_sql("PRAGMA table_info(assistant_ai_settings)")
                    }
                    self.assertNotIn("docchat_provider", assistant_columns)
                    self.assertNotIn("docchat_model", assistant_columns)
                    self.assertEqual(connection.execute(text(
                        "SELECT COUNT(*) FROM tools WHERE id = 'diagramador'"
                    )).scalar_one(), 0)
                    self.assertEqual(connection.execute(text(
                        "SELECT COUNT(*) FROM tools WHERE id = 'analytics_instagram'"
                    )).scalar_one(), 1)
                    self.assertEqual(connection.execute(text(
                        "SELECT COUNT(*) FROM plan_tools WHERE tool_id = 'diagramador'"
                    )).scalar_one(), 0)
                    self.assertEqual(connection.execute(text(
                        "SELECT COUNT(*) FROM user_tool_overrides WHERE tool_id = 'diagramador'"
                    )).scalar_one(), 0)
                    self.assertEqual(connection.execute(text(
                        "SELECT COUNT(*) FROM tool_visibility_settings "
                        "WHERE scope_id IN ('editorial', 'diagramador')"
                    )).scalar_one(), 0)
                    self.assertEqual(connection.execute(text(
                        "SELECT COUNT(*) FROM tool_visibility_settings WHERE scope_id = 'research_quali'"
                    )).scalar_one(), 1)
                    self.assertEqual(connection.execute(text(
                        "SELECT COUNT(*) FROM audit_logs WHERE id = 'editorial-audit'"
                    )).scalar_one(), 0)
                    self.assertEqual(connection.execute(text(
                        "SELECT COUNT(*) FROM audit_logs WHERE id = 'quali-audit'"
                    )).scalar_one(), 1)
                    self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                    self.assertEqual(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), self.removal_revision)

                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
