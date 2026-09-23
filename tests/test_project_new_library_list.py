"""A seleção compacta de bibliotecas preserva o formulário de projetos."""

import unittest

from sqlalchemy import select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import Project, ProjectLibrary, VocabularyLibrary
from platform_core.vocabularies import project_store


class ProjectNewLibraryListTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        admin = create_user("Admin", "admin-project-list@example.org", role="admin")
        login(self.client, admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_initial_selection_is_compact_and_official_library_remains_checked(self):
        response = self.client.get("/projetos/novo")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('class="platform-view-list"', html)
        self.assertIn('class="platform-list-item platform-library-choice d-flex align-items-center gap-3"', html)
        self.assertIn('type="checkbox" name="libraries" value="relacoes_raciais" checked', html)
        self.assertIn("Relações raciais", html)
        self.assertIn("8 grupos · 75 entidades · 117 variantes · v1", html)
        self.assertNotIn("platform-library-grid", html)
        self.assertNotIn("platform-library-option", html)
        self.assertNotIn("Visualização em cartões", html)
        self.assertNotIn(db.session.get(VocabularyLibrary, "relacoes_raciais").description, html)

    def test_required_selection_and_project_creation_are_unchanged(self):
        response = self.client.post("/projetos/novo", data={
            "csrf_token": csrf_from(self.client.get("/projetos/novo")),
            "name": "Projeto sem biblioteca", "description": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Selecione ao menos uma biblioteca disponível.", response.get_data(as_text=True))
        self.assertIsNone(db.session.scalar(select(Project).where(Project.name == "Projeto sem biblioteca")))

        response = self.client.post("/projetos/novo", data={
            "csrf_token": csrf_from(self.client.get("/projetos/novo")),
            "name": "Projeto com biblioteca", "description": "",
            "libraries": ["relacoes_raciais"],
        })
        self.assertEqual(response.status_code, 302)
        project = db.session.scalar(select(Project).where(Project.name == "Projeto com biblioteca"))
        self.assertIsNotNone(project)
        self.assertIsNotNone(db.session.get(ProjectLibrary, (project.id, "relacoes_raciais")))
        self.assertEqual(project_store(project.id).capturar_ativa()["version"], "v1.0")

    def test_admin_library_listing_is_unchanged(self):
        html = self.client.get("/admin/bibliotecas").get_data(as_text=True)
        self.assertIn('id="admin-libraries-list" data-view-container', html)
        self.assertIn('data-view-mode="cardbox"', html)
        self.assertIn("8 grupos · 75 entidades · 117 variantes · v1", html)


if __name__ == "__main__":
    unittest.main()
