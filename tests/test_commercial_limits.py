"""Políticas de limite comerciais: persistência, contratos e administração."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, select

from app import create_app
from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.commercial_catalog import (
    COMMERCIAL_LIMIT_RESOURCES,
    COMMERCIAL_PLANS,
    commercial_plan_initial_limits,
)
from platform_core.commercial_limit_services import (
    CommercialLimitError,
    current_plan_limits,
    effective_subscription_limits,
    grant_subscription_limit_adjustment,
    plan_limit_policy_history,
    publish_plan_limit_policy,
    revoke_subscription_limit_adjustment,
)
from platform_core.extensions import db
from platform_core.models import (
    AccessGrant,
    AuditLog,
    Plan,
    PlanLimitPolicy,
    PlanTool,
    Tool,
    User,
    UserToolOverride,
    WorkspaceSubscription,
    WorkspaceSubscriptionLimitAdjustment,
)
from platform_core.workspace_services import (
    create_individual_workspace,
    create_workspace_subscription,
    end_workspace_subscription,
)


class CommercialLimitCatalogAndAdminTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_initial_policies_match_the_seven_commercial_plans_and_seed_is_idempotent(self):
        self.assertEqual(
            {resource.resource_id for resource in COMMERCIAL_LIMIT_RESOURCES},
            {"instagram_accounts", "ojs_journals"},
        )
        self.assertEqual(
            {row.plan_id for row in db.session.scalars(select(PlanLimitPolicy))},
            {definition.plan_id for definition in COMMERCIAL_PLANS},
        )
        for definition in COMMERCIAL_PLANS:
            with self.subTest(plan_id=definition.plan_id):
                plan = db.session.get(Plan, definition.plan_id)
                self.assertEqual(current_plan_limits(plan), commercial_plan_initial_limits(plan.id))
                history = plan_limit_policy_history(plan)
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0].version, 1)
                self.assertEqual(history[0].limits_json, commercial_plan_initial_limits(plan.id))

        immutable = plan_limit_policy_history(db.session.get(Plan, "marketing_instagram"))[0]
        immutable.limits_json = {"instagram_accounts": 99, "ojs_journals": 99}
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()

        # Repetir bootstrap não cria política duplicada nem altera a publicada.
        from platform_core.services import seed_platform
        seed_platform()
        self.assertEqual(db.session.scalar(select(func.count()).select_from(PlanLimitPolicy)), 7)

    def test_administrator_publishes_limits_with_history_and_audit(self):
        admin = create_user("Admin", "limits-admin@example.org", role="admin", plan="institutional")
        login(self.client, admin.email)
        page = self.client.get("/admin/planos")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Limites e recursos", page.get_data(as_text=True))
        token = csrf_from(page)
        response = self.client.post("/admin/planos/marketing_complete/limites", data={
            "csrf_token": token,
            "instagram_accounts": "5",
            "ojs_journals": "2",
            "justification": "Ampliação contratual aprovada.",
        })
        self.assertEqual(response.status_code, 302)
        plan = db.session.get(Plan, "marketing_complete")
        self.assertEqual(current_plan_limits(plan), {"instagram_accounts": 5, "ojs_journals": 2})
        history = plan_limit_policy_history(plan)
        self.assertEqual([row.version for row in history], [2, 1])
        self.assertEqual(history[1].limits_json, {"instagram_accounts": 3, "ojs_journals": 1})
        self.assertEqual(history[0].published_by_id, admin.id)
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "plan_limit_policy_published", AuditLog.target_id == history[0].id
            )),
            1,
        )

        token = csrf_from(self.client.get("/admin/planos"))
        duplicate = self.client.post("/admin/planos/marketing_complete/limites", data={
            "csrf_token": token,
            "instagram_accounts": "5",
            "ojs_journals": "2",
        })
        self.assertEqual(duplicate.status_code, 302)
        self.assertEqual(len(plan_limit_policy_history(plan)), 2)
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "plan_limit_policy_published"
            )),
            1,
        )

    def test_invalid_values_and_non_administrators_do_not_publish_policy(self):
        user = create_user("Usuário", "limits-user@example.org", plan="student")
        login(self.client, user.email)
        self.assertEqual(self.client.get("/admin/planos").status_code, 403)
        token = csrf_from(self.client.get("/"))
        denied = self.client.post("/admin/planos/marketing_instagram/limites", data={
            "csrf_token": token, "instagram_accounts": "2", "ojs_journals": "0",
        })
        self.assertEqual(denied.status_code, 403)

        admin = create_user("Admin", "limits-invalid-admin@example.org", role="admin", plan="institutional")
        logout_token = csrf_from(self.client.get("/"))
        self.assertEqual(self.client.post("/logout", data={"csrf_token": logout_token}).status_code, 302)
        login(self.client, admin.email)
        token = csrf_from(self.client.get("/admin/planos"))
        invalid = self.client.post("/admin/planos/marketing_instagram/limites", data={
            "csrf_token": token, "instagram_accounts": "-1", "ojs_journals": "0",
        })
        self.assertEqual(invalid.status_code, 302)
        plan = db.session.get(Plan, "marketing_instagram")
        self.assertEqual(len(plan_limit_policy_history(plan)), 1)
        self.assertEqual(current_plan_limits(plan), {"instagram_accounts": 1, "ojs_journals": 0})
        token = csrf_from(self.client.get("/admin/planos"))
        decimal = self.client.post("/admin/planos/marketing_instagram/limites", data={
            "csrf_token": token, "instagram_accounts": "1.5", "ojs_journals": "0",
        })
        self.assertEqual(decimal.status_code, 302)
        self.assertEqual(len(plan_limit_policy_history(plan)), 1)

    def test_snapshots_are_not_retroactively_changed_and_adjustments_are_explicit(self):
        owner = create_user("Titular", "limits-owner@example.org", plan="student")
        admin = create_user("Admin", "limits-workspace-admin@example.org", role="admin", plan="institutional")
        workspace = create_individual_workspace(owner, "Contrato comercial")
        first = create_workspace_subscription(admin, workspace.id, "marketing_complete")
        db.session.commit()
        self.assertEqual(first.terms_json["plan_limits"], {"instagram_accounts": 3, "ojs_journals": 1})
        self.assertEqual(first.terms_json["plan_policy_version"], 1)

        publish_plan_limit_policy(admin, "marketing_complete", {
            "instagram_accounts": 4, "ojs_journals": 2,
        }, justification="Nova oferta para contratos futuros.")
        db.session.commit()
        self.assertEqual(first.terms_json["plan_limits"], {"instagram_accounts": 3, "ojs_journals": 1})

        adjustment = grant_subscription_limit_adjustment(
            admin, first.id, "instagram_accounts", 2, justification="Duas conexões adicionais.",
        )
        db.session.commit()
        effective = effective_subscription_limits(first)
        self.assertEqual(effective["resources"]["instagram_accounts"], {
            "contractual_limit": 3,
            "additional_units": 2,
            "effective_limit": 5,
            "commercially_available": True,
        })
        self.assertEqual(effective["resources"]["ojs_journals"]["effective_limit"], 1)
        revoked, changed = revoke_subscription_limit_adjustment(admin, adjustment.id, reason="Término do adicional.")
        db.session.commit()
        self.assertTrue(changed)
        self.assertEqual(revoked.status, "revoked")
        self.assertEqual(effective_subscription_limits(first)["resources"]["instagram_accounts"]["effective_limit"], 3)
        _same, changed_again = revoke_subscription_limit_adjustment(admin, adjustment.id)
        self.assertFalse(changed_again)
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "workspace_subscription_limit_adjustment_revoked"
            )),
            1,
        )

        end_workspace_subscription(admin, workspace.id)
        second = create_workspace_subscription(admin, workspace.id, "marketing_complete")
        db.session.commit()
        self.assertEqual(second.terms_json["plan_limits"], {"instagram_accounts": 4, "ojs_journals": 2})
        self.assertEqual(second.terms_json["plan_policy_version"], 2)
        self.assertEqual(effective_subscription_limits(first)["resources"]["instagram_accounts"]["effective_limit"], 0)

    def test_adjustments_do_not_cross_workspace_boundaries_or_enable_access(self):
        owner_one = create_user("Titular 1", "limits-one@example.org", plan="student")
        owner_two = create_user("Titular 2", "limits-two@example.org", plan="student")
        admin = create_user("Admin", "limits-isolation-admin@example.org", role="admin", plan="institutional")
        subscription_one = create_workspace_subscription(
            admin, create_individual_workspace(owner_one, "Área um").id, "marketing_instagram"
        )
        subscription_two = create_workspace_subscription(
            admin, create_individual_workspace(owner_two, "Área dois").id, "marketing_instagram"
        )
        db.session.commit()
        grant_subscription_limit_adjustment(admin, subscription_one.id, "instagram_accounts", 3)
        db.session.commit()
        self.assertEqual(effective_subscription_limits(subscription_one)["resources"]["instagram_accounts"]["effective_limit"], 4)
        self.assertEqual(effective_subscription_limits(subscription_two)["resources"]["instagram_accounts"]["effective_limit"], 1)
        with self.assertRaises(CommercialLimitError):
            grant_subscription_limit_adjustment(owner_one, subscription_one.id, "instagram_accounts", 1)


class CommercialLimitMigrationTests(unittest.TestCase):
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")
    previous_revision = "b5c6d7e8f90"
    workspace_revision = "e8f901a2b3c4"
    revision = "f9a0b1c2d3e4"

    def _app(self, root: str):
        database = Path(root) / "platform.sqlite3"
        return create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "PLATFORM_DATA_DIR": root,
            "SECRET_KEY": "commercial-limit-migration-test-only",
        })

    def test_upgrade_from_base_initializes_policies_and_preserves_legacy_records(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-commercial-limits-") as root:
            app = self._app(root)
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.previous_revision)
                user = User(name="Legado", email="limits-legacy@example.org", role="user", status="active")
                user.set_password("senha-de-teste-segura-123")
                db.session.add_all((
                    Plan(id="student", name="Estudante", description="Legado", active=True),
                    Tool(id="pdf_scraper", name="Raspagem por termos", route="/raspagem-livre", active=True),
                    user,
                ))
                db.session.flush()
                db.session.add_all((
                    PlanTool(plan_id="student", tool_id="pdf_scraper"),
                    AccessGrant(user_id=user.id, plan_id="student", access_mode="student_free", status="active"),
                    UserToolOverride(user_id=user.id, tool_id="pdf_scraper", decision="deny"),
                ))
                db.session.commit()
                upgrade(directory=self.migrations, revision=self.revision)
                self.assertEqual(db.session.get(Plan, "student").name, "Estudante")
                self.assertEqual(db.session.scalar(select(func.count()).select_from(PlanLimitPolicy)), 7)
                self.assertEqual(current_plan_limits(db.session.get(Plan, "integral")), {
                    "instagram_accounts": 5, "ojs_journals": 1,
                })
                self.assertEqual(db.session.scalar(select(func.count()).select_from(AccessGrant)), 1)
                self.assertEqual(db.session.scalar(select(func.count()).select_from(UserToolOverride)), 1)
                with db.engine.connect() as connection:
                    tables = {row[0] for row in connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )}
                    self.assertTrue({"plan_limit_policies", "workspace_subscription_limit_adjustments"}.issubset(tables))
                    self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                    self.assertEqual(connection.exec_driver_sql(
                        "SELECT version_num FROM alembic_version"
                    ).scalar_one(), self.revision)
                db.session.remove()
                db.engine.dispose()

    def test_upgrade_from_workspace_revision_preserves_subscription_snapshot_and_downgrades_cleanly(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-commercial-limits-workspace-") as root:
            app = self._app(root)
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.workspace_revision)
                owner = User(name="Titular", email="limits-workspace-owner@example.org", role="user", status="active")
                owner.set_password("senha-de-teste-segura-123")
                admin = User(name="Admin", email="limits-workspace-migration-admin@example.org", role="admin", status="active")
                admin.set_password("senha-de-teste-segura-123")
                db.session.add_all((owner, admin))
                db.session.flush()
                workspace = create_individual_workspace(owner, "Snapshot anterior")
                plan = db.session.get(Plan, "marketing_instagram")
                plan.limits_json = {"instagram_accounts": 9}
                subscription = create_workspace_subscription(admin, workspace.id, plan.id)
                db.session.commit()
                snapshot_before = dict(subscription.terms_json)

                upgrade(directory=self.migrations, revision=self.revision)
                db.session.expire_all()
                subscription = db.session.get(WorkspaceSubscription, subscription.id)
                self.assertEqual(subscription.terms_json, snapshot_before)
                self.assertEqual(current_plan_limits(db.session.get(Plan, plan.id)), {
                    "instagram_accounts": 9, "ojs_journals": 0,
                })
                self.assertEqual(db.session.scalar(select(func.count()).select_from(PlanLimitPolicy)), 7)

                downgrade(directory=self.migrations, revision=self.workspace_revision)
                with db.engine.connect() as connection:
                    tables = {row[0] for row in connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )}
                    self.assertFalse({"plan_limit_policies", "workspace_subscription_limit_adjustments"} & tables)
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                db.session.remove()
                db.engine.dispose()

    def test_downgrade_is_blocked_after_a_real_adjustment(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-commercial-limits-downgrade-") as root:
            app = self._app(root)
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.revision)
                admin = User(name="Admin", email="limits-downgrade-admin@example.org", role="admin", status="active")
                owner = User(name="Titular", email="limits-downgrade-owner@example.org", role="user", status="active")
                admin.set_password("senha-de-teste-segura-123")
                owner.set_password("senha-de-teste-segura-123")
                db.session.add_all((admin, owner))
                db.session.flush()
                workspace = create_individual_workspace(owner, "Contrato")
                subscription = create_workspace_subscription(admin, workspace.id, "marketing_instagram")
                grant_subscription_limit_adjustment(admin, subscription.id, "instagram_accounts", 1)
                db.session.commit()
                with self.assertRaises(SystemExit) as blocked:
                    downgrade(directory=self.migrations, revision=self.workspace_revision)
                self.assertEqual(blocked.exception.code, 1)
                with db.engine.connect() as connection:
                    self.assertEqual(connection.exec_driver_sql(
                        "SELECT version_num FROM alembic_version"
                    ).scalar_one(), self.revision)
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
