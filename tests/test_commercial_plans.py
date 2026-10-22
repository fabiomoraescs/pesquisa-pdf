"""Catálogo comercial, ativação administrativa e migration de dados isolada."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, select

from app import create_app
from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.commercial_catalog import (
    COMMERCIAL_GROUPS,
    COMMERCIAL_PLANS,
    commercial_plan_definition,
    commercial_plan_display_name,
    validate_commercial_catalog,
)
from platform_core.extensions import db
from platform_core.models import AccessGrant, AuditLog, Plan, PlanTool, Tool, User, UserToolOverride
from platform_core.services import can_use_tool, current_grant, seed_platform


QUALI_TOOLS = {"pdf_scraper", "document_analysis", "qualitative_analysis"}


class CommercialCatalogTests(unittest.TestCase):
    def test_catalog_has_exactly_three_groups_and_seven_unique_plans(self):
        validate_commercial_catalog()
        self.assertEqual(len(COMMERCIAL_GROUPS), 3)
        self.assertEqual(len(COMMERCIAL_PLANS), 7)
        self.assertEqual(len({group.group_id for group in COMMERCIAL_GROUPS}), 3)
        self.assertEqual(len({plan.plan_id for plan in COMMERCIAL_PLANS}), 7)
        self.assertEqual(
            {plan.plan_id for plan in COMMERCIAL_PLANS},
            {
                "research_quali", "research_quanti", "research_complete",
                "marketing_instagram", "marketing_sites", "marketing_complete", "integral",
            },
        )

    def test_catalog_distinguishes_available_and_future_tools(self):
        self.assertEqual(
            commercial_plan_definition("research_quali").available_tool_ids,
            ("pdf_scraper", "document_analysis", "qualitative_analysis"),
        )
        self.assertTrue(commercial_plan_definition("research_quanti").eligible_for_new_assignment)
        self.assertTrue(commercial_plan_definition("marketing_sites").eligible_for_new_assignment)
        self.assertIn("ojs_seo", commercial_plan_definition("marketing_sites").planned_tool_ids)
        self.assertIsNone(commercial_plan_definition("student"))

    def test_commercial_labels_use_definitive_names_without_changing_technical_plan_ids(self):
        self.assertEqual(
            tuple(group.name for group in COMMERCIAL_GROUPS),
            ("Pesquisador", "Gestão", "Pesquisador + Gestão"),
        )
        self.assertEqual(
            tuple(plan.name for plan in COMMERCIAL_PLANS),
            ("Quali", "Quanti", "Quali + Quanti", "Instagram", "OJS", "Insta + OJS", "Integral"),
        )
        self.assertEqual(commercial_plan_definition("marketing_sites").name, "OJS")
        self.assertEqual(commercial_plan_definition("marketing_complete").name, "Insta + OJS")
        self.assertEqual(commercial_plan_display_name("research_complete", "Plano Pesquisa Completo"), "Quali + Quanti")
        self.assertEqual(commercial_plan_display_name("legacy_plan", "Plano histórico"), "Plano histórico")
        visible_text = " ".join(
            [group.name + " " + group.description for group in COMMERCIAL_GROUPS]
            + [plan.name + " " + plan.description for plan in COMMERCIAL_PLANS]
        )
        self.assertNotIn("marketing", visible_text.casefold())

    def test_commercial_plan_names_have_local_nonwrapping_presentation_rules(self):
        root = Path(__file__).resolve().parents[1]
        stylesheet = (root / "static" / "css" / "style.css").read_text(encoding="utf-8")
        matrix_template = (root / "templates" / "platform" / "access_matrix.html").read_text(encoding="utf-8")

        self.assertIn(
            ".platform-commercial-plan-name { display: inline-block; white-space: nowrap; }",
            stylesheet,
        )
        self.assertIn(
            ".platform-commercial-plan-matrix { min-width: 100%; width: max-content; }",
            stylesheet,
        )
        self.assertIn("platform-commercial-plan-header", matrix_template)
        self.assertIn("commercial_plan_name(plan.id, plan.name)", matrix_template)


class CommercialPlanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_seed_adds_commercial_plans_and_only_real_tool_links_idempotently(self):
        self.assertEqual(
            {plan.id for plan in db.session.scalars(select(Plan))},
            {definition.plan_id for definition in COMMERCIAL_PLANS},
        )
        links = {(row.plan_id, row.tool_id) for row in db.session.scalars(select(PlanTool))}
        expected = {
            (definition.plan_id, tool_id)
            for definition in COMMERCIAL_PLANS
            for tool_id in definition.available_tool_ids
        }
        self.assertTrue(expected.issubset(links))
        self.assertFalse(any(tool_id in {"quantitative_analysis", "ojs_seo", "ojs_analytics"}
                             for _plan_id, tool_id in links))
        snapshot = (set(links), {plan.id for plan in db.session.scalars(select(Plan))})
        seed_platform()
        self.assertEqual(
            snapshot,
            (
                {(row.plan_id, row.tool_id) for row in db.session.scalars(select(PlanTool))},
                {plan.id for plan in db.session.scalars(select(Plan))},
            ),
        )

    def test_seed_does_not_migrate_a_partially_initialized_legacy_database(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add_all((
            Plan(id="student", name="Estudante", description="Legado", active=True),
            Tool(id="pdf_scraper", name="Raspagem por termos", route="/raspagem-livre", active=True),
        ))
        db.session.commit()

        seed_platform()

        self.assertEqual(
            {plan.id for plan in db.session.scalars(select(Plan))},
            {"student"},
        )

    def test_available_plan_access_matrix_preserves_overrides_and_requires_main_grant(self):
        expected = {
            "research_quali": (QUALI_TOOLS, set()),
            "research_complete": (QUALI_TOOLS, set()),
            "marketing_instagram": ({"analytics_instagram"}, QUALI_TOOLS),
            "marketing_complete": ({"analytics_instagram"}, QUALI_TOOLS),
            "integral": (QUALI_TOOLS | {"analytics_instagram"}, set()),
        }
        for index, (plan_id, (allowed, denied)) in enumerate(expected.items()):
            with self.subTest(plan_id=plan_id):
                user = create_user(
                    name=f"Usuário {index}", email=f"commercial-{index}@example.org", plan=plan_id,
                )
                for tool_id in allowed:
                    self.assertTrue(can_use_tool(user, tool_id))
                for tool_id in denied:
                    self.assertFalse(can_use_tool(user, tool_id))

        user = create_user("Com override", "override-commercial@example.org", plan="research_quali")
        db.session.add(UserToolOverride(user_id=user.id, tool_id="document_analysis", decision="deny"))
        db.session.commit()
        self.assertFalse(can_use_tool(user, "document_analysis"))

        pending = User(name="Pendente", email="pending-commercial@example.org", role="user", status="active")
        pending.set_password("senha-de-teste-segura-123")
        db.session.add(pending)
        db.session.flush()
        db.session.add(UserToolOverride(user_id=pending.id, tool_id="analytics_instagram", decision="allow"))
        db.session.commit()
        self.assertIsNone(current_grant(pending))
        self.assertFalse(can_use_tool(pending, "analytics_instagram"))

    def test_administrator_sees_and_assigns_all_commercial_plans_in_catalog_order(self):
        admin = create_user("Admin", "admin-commercial@example.org", role="admin", plan="institutional")
        pending = User(name="Pendente", email="pending-admin@example.org", role="user", status="active")
        pending.set_password("senha-de-teste-segura-123")
        db.session.add(pending)
        db.session.commit()
        login(self.client, admin.email)
        url = f"/admin/usuarios/{pending.id}/acesso"
        detail = self.client.get(f"/admin/usuarios/{pending.id}")
        detail_html = detail.get_data(as_text=True)
        self.assertNotIn('value="student"', detail_html)
        self.assertNotIn('value="institutional"', detail_html)
        assignment_select = detail_html.split('id="plan-id"', 1)[1].split("</select>", 1)[0]
        trial_select = detail_html.split('id="trial-plan-id"', 1)[1].split("</select>", 1)[0]
        for label in (
            "Plano Quali", "Plano Quanti", "Plano Quali + Quanti", "Plano Instagram",
            "Plano OJS", "Plano Insta + OJS", "Plano Integral",
        ):
            self.assertIn(f">{label}</option>", assignment_select)
        for label in ("Quali", "Quanti", "Quali + Quanti", "Instagram", "OJS", "Insta + OJS", "Integral"):
            self.assertIn(f">{label}</option>", trial_select)
        self.assertNotIn("disabled", assignment_select)
        self.assertNotIn("disabled", trial_select)
        self.assertNotIn("Plano Gestão Completa", detail_html)
        plan_positions = [assignment_select.index(f'value="{plan_id}"') for plan_id in (
            'research_quali', 'research_quanti', 'research_complete', 'marketing_instagram',
            'marketing_sites', 'marketing_complete', 'integral',
        )]
        self.assertEqual(plan_positions, sorted(plan_positions))
        for group_name in ("Pesquisador", "Gestão", "Pesquisador + Gestão"):
            self.assertIn(f'<optgroup label="{group_name}">', assignment_select)
            self.assertIn(f'<optgroup label="{group_name}">', trial_select)

        token = csrf_from(detail)
        response = self.client.post(url, data={
            "csrf_token": token, "plan_id": "research_quanti",
            "access_mode": "admin_courtesy", "access_status": "active",
        })
        self.assertEqual(response.status_code, 302)
        first = current_grant(pending)
        self.assertEqual(first.plan_id, "research_quanti")
        self.assertFalse(can_use_tool(pending, "quantitative_analysis"))

        token = csrf_from(self.client.get(f"/admin/usuarios/{pending.id}"))
        response = self.client.post(url, data={
            "csrf_token": token, "plan_id": "marketing_sites",
            "access_mode": "admin_courtesy", "access_status": "active",
        })
        self.assertEqual(response.status_code, 302)
        second = current_grant(pending)
        self.assertEqual(second.plan_id, "marketing_sites")
        self.assertIsNotNone(db.session.get(AccessGrant, first.id).ended_at)
        self.assertFalse(can_use_tool(pending, "ojs_seo"))

        token = csrf_from(self.client.get(f"/admin/usuarios/{pending.id}"))
        response = self.client.post(url, data={
            "csrf_token": token, "plan_id": "research_quali",
            "access_mode": "admin_courtesy", "access_status": "active",
        })
        self.assertEqual(response.status_code, 302)
        third = current_grant(pending)
        self.assertEqual(third.plan_id, "research_quali")
        self.assertTrue(can_use_tool(pending, "pdf_scraper"))

        token = csrf_from(self.client.get(f"/admin/usuarios/{pending.id}"))
        response = self.client.post(url, data={
            "csrf_token": token, "plan_id": "marketing_instagram",
                    "access_mode": "admin_courtesy", "access_status": "active",
        })
        self.assertEqual(response.status_code, 302)
        fourth = current_grant(pending)
        self.assertEqual(fourth.plan_id, "marketing_instagram")
        self.assertIsNotNone(db.session.get(AccessGrant, third.id).ended_at)
        self.assertTrue(can_use_tool(pending, "analytics_instagram"))
        self.assertFalse(can_use_tool(pending, "pdf_scraper"))
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "access_grant_changed", AuditLog.target_id == pending.id
            )),
            4,
        )

    def test_deactivated_plan_stays_visible_but_cannot_be_assigned_or_tested(self):
        admin = create_user("Admin", "admin-commercial-inactive@example.org", role="admin", plan="institutional")
        pending = User(name="Pendente", email="pending-inactive@example.org", role="user", status="active")
        pending.set_password("senha-de-teste-segura-123")
        db.session.add(pending)
        db.session.get(Plan, "research_quanti").active = False
        db.session.commit()
        login(self.client, admin.email)

        page = self.client.get(f"/admin/usuarios/{pending.id}")
        html = page.get_data(as_text=True)
        assignment_select = html.split('id="plan-id"', 1)[1].split("</select>", 1)[0]
        trial_select = html.split('id="trial-plan-id"', 1)[1].split("</select>", 1)[0]
        self.assertIn('<option value="research_quanti" disabled>Plano Quanti — plano inativo</option>', assignment_select)
        self.assertIn('<option value="research_quanti" disabled>Quanti — plano inativo</option>', trial_select)

        response = self.client.post(f"/admin/usuarios/{pending.id}/acesso", data={
            "csrf_token": csrf_from(page), "plan_id": "research_quanti",
            "access_mode": "paid", "access_status": "active",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(current_grant(pending))

        response = self.client.post(f"/admin/usuarios/{pending.id}/periodo-de-teste", data={
            "csrf_token": csrf_from(self.client.get(f"/admin/usuarios/{pending.id}")),
            "plan_id": "research_quanti", "cpf": "529.982.247-25",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(current_grant(pending))

    def test_plans_panel_contains_only_current_commercial_groups(self):
        admin = create_user("Admin", "admin-commercial-list@example.org", role="admin", plan="institutional")
        db.session.get(Plan, "marketing_complete").name = "Plano Gestão Completa"
        db.session.commit()
        login(self.client, admin.email)
        html = self.client.get("/admin/planos").get_data(as_text=True)
        for name in ("Pesquisador", "Gestão", "Pesquisador + Gestão"):
            self.assertIn(name, html)
        for obsolete_label in ("Marketing e Gestão", "Pesquisador + Marketing", "Plano Sites", "Plano Marketing Completo"):
            self.assertNotIn(obsolete_label, html)
        for plan in COMMERCIAL_PLANS:
            self.assertIn(plan.name, html)
        self.assertNotIn("Plano Gestão Completa", html)
        self.assertIn('class="platform-commercial-plan-name"', html)
        self.assertNotIn("Planos legados", html)
        self.assertNotIn("Estudante", html)

    def test_access_matrix_uses_only_commercial_plans_in_catalog_order_and_preserves_legacy_links(self):
        admin = create_user("Admin", "admin-commercial-matrix@example.org", role="admin", plan="institutional")
        db.session.add(Plan(id="student", name="Estudante", description="Legado", active=True))
        db.session.flush()
        db.session.add(PlanTool(plan_id="student", tool_id="pdf_scraper"))
        db.session.commit()
        login(self.client, admin.email)

        db.session.get(Plan, "research_complete").name = "Plano Pesquisa Completo"
        db.session.commit()

        page = self.client.get("/admin/definir-acessos")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertNotIn(">Estudante<", html)
        self.assertNotIn('value="student|pdf_scraper"', html)
        group_positions = [html.index(label) for label in (
            ">Pesquisador<", ">Gestão<", ">Pesquisador + Gestão<",
        )]
        plan_positions = [html.index(label) for label in (
            'platform-commercial-plan-header"><span class="platform-commercial-plan-name">Quali</span>',
            'platform-commercial-plan-header"><span class="platform-commercial-plan-name">Quanti</span>',
            'platform-commercial-plan-header"><span class="platform-commercial-plan-name">Quali + Quanti</span>',
            'platform-commercial-plan-header"><span class="platform-commercial-plan-name">Instagram</span>',
            'platform-commercial-plan-header"><span class="platform-commercial-plan-name">OJS</span>',
            'platform-commercial-plan-header"><span class="platform-commercial-plan-name">Insta + OJS</span>',
            'platform-commercial-plan-header"><span class="platform-commercial-plan-name">Integral</span>',
        )]
        self.assertEqual(group_positions, sorted(group_positions))
        self.assertEqual(plan_positions, sorted(plan_positions))
        self.assertNotIn("Plano Pesquisa Completo", html)
        self.assertIn('platform-commercial-plan-matrix', html)
        self.assertIn('platform-commercial-plan-header', html)

        commercial_plan_ids = {definition.plan_id for definition in COMMERCIAL_PLANS}
        submitted = [
            f"{row.plan_id}|{row.tool_id}"
            for row in db.session.scalars(select(PlanTool))
            if row.plan_id in commercial_plan_ids
        ]
        response = self.client.post("/admin/definir-acessos", data={
            "csrf_token": csrf_from(page), "access": submitted,
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(db.session.get(PlanTool, ("student", "pdf_scraper")))

    def test_admin_without_grant_remains_operational(self):
        admin = User(name="Admin sem plano", email="admin-no-plan@example.org", role="admin", status="active")
        admin.set_password("senha-de-teste-segura-123")
        db.session.add(admin)
        db.session.commit()
        login(self.client, admin.email)
        self.assertIsNone(current_grant(admin))
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertTrue(can_use_tool(admin, "pdf_scraper"))


class CommercialPlanMigrationTests(unittest.TestCase):
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")
    previous_revision = "b5c6d7e8f90"
    commercial_revision = "c6d7e8f901a2"
    revision = "d7e8f901a2b3"

    def test_upgrade_and_downgrade_are_isolated_and_preserve_legacy_data(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-commercial-plans-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "commercial-plans-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.previous_revision)
                legacy = Plan(id="student", name="Estudante", description="Legado", active=True)
                tool = Tool(id="pdf_scraper", name="Raspagem por termos", route="/raspagem-livre", active=True)
                user = User(name="Legado", email="legacy-commercial@example.org", role="user", status="active")
                user.set_password("senha-de-teste-segura-123")
                db.session.add_all((legacy, tool, user))
                db.session.flush()
                db.session.add(PlanTool(plan_id="student", tool_id="pdf_scraper"))
                db.session.add(AccessGrant(user_id=user.id, plan_id="student", access_mode="student_free", status="active"))
                db.session.commit()

                # O caminho completo precisa passar pela revisão de catálogo
                # original antes de aplicar somente a correção de metadados.
                upgrade(directory=self.migrations, revision=self.commercial_revision)
                self.assertEqual(db.session.get(Plan, "marketing_sites").name, "Plano Sites")
                upgrade(directory=self.migrations, revision=self.revision)
                self.assertEqual(
                    {plan.id for plan in db.session.scalars(select(Plan))},
                    {"student"} | {definition.plan_id for definition in COMMERCIAL_PLANS},
                )
                self.assertEqual(current_grant(user).plan_id, "student")
                self.assertEqual(
                    (db.session.get(Plan, "marketing_sites").name,
                     db.session.get(Plan, "marketing_sites").description),
                    ("Plano OJS", "OJS SEO e OJS Analytics para revistas científicas. Em desenvolvimento."),
                )
                self.assertEqual(
                    (db.session.get(Plan, "marketing_complete").name,
                     db.session.get(Plan, "marketing_complete").description),
                    ("Plano Gestão Completa",
                     "Analytics Instagram disponível; OJS SEO e OJS Analytics previstos para fase futura."),
                )
                self.assertEqual(
                    {(row.plan_id, row.tool_id) for row in db.session.scalars(select(PlanTool))},
                    {("student", "pdf_scraper"), ("research_quali", "pdf_scraper"),
                     ("research_complete", "pdf_scraper"), ("integral", "pdf_scraper")},
                )
                with db.engine.connect() as connection:
                    self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                    self.assertEqual(
                        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                        self.revision,
                    )

                downgrade(directory=self.migrations, revision=self.commercial_revision)
                db.session.expire_all()
                self.assertEqual(db.session.get(Plan, "marketing_sites").name, "Plano Sites")
                self.assertEqual(db.session.get(Plan, "marketing_complete").name, "Plano Marketing Completo")
                self.assertEqual(current_grant(user).plan_id, "student")

                downgrade(directory=self.migrations, revision=self.previous_revision)
                self.assertEqual({plan.id for plan in db.session.scalars(select(Plan))}, {"student"})
                self.assertEqual(current_grant(user).plan_id, "student")
                self.assertEqual(
                    {(row.plan_id, row.tool_id) for row in db.session.scalars(select(PlanTool))},
                    {("student", "pdf_scraper")},
                )
                db.session.remove()
                db.engine.dispose()

    def test_downgrade_refuses_to_remove_plan_with_commercial_grant(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-commercial-plan-grant-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "commercial-plan-grant-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.commercial_revision)
                user = User(name="Comercial", email="commercial-grant@example.org", role="user", status="active")
                user.set_password("senha-de-teste-segura-123")
                db.session.add(user)
                db.session.flush()
                db.session.add(AccessGrant(
                    user_id=user.id,
                    plan_id="research_quali",
                    access_mode="admin_courtesy",
                    status="active",
                ))
                db.session.commit()
                with self.assertRaises(SystemExit) as blocked:
                    downgrade(directory=self.migrations, revision=self.previous_revision)
                self.assertEqual(blocked.exception.code, 1)
                db.session.remove()
                with db.engine.connect() as connection:
                    self.assertEqual(
                        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                        self.commercial_revision,
                    )
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                db.engine.dispose()

    def test_upgrade_from_previously_applied_commercial_revision_preserves_grants_and_links(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-commercial-labels-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "commercial-labels-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.commercial_revision)
                user = User(name="Concessão comercial", email="commercial-labels@example.org", role="user", status="active")
                user.set_password("senha-de-teste-segura-123")
                db.session.add(user)
                db.session.flush()
                db.session.add(AccessGrant(
                    user_id=user.id,
                    plan_id="marketing_instagram",
                    access_mode="paid",
                    status="active",
                ))
                db.session.commit()
                links_before = {
                    (row.plan_id, row.tool_id) for row in db.session.scalars(select(PlanTool))
                }

                upgrade(directory=self.migrations, revision=self.revision)
                self.assertEqual(current_grant(user).plan_id, "marketing_instagram")
                self.assertEqual(
                    links_before,
                    {(row.plan_id, row.tool_id) for row in db.session.scalars(select(PlanTool))},
                )
                self.assertEqual(db.session.get(Plan, "marketing_sites").name, "Plano OJS")
                self.assertEqual(db.session.get(Plan, "marketing_complete").name, "Plano Gestão Completa")
                with db.engine.connect() as connection:
                    self.assertEqual(
                        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                        self.revision,
                    )
                    self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])

                downgrade(directory=self.migrations, revision=self.commercial_revision)
                db.session.expire_all()
                self.assertEqual(current_grant(user).plan_id, "marketing_instagram")
                self.assertEqual(db.session.get(Plan, "marketing_sites").name, "Plano Sites")
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
