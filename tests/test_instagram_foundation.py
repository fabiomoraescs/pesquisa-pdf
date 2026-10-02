"""Fundação da ferramenta Análysis Instagram, sem domínio analítico persistido."""

import unittest

from flask import template_rendered
from sqlalchemy import func

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import PlanTool, Tool, UserToolOverride
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL, seed_platform


class InstagramFoundationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.user = create_user()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    @staticmethod
    def _grant_plan_access():
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        db.session.commit()

    def test_seed_adds_the_tool_to_an_existing_catalog_once_without_granting_all_plans(self):
        tool = db.session.get(Tool, ANALYTICS_INSTAGRAM_TOOL)
        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, "Análysis Instagram")
        self.assertEqual(tool.route, "/analytics/instagram")

        # Simula uma instalação prévia: planos e outras ferramentas já existem,
        # mas a nova Tool ainda não foi provisionada.
        db.session.delete(tool)
        db.session.commit()
        self.assertIsNone(db.session.get(Tool, ANALYTICS_INSTAGRAM_TOOL))

        seed_platform()
        seed_platform()
        self.assertEqual(
            db.session.scalar(
                db.select(func.count()).select_from(Tool).where(Tool.id == ANALYTICS_INSTAGRAM_TOOL)
            ),
            1,
        )
        self.assertIsNone(db.session.get(PlanTool, ("student", ANALYTICS_INSTAGRAM_TOOL)))

    def test_authorized_user_gets_the_initial_page_and_expected_template(self):
        self._grant_plan_access()
        templates = []

        def record_template(_sender, template, context, **_extra):
            templates.append(template.name)

        template_rendered.connect(record_template, self.app)
        try:
            response = self.client.get("/analytics/instagram")
        finally:
            template_rendered.disconnect(record_template, self.app)

        self.assertEqual(response.status_code, 200)
        self.assertIn("platform/instagram/index.html", templates)
        html = response.get_data(as_text=True)
        self.assertIn("Análysis Instagram", html)
        self.assertNotIn("Acompanhe desempenho, recepção e evolução", html)
        self.assertNotIn("<p class=\"platform-muted mb-1\">ANALYTICS</p>", html)
        self.assertIn("Você ainda não possui projetos no Análysis Instagram.", html)
        self.assertIn(">Novo projeto</a>", html)
        self.assertIn('data-view-target="instagram-projects-list"', html)
        self.assertIn('href="/analytics/instagram"', html)

    def test_authenticated_user_without_access_gets_403_and_does_not_see_navigation(self):
        denied = self.client.get("/analytics/instagram")
        self.assertEqual(denied.status_code, 403)

        navigation = self.client.get("/").get_data(as_text=True).split('<nav class="platform-nav"', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn("Analytics", navigation)
        self.assertNotIn("Análysis Instagram", navigation)
        self.assertIn("Busca por termos", navigation)
        self.assertIn("Busca estruturada", navigation)
        self.assertIn("Análise quali-dados", navigation)

    def test_plan_access_shows_the_analytics_navigation_without_hiding_existing_tools(self):
        self._grant_plan_access()
        navigation = self.client.get("/").get_data(as_text=True).split('<nav class="platform-nav"', 1)[1].split("</nav>", 1)[0]
        self.assertIn("<span>Analytics</span>", navigation)
        self.assertIn("Análysis Instagram", navigation)
        self.assertIn('href="/analytics/instagram"', navigation)
        self.assertIn("Busca por termos", navigation)
        self.assertIn("Busca estruturada", navigation)
        self.assertIn("Análise quali-dados", navigation)

    def test_admin_bypass_and_user_overrides_keep_the_existing_authorization_rules(self):
        admin = create_user("Admin", "admin@example.org", role="admin", plan="institutional")
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, admin.email)
        self.assertEqual(self.client.get("/analytics/instagram").status_code, 200)

        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.user.email)

        db.session.add(UserToolOverride(
            user_id=self.user.id, tool_id=ANALYTICS_INSTAGRAM_TOOL, decision="allow"
        ))
        db.session.commit()
        self.assertEqual(self.client.get("/analytics/instagram").status_code, 200)

        self._grant_plan_access()
        override = db.session.get(UserToolOverride, (self.user.id, ANALYTICS_INSTAGRAM_TOOL))
        override.decision = "deny"
        db.session.commit()
        self.assertEqual(self.client.get("/analytics/instagram").status_code, 403)

    def test_unauthenticated_request_keeps_the_platform_login_redirect(self):
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        response = self.client.get("/analytics/instagram")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login?next=", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
