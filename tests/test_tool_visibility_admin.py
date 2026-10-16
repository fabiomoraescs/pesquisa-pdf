"""Persistência administrativa da visibilidade, sem integrar a sidebar ainda."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import create_app
from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AuditLog, PlanTool, Tool, ToolVisibilitySetting, UserToolOverride
from platform_core.services import (
    set_visibility_preference,
    visibility_control_catalog,
    visibility_preferences,
)


class ToolVisibilityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.admin = create_user("Admin", "admin-visibility@example.org", "admin", "researcher")
        self.user = create_user("Usuário", "user-visibility@example.org", "user", "student")
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_default_preferences_preserve_current_visibility_and_catalog(self):
        self.assertEqual(visibility_preferences(), ({}, {}))
        groups = visibility_control_catalog()
        self.assertEqual([group["id"] for group in groups], [
            "research_quali", "research_quanti", "analytics",
        ])
        self.assertTrue(all(group["visible"] for group in groups))
        self.assertTrue(all(tool["visible"] for group in groups for tool in group["tools"]))
        self.assertEqual(db.session.scalars(select(ToolVisibilitySetting)).all(), [])

    def test_create_update_and_group_reactivation_preserve_individual_preference(self):
        self.assertTrue(set_visibility_preference(
            self.admin, scope_type="tool", scope_id="pdf_scraper", visible=False,
        ))
        db.session.commit()
        self.assertTrue(set_visibility_preference(
            self.admin, scope_type="group", scope_id="research_quali", visible=False,
        ))
        db.session.commit()
        groups, tools = visibility_preferences()
        self.assertFalse(groups["research_quali"])
        self.assertFalse(tools["pdf_scraper"])

        self.assertTrue(set_visibility_preference(
            self.admin, scope_type="group", scope_id="research_quali", visible=True,
        ))
        db.session.commit()
        groups, tools = visibility_preferences()
        self.assertTrue(groups["research_quali"])
        self.assertFalse(tools["pdf_scraper"])

        settings = db.session.scalars(select(ToolVisibilitySetting)).all()
        self.assertEqual(len(settings), 2)
        logs = db.session.scalars(select(AuditLog).where(AuditLog.action == "tool_visibility_changed")).all()
        self.assertEqual(len(logs), 3)
        changes = [(log.before_json["visible"], log.after_json["visible"]) for log in logs]
        self.assertIn((True, False), changes)
        self.assertIn((False, True), changes)

    def test_noop_does_not_create_a_default_record_or_duplicate_audit(self):
        self.assertFalse(set_visibility_preference(
            self.admin, scope_type="tool", scope_id="pdf_scraper", visible=True,
        ))
        self.assertEqual(db.session.scalars(select(ToolVisibilitySetting)).all(), [])
        self.assertEqual(db.session.scalars(select(AuditLog).where(
            AuditLog.action == "tool_visibility_changed"
        )).all(), [])

        self.assertTrue(set_visibility_preference(
            self.admin, scope_type="tool", scope_id="pdf_scraper", visible=False,
        ))
        db.session.commit()
        self.assertFalse(set_visibility_preference(
            self.admin, scope_type="tool", scope_id="pdf_scraper", visible=False,
        ))
        self.assertEqual(len(db.session.scalars(select(AuditLog).where(
            AuditLog.action == "tool_visibility_changed"
        )).all()), 1)

    def test_invalid_targets_and_duplicate_scope_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "inexistente"):
            set_visibility_preference(self.admin, scope_type="tool", scope_id="missing", visible=False)
        with self.assertRaisesRegex(ValueError, "inválido"):
            set_visibility_preference(self.admin, scope_type="invalid", scope_id="analytics", visible=False)
        self.assertEqual(db.session.scalars(select(ToolVisibilitySetting)).all(), [])

        db.session.add_all((
            ToolVisibilitySetting(scope_type="tool", scope_id="pdf_scraper", visible=False),
            ToolVisibilitySetting(scope_type="tool", scope_id="pdf_scraper", visible=True),
        ))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_stale_removed_visibility_target_does_not_break_the_dashboard(self):
        """Registros legados não podem transformar a página inicial em erro 500."""
        db.session.add(ToolVisibilitySetting(
            scope_type="group", scope_id="editorial", visible=False,
        ))
        db.session.commit()

        login(self.client, self.admin.email)
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(visibility_preferences(), ({}, {}))

    def test_visibility_configuration_does_not_change_operational_access_models(self):
        tool = db.session.get(Tool, "pdf_scraper")
        tool.active = False
        plan_links_before = {(item.plan_id, item.tool_id) for item in db.session.scalars(select(PlanTool))}
        overrides_before = {(item.user_id, item.tool_id, item.decision) for item in db.session.scalars(select(UserToolOverride))}
        self.assertTrue(set_visibility_preference(
            self.admin, scope_type="group", scope_id="research_quali", visible=False,
        ))
        db.session.commit()
        self.assertFalse(db.session.get(Tool, "pdf_scraper").active)
        self.assertEqual(plan_links_before, {(item.plan_id, item.tool_id) for item in db.session.scalars(select(PlanTool))})
        self.assertEqual(overrides_before, {(item.user_id, item.tool_id, item.decision) for item in db.session.scalars(select(UserToolOverride))})

    def test_admin_route_requires_csrf_and_admin_role_and_records_effective_change(self):
        login(self.client, self.admin.email)
        response = self.client.get("/admin/ferramentas")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Visibilidade na navegação", html)
        self.assertIn('data-visibility-group="analytics"', html)
        self.assertIn('data-visibility-tool="analytics_instagram"', html)
        self.assertIn("Painel de controle", html)

        url = "/admin/visibilidade/group/analytics"
        self.assertEqual(self.client.post(url, data={"visible": "false"}).status_code, 400)
        token = csrf_from(self.client.get("/admin/ferramentas"))
        self.assertEqual(self.client.post(url, data={"csrf_token": token, "visible": "invalid"}).status_code, 400)
        token = csrf_from(self.client.get("/admin/ferramentas"))
        changed = self.client.post(url, data={"csrf_token": token, "visible": "false"})
        self.assertEqual(changed.status_code, 302)
        self.assertFalse(db.session.scalar(select(ToolVisibilitySetting.visible).where(
            ToolVisibilitySetting.scope_type == "group",
            ToolVisibilitySetting.scope_id == "analytics",
        )))
        audit = db.session.scalar(select(AuditLog).where(
            AuditLog.action == "tool_visibility_changed",
            AuditLog.target_id == "group:analytics",
        ))
        self.assertEqual(audit.admin_user_id, self.admin.id)
        self.assertFalse(audit.after_json["visible"])

        token = csrf_from(self.client.get("/admin/ferramentas"))
        self.assertEqual(self.client.post(url, data={"csrf_token": token, "visible": "false"}).status_code, 302)
        self.assertEqual(len(db.session.scalars(select(AuditLog).where(
            AuditLog.target_id == "group:analytics"
        )).all()), 1)

        logout_token = csrf_from(self.client.get("/"))
        self.assertEqual(self.client.post("/logout", data={"csrf_token": logout_token}).status_code, 302)
        login(self.client, self.user.email)
        user_token = csrf_from(self.client.get("/"))
        self.assertEqual(self.client.post(url, data={
            "csrf_token": user_token, "visible": "true",
        }).status_code, 403)

    def test_visibility_endpoint_returns_json_for_asynchronous_switch_saves(self):
        login(self.client, self.admin.email)
        page = self.client.get("/admin/ferramentas")
        html = page.get_data(as_text=True)
        self.assertIn('class="form-check form-switch mb-0" data-tool-visibility-form', html)
        self.assertIn('data-tool-visibility-control', html)
        self.assertIn('js/admin_tool_visibility.js', html)
        self.assertIn('data-tool-visibility-refresh', html)
        self.assertIn('type="button" data-tool-visibility-refresh', html)
        self.assertIn('href="/admin/ferramentas"', html)
        self.assertNotIn('id="visibility-group-analytics" name="visible" style=', html)
        self.assertNotIn('data-tool-visibility-state', html)
        self.assertNotIn('Temporariamente indisponível porque o grupo está oculto.', html)
        visibility_markup = html.split('id="tool-visibility-title"', 1)[1].split(
            'js/admin_tool_visibility.js', 1,
        )[0]
        self.assertNotIn('>Implementada<', visibility_markup)
        self.assertNotIn('>Em desenvolvimento<', visibility_markup)
        self.assertNotIn('>Futura<', visibility_markup)
        self.assertNotIn('>On<', html)
        self.assertNotIn('>Off<', html)
        self.assertIn('aria-label="Visibilidade de Analytics"', html)
        self.assertIn('role="switch"', html)

        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        url = "/admin/visibilidade/group/analytics"
        changed = self.client.post(url, data={
            "csrf_token": csrf_from(page), "visible": "false",
        }, headers=headers)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json, {
            "ok": True,
            "changed": True,
            "scope_type": "group",
            "scope_id": "analytics",
            "visible": False,
            "message": "Visibilidade atualizada.",
        })
        self.assertFalse(db.session.scalar(select(ToolVisibilitySetting.visible).where(
            ToolVisibilitySetting.scope_type == "group",
            ToolVisibilitySetting.scope_id == "analytics",
        )))

        unchanged = self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get("/admin/ferramentas")), "visible": "false",
        }, headers=headers)
        self.assertEqual(unchanged.status_code, 200)
        self.assertTrue(unchanged.json["ok"])
        self.assertFalse(unchanged.json["changed"])
        self.assertEqual(len(db.session.scalars(select(AuditLog).where(
            AuditLog.target_id == "group:analytics"
        )).all()), 1)

    def test_refresh_control_is_read_only_and_remains_in_the_tools_section(self):
        login(self.client, self.admin.email)
        settings_before = [
            (item.scope_type, item.scope_id, item.visible)
            for item in db.session.scalars(select(ToolVisibilitySetting)).all()
        ]
        audits_before = db.session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "tool_visibility_changed"
        ))

        page = self.client.get("/admin/ferramentas")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        visibility_markup = html.split('id="tool-visibility-title"', 1)[1].split(
            'js/admin_tool_visibility.js', 1,
        )[0]
        self.assertLess(
            visibility_markup.index('id="admin-tool-visibility-list"'),
            visibility_markup.index('data-tool-visibility-refresh'),
        )
        self.assertIn('type="button" data-tool-visibility-refresh', visibility_markup)
        self.assertIn('href="/admin/ferramentas"', visibility_markup)

        settings_after = [
            (item.scope_type, item.scope_id, item.visible)
            for item in db.session.scalars(select(ToolVisibilitySetting)).all()
        ]
        audits_after = db.session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "tool_visibility_changed"
        ))
        self.assertEqual(settings_after, settings_before)
        self.assertEqual(audits_after, audits_before)

    def test_hidden_group_retracts_children_without_changing_individual_preferences(self):
        login(self.client, self.admin.email)
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

        tool_url = "/admin/visibilidade/tool/document_analysis"
        page = self.client.get("/admin/ferramentas")
        tool_change = self.client.post(tool_url, data={
            "csrf_token": csrf_from(page), "visible": "false",
        }, headers=headers)
        self.assertEqual(tool_change.status_code, 200)

        group_url = "/admin/visibilidade/group/research_quali"
        group_change = self.client.post(group_url, data={
            "csrf_token": csrf_from(self.client.get("/admin/ferramentas")), "visible": "false",
        }, headers=headers)
        self.assertEqual(group_change.status_code, 200)
        hidden_page = self.client.get("/admin/ferramentas").get_data(as_text=True)
        self.assertIn(
            'class="platform-admin-submenu mb-3" data-tool-visibility-children="research_quali" hidden',
            hidden_page,
        )
        self.assertIn('data-visibility-tool="document_analysis"', hidden_page)
        self.assertIn('data-visibility-current="false"', hidden_page)
        self.assertNotIn('Temporariamente indisponível porque o grupo está oculto.', hidden_page)

        restored = self.client.post(group_url, data={
            "csrf_token": csrf_from(self.client.get("/admin/ferramentas")), "visible": "true",
        }, headers=headers)
        self.assertEqual(restored.status_code, 200)
        restored_page = self.client.get("/admin/ferramentas").get_data(as_text=True)
        self.assertIn(
            'class="platform-admin-submenu mb-3" data-tool-visibility-children="research_quali">',
            restored_page,
        )
        self.assertIn('data-visibility-current="false"', restored_page)
        self.assertFalse(db.session.scalar(select(ToolVisibilitySetting.visible).where(
            ToolVisibilitySetting.scope_type == "tool",
            ToolVisibilitySetting.scope_id == "document_analysis",
        )))
        audit_targets = [row.target_id for row in db.session.scalars(select(AuditLog).where(
            AuditLog.action == "tool_visibility_changed",
        ))]
        self.assertEqual(audit_targets.count("tool:document_analysis"), 1)
        self.assertEqual(audit_targets.count("group:research_quali"), 2)

    def test_visibility_endpoint_returns_a_safe_json_error_when_persistence_fails(self):
        login(self.client, self.admin.email)
        page = self.client.get("/admin/ferramentas")
        with patch("platform_core.admin.set_visibility_preference", side_effect=SQLAlchemyError("test-only")):
            response = self.client.post(
                "/admin/visibilidade/tool/pdf_scraper",
                data={"csrf_token": csrf_from(page), "visible": "false"},
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json, {
            "ok": False,
            "changed": False,
            "scope_type": "tool",
            "scope_id": "pdf_scraper",
            "visible": None,
            "message": "Não foi possível atualizar a visibilidade com segurança.",
        })
        self.assertEqual(db.session.scalars(select(ToolVisibilitySetting)).all(), [])


class ToolVisibilityMigrationTests(unittest.TestCase):
    def test_visibility_migration_is_reversible_in_an_isolated_database(self):
        migrations = str(Path(__file__).resolve().parents[1] / "migrations")
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-tool-visibility-migration-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "tool-visibility-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=migrations, revision="e3f4a5b6c7d8")
                self.assertNotIn("tool_visibility_settings", inspect(db.engine).get_table_names())
                upgrade(directory=migrations, revision="f4a5b6c7d8e9")
                inspector = inspect(db.engine)
                self.assertIn("tool_visibility_settings", inspector.get_table_names())
                columns = {column["name"] for column in inspector.get_columns("tool_visibility_settings")}
                self.assertEqual(columns, {
                    "id", "scope_type", "scope_id", "visible", "changed_at",
                    "changed_by_id", "changed_by_name_snapshot",
                })
                self.assertIn("uq_tool_visibility_setting_scope", {
                    constraint["name"] for constraint in inspector.get_unique_constraints("tool_visibility_settings")
                })
                self.assertIn("ck_tool_visibility_setting_scope_type", {
                    constraint["name"] for constraint in inspector.get_check_constraints("tool_visibility_settings")
                })
                with db.engine.connect() as connection:
                    self.assertEqual(
                        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                        "f4a5b6c7d8e9",
                    )
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                downgrade(directory=migrations, revision="e3f4a5b6c7d8")
                self.assertNotIn("tool_visibility_settings", inspect(db.engine).get_table_names())
                with db.engine.connect() as connection:
                    self.assertEqual(
                        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                        "e3f4a5b6c7d8",
                    )
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
