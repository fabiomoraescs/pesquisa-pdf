"""Regressão da página inicial e dos atalhos administrativos."""

import unittest
from urllib.parse import urlparse

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import PlanTool


class HomeProfileTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_root_and_normal_login_open_dashboard(self):
        create_user()
        anonymous = self.client.get("/")
        self.assertEqual(anonymous.status_code, 302)
        self.assertEqual(urlparse(anonymous.headers["Location"]).path, "/login")
        token = csrf_from(self.client.get("/login"))
        response = self.client.post("/login", data={
            "csrf_token": token, "email": "pesquisador@example.org",
            "password": "senha-de-teste-segura-123",
        })
        self.assertEqual(response.headers["Location"], "/")
        root = self.client.get("/")
        self.assertEqual(root.status_code, 200)
        self.assertIn("Bases de análise recentes", root.get_data(as_text=True))
        self.assertNotIn("Projetos recentes", root.get_data(as_text=True))
        self.assertEqual(self.client.get("/perfil").status_code, 200)
        self.assertEqual(self.client.get("/raspagem-livre").status_code, 200)

    def test_login_next_keeps_requested_protected_destination(self):
        create_user()
        destination = self.client.get("/projetos/livres")
        self.assertEqual(destination.status_code, 302)
        login_url = destination.headers["Location"]
        self.assertEqual(urlparse(login_url).path, "/login")
        response = self.client.post(login_url, data={
            "csrf_token": csrf_from(self.client.get(login_url)),
            "email": "pesquisador@example.org",
            "password": "senha-de-teste-segura-123",
        })
        self.assertEqual(urlparse(response.headers["Location"]).path, "/projetos/livres")
        self.assertEqual(self.client.get(response.headers["Location"]).status_code, 200)

    def test_login_rejects_external_next_and_opens_dashboard(self):
        create_user()
        login_url = "/login?next=//example.invalid"
        response = self.client.post(login_url, data={
            "csrf_token": csrf_from(self.client.get(login_url)),
            "email": "pesquisador@example.org", "password": "senha-de-teste-segura-123",
        })
        self.assertEqual(response.headers["Location"], "/")

    def test_dashboard_and_profile_do_not_require_free_scraper_permission(self):
        create_user()
        db.session.delete(db.session.get(PlanTool, ("student", "pdf_scraper")))
        db.session.commit()
        login(self.client)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/perfil").status_code, 200)
        self.assertEqual(self.client.get("/raspagem-livre").status_code, 403)
        self.assertEqual(self.client.get("/projetos/livres").status_code, 403)

    def test_new_library_reuses_secondary_action_style(self):
        create_user("Admin", "admin@example.org", "admin", "institutional")
        login(self.client, "admin@example.org")
        users = self.client.get("/admin/usuarios").get_data(as_text=True)
        libraries = self.client.get("/admin/bibliotecas").get_data(as_text=True)
        self.assertIn('class="btn btn-outline-primary btn-sm" href="/admin/usuarios/excluidos">Usuários excluídos</a>', users)
        self.assertIn('class="btn btn-outline-primary btn-sm" href="/admin/bibliotecas/nova">Nova biblioteca</a>', libraries)
        self.assertIn('class="d-flex flex-wrap gap-2 align-items-center"><a class="btn btn-outline-primary btn-sm" href="/admin/bibliotecas/nova"', libraries)
        self.assertNotIn("+ Nova biblioteca", libraries)


if __name__ == "__main__":
    unittest.main()
