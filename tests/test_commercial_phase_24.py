"""Regressões da Fase Comercial 2.4, sem tocar o banco de desenvolvimento."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, select

from app import create_app
from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.commercial_catalog import COMMERCIAL_PLANS
from platform_core.commercial_limit_services import current_plan_limits, plan_limit_policy_history
from platform_core.commercial_trial_services import CommercialTrialError, activate_commercial_trial, trial_state
from platform_core.extensions import db
from platform_core.models import AccessGrant, AuditLog, CommercialTrial, Plan, PlanLimitPolicy, Project, User, utcnow
from platform_core.services import can_use_tool, current_grant, seed_platform


VALID_CPF = "529.982.247-25"
VALID_CPF_FOR_FUTURE_PLANS = "111.444.777-35"


class CommercialPhase24Tests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.app.config["PESQUISAPDF_CPF_HMAC_SECRET"] = "trial-test-secret-that-is-long-enough-123456"
        self.client = self.app.test_client()
        self.admin = create_user("Admin", "phase24-admin@example.org", "admin")
        login(self.client, self.admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def _pending_user(self, suffix: str) -> User:
        user = User(name=f"Pendente {suffix}", email=f"phase24-{suffix}@example.org", role="user", status="active")
        user.set_password("senha-de-teste-segura-123")
        db.session.add(user)
        db.session.commit()
        return user

    def test_plans_panel_hides_research_quotas_and_uses_modal_for_management_and_integral(self):
        html = self.client.get("/admin/planos").get_data(as_text=True)
        self.assertEqual(self.client.get("/admin/planos").status_code, 200)
        self.assertNotIn("Justificativa (opcional)", html)
        self.assertNotIn("Planos legados", html)
        self.assertNotIn('data-commercial-limit-open="plan-limits-research_quali"', html)
        self.assertIn('data-commercial-limit-open="plan-limits-marketing_instagram"', html)
        self.assertIn('data-commercial-limit-open="plan-limits-marketing_sites"', html)
        self.assertIn('data-commercial-limit-open="plan-limits-marketing_complete"', html)
        self.assertIn('data-commercial-limit-open="plan-limits-integral"', html)
        self.assertEqual(html.count('class="platform-commercial-limit-fields"'), 4)
        self.assertEqual(html.count('class="platform-commercial-limit-field"'), 6)
        self.assertNotIn('class="row g-3 align-items-end"', html.split('id="plan-limits-marketing_complete"', 1)[1])

        token = csrf_from(self.client.get("/admin/planos"))
        saved = self.client.post("/admin/planos/marketing_complete/limites", data={
            "csrf_token": token, "instagram_accounts": "4", "ojs_journals": "2",
        })
        self.assertEqual(saved.status_code, 302)
        plan = db.session.get(Plan, "marketing_complete")
        self.assertEqual(current_plan_limits(plan), {"instagram_accounts": 4, "ojs_journals": 2})
        history = plan_limit_policy_history(plan)
        self.assertEqual(history[0].justification, "")
        self.assertEqual(history[0].published_by_id, self.admin.id)
        self.assertIsNotNone(db.session.scalar(select(AuditLog).where(
            AuditLog.action == "plan_limit_policy_published", AuditLog.target_id == history[0].id
        )))
        self.assertIn("Valores anteriores", self.client.get("/admin/planos").get_data(as_text=True))
        self.assertEqual(
            self.client.post("/admin/planos/research_quali/limites", data={"csrf_token": token}).status_code,
            404,
        )

    def test_limit_endpoint_stays_admin_only_and_does_not_retroactively_change_snapshot(self):
        self.admin.role = "user"
        db.session.commit()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.assertEqual(self.client.post("/admin/planos/marketing_instagram/limites", data={
            "instagram_accounts": "9",
        }).status_code, 403)
        self.app.config["WTF_CSRF_ENABLED"] = True

    def test_trial_is_unique_by_cpf_and_group_and_expires_server_side(self):
        first = self._pending_user("first")
        started = utcnow()
        trial = activate_commercial_trial(self.admin, first, "research_quali", VALID_CPF, now=started)
        db.session.commit()
        self.assertEqual(trial.commercial_group_id, "research")
        self.assertEqual(
            trial.expires_at.replace(tzinfo=started.tzinfo),
            started + timedelta(days=7),
        )
        self.assertEqual(trial_state(trial, at=started + timedelta(days=6, hours=23)), "active")
        self.assertEqual(trial_state(trial, at=started + timedelta(days=7)), "expired")
        grant = current_grant(first)
        self.assertEqual((grant.plan_id, grant.access_mode, grant.status), ("research_quali", "trial", "trial"))
        self.assertTrue(can_use_tool(first, "pdf_scraper"))
        self.assertFalse(can_use_tool(first, "analytics_instagram"))

        same_group_other_plan = self._pending_user("same-group")
        with self.assertRaisesRegex(CommercialTrialError, "já utilizou"):
            activate_commercial_trial(self.admin, same_group_other_plan, "research_complete", VALID_CPF)
        self.assertIsNone(current_grant(same_group_other_plan))

        other_group = self._pending_user("other-group")
        management_trial = activate_commercial_trial(self.admin, other_group, "marketing_instagram", VALID_CPF)
        db.session.commit()
        self.assertEqual(management_trial.commercial_group_id, "management")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(CommercialTrial)), 2)
        self.assertNotIn("52998224725", trial.cpf_fingerprint)
        self.assertNotIn("529.982.247-25", str(db.session.scalar(select(AuditLog.after_json).where(
            AuditLog.action == "commercial_trial_activated"
        ))))

        grant.expires_at = started - timedelta(seconds=1)
        db.session.commit()
        self.assertFalse(can_use_tool(first, "pdf_scraper"))

    def test_trial_route_validates_cpf_and_requires_administrator(self):
        pending = self._pending_user("route")
        missing_csrf = self.client.post(
            f"/admin/usuarios/{pending.id}/periodo-de-teste",
            data={"plan_id": "research_quali", "cpf": VALID_CPF},
        )
        self.assertEqual(missing_csrf.status_code, 400)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(CommercialTrial)), 0)
        token = csrf_from(self.client.get(f"/admin/usuarios/{pending.id}"))
        invalid = self.client.post(f"/admin/usuarios/{pending.id}/periodo-de-teste", data={
            "csrf_token": token, "plan_id": "research_quali", "cpf": "111.111.111-11",
        })
        self.assertEqual(invalid.status_code, 302)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(CommercialTrial)), 0)
        token = csrf_from(self.client.get(f"/admin/usuarios/{pending.id}"))
        activated = self.client.post(f"/admin/usuarios/{pending.id}/periodo-de-teste", data={
            "csrf_token": token, "plan_id": "research_quali", "cpf": VALID_CPF,
        })
        self.assertEqual(activated.status_code, 302)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(CommercialTrial)), 1)

        self.admin.role = "user"
        db.session.commit()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.assertEqual(self.client.post(f"/admin/usuarios/{pending.id}/periodo-de-teste", data={
            "plan_id": "research_quali", "cpf": VALID_CPF,
        }).status_code, 403)
        self.app.config["WTF_CSRF_ENABLED"] = True

    def test_future_plan_trials_are_allowed_without_enabling_future_tools(self):
        quanti_user = self._pending_user("trial-quanti")
        quanti_trial = activate_commercial_trial(
            self.admin, quanti_user, "research_quanti", VALID_CPF_FOR_FUTURE_PLANS,
        )
        db.session.commit()
        self.assertEqual(quanti_trial.commercial_group_id, "research")
        self.assertEqual(current_grant(quanti_user).plan_id, "research_quanti")
        self.assertFalse(can_use_tool(quanti_user, "quantitative_analysis"))

        ojs_user = self._pending_user("trial-ojs")
        ojs_trial = activate_commercial_trial(
            self.admin, ojs_user, "marketing_sites", VALID_CPF_FOR_FUTURE_PLANS,
        )
        db.session.commit()
        self.assertEqual(ojs_trial.commercial_group_id, "management")
        self.assertEqual(current_grant(ojs_user).plan_id, "marketing_sites")
        self.assertFalse(can_use_tool(ojs_user, "ojs_seo"))

        same_group_user = self._pending_user("trial-quanti-repeat")
        with self.assertRaisesRegex(CommercialTrialError, "já utilizou"):
            activate_commercial_trial(
                self.admin, same_group_user, "research_quali", VALID_CPF_FOR_FUTURE_PLANS,
            )


class CommercialPhase24MigrationTests(unittest.TestCase):
    revision = "0a1b2c3d4e5f"
    previous_revision = "f9a0b1c2d3e4"
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")

    def _app(self, root: str):
        database = Path(root) / "commercial-phase24.sqlite3"
        return create_app({
            "TESTING": True,
            "SECRET_KEY": "commercial-phase24-migration-test-only",
            "PESQUISAPDF_CPF_HMAC_SECRET": "commercial-phase24-hmac-secret-0123456789",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "PLATFORM_DATA_DIR": root,
        })

    def test_upgrade_removes_only_legacy_commercial_records_and_creates_trial_schema(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-commercial-phase24-") as root:
            app = self._app(root)
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.previous_revision)
                legacy_user = User(name="Legado", email="phase24-legacy@example.org", role="user", status="active")
                legacy_user.set_password("senha-de-teste-segura-123")
                commercial_user = User(name="Comercial", email="phase24-commercial@example.org", role="user", status="active")
                commercial_user.set_password("senha-de-teste-segura-123")
                db.session.add_all((
                    Plan(id="student", name="Estudante", description="Teste legado", active=True),
                    Plan(id="researcher", name="Pesquisador legado", description="Teste legado", active=True),
                    Plan(id="pro", name="Pro", description="Teste legado", active=True),
                    Plan(id="institutional", name="Institucional", description="Teste legado", active=True),
                    legacy_user,
                    commercial_user,
                ))
                db.session.flush()
                db.session.add_all((
                    AccessGrant(user_id=legacy_user.id, plan_id="student", access_mode="student_free", status="active"),
                    AccessGrant(user_id=commercial_user.id, plan_id="research_quali", access_mode="paid", status="active"),
                    Project(owner_user_id=legacy_user.id, name="Projeto preservado", description=""),
                ))
                db.session.commit()
                legacy_user_id, commercial_user_id = legacy_user.id, commercial_user.id

                upgrade(directory=self.migrations, revision=self.revision)
                db.session.remove()
                legacy_ids = {"student", "researcher", "pro", "institutional"}
                self.assertFalse(legacy_ids & {item.id for item in db.session.scalars(select(Plan)).all()})
                self.assertEqual({item.plan_id for item in db.session.scalars(select(AccessGrant)).all()}, {"research_quali"})
                self.assertIsNotNone(db.session.get(User, legacy_user_id))
                self.assertIsNotNone(db.session.get(User, commercial_user_id))
                self.assertEqual(db.session.scalar(select(func.count()).select_from(Project)), 1)
                self.assertEqual(
                    {item.id for item in db.session.scalars(select(Plan)).all()},
                    {definition.plan_id for definition in COMMERCIAL_PLANS},
                )
                with db.engine.connect() as connection:
                    self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                    self.assertIn("commercial_trials", {
                        row[0] for row in connection.exec_driver_sql(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    })
                    self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), self.revision)

                with self.assertRaises(SystemExit):
                    downgrade(directory=self.migrations, revision=self.previous_revision)
                db.session.remove()
                db.engine.dispose()
