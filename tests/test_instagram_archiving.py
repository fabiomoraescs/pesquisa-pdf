"""Arquivamento Instagram reutiliza status/archived_at sem apagar histórico."""

import unittest
from uuid import uuid4

from sqlalchemy import func, select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AnalyticsProject, AnalyticsRun, PlanTool
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


class InstagramArchivingTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.user = create_user()
        self.other = create_user("Outra", "outra@example.org")
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        db.session.commit()
        login(self.client)
        self.project = AnalyticsProject(owner_user_id=self.user.id, name="Pesquisa Instagram")
        db.session.add(self.project)
        db.session.flush()
        self.run = AnalyticsRun(analytics_project_id=self.project.id, status="completed", record_count=1)
        db.session.add(self.run)
        db.session.commit()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def post_action(self, action, project_id=None, *, csrf=True):
        url = f"/analytics/instagram/projects/{project_id or self.project.id}/{action}"
        data = {"confirm": "yes"}
        if csrf:
            data["csrf_token"] = csrf_from(self.client.get("/analytics/instagram"))
        return self.client.post(url, data=data)

    def test_archive_restore_preserves_run_and_separates_lists(self):
        self.assertIn("Pesquisa Instagram", self.client.get("/analytics/instagram").get_data(as_text=True))
        self.assertIn("Arquivar projeto", self.client.get(
            f"/analytics/instagram/projects/{self.project.id}").get_data(as_text=True))
        self.assertEqual(self.post_action("archive").status_code, 302)
        db.session.refresh(self.project)
        self.assertEqual(self.project.status, "archived")
        self.assertIsNotNone(self.project.archived_at)
        self.assertNotIn("Pesquisa Instagram", self.client.get("/analytics/instagram").get_data(as_text=True))
        archived = self.client.get("/analytics/instagram/projects/archived").get_data(as_text=True)
        self.assertIn("Pesquisa Instagram", archived)
        self.assertIn("Restaurar projeto", archived)
        self.assertIn('<span class="platform-header-context">Projetos arquivados</span>', archived)
        self.assertIn("Histórico de importações", self.client.get(
            f"/analytics/instagram/projects/{self.project.id}").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new").status_code, 404)
        self.assertEqual(self.post_action("restore").status_code, 302)
        db.session.refresh(self.project)
        self.assertEqual(self.project.status, "active")
        self.assertIsNone(self.project.archived_at)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 1)
        self.assertIn("Pesquisa Instagram", self.client.get("/analytics/instagram").get_data(as_text=True))
        self.assertNotIn("Pesquisa Instagram", self.client.get("/analytics/instagram/projects/archived").get_data(as_text=True))

    def test_ownership_csrf_and_post_only(self):
        self.assertEqual(self.post_action("archive", csrf=False).status_code, 400)
        self.assertEqual(self.client.get(f"/analytics/instagram/projects/{self.project.id}/archive").status_code, 405)
        self.assertEqual(self.client.get(f"/analytics/instagram/projects/{self.project.id}/restore").status_code, 405)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.other.email)
        self.assertEqual(self.post_action("archive").status_code, 404)
        self.assertEqual(self.post_action("restore").status_code, 404)
        self.assertNotIn("Pesquisa Instagram", self.client.get("/analytics/instagram/projects/archived").get_data(as_text=True))
        self.assertEqual(self.post_action("archive", uuid4()).status_code, 404)


if __name__ == "__main__":
    unittest.main()
