"""Sidebar dinâmica e bloqueio central de ferramentas ocultas."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AnalyticsProject, AnalyticsRun, Tool
from platform_core.services import can_access_tool, set_visibility_preference


def _sidebar(html: str) -> str:
    return html.split('<nav class="platform-nav"', 1)[1].split("</nav>", 1)[0]


class ToolVisibilitySidebarTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.admin = create_user("Admin", "sidebar-admin@example.org", "admin")
        self.user = create_user("Pessoa", "sidebar-user@example.org", "user", "integral")
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def _set_visibility(self, scope_type: str, scope_id: str, visible: bool) -> None:
        set_visibility_preference(
            self.admin,
            scope_type=scope_type,
            scope_id=scope_id,
            visible=visible,
        )
        db.session.commit()

    def test_sidebar_filters_groups_and_tools_without_empty_containers_and_restores_catalog_order(self):
        login(self.client, self.user.email)
        initial = _sidebar(self.client.get("/").get_data(as_text=True))
        self.assertLess(initial.index("Pesquisa Quali"), initial.index("Pesquisa Quanti"))
        self.assertLess(initial.index("Pesquisa Quanti"), initial.index("Analytics"))
        self.assertIn("Raspagem estruturada", initial)
        self.assertIn("OJS SEO", initial)
        self.assertIn("OJS Analytics", initial)
        self.assertIn("Busca Bibliográfica", initial)
        self.assertNotIn('href="/ojs-seo"', initial)
        self.assertNotIn('href="/ojs-analytics"', initial)
        self.assertNotIn("Editorial", initial)

        self._set_visibility("tool", "document_analysis", False)
        partial = _sidebar(self.client.get("/").get_data(as_text=True))
        self.assertNotIn("Raspagem estruturada", partial)
        self.assertIn("Raspagem por termos", partial)
        self.assertIn("Análise Documental", partial)
        self.assertNotIn("data-tool-visibility", partial)

        self._set_visibility("group", "research_quali", False)
        hidden_group = _sidebar(self.client.get("/").get_data(as_text=True))
        self.assertNotIn("Pesquisa Quali", hidden_group)
        self.assertNotIn("Raspagem por termos", hidden_group)
        self.assertIn("Analytics", hidden_group)

        self._set_visibility("group", "research_quali", True)
        restored = _sidebar(self.client.get("/").get_data(as_text=True))
        self.assertLess(restored.index("Pesquisa Quali"), restored.index("Pesquisa Quanti"))
        self.assertLess(restored.index("Pesquisa Quanti"), restored.index("Analytics"))
        self.assertLess(restored.index("Raspagem por termos"), restored.index("Análise Documental"))
        self.assertNotIn("Raspagem estruturada", restored)
        for dropdown in re.findall(r'<details class="platform-nav-dropdown".*?</details>', restored, re.S):
            self.assertRegex(dropdown, r'platform-nav-tool-(?:link|unavailable)')

    def test_hidden_tool_returns_not_found_for_common_user_but_preserves_admin_maintenance_access(self):
        self._set_visibility("tool", "pdf_scraper", False)
        login(self.client, self.user.email)
        self.assertEqual(self.client.get("/raspagem-livre").status_code, 404)
        self.assertEqual(self.client.post("/raspagem-livre", data={
            "csrf_token": csrf_from(self.client.get("/")),
        }).status_code, 404)
        self.assertEqual(self.client.get("/analises").status_code, 404)

        self.assertTrue(can_access_tool(self.admin, "pdf_scraper"))
        db.session.get(Tool, "pdf_scraper").active = False
        db.session.commit()
        self.assertFalse(can_access_tool(self.admin, "pdf_scraper"))

    def test_hidden_group_blocks_structured_routes_and_private_library_operations(self):
        self._set_visibility("group", "research_quali", False)
        login(self.client, self.user.email)
        self.assertEqual(self.client.get("/projetos").status_code, 404)
        self.assertEqual(self.client.get("/analise-documental").status_code, 404)
        self.assertEqual(self.client.get("/busca-estruturada/bibliotecas").status_code, 404)

    def test_future_and_standalone_items_follow_visibility_without_receiving_operational_links(self):
        login(self.client, self.user.email)
        initial = _sidebar(self.client.get("/").get_data(as_text=True))
        self.assertIn("Pesquisa Quanti", initial)
        self.assertIn("OJS SEO", initial)
        self.assertIn("OJS Analytics", initial)
        self.assertIn("Busca Bibliográfica", initial)
        self.assertNotIn('href="/busca-bibliografica"', initial)

        self._set_visibility("tool", "ojs_seo", False)
        self._set_visibility("tool", "bibliographic_search", False)
        hidden = _sidebar(self.client.get("/").get_data(as_text=True))
        self.assertNotIn("OJS SEO", hidden)
        self.assertNotIn("Busca Bibliográfica", hidden)
        self.assertIn("OJS Analytics", hidden)

        self._set_visibility("tool", "ojs_seo", True)
        self._set_visibility("tool", "bibliographic_search", True)
        restored = _sidebar(self.client.get("/").get_data(as_text=True))
        self.assertLess(restored.index("Instagram"), restored.index("OJS SEO"))
        self.assertLess(restored.index("OJS SEO"), restored.index("OJS Analytics"))
        self.assertLess(restored.index("OJS Analytics"), restored.index("Busca Bibliográfica"))

    def test_hidden_instagram_blocks_new_operations_without_altering_an_existing_run(self):
        project = AnalyticsProject(owner_user_id=self.user.id, module_key="instagram", name="Instagram")
        db.session.add(project)
        db.session.flush()
        run = AnalyticsRun(
            analytics_project_id=project.id,
            source_kind="meta_api",
            status="running",
            processor_version="instagram-meta-sync-v1",
            parameters_json={"include_comments": True},
        )
        db.session.add(run)
        db.session.commit()
        run_id = run.id

        self._set_visibility("tool", "analytics_instagram", False)
        login(self.client, self.user.email)
        self.assertEqual(self.client.get("/analytics/instagram").status_code, 404)
        self.assertEqual(self.client.get(f"/analytics/instagram/projects/{project.id}/dashboard").status_code, 404)
        self.assertEqual(self.client.post(f"/analytics/instagram/projects/{project.id}/imports/meta/sync", data={
            "csrf_token": csrf_from(self.client.get("/")),
        }).status_code, 404)
        preserved = db.session.get(AnalyticsRun, run_id)
        self.assertIsNotNone(preserved)
        self.assertEqual(preserved.status, "running")
        self.assertTrue(preserved.parameters_json["include_comments"])

    def test_only_marked_child_switches_use_the_green_visibility_style(self):
        login(self.client, self.admin.email)
        html = self.client.get("/admin/ferramentas").get_data(as_text=True)
        visibility = html.split('id="tool-visibility-title"', 1)[1].split(
            'js/admin_tool_visibility.js', 1,
        )[0]
        self.assertIn('data-visibility-tool="pdf_scraper"', visibility)
        self.assertIn('class="form-check-input tool-visibility-child-switch"', visibility)
        self.assertNotIn('text-success', visibility)
        self.assertIn(
            'class="platform-admin-submenu mb-3" data-tool-visibility-children="research_quali">',
            visibility,
        )
        self.assertRegex(
            html,
            r'<link\s+href="[^"]*/static/css/style\.css(?:\?[^"]*)?"\s+rel="stylesheet">',
        )
        group = re.search(r'<article[^>]*data-visibility-group="research_quali"[^>]*>(.*?)</article>', visibility, re.S)
        self.assertIsNotNone(group)
        self.assertNotIn('tool-visibility-child-switch', group.group(1))

        stylesheet = Path(__file__).resolve().parents[1] / "static" / "css" / "style.css"
        css = stylesheet.read_text(encoding="utf-8")
        self.assertIn('.form-check-input:checked { background-color: var(--blue);', css)
        self.assertIn('.form-check-input.tool-visibility-child-switch:checked {', css)
        self.assertIn('background-color: var(--tool-visibility-switch-on);', css)
        dark_default = 'html[data-theme="dark"] .form-check-input:checked'
        dark_child = 'html[data-theme="dark"] .form-check-input.tool-visibility-child-switch:checked'
        self.assertGreater(css.index(dark_child), css.index(dark_default))

        sidebar_css = (Path(__file__).resolve().parents[1] / "static" / "css" / "platform.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".platform-admin-submenu {", sidebar_css)
        self.assertIn("border-left: 1px solid var(--border);", sidebar_css)
        self.assertIn(".platform-admin-submenu[hidden] { display: none; }", sidebar_css)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
