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
            ("/", "Dashboard"),
            ("/perfil", "Perfil"),
            ("/projetos", "Projetos"),
            ("/projetos/arquivados", "Projetos arquivados"),
            ("/raspagem-livre", "Raspagem livre"),
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
                self.assertIn('<strong>Análysis</strong>', sidebar.group(1))
                self.assertIn('<small>ferramentas para pesquisa</small>', sidebar.group(1))
                self.assertEqual(header.group(1).strip(), heading)

    def test_sidebar_identity_is_above_navigation_and_footer_contains_only_logout(self):
        html = self.client.get("/projetos").get_data(as_text=True)
        sidebar = html.split('<aside class="platform-sidebar"', 1)[1].split("</aside>", 1)[0]
        identity = '<strong>Análysis</strong><small>ferramentas para pesquisa</small>'
        self.assertEqual(sidebar.count(identity), 1)
        self.assertLess(sidebar.index(identity), sidebar.index('<nav class="platform-nav"'))
        footer = sidebar.split('<div class="platform-sidebar-footer">', 1)[1]
        self.assertNotIn("Análysis", footer)
        self.assertNotIn("ferramentas para pesquisa", footer)
        self.assertEqual(re.findall(r"<button[^>]*>(.*?)</button>", footer), ["Sair"])

    def test_navigation_order_and_admin_submenu_are_unchanged(self):
        html = self.client.get("/admin/usuarios").get_data(as_text=True)
        nav = html.split('<nav class="platform-nav"', 1)[1].split("</nav>", 1)[0]
        positions = [nav.index(marker) for marker in (
            'href="/"', '<span>Raspagem de dados</span>',
            '<span>Análise qualitativa</span>', '<span>Análise quantitativa</span>',
            'id="platform-admin-toggle"',
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('href="/projetos/livres"', nav)
        self.assertIn('href="/projetos"', nav)
        self.assertNotIn('href="/perfil"', nav)
        self.assertEqual(nav.count('>Novo projeto</a>'), 2)
        self.assertEqual(nav.count('>Projetos</a>'), 2)
        self.assertNotIn('>Bases de análise</a>', nav)
        self.assertNotIn('Projetos arquivados', nav)
        self.assertNotIn('Em breve', nav)
        self.assertIn('class="platform-nav-subdropdown"', nav)
        self.assertIn('aria-disabled="true"', nav)
        self.assertIn('aria-expanded="true"', nav)
        self.assertIn('aria-controls="platform-admin-submenu"', nav)
        self.assertIn('id="platform-admin-submenu"', nav)
        self.assertIn('id="platform-menu-toggle"', html)
        self.assertIn('aria-label="Nome da plataforma"><span class="platform-brand-mark">', html)
        self.assertIn('<strong>Análysis</strong><small>ferramentas para pesquisa</small>', html)


if __name__ == "__main__":
    unittest.main()
