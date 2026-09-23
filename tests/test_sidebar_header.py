"""Identidade fixa na sidebar e contexto dinâmico no cabeçalho."""

import re
import unittest

from platform_helpers import create_user, isolated_platform, login


class SidebarHeaderTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        admin = create_user("Admin", "admin@example.org", role="admin", plan="researcher")
        login(self.client, admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_sidebar_brand_is_fixed_and_main_header_tracks_section(self):
        pages = (
            ("/perfil", "Perfil"),
            ("/projetos", "Projetos"),
            ("/projetos/arquivados", "Projetos arquivados"),
            ("/", "Raspagem padrão"),
            ("/admin", "Visão geral"),
            ("/admin/usuarios", "Usuários"),
            ("/admin/planos", "Planos"),
            ("/admin/bibliotecas", "Bibliotecas"),
        )
        for path, heading in pages:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                sidebar = re.search(r'<div class="platform-section-title"[^>]*>(.*?)</div>', html, re.S)
                header = re.search(r'<span class="platform-header-context">(.*?)</span>', html, re.S)
                self.assertIsNotNone(sidebar)
                self.assertIsNotNone(header)
                self.assertEqual(sidebar.group(1).strip(), "Raspagem de Dados")
                self.assertEqual(header.group(1).strip(), heading)

    def test_navigation_order_and_admin_submenu_are_unchanged(self):
        html = self.client.get("/admin/usuarios").get_data(as_text=True)
        nav = html.split('<nav class="platform-nav"', 1)[1].split("</nav>", 1)[0]
        positions = [nav.index(marker) for marker in (
            'href="/perfil"', 'href="/projetos"', 'href="/projetos/arquivados"',
            'href="/"', 'id="platform-admin-toggle"',
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('aria-expanded="true"', nav)
        self.assertIn('aria-controls="platform-admin-submenu"', nav)
        self.assertIn('id="platform-admin-submenu"', nav)
        self.assertIn('id="platform-menu-toggle"', html)
        self.assertIn('aria-label="Nome da plataforma">Raspagem de Dados', html)


if __name__ == "__main__":
    unittest.main()
