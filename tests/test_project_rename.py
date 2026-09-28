"""Renomeação pontual de projetos nas listas, sem alterar seus vínculos."""

import unittest

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import Analysis, AnalysisDocument, Project, UserToolOverride
from platform_core.scraping_types import QUALITATIVE_TOOL


class ProjectRenameTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.owner = create_user()
        db.session.add(UserToolOverride(user_id=self.owner.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def project(self, kind="free", name="Projeto original", status="active", owner=None):
        project = Project(owner_user_id=(owner or self.owner).id, name=name,
                          description="Descrição preservada", scrape_type=kind, status=status)
        db.session.add(project)
        db.session.commit()
        return project

    def test_icon_and_form_appear_in_all_project_lists(self):
        for kind, path in (("free", "/projetos/livres"),
                           ("systematic", "/projetos"),
                           ("qualitative", "/projetos/qualitativos")):
            with self.subTest(kind=kind):
                project = self.project(kind=kind, name=f"Projeto {kind}")
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('title="Renomear projeto" aria-label="Renomear projeto"', html)
                self.assertIn(f'action="/projetos/{project.id}/renomear"', html)
                self.assertIn('name="name" value="Projeto ', html)
                self.assertIn('maxlength="200" required', html)
                self.assertIn('name="csrf_token"', html)

        archived = self.project(status="archived", name="Projeto arquivado")
        html = self.client.get("/projetos/livres/arquivados").get_data(as_text=True)
        self.assertIn(f'action="/projetos/{archived.id}/renomear"', html)

    def test_valid_rename_preserves_description_modality_base_and_document(self):
        project = self.project()
        analysis = Analysis(user_id=self.owner.id, project_id=project.id, name="Base original",
                            source_type="project", tool_id="pdf_scraper", tool_version="v1",
                            status="concluida")
        db.session.add(analysis)
        db.session.flush()
        document = AnalysisDocument(analysis_id=analysis.id, original_name="Fonte.pdf",
                                    stored_name="fonte.pdf")
        db.session.add(document)
        db.session.commit()
        initial = (project.description, project.scrape_type, project.status,
                   project.created_at, analysis.id, document.id)

        token = csrf_from(self.client.get("/projetos/livres"))
        response = self.client.post(f"/projetos/{project.id}/renomear",
                                    data={"csrf_token": token, "name": "  Novo nome  "})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/projetos/livres"))
        db.session.refresh(project)
        db.session.refresh(analysis)
        db.session.refresh(document)
        self.assertEqual(project.name, "Novo nome")
        self.assertEqual((project.description, project.scrape_type, project.status,
                          project.created_at, analysis.id, document.id), initial)
        self.assertEqual(analysis.project_id, project.id)
        self.assertEqual(document.analysis_id, analysis.id)

    def test_empty_oversize_and_missing_csrf_are_rejected(self):
        project = self.project()
        url = f"/projetos/{project.id}/renomear"
        token = csrf_from(self.client.get("/projetos/livres"))
        for name in ("", "   ", "x" * 201):
            with self.subTest(name=name[:20]):
                self.assertEqual(self.client.post(url, data={"csrf_token": token,
                                                             "name": name}).status_code, 400)
                db.session.refresh(project)
                self.assertEqual(project.name, "Projeto original")
        self.assertEqual(self.client.post(url, data={"name": "Sem CSRF"}).status_code, 400)
        db.session.refresh(project)
        self.assertEqual(project.name, "Projeto original")

    def test_other_users_project_cannot_be_renamed(self):
        other = create_user("Outro", "outro-rename@example.org")
        project = self.project(owner=other)
        token = csrf_from(self.client.get("/projetos/livres"))
        response = self.client.post(f"/projetos/{project.id}/renomear",
                                    data={"csrf_token": token, "name": "Nome indevido"})
        self.assertEqual(response.status_code, 404)
        db.session.refresh(project)
        self.assertEqual(project.name, "Projeto original")

    def test_archived_project_rename_keeps_it_archived(self):
        project = self.project(status="archived")
        token = csrf_from(self.client.get("/projetos/livres/arquivados"))
        response = self.client.post(f"/projetos/{project.id}/renomear",
                                    data={"csrf_token": token, "name": "Nome do arquivo"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/projetos/livres/arquivados"))
        db.session.refresh(project)
        self.assertEqual((project.name, project.status), ("Nome do arquivo", "archived"))


if __name__ == "__main__":
    unittest.main()
