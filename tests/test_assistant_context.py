"""Contextos curados e inclusão única do Assistente Análysis."""

from html import unescape
import json
from pathlib import Path
import re
import unittest
from uuid import uuid4

from platform_helpers import create_project, create_user, isolated_platform, login
from platform_core.assistant_context import ASSISTANT_CONTEXTS, assistant_context_for_endpoint
import test_qualitative_context as qualitative_fixture


ROOT = Path(__file__).resolve().parents[1]


class AssistantContextTests(unittest.TestCase):
    def test_each_context_has_exactly_five_distinct_questions_with_curated_answers(self):
        self.assertEqual(set(ASSISTANT_CONTEXTS), {
            "home", "projects", "term_search", "structured_search",
            "qualitative", "coding_report", "fallback",
        })
        for key, context in ASSISTANT_CONTEXTS.items():
            with self.subTest(context=key):
                self.assertTrue(context["intro"].strip())
                self.assertEqual(len(context["suggestions"]), 5)
                self.assertEqual(len({item["question"] for item in context["suggestions"]}), 5)
                self.assertTrue(all(item["question"] and item["answer"]
                                    for item in context["suggestions"]))

    def test_endpoint_mapping_and_safe_project_reference(self):
        cases = {
            "home": "home",
            "projects.list_free_projects": "projects",
            "projects.list_projects": "projects",
            "projects.list_qualitative_projects": "projects",
            "inicio": "term_search",
            "resultado": "term_search",
            "historico_racial.inicio_projeto": "structured_search",
            "user_libraries.new_library": "structured_search",
            "qualitative.page": "qualitative",
            "qualitative.project_workspace": "qualitative",
            "qualitative.coding_report": "coding_report",
            "admin.home": "fallback",
            "profile.my_profile": "fallback",
        }
        for endpoint, expected in cases.items():
            with self.subTest(endpoint=endpoint):
                self.assertEqual(assistant_context_for_endpoint(endpoint)["key"], expected)
        identifier = uuid4()
        context = assistant_context_for_endpoint("qualitative.page", {
            "project_id": identifier, "unused": "private data",
        })
        self.assertEqual(context["reference"], {"project_id": str(identifier)})
        self.assertEqual(context["tool"], "qualitative_analysis")
        self.assertNotIn("private data", json.dumps(context, ensure_ascii=False))

    def test_qualitative_questions_cover_requested_subjects(self):
        questions = " ".join(item["question"] for item in ASSISTANT_CONTEXTS["qualitative"]["suggestions"])
        for subject in ("Literal", "Lexical", "Semântica", "Rejeição contextual",
                        "códigos", "Margem analítica", "Explorador", "metodologia"):
            self.assertIn(subject, questions)
        answers = " ".join(item["answer"] for item in ASSISTANT_CONTEXTS["qualitative"]["suggestions"])
        self.assertIn("trecho concreto", answers)
        self.assertIn("ainda não examino", answers)

    def test_avatar_is_exact_unmodified_copy_and_info_controls_remain(self):
        original = (ROOT / "img/robot_assistente.png").read_bytes()
        served = (ROOT / "static/img/assistant/robot_assistente.png").read_bytes()
        self.assertEqual(original, served)
        reader = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        for key in ("regex", "case", "automatic", "multiple", "rejection",
                    "literal", "lexical", "semantic"):
            self.assertIn(f"help_button('{key}')", reader)


class AssistantPageTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.user = create_user()
        self.client = self.app.test_client()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def assert_one_assistant(self, response, key):
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count('data-assistant-toggle'), 1)
        self.assertEqual(html.count('data-assistant-panel'), 1)
        self.assertEqual(html.count('data-assistant-question='), 5)
        self.assertEqual(html.count('js/platform_assistant.js'), 1)
        self.assertIn(f'data-assistant-context="{key}"', html)
        self.assertIn('aria-label="Abrir Assistente Análysis"', html)
        self.assertIn('aria-label="Fechar Assistente Análysis"', html)
        match = re.search(r'<script type="application/json" data-assistant-payload>(.*?)</script>', html, re.S)
        self.assertIsNotNone(match)
        payload = json.loads(unescape(match.group(1)))
        self.assertEqual(payload["key"], key)
        self.assertEqual(len(payload["suggestions"]), 5)
        self.assertNotIn("senha-de-teste-segura-123", match.group(1))
        return html

    def test_dashboard_and_project_list_have_one_assistant_each(self):
        self.assert_one_assistant(self.client.get("/"), "home")
        self.assert_one_assistant(self.client.get("/projetos/livres"), "projects")

    def test_fallback_and_static_avatar(self):
        self.assert_one_assistant(self.client.get("/perfil"), "fallback")
        image = self.client.get("/static/img/assistant/robot_assistente.png")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.data, (ROOT / "img/robot_assistente.png").read_bytes())

    def test_term_and_structured_upload_pages_use_their_own_contexts(self):
        self.assert_one_assistant(self.client.get("/raspagem-livre"), "term_search")
        project_id = create_project(self.client, name="Estruturado")
        self.assert_one_assistant(
            self.client.get(f"/analise-documental/projetos/{project_id}"), "structured_search")

    def test_anonymous_login_has_no_assistant(self):
        response = self.app.test_client().get("/login")
        self.assertNotIn("data-assistant-toggle", response.get_data(as_text=True))


class AssistantQualitativePageTests(unittest.TestCase):
    setUp = qualitative_fixture.QualitativeContextTests.setUp
    tearDown = qualitative_fixture.QualitativeContextTests.tearDown

    def test_reader_and_report_have_specific_context_and_one_panel(self):
        for url, key in ((self.base_url, "qualitative"),
                         (f"/analise-qualitativa/bases/{self.analysis.id}/relatorio-codificacao",
                          "coding_report")):
            with self.subTest(context=key):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertEqual(html.count('data-assistant-toggle'), 1)
                self.assertEqual(html.count('data-assistant-question='), 5)
                self.assertIn(f'data-assistant-context="{key}"', html)
                self.assertIn(f'data-project-id="{self.project.id}"', html)


if __name__ == "__main__":
    unittest.main()
