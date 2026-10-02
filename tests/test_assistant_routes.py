"""Contrato HTTP da pergunta livre do Assistente, com CSRF ativo."""

import json
import unittest
from unittest.mock import patch

from platform_helpers import create_user, csrf_from, isolated_platform, login


class TextProvider:
    model = "test-model"

    def generate(self, **_kwargs):
        return {"output_text": "Resposta de IA de teste."}

    def continue_with_tool_outputs(self, **_kwargs):
        raise AssertionError("Não esperado")


class GeminiToolLoopFake:
    """Duplo determinístico do turno Gemini, sem rede nem SDK externo."""

    provider_id = "gemini"
    model = "gemini-test"

    def __init__(self):
        self.generate_calls = []
        self.continue_calls = []

    def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        return {
            "output": [{
                "type": "function_call", "name": "get_platform_help",
                "call_id": "gemini-help-1", "arguments": '{"topic": "ajuda"}',
            }],
        }

    def continue_with_tool_outputs(self, **kwargs):
        self.continue_calls.append(kwargs)
        return {"output_text": "Resposta final fundamentada na documentação interna."}


class SelectedProviderToolLoopFake(GeminiToolLoopFake):
    """Duplo do provider escolhido pela configuração persistida, sem rede."""

    def __init__(self, provider_id, model):
        super().__init__()
        self.provider_id = provider_id
        self.model = model


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

    def test_unexpected_internal_failure_returns_safe_json(self):
        with patch("platform_core.assistant_routes.ask_with_ai", side_effect=RuntimeError("detalhe interno")):
            response = self.ask({"question": "Ajuda", "context": "term_search"})
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        self.assertEqual(response.json, {
            "ok": False,
            "reason_class": "internal_error",
            "error": "O Assistente encontrou um erro ao processar esta pergunta.",
        })

    def test_non_serializable_assistant_payload_also_remains_json(self):
        with patch("platform_core.assistant_routes.ask_with_ai", return_value={"answer": object()}):
            response = self.ask({"question": "Ajuda", "context": "structured_search"})
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        self.assertEqual(response.json["reason_class"], "internal_error")

    def test_suggestion_scope_failure_falls_back_to_the_textual_policy(self):
        self.app.extensions["assistant_ai_provider"] = GeminiToolLoopFake()
        with patch("platform_core.assistant_routes.suggestion_scope_for_id", side_effect=RuntimeError("stale-cache")):
            response = self.ask({
                "question": "Para que serve a Busca por termos?",
                "context": "term_search",
                "suggestion_id": "opaque-id",
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        self.assertEqual(response.json["answer"], "Resposta final fundamentada na documentação interna.")

    def test_term_and_structured_help_complete_a_gemini_tool_round_as_json(self):
        cases = (
            ("term_search", "Para que serve a Busca por termos?", "term_search"),
            ("term_search", "Como funciona o método Híbrido?", "term_search"),
            ("structured_search", "Para que serve a Busca estruturada?", "structured_search"),
            ("structured_search", "Qual a diferença entre grupo, entidade e variante?", "structured_search"),
            # Controle: a ajuda do Quali-dados continua no mesmo contrato.
            ("qualitative", "Como começo a analisar meus documentos aqui?", "qualitative"),
        )
        for context, question, tutorial_key in cases:
            with self.subTest(context=context, question=question):
                provider = GeminiToolLoopFake()
                self.app.extensions["assistant_ai_provider"] = provider
                response = self.ask({"question": question, "context": context})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.is_json)
                self.assertEqual(response.json["answer"], "Resposta final fundamentada na documentação interna.")
                self.assertEqual(provider.generate_calls[0]["tool_mode"], "required")
                self.assertEqual(provider.generate_calls[0]["allowed_tool_names"], ("get_platform_help",))
                self.assertEqual(len(provider.continue_calls), 1)
                output = provider.continue_calls[0]["tool_outputs"][0]
                self.assertEqual(output["call_id"], "gemini-help-1")
                self.assertEqual(json.loads(output["output"])["official_documentation"]["page"]["key"], tutorial_key)

    def test_persisted_gemini_primary_handles_term_structured_and_quali_contexts(self):
        """Percorre HTTP, policy, tool, continuação e JSON com Gemini selecionado."""
        from platform_core.assistant_ai_manager import AIProviderManager
        from platform_core.extensions import db

        self.app.extensions.pop("assistant_ai_provider", None)
        manager = AIProviderManager()
        manager.save_configuration({
            "strategy": "single",
            "primary_provider": "gemini",
            "enabled_providers": ["gemini", "analysis_native"],
            "fallback_order": [],
            "gemini_model": "gemini-fake-selected",
            "openai_model": "",
            "anthropic_model": "",
            "analysis_native_model": "analysis-native-local-v1",
        })
        db.session.commit()
        cases = (
            ("term_search", "Para que serve a Busca por termos?"),
            ("term_search", "Explique como funciona o método Híbrido e o que o limiar modifica."),
            ("structured_search", "Para que serve a Busca estruturada?"),
            ("structured_search", "Qual é a diferença entre grupo, entidade e variante?"),
            ("qualitative", "Como funciona esta ferramenta?"),
        )
        for context, question in cases:
            with self.subTest(context=context, question=question):
                gemini = SelectedProviderToolLoopFake("gemini", "gemini-fake-selected")
                with patch.object(AIProviderManager, "build_provider", autospec=True, return_value=gemini) as build:
                    response = self.ask({"question": question, "context": context})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.is_json)
                self.assertEqual(response.json["provider_used"], "gemini")
                self.assertEqual(response.json["model_used"], "gemini-fake-selected")
                self.assertEqual(gemini.generate_calls[0]["tool_mode"], "required")
                self.assertEqual(gemini.generate_calls[0]["allowed_tool_names"], ("get_platform_help",))
                self.assertEqual(len(gemini.continue_calls), 1)
                build.assert_called_once()
                self.assertEqual(build.call_args.args[1], "gemini")

    def test_persisted_native_primary_does_not_invoke_gemini(self):
        from platform_core.assistant_ai_manager import AIProviderManager
        from platform_core.extensions import db

        self.app.extensions.pop("assistant_ai_provider", None)
        manager = AIProviderManager()
        manager.save_configuration({
            "strategy": "single",
            "primary_provider": "analysis_native",
            "enabled_providers": ["analysis_native", "gemini"],
            "fallback_order": [],
            "gemini_model": "gemini-must-not-run",
            "openai_model": "",
            "anthropic_model": "",
            "analysis_native_model": "analysis-native-local-v1",
        })
        db.session.commit()
        native = SelectedProviderToolLoopFake("analysis_native", "analysis-native-local-v1")

        def build_provider(_manager, provider_id, settings=None):
            if provider_id == "gemini":
                raise AssertionError("Gemini não deve ser construído quando Análysis IA é primária.")
            self.assertEqual(provider_id, "analysis_native")
            return native

        with patch.object(AIProviderManager, "build_provider", autospec=True, side_effect=build_provider):
            response = self.ask({
                "question": "O que é o Análysis?",
                "context": "home",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["provider_used"], "analysis_native")
        self.assertEqual(native.generate_calls[0]["tool_mode"], "required")
        self.assertEqual(native.generate_calls[0]["allowed_tool_names"], ("get_platform_help",))


if __name__ == "__main__":
    unittest.main()
