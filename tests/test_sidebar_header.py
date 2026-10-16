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

    def test_main_sidebar_icons_are_distinct_including_analytics(self):
        html = self.client.get("/").get_data(as_text=True)
        nav = html.split('<nav class="platform-nav"', 1)[1].split("</nav>", 1)[0]
        icons = re.findall(r'<svg class="platform-icon"[^>]*>(.*?)</svg>', nav, re.S)
        self.assertGreaterEqual(len(icons), 4)
        self.assertEqual(len(icons), len(set(icons)))
        analytics = re.search(r'<summary[^>]*>(<svg.*?</svg>)<span>Analytics</span></summary>', nav, re.S)
        self.assertIsNotNone(analytics)
        self.assertIn('M5 16l5-5 4 3 5-8', analytics.group(1))

    def test_sidebar_brand_is_fixed_and_main_header_tracks_section(self):
        pages = (
            ("/", "Dashboard"),
            ("/perfil", "Perfil"),
            ("/projetos", "Projetos de Raspagem estruturada"),
            ("/projetos/arquivados", "Projetos arquivados de Raspagem estruturada"),
            ("/raspagem-livre", "Raspagem por termos"),
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

    def test_navigation_order_and_admin_submenu_are_preserved(self):
        html = self.client.get("/admin/usuarios").get_data(as_text=True)
        nav = html.split('<nav class="platform-nav"', 1)[1].split("</nav>", 1)[0]
        positions = [nav.index(marker) for marker in (
            'href="/"', '<span>Pesquisa Quali</span>',
            '>Raspagem por termos<', '>Raspagem estruturada<', '>Análise Documental<',
            '<span>Analytics</span>', '>Instagram<',
            'id="platform-admin-toggle"',
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('href="/projetos/livres"', nav)
        self.assertIn('href="/projetos"', nav)
        self.assertNotIn('href="/perfil"', nav)
        self.assertEqual(nav.count('>Novo projeto</a>'), 0)
        self.assertEqual(nav.count('>Projetos</a>'), 0)
        self.assertIn('href="/projetos/livres"', nav)
        self.assertIn('href="/projetos"', nav)
        self.assertIn('Raspagem por termos', nav)
        self.assertIn('Raspagem estruturada', nav)
        self.assertIn('Análise Documental', nav)
        self.assertIn('Instagram', nav)
        self.assertNotIn('Análise de Comentários', nav)
        self.assertNotIn('Pesquisa Quanti', nav)
        self.assertNotIn('Editorial', nav)
        self.assertNotIn('ChatDoc', nav)
        self.assertNotIn('Diagramador', nav)
        self.assertNotIn('SEO', nav)
        self.assertNotIn('>Bases de análise</a>', nav)
        self.assertNotIn('Projetos arquivados', nav)
        self.assertNotIn('href="/chatdoc"', nav)
        self.assertNotIn('href="/editorial/diagramador/"', nav)
        self.assertNotIn('href="/analytics/seo"', nav)
        for legacy_name in (
            "Busca por termos",
            "Busca estruturada",
            "Análise quali-dados",
            "Análysis Instagram",
            "Análysis SEO",
        ):
            self.assertNotIn(legacy_name, nav)
        self.assertNotIn('class="platform-nav-subdropdown"', nav)
        self.assertNotIn('aria-disabled="true"', nav)
        self.assertIn('aria-expanded="true"', nav)
        self.assertEqual(nav.count('class="platform-admin-submenu platform-nav-dropdown-menu"'), 2)
        self.assertIn('aria-controls="platform-admin-submenu"', nav)
        self.assertIn('id="platform-admin-submenu"', nav)
        self.assertIn('id="platform-menu-toggle"', html)
        self.assertIn('aria-label="Nome da plataforma"><span class="platform-brand-mark">', html)
        self.assertIn('<strong>Análysis</strong><small>ferramentas para pesquisa</small>', html)

    def test_tool_links_are_direct_and_manage_icon_is_distinct_from_theme(self):
        html = self.client.get("/projetos/livres").get_data(as_text=True)
        nav = html.split('<nav class="platform-nav"', 1)[1].split("</nav>", 1)[0]
        self.assertIn('href="/projetos/livres"', nav)
        self.assertIn('href="/projetos"', nav)
        self.assertIn('href="/projetos/qualitativos"', nav)
        self.assertNotIn('platform-nav-subdropdown', nav)
        self.assertNotIn('Raspagem livre', nav)
        self.assertNotIn('Raspagem sistemática', nav)
        manage = re.search(r'<button class="platform-admin-toggle".*?</button>', nav, re.S)
        theme = re.search(r'<button class="btn btn-outline-primary theme-icon-button".*?</button>', html, re.S)
        self.assertIsNotNone(manage)
        self.assertIsNotNone(theme)
        self.assertIn('<circle cx="12" cy="12" r="3"/>', manage.group(0))
        self.assertNotEqual(manage.group(0), theme.group(0))
        for path, title in (("/projetos/livres", "Projetos de Raspagem por termos"),
                            ("/projetos", "Projetos de Raspagem estruturada"),
                            ("/projetos/qualitativos", "Projetos de Análise Documental")):
            with self.subTest(path=path):
                self.assertIn(title, self.client.get(path).get_data(as_text=True))

    def test_active_tool_keeps_its_area_visibly_current(self):
        cases = (
            ("/raspagem-livre", "Pesquisa Quali", "Raspagem por termos"),
            ("/projetos", "Pesquisa Quali", "Raspagem estruturada"),
            ("/projetos/qualitativos", "Pesquisa Quali", "Análise Documental"),
            ("/analytics/instagram", "Analytics", "Instagram"),
        )
        for path, area, tool in cases:
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                nav = html.split('<nav class="platform-nav"', 1)[1].split("</nav>", 1)[0]
                group = re.search(
                    rf'<details class="platform-nav-dropdown" open>\s*'
                    rf'<summary class="is-current">.*?<span>{re.escape(area)}</span></summary>(.*?)</details>',
                    nav,
                    re.S,
                )
                self.assertIsNotNone(group)
                self.assertRegex(
                    group.group(1),
                    rf'<a class="platform-nav-tool-link is-current"[^>]*aria-current="location"[^>]*>{re.escape(tool)}</a>',
                )


if __name__ == "__main__":
    unittest.main()
