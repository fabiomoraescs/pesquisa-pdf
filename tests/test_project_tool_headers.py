"""Contrato dos cabeçalhos compartilhados de ferramentas vinculadas a projetos."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

from flask import render_template_string

from platform_helpers import isolated_platform


ROOT = Path(__file__).resolve().parents[1]
PROJECT_BOUND_TEMPLATES = (
    "templates/index.html",
    "templates/historico_racial/index.html",
    "templates/historico_racial/vocabulario.html",
    "templates/historico_racial/resultado.html",
    "templates/platform/analysis_history.html",
    "templates/platform/analysis_dashboard.html",
    "templates/resultado.html",
    "templates/platform/_qualitative_page_intro.html",
    "templates/platform/qualitative_pending.html",
    "templates/platform/qualitative_bases.html",
    "templates/platform/qualitative_new_base.html",
    "templates/platform/qualitative_legacy_choice.html",
    "templates/platform/qualitative_coding_report.html",
    "templates/platform/instagram/project_detail.html",
    "templates/platform/instagram/dashboard.html",
    "templates/platform/instagram/import_new.html",
    "templates/platform/instagram/comments.html",
    "templates/platform/instagram/classifications.html",
    "templates/platform/instagram/taxonomies.html",
    "templates/platform/instagram/insights.html",
    "templates/platform/instagram/reports.html",
)


class ProjectToolHeaderTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def render_label(self, tool_name, project):
        with self.app.test_request_context("/"):
            return render_template_string(
                "{% from 'platform/_project_tool_heading.html' import project_tool_label %}"
                "{{ project_tool_label(tool_name, project) }}",
                tool_name=tool_name,
                project=project,
            )

    def test_header_uses_the_persisted_project_name_and_falls_back_to_tool_only(self):
        project = SimpleNamespace(name="Revista Latitude — Ciência & Educação")
        self.assertIn("Instagram: Revista Latitude — Ciência &amp; Educação", self.render_label("Instagram", project))
        self.assertEqual("Instagram", self.render_label("Instagram", None).strip())
        self.assertNotIn("None", self.render_label("Instagram", None))

    def test_header_follows_rename_without_cached_or_placeholder_values(self):
        project = SimpleNamespace(name="Nome curto")
        self.assertIn("Análise Documental: Nome curto", self.render_label("Análise Documental", project))
        project.name = "Nome longo com acentos — edição 2026"
        self.assertIn("Análise Documental: Nome longo com acentos — edição 2026", self.render_label("Análise Documental", project))

    def test_project_bound_workspaces_keep_the_tool_name_out_of_internal_titles(self):
        for relative_path in PROJECT_BOUND_TEMPLATES:
            with self.subTest(template=relative_path):
                self.assertNotIn("project_tool_heading", (ROOT / relative_path).read_text(encoding="utf-8"))

    def test_platform_header_owns_the_project_tool_composition(self):
        header = (ROOT / "templates/platform/_header.html").read_text(encoding="utf-8")
        self.assertIn("project_tool_label(project_tool_name, header_project)", header)
        for tool_name in ("Instagram", "Raspagem por termos", "Raspagem estruturada", "Análise Documental"):
            with self.subTest(tool=tool_name):
                self.assertIn(tool_name, header)

    def test_authenticated_header_is_sticky_without_removing_its_space_from_the_layout(self):
        css = (ROOT / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn(
            ".platform-header-authenticated { position: sticky; top: 0; z-index: 20;",
            css,
        )
        self.assertNotIn(".platform-header-authenticated { position: fixed;", css)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
