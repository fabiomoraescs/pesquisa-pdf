"""Proteções estruturais para os controles visuais do módulo OJS SEO."""

from pathlib import Path
import unittest

from app import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OJS_TEMPLATE_ROOT = PROJECT_ROOT / "templates" / "platform" / "ojs_seo"
PLATFORM_CSS = PROJECT_ROOT / "static" / "css" / "platform.css"


class OjsSeoButtonLayoutTests(unittest.TestCase):
    def test_ojs_templates_compile_in_the_application_environment(self):
        app = create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-button-layout-test-secret-with-adequate-length",
        })
        template_names = tuple(
            f"platform/ojs_seo/{template.name}"
            for template in sorted(OJS_TEMPLATE_ROOT.glob("*.html"))
        )

        # As páginas de arquivamento e de confirmação de exclusão permanente
        # e o parcial compartilhado de carregamento também são partes nativas
        # do módulo e precisam compilar no mesmo ambiente Jinja. Não fixe uma
        # contagem: novas telas OJS não devem tornar a proteção obsoleta.
        self.assertIn("platform/ojs_seo/_loading_overlay.html", template_names)
        self.assertIn("platform/ojs_seo/journal_delete.html", template_names)
        self.assertIn("platform/ojs_seo/html_records.html", template_names)
        for template_name in template_names:
            with self.subTest(template=template_name):
                app.jinja_env.get_template(template_name)

    def test_heading_and_form_actions_use_the_ojs_scoped_layout_helpers(self):
        expected_heading_templates = {
            "audit_detail.html",
            "article_detail.html",
            "articles.html",
            "journal_detail.html",
        }
        for template_name in expected_heading_templates:
            with self.subTest(template=template_name):
                content = (OJS_TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
                self.assertIn("ojs-seo-heading-actions", content)

        for template_name in ("journal_detail.html", "journal_new.html"):
            with self.subTest(template=template_name):
                content = (OJS_TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
                self.assertIn("ojs-seo-action-group", content)

    def test_journal_list_reuses_the_standard_icon_open_action(self):
        content = (OJS_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("platform-action-button", content)
        self.assertIn("icon('abrir')", content)
        self.assertIn('title="Abrir visão geral"', content)
        self.assertIn('aria-label="Abrir visão geral de {{ item.journal.analytics_project.name }}"', content)
        self.assertIn('title="Arquivar periódico"', content)

    def test_history_uses_one_global_info_button_and_a_split_action_menu(self):
        content = (OJS_TEMPLATE_ROOT / "journal_history.html").read_text(encoding="utf-8")

        self.assertEqual(1, content.count("{{ tutorial_button("))
        self.assertIn("ojs-history-split-action", content)
        self.assertIn('popovertarget="ojs-history-advanced-actions"', content)
        self.assertIn('data-temporary-popover', content)
        self.assertIn('data-popover-align-end', content)
        self.assertIn('role="menu"', content)
        self.assertIn("Dados bibliográficos", content)
        self.assertIn("Analisar todas as páginas", content)
        self.assertIn("Campanhas de análise das páginas", content)
        self.assertIn("Análises e resultados", content)
        self.assertIn("Nenhuma campanha de análise das páginas foi iniciada.", content)
        self.assertNotIn("Análises completas das páginas", content)
        self.assertNotIn("Realizar análise", content)
        self.assertNotIn("Análises anteriores", content)
        self.assertNotIn("<details", content)

    def test_history_coverage_legend_uses_centered_colored_dots(self):
        template = (OJS_TEMPLATE_ROOT / "journal_history.html").read_text(encoding="utf-8")
        css = PLATFORM_CSS.read_text(encoding="utf-8")

        self.assertEqual(4, template.count('<span class="ojs-html-coverage-legend__dot '))
        self.assertIn(".ojs-html-coverage-legend { justify-content: center;", css)
        self.assertIn("--ojs-history-split-divider: var(--paper);", css)
        self.assertIn(".ojs-history-split-action__toggle::before", css)
        self.assertIn("width: 2px; background: var(--ojs-history-split-divider);", css)
        self.assertIn("width: max-content; max-width: calc(100vw - 1rem);", css)
        self.assertIn('class="ojs-history-action-menu__item"', template)
        self.assertNotIn('class="btn btn-outline-primary btn-sm" type="submit" role="menuitem"', template)
        self.assertIn(".ojs-history-action-menu form + form { border-top: 1px solid var(--border); }", css)
        self.assertIn(".ojs-history-action-menu__item { appearance: none; cursor: pointer; white-space: nowrap; }", css)
        self.assertIn("font-size: .82rem; line-height: 1.25;", css)
        for state, color in (
            ("verified", "var(--bs-success)"),
            ("reused", "var(--bs-info)"),
            ("unverified", "var(--bs-warning)"),
            ("unprocessed", "var(--bs-secondary)"),
        ):
            with self.subTest(state=state):
                self.assertIn(
                    f".ojs-html-coverage-legend__dot--{state} {{ background: {color}; }}",
                    css,
                )

    def test_scoped_css_prevents_cross_axis_stretch_without_overriding_button_style(self):
        css = PLATFORM_CSS.read_text(encoding="utf-8")

        self.assertIn(".ojs-seo-heading-actions { align-items: flex-start; }", css)
        self.assertIn(".ojs-seo-action-group { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; }", css)
        self.assertIn(".ojs-seo-action-group .btn { flex: 0 0 auto; white-space: nowrap; }", css)
        self.assertNotIn(".btn { height:", css)


if __name__ == "__main__":
    unittest.main()
