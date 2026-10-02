"""Contextos curados e inclusão única do Assistente Análysis."""

from html import unescape
import json
from pathlib import Path
import re
import unittest
from uuid import uuid4

from platform_helpers import create_project, create_user, isolated_platform, login
from platform_core.assistant_context import (
    ASSISTANT_CONTEXTS,
    CONTEXTUAL_PROMPT_SCOPES,
    CONTEXTUAL_PROMPT_TOOL_HINTS,
    SUGGESTION_SCOPES,
    assistant_context_for_endpoint,
    assistant_contextual_prompt_scope,
    assistant_contextual_prompts,
)
import test_qualitative_context as qualitative_fixture


ROOT = Path(__file__).resolve().parents[1]


class AssistantContextTests(unittest.TestCase):
    def test_each_context_has_a_greeting_and_exactly_three_contextual_prompts(self):
        self.assertEqual(set(ASSISTANT_CONTEXTS), {
            "home", "projects", "project", "term_search", "term_analysis",
            "structured_search", "structured_analysis", "qualitative", "qualitative_reader",
            "qualitative_search", "coding_report", "libraries", "admin_ai_settings", "admin", "fallback",
        })
        for key, context in ASSISTANT_CONTEXTS.items():
            with self.subTest(context=key):
                self.assertTrue(context["intro"].strip())
                prompts = assistant_contextual_prompts(key)
                self.assertEqual(len(prompts), 3)
                self.assertEqual(len(set(prompts)), 3)
                self.assertTrue(all(prompt.strip() for prompt in prompts))
                self.assertEqual(len(CONTEXTUAL_PROMPT_TOOL_HINTS[key]), 3)
                self.assertEqual(len(CONTEXTUAL_PROMPT_SCOPES[key]), 3)
                self.assertTrue(set(CONTEXTUAL_PROMPT_SCOPES[key]).issubset(SUGGESTION_SCOPES))
                self.assertEqual(assistant_contextual_prompt_scope(key, prompts[0]), CONTEXTUAL_PROMPT_SCOPES[key][0])

    def test_dashboard_prompts_are_exact_and_static(self):
        self.assertEqual(assistant_contextual_prompts("home"), (
            "O que é o Análysis?",
            "Que ferramentas estão disponíveis no meu plano?",
            "Explique as funcionalidades das ferramentas disponíveis.",
        ))
        self.assertEqual(assistant_contextual_prompts("unknown"), assistant_contextual_prompts("fallback"))

    def test_endpoint_mapping_and_safe_project_reference(self):
        cases = {
            "home": "home",
            "projects.list_free_projects": "projects",
            "projects.list_projects": "projects",
            "projects.list_qualitative_projects": "projects",
            "inicio": "term_search",
            "resultado": "term_analysis",
            "historico_racial.inicio_projeto": "structured_analysis",
            "user_libraries.new_library": "libraries",
            "qualitative.base": "qualitative",
            "qualitative.page": "qualitative_reader",
            "qualitative.search": "qualitative_search",
            "qualitative.project_workspace": "project",
            "qualitative.coding_report": "coding_report",
            "admin.assistant_ai_settings": "admin_ai_settings",
            "admin.home": "admin",
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

    def test_avatar_asset_and_info_controls_are_limited_to_main_tool_entries(self):
        served = (ROOT / "static/img/assistant/robot_assistente.png").read_bytes()
        self.assertTrue(served.startswith(b"\x89PNG\r\n\x1a\n"))
        original = ROOT / "img/robot_assistente.png"
        if original.exists():
            self.assertEqual(original.read_bytes(), served)
        reader = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        self.assertNotIn("help_button(", reader)
        qualitative_intro = (ROOT / "templates/platform/_qualitative_page_intro.html").read_text(encoding="utf-8")
        self.assertIn("tutorial_button", qualitative_intro)
        self.assertIn("Como funciona a Análise quali-dados", qualitative_intro)


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
        self.assertEqual(html.count('data-assistant-question='), 0)
        self.assertEqual(html.count('data-assistant-suggestion>'), 0)
        self.assertEqual(html.count('data-assistant-suggestions-loading role='), 1)
        self.assertEqual(html.count('js/platform_assistant.js'), 1)
        self.assertIn(f'data-assistant-context="{key}"', html)
        self.assertIn('aria-label="Abrir Assistente Análysis"', html)
        self.assertIn('aria-label="Fechar Assistente Análysis"', html)
        match = re.search(r'<script type="application/json" data-assistant-payload>(.*?)</script>', html, re.S)
        self.assertIsNotNone(match)
        payload = json.loads(unescape(match.group(1)))
        self.assertEqual(payload["key"], key)
        self.assertTrue(payload["dynamic_suggestions"])
        self.assertEqual(payload["onboarding_prompts"], list(assistant_contextual_prompts(key)))
        self.assertEqual(len(payload["onboarding_prompts"]), 3)
        self.assertNotIn("senha-de-teste-segura-123", match.group(1))
        return html

    def test_dashboard_and_project_list_have_one_assistant_each(self):
        self.assert_one_assistant(self.client.get("/"), "home")
        self.assert_one_assistant(self.client.get("/projetos/livres"), "projects")

    def test_fallback_context_and_avatar_use_the_universal_dynamic_cycle(self):
        self.assert_one_assistant(self.client.get("/perfil"), "fallback")
        image = self.client.get("/static/img/assistant/robot_assistente.png")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.data, (ROOT / "static/img/assistant/robot_assistente.png").read_bytes())

    def test_term_and_structured_upload_pages_use_their_own_contexts(self):
        self.assert_one_assistant(self.client.get("/raspagem-livre"), "term_search")
        project_id = create_project(self.client, name="Estruturado")
        self.assert_one_assistant(
            self.client.get(f"/analise-documental/projetos/{project_id}"), "structured_analysis")

    def test_anonymous_login_has_no_assistant(self):
        response = self.app.test_client().get("/login")
        self.assertNotIn("data-assistant-toggle", response.get_data(as_text=True))


class AssistantQualitativePageTests(unittest.TestCase):
    setUp = qualitative_fixture.QualitativeContextTests.setUp
    tearDown = qualitative_fixture.QualitativeContextTests.tearDown

    def test_reader_and_report_have_specific_context_and_one_panel(self):
        for url, key in ((self.base_url, "qualitative_reader"),
                         (f"/analise-qualitativa/bases/{self.analysis.id}/relatorio-codificacao",
                          "coding_report")):
            with self.subTest(context=key):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertEqual(html.count('data-assistant-toggle'), 1)
                self.assertEqual(html.count('data-assistant-question='), 0)
                self.assertEqual(html.count('data-assistant-suggestion>'), 0)
                self.assertIn('data-assistant-suggestions-loading', html)
                self.assertIn(f'data-assistant-context="{key}"', html)
                self.assertIn(f'data-project-id="{self.project.id}"', html)
                payload_match = re.search(r'<script type="application/json" data-assistant-payload>(.*?)</script>', html, re.S)
                self.assertIsNotNone(payload_match)
                payload = json.loads(unescape(payload_match.group(1)))
                self.assertTrue(payload["dynamic_suggestions"])
                self.assertEqual(len(payload["onboarding_prompts"]), 3)


if __name__ == "__main__":
    unittest.main()
