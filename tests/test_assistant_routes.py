"""Contrato HTTP da pergunta livre do Assistente, com CSRF ativo."""

import unittest

from platform_helpers import create_user, csrf_from, isolated_platform, login


class TextProvider:
    model = "test-model"

    def generate(self, **_kwargs):
        return {"output_text": "Resposta de IA de teste."}

    def continue_with_tool_outputs(self, **_kwargs):
        raise AssertionError("Não esperado")


class AssistantRoutesTests(unittest.TestCase):
    def setUp(self):
        self.platform = isolated_platform()
        self.app = self.platform.__enter__()
        self.app.extensions["assistant_ai_provider"] = TextProvider()
        create_user()
        self.client = self.app.test_client()
        login(self.client)
        self.token = csrf_from(self.client.get("/"))

    def tearDown(self):
        self.platform.__exit__(None, None, None)

    def ask(self, payload, *, csrf=True):
        headers = {"X-CSRFToken": self.token} if csrf else {}
        return self.client.post("/assistant/ask", json=payload, headers=headers)

    def test_blueprint_registered_in_isolated_app_and_valid_question(self):
        self.assertIn("assistant.ask", self.app.view_functions)
        response = self.ask({
            "question": "  Como funciona?  ", "context": "qualitative",
            "page": "qualitative.page", "reference": {"project_id": "inexistente"},
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["context"], "qualitative")
        self.assertEqual(response.json["answer"], "Resposta de IA de teste.")
        self.assertNotIn("inexistente", response.json["answer"])

    def test_unknown_context_uses_fallback(self):
        response = self.ask({"question": "Ajuda", "context": "unknown"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["context"], "fallback")

    def test_question_at_exact_limit_is_accepted(self):
        response = self.ask({"question": "a" * 1000, "context": "home"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["answer"], "Resposta de IA de teste.")

    def test_invalid_questions(self):
        for question in ("", "   ", "a" * 1001, None, 123):
            with self.subTest(question=repr(question)[:30]):
                response = self.ask({"question": question, "context": "home"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json)

    def test_invalid_json_or_payload(self):
        response = self.client.post("/assistant/ask", data="{", content_type="application/json",
                                    headers={"X-CSRFToken": self.token})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.ask(["não é objeto"]).status_code, 400)
        response = self.client.post("/assistant/ask", data="question=Teste",
                                    headers={"X-CSRFToken": self.token})
        self.assertEqual(response.status_code, 400)

    def test_missing_csrf_is_rejected(self):
        self.assertEqual(self.ask({"question": "Ajuda"}, csrf=False).status_code, 400)

    def test_anonymous_cannot_ask(self):
        self.assertEqual(self.client.post("/logout", data={"csrf_token": self.token}).status_code, 302)
        token = csrf_from(self.client.get("/login"))
        response = self.client.post("/assistant/ask", json={"question": "Ajuda"},
                                    headers={"X-CSRFToken": token})
        self.assertIn(response.status_code, (302, 401))


if __name__ == "__main__":
    unittest.main()
