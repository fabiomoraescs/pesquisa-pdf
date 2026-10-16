"""Catálogo administrativo completo e navegação sem ferramentas futuras."""
import json
import re
import unittest
from pathlib import Path

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import Analysis, PlanTool, Tool
from platform_core.services import tool_catalog


class ToolCatalogTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.admin = create_user(role="admin")
        self.client = self.app.test_client()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_future_tools_remain_administrative_and_cannot_receive_access_or_activation(self):
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"))
        icon = env.get_template("platform/_icons.html").module.icon
        tools_html = self.client.get('/admin/ferramentas').text
        matrix_response = self.client.get('/admin/definir-acessos')
        matrix = matrix_response.text
        sidebar = self.client.get('/').text.split('<nav class="platform-nav"', 1)[1].split('</nav>', 1)[0]
        token = csrf_from(matrix_response)
        before = {(row.plan_id, row.tool_id) for row in db.session.scalars(db.select(PlanTool))}
        for identifier, name, symbol in (
            ("comments_analysis", "Análise de Comentários", "comentarios"),
            ("quantitative_analysis", "Pesquisa Quanti", "quantitativa"),
            ("analytics_seo", "SEO", "seo"),
        ):
            with self.subTest(tool=identifier):
                card = re.search(rf'<article[^>]*data-tool-id="{identifier}"[^>]*>(.*?)</article>', tools_html, re.S)[1]
                row = re.search(rf'<tr data-tool-id="{identifier}">(.*?)</tr>', matrix, re.S)[1]
                for fragment in (card, row):
                    self.assertIn(name, fragment)
                    self.assertIn(str(icon(symbol)), fragment)
                self.assertNotIn(name, sidebar)
                self.assertIn("Indisponível", card)
                self.assertNotIn('<form', card)
                inputs = re.findall(r'<input\b[^>]*>', row)
                self.assertEqual(len(inputs), 4)
                for control in inputs:
                    self.assertIn('disabled', control)
                    self.assertNotIn('name="access"', control)
                self.assertIsNone(db.session.get(Tool, identifier))
                self.assertEqual(self.client.post('/admin/definir-acessos', data={
                    "csrf_token": token, "access": [f"student|{identifier}"]}).status_code, 400)
                self.assertEqual(self.client.post(f'/admin/ferramentas/{identifier}/estado', data={
                    "csrf_token": token, "confirm": "yes"}).status_code, 400)
                self.assertFalse(any(identifier in rule.rule for rule in self.app.url_map.iter_rules()))
                self.assertFalse(any(identifier in table for table in db.metadata.tables))
        self.assertEqual(before, {(row.plan_id, row.tool_id) for row in db.session.scalars(db.select(PlanTool))})
        self.assertEqual(self.client.post('/admin/definir-acessos', data={"access": []}).status_code, 400)

    def test_editorial_tools_and_routes_are_absent(self):
        self.assertIsNone(db.session.get(Tool, "diagramador"))
        self.assertFalse(any("editorial" in rule.rule for rule in self.app.url_map.iter_rules()))

    def _usage(self):
        html = self.client.get('/').text
        data = json.loads(re.search(
            r'<script id="dashboard-admin-chart-data" type="application/json">(.*?)</script>', html, re.S
        )[1])
        return dict(zip(data["usage"]["labels"], data["usage"]["values"]))

    def test_catalog_remains_complete_and_feeds_historical_admin_usage_chart(self):
        self.assertEqual(self._usage(), {
            "Raspagem por termos": 0,
            "Raspagem estruturada": 0,
            "Análise Documental": 0,
            "Análise de Comentários": 0,
            "Pesquisa Quanti": 0,
            "Instagram": 0,
            "SEO": 0,
        })
        # Qualquer outra ferramenta persistida permanece no catálogo administrativo
        # e é exibida como categoria mesmo quando não possui rota implementada.
        db.session.add(Tool(id="extra_tool", name="Outra ferramenta", route="/test-only", active=False))
        db.session.commit()
        for tool, total in (("pdf_scraper", 2), ("document_analysis", 1), ("qualitative_analysis", 3), ("extra_tool", 1)):
            for status in ["concluida"] * total + ["processando", "erro"]:
                db.session.add(Analysis(user_id=self.admin.id, name="Execução de teste", source_type="upload",
                                       tool_id=tool, tool_version="v1", status=status))
        db.session.commit()
        catalog = tool_catalog()
        self.assertEqual(len(catalog), len({tool["id"] for tool in catalog}))
        self.assertEqual({tool["name"] for tool in catalog}, {
            "Raspagem por termos", "Raspagem estruturada", "Análise Documental",
            "Análise de Comentários", "Pesquisa Quanti", "Instagram", "SEO", "Outra ferramenta",
        })
        for path in ('/admin/ferramentas', '/admin/definir-acessos'):
            html = self.client.get(path).text
            for tool in catalog:
                self.assertEqual(html.count(f'data-tool-id="{tool["id"]}"'), 1)
        self.assertEqual(self._usage(), {
            "Raspagem por termos": 2,
            "Raspagem estruturada": 1,
            "Análise Documental": 3,
            "Análise de Comentários": 0,
            "Pesquisa Quanti": 0,
            "Instagram": 0,
            "SEO": 0,
            "Outra ferramenta": 1,
        })
