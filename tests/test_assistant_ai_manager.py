"""Configuração persistida, Gemini e fallback seguro sem rede externa."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask_migrate import upgrade

from app import create_app
from platform_core.assistant_ai_manager import (
    AIProviderManager,
    AISettingsValidationError,
    CONNECTION_AUDIT_ACTION,
    CONNECTION_AVAILABLE,
    CONNECTION_FAILED,
    CONNECTION_NATIVE_AVAILABLE,
    CONNECTION_NOT_TESTED,
)
from platform_core.assistant_ai_provider import (
    AIProviderError,
    DEFAULT_GEMINI_ASSISTANT_MODEL,
    GeminiProvider,
)
from platform_core.extensions import db
from platform_core.models import AssistantAISettings, AuditLog
from platform_helpers import create_user, csrf_from, isolated_platform, login


class AssistantAIManagerTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_seeded_singleton_defaults_to_gemini_and_persists(self):
        manager = AIProviderManager()
        settings = manager.get_settings()
        self.assertEqual(settings.id, 1)
        self.assertEqual(settings.strategy, "single")
        self.assertEqual(settings.primary_provider, "gemini")
        self.assertEqual(settings.enabled_providers, ["gemini"])
        self.assertEqual(db.session.query(AssistantAISettings).count(), 1)
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(AIProviderManager().get_settings().gemini_model, DEFAULT_GEMINI_ASSISTANT_MODEL)

    def test_valid_fallback_configuration_is_saved_atomically(self):
        manager = AIProviderManager()
        settings = manager.save_configuration({
            "strategy": "fallback", "primary_provider": "gemini",
            "enabled_providers": ["gemini", "openai"], "fallback_order": ["openai"],
            "gemini_model": "gemini-test", "openai_model": "openai-test", "anthropic_model": "",
        })
        db.session.commit()
        self.assertEqual(settings.fallback_order, ["openai"])
        self.assertEqual(manager.provider_ids_for_turn(), ["gemini", "openai"])

    def test_invalid_configuration_cannot_activate_future_or_invalid_fallback(self):
        manager = AIProviderManager()
        with self.assertRaisesRegex(AISettingsValidationError, "implementados"):
            manager.save_configuration({
                "strategy": "single", "primary_provider": "anthropic", "enabled_providers": ["anthropic"],
                "fallback_order": [], "gemini_model": "", "openai_model": "", "anthropic_model": "future",
            })
        with self.assertRaisesRegex(AISettingsValidationError, "fallback"):
            manager.save_configuration({
                "strategy": "fallback", "primary_provider": "gemini", "enabled_providers": ["gemini", "openai"],
                "fallback_order": ["gemini"], "gemini_model": "gemini-test", "openai_model": "openai-test", "anthropic_model": "",
            })

    def test_statuses_disclose_only_configuration_booleans_not_credentials(self):
        self.app.config["GEMINI_API_KEY"] = "never-render-this-secret"
        statuses = AIProviderManager().provider_statuses()
        gemini = next(item for item in statuses if item["id"] == "gemini")
        anthropic = next(item for item in statuses if item["id"] == "anthropic")
        self.assertTrue(gemini["credential_configured"])
        self.assertFalse(anthropic["implemented"])
        self.assertNotIn("never-render-this-secret", json.dumps(statuses))

    def test_connection_status_defaults_are_safe_and_native_is_local(self):
        statuses = {item["id"]: item for item in AIProviderManager().provider_statuses()}
        self.assertEqual(statuses["gemini"]["connection_state"], CONNECTION_NOT_TESTED)
        self.assertEqual(statuses["gemini"]["connection_status"], "Não testado")
        self.assertEqual(statuses["analysis_native"]["connection_state"], CONNECTION_NATIVE_AVAILABLE)
        self.assertEqual(statuses["analysis_native"]["connection_status"], "Motor nativo disponível")


class GeminiProviderTests(unittest.TestCase):
    class FakeTypes:
        class Tool:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FunctionCallingConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class ToolConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class GenerateContentConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FunctionResponse:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class Part:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class Content:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    class FakeModels:
        def __init__(self, response):
            self.response = response
            self.responses = list(response) if isinstance(response, list) else None
            self.calls = []

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            response = self.responses.pop(0) if self.responses is not None else self.response
            if isinstance(response, Exception):
                raise response
            return response

    def _provider(self, response):
        provider = GeminiProvider(api_key="test-gemini-key", model="gemini-test")
        provider._sdk = lambda: (object(), self.FakeTypes)
        provider._client = SimpleNamespace(models=self.FakeModels(response))
        return provider

    @staticmethod
    def _text_response(text="Resposta final."):
        return SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text=text)]))],
            usage_metadata=None,
            text=text,
        )

    def test_gemini_uses_manual_function_calling_without_external_tools(self):
        function = SimpleNamespace(id="call_abc123", name="get_platform_help", args={"topic": "quali_dados"})
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(function_call=function)]))],
            usage_metadata=SimpleNamespace(prompt_token_count=4, candidates_token_count=2), text="",
        )
        provider = self._provider(response)
        result = provider.generate(
            instructions="instrucao", input_items=[{"role": "user", "content": "oi"}],
            tools=[{"type": "function", "name": "get_platform_help", "description": "ajuda",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}],
        )
        call = provider._client.models.calls[0]
        self.assertEqual(call["model"], "gemini-test")
        self.assertTrue(call["config"].kwargs["automatic_function_calling"]["disable"])
        function_config = call["config"].kwargs["tool_config"].kwargs["function_calling_config"]
        self.assertEqual(function_config.kwargs, {"mode": "AUTO"})
        self.assertNotIn("google_search", str(call))
        self.assertEqual(result["output"][0]["name"], "get_platform_help")
        self.assertEqual(result["output"][0]["call_id"], "call_abc123")
        self.assertEqual(result["usage"]["input_tokens"], 4)

    def test_gemini_continuation_returns_native_function_call_id_with_supported_response_role(self):
        function = SimpleNamespace(id="call_abc123", name="get_platform_help", args={"topic": "busca lexical"})
        function_content = SimpleNamespace(parts=[SimpleNamespace(function_call=function)])
        initial_native = SimpleNamespace(
            candidates=[SimpleNamespace(content=function_content)], usage_metadata=None, text="",
        )
        final_native = SimpleNamespace(candidates=[], usage_metadata=None, text="Resposta final.")
        provider = self._provider(initial_native)
        initial = provider.generate(
            instructions="instrucao", input_items=[{"role": "user", "content": "Qual a diferença?"}],
            tools=[{"type": "function", "name": "get_platform_help", "parameters": {}}],
            tool_mode="required", allowed_tool_names=("get_platform_help",),
        )
        initial_config = provider._client.models.calls[0]["config"].kwargs["tool_config"].kwargs["function_calling_config"]
        self.assertEqual(initial_config.kwargs, {
            "mode": "ANY", "allowed_function_names": ["get_platform_help"],
        })
        provider._client.models.response = final_native

        provider.continue_with_tool_outputs(
            instructions="instrucao",
            input_items=[{"role": "user", "content": "Qual a diferença?"}],
            response=initial,
            tool_outputs=[{"type": "function_call_output", "call_id": "call_abc123", "output": '{"summary": "ok"}'}],
            tools=[{"type": "function", "name": "get_platform_help", "parameters": {}}],
        )

        continuation_contents = provider._client.models.calls[1]["contents"]
        self.assertIs(continuation_contents[-2], function_content)
        tool_content = continuation_contents[-1]
        self.assertEqual(tool_content.kwargs["role"], "user")
        response_part = tool_content.kwargs["parts"][0]
        function_response = response_part.kwargs["functionResponse"]
        self.assertEqual(function_response.kwargs["id"], "call_abc123")
        self.assertEqual(function_response.kwargs["name"], "get_platform_help")
        self.assertEqual(function_response.kwargs["response"], {"result": {"summary": "ok"}})
        continuation_config = provider._client.models.calls[1]["config"].kwargs["tool_config"].kwargs["function_calling_config"]
        self.assertEqual(continuation_config.kwargs, {"mode": "AUTO"})

    def test_gemini_continuation_keeps_distinct_ids_for_repeated_function_name(self):
        function_a = SimpleNamespace(id="call_a", name="read_document_pages", args={"page": 1})
        function_b = SimpleNamespace(id="call_b", name="read_document_pages", args={"page": 2})
        initial_native = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=function_a), SimpleNamespace(function_call=function_b),
            ]))], usage_metadata=None, text="",
        )
        provider = self._provider(initial_native)
        initial = provider.generate(
            instructions="instrucao", input_items=[{"role": "user", "content": "Leia as páginas."}],
            tools=[{"type": "function", "name": "read_document_pages", "parameters": {}}],
        )
        provider._client.models.response = SimpleNamespace(candidates=[], usage_metadata=None, text="Resposta final.")

        provider.continue_with_tool_outputs(
            instructions="instrucao",
            input_items=[{"role": "user", "content": "Leia as páginas."}],
            response=initial,
            tool_outputs=[
                {"type": "function_call_output", "call_id": "call_a", "output": '{"page": 1}'},
                {"type": "function_call_output", "call_id": "call_b", "output": '{"page": 2}'},
            ],
            tools=[{"type": "function", "name": "read_document_pages", "parameters": {}}],
        )

        tool_parts = provider._client.models.calls[1]["contents"][-1].kwargs["parts"]
        responses = [part.kwargs["functionResponse"].kwargs for part in tool_parts]
        self.assertEqual(
            [(entry["id"], entry["name"]) for entry in responses],
            [("call_a", "read_document_pages"), ("call_b", "read_document_pages")],
        )

    def test_gemini_maps_quota_errors_as_eligible_for_pre_tool_fallback(self):
        class ResourceExhausted(Exception):
            pass

        provider = GeminiProvider(api_key="test-gemini-key")
        provider._sdk = lambda: (object(), self.FakeTypes)
        provider._client = SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda **_kwargs: (_ for _ in ()).throw(ResourceExhausted())
        ))
        with self.assertRaises(AIProviderError) as raised:
            provider.generate(instructions="x", input_items=[], tools=[])
        self.assertTrue(raised.exception.fallback_eligible)
        self.assertEqual(raised.exception.reason_class, "quota")

    def test_gemini_retries_one_transient_failure_in_the_same_provider(self):
        class ServerError(Exception):
            status = "UNAVAILABLE"
            code = 503

        provider = self._provider([ServerError(), self._text_response()])
        with patch("platform_core.assistant_ai_provider.time.sleep") as sleep:
            result = provider.generate(instructions="x", input_items=[], tools=[])
        self.assertEqual(result["output_text"], "Resposta final.")
        self.assertEqual(len(provider._client.models.calls), 2)
        sleep.assert_called_once_with(1.0)

    def test_gemini_retries_two_transient_failures_before_succeeding(self):
        class ServerError(Exception):
            status = "UNAVAILABLE"
            code = 503

        provider = self._provider([ServerError(), ServerError(), self._text_response()])
        with patch("platform_core.assistant_ai_provider.time.sleep") as sleep:
            result = provider.generate(instructions="x", input_items=[], tools=[])
        self.assertEqual(result["output_text"], "Resposta final.")
        self.assertEqual(len(provider._client.models.calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 3.0])

    def test_gemini_stops_after_three_transient_failures(self):
        class ServerError(Exception):
            status = "UNAVAILABLE"
            code = 503

        provider = self._provider([ServerError(), ServerError(), ServerError()])
        with patch("platform_core.assistant_ai_provider.time.sleep") as sleep:
            with self.assertRaises(AIProviderError) as raised:
                provider.generate(instructions="x", input_items=[], tools=[])
        self.assertEqual(raised.exception.reason_class, "unavailable")
        self.assertEqual(len(provider._client.models.calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 3.0])

    def test_gemini_does_not_retry_authentication_or_model_errors(self):
        class AuthenticationError(Exception):
            status = 401
            code = "unauthenticated"

        class ModelNotFound(Exception):
            status = 404
            code = "model_not_found"

        for error, reason_class in ((AuthenticationError(), "authentication"), (ModelNotFound(), "model_unavailable")):
            with self.subTest(reason_class=reason_class):
                provider = self._provider([error, self._text_response()])
                with patch("platform_core.assistant_ai_provider.time.sleep") as sleep:
                    with self.assertRaises(AIProviderError) as raised:
                        provider.generate(instructions="x", input_items=[], tools=[])
                self.assertEqual(raised.exception.reason_class, reason_class)
                self.assertEqual(len(provider._client.models.calls), 1)
                sleep.assert_not_called()

    def test_gemini_logs_only_safe_provider_failure_metadata(self):
        class ServerError(Exception):
            status = "UNAVAILABLE"
            code = 503

        provider = GeminiProvider(api_key="test-gemini-key")
        provider._sdk = lambda: (object(), self.FakeTypes)
        provider._client = SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda **_kwargs: (_ for _ in ()).throw(ServerError())
        ))
        with self.assertLogs("platform_core.assistant_ai_provider", level="WARNING") as logged:
            with self.assertRaises(AIProviderError):
                provider.generate(instructions="x", input_items=[], tools=[])
        output = "\n".join(logged.output)
        self.assertIn("provider=gemini", output)
        self.assertIn("exception_type=ServerError", output)
        self.assertIn("status=UNAVAILABLE", output)
        self.assertIn("code=503", output)
        self.assertIn("reason_class=unavailable", output)
        self.assertNotIn("test-gemini-key", output)


class AssistantAIFallbackTests(unittest.TestCase):
    class Provider:
        def __init__(self, *responses, model="test-model", provider_id="test"):
            self.responses = list(responses)
            self.model = model
            self.provider_id = provider_id
            self.calls = 0

        def generate(self, **_kwargs):
            self.calls += 1
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        def continue_with_tool_outputs(self, **_kwargs):
            self.calls += 1
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    class Manager:
        def __init__(self, providers):
            self.providers = providers

        def provider_attempt_ids(self):
            return list(self.providers)

        def provider_for_attempt(self, identifier):
            return self.providers[identifier]

    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.user = create_user()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def _ask(self, manager):
        from platform_core.assistant_service import ask_with_ai
        self.app.extensions["assistant_ai_manager"] = manager
        with self.app.test_request_context("/assistant/ask"):
            return ask_with_ai(user=self.user, question="Como funciona?", context_key="fallback", project_context=None)

    @staticmethod
    def _gemini_text_response(text="Resposta final."):
        return GeminiProviderTests._text_response(text)

    @staticmethod
    def _gemini_provider(responses):
        provider = GeminiProvider(api_key="test-gemini-key", model="gemini-test")
        provider._sdk = lambda: (object(), GeminiProviderTests.FakeTypes)
        provider._client = SimpleNamespace(models=GeminiProviderTests.FakeModels(responses))
        return provider

    def test_technical_error_before_tools_falls_back_to_next_provider(self):
        first = self.Provider(AIProviderError("quota", reason_class="quota", fallback_eligible=True), provider_id="gemini")
        second = self.Provider({"output_text": "Resposta do fallback."}, provider_id="openai")
        result = self._ask(self.Manager({"gemini": first, "openai": second}))
        self.assertEqual(result["answer"], "Resposta do fallback.")
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)

    def test_answer_without_evidence_does_not_trigger_fallback(self):
        first = self.Provider({"output_text": "Não encontrei evidência no corpus."}, provider_id="gemini")
        second = self.Provider({"output_text": "Não deveria ser chamado."}, provider_id="openai")
        result = self._ask(self.Manager({"gemini": first, "openai": second}))
        self.assertEqual(result["answer"], "Não encontrei evidência no corpus.")
        self.assertEqual(second.calls, 0)

    def test_error_after_tool_round_does_not_resend_context_to_fallback(self):
        first = self.Provider(
            {"output": [{"type": "function_call", "name": "get_current_ui_context", "call_id": "call-ui", "arguments": "{}"}]},
            AIProviderError("timeout", reason_class="timeout", fallback_eligible=True), provider_id="gemini",
        )
        second = self.Provider({"output_text": "Não deveria ser chamado."}, provider_id="openai")
        with self.assertRaises(AIProviderError):
            self._ask(self.Manager({"gemini": first, "openai": second}))
        self.assertEqual(second.calls, 0)

    def test_gemini_retries_continuation_without_reexecuting_tool_or_switching_provider(self):
        class ServerError(Exception):
            status = "UNAVAILABLE"
            code = 503

        function = SimpleNamespace(id="call_help", name="get_platform_help", args={"topic": "buscas"})
        initial = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(function_call=function)]))],
            usage_metadata=None,
            text="",
        )
        gemini = self._gemini_provider([initial, ServerError(), self._gemini_text_response()])
        openai = self.Provider({"output_text": "Não deveria ser chamado."}, provider_id="openai")
        with patch("platform_core.assistant_ai_provider.time.sleep") as sleep, patch(
            "platform_core.assistant_service.AssistantToolExecutor.execute",
            return_value=({"status": "ok"}, []),
        ) as execute:
            result = self._ask(self.Manager({"gemini": gemini, "openai": openai}))
        self.assertEqual(result["answer"], "Resposta final.")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(gemini._client.models.calls), 3)
        self.assertEqual(openai.calls, 0)
        sleep.assert_called_once_with(1.0)

    def test_gemini_exhausted_continuation_never_switches_provider_after_tool_round(self):
        class ServerError(Exception):
            status = "UNAVAILABLE"
            code = 503

        function = SimpleNamespace(id="call_help", name="get_platform_help", args={"topic": "buscas"})
        initial = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(function_call=function)]))],
            usage_metadata=None,
            text="",
        )
        gemini = self._gemini_provider([initial, ServerError(), ServerError(), ServerError()])
        openai = self.Provider({"output_text": "Não deveria ser chamado."}, provider_id="openai")
        with patch("platform_core.assistant_ai_provider.time.sleep") as sleep, patch(
            "platform_core.assistant_service.AssistantToolExecutor.execute",
            return_value=({"status": "ok"}, []),
        ) as execute:
            with self.assertRaises(AIProviderError) as raised:
                self._ask(self.Manager({"gemini": gemini, "openai": openai}))
        self.assertEqual(raised.exception.reason_class, "unavailable")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(gemini._client.models.calls), 4)
        self.assertEqual(openai.calls, 0)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 3.0])


class AssistantAIAdminTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.app.config["GEMINI_API_KEY"] = "never-render-this-secret"
        self.admin = create_user("Admin", "assistant-ai-admin@example.org", role="admin", plan="institutional")
        self.client = self.app.test_client()
        login(self.client, self.admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_panel_requires_admin_and_never_renders_credentials(self):
        response = self.client.get("/admin/inteligencia-artificial")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Configuração operacional de Inteligência Artificial", html)
        self.assertIn("Claude / Anthropic", html)
        self.assertIn("Credencial configurada", html)
        self.assertIn("Provider primário", html)
        self.assertIn("PRIMÁRIO", html)
        self.assertIn("Habilitado", html)
        self.assertIn("Não testado", html)
        self.assertIn('class="platform-ai-provider-card platform-ai-docchat"', html)
        self.assertIn("DocChat (em breve)", html)
        self.assertIn('form="docchat-settings-form"', html)
        self.assertIn('action="/admin/inteligencia-artificial/docchat"', html)
        docchat_card = html.split('class="platform-ai-provider-card platform-ai-docchat"', 1)[1].split('</article>', 1)[0]
        self.assertNotIn('value="analysis_native"', docchat_card)
        self.assertNotIn("never-render-this-secret", html)
        self.client.post("/logout", data={"csrf_token": csrf_from(response)})
        regular = create_user("Regular", "assistant-ai-regular@example.org")
        login(self.client, regular.email)
        self.assertEqual(self.client.get("/admin/inteligencia-artificial").status_code, 403)

    def test_admin_saves_fallback_and_audits_without_secret(self):
        page = self.client.get("/admin/inteligencia-artificial")
        response = self.client.post("/admin/inteligencia-artificial", data={
            "csrf_token": csrf_from(page), "strategy": "fallback", "primary_provider": "gemini",
            "enabled_providers": ["gemini", "openai"], "fallback_order": ["openai"],
            "gemini_model": "gemini-test", "openai_model": "openai-test", "anthropic_model": "",
        })
        self.assertEqual(response.status_code, 302)
        settings = db.session.get(AssistantAISettings, 1)
        self.assertEqual(settings.fallback_order, ["openai"])
        audit = db.session.scalar(db.select(AuditLog).where(AuditLog.action == "assistant_ai_settings_changed"))
        self.assertEqual(audit.after_json["primary_provider"], "gemini")
        self.assertNotIn("never-render-this-secret", json.dumps(audit.after_json))

    def test_docchat_card_saves_only_its_future_provider_and_model(self):
        page = self.client.get("/admin/inteligencia-artificial")
        response = self.client.post("/admin/inteligencia-artificial/docchat", data={
            "csrf_token": csrf_from(page), "docchat_provider": "gemini", "docchat_model": "gemini-docchat-future",
        })
        self.assertEqual(response.status_code, 302)
        settings = db.session.get(AssistantAISettings, 1)
        self.assertEqual((settings.docchat_provider, settings.docchat_model), ("gemini", "gemini-docchat-future"))
        self.assertEqual(settings.primary_provider, "gemini")
        audit = db.session.scalar(db.select(AuditLog).where(AuditLog.action == "docchat_ai_settings_changed"))
        self.assertEqual(audit.after_json["docchat_provider"], "gemini")

    def test_connection_route_uses_no_corpus_and_returns_friendly_flash(self):
        page = self.client.get("/admin/inteligencia-artificial")
        with patch("platform_core.admin.AIProviderManager.test_provider_connection") as connection:
            response = self.client.post("/admin/inteligencia-artificial/gemini/testar-conexao", data={
                "csrf_token": csrf_from(page),
            }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        connection.assert_called_once_with("gemini")
        self.assertIn("Conexão disponível.", response.get_data(as_text=True))

    def test_connection_status_json_updates_only_tested_provider_and_persists_after_reload(self):
        page = self.client.get("/admin/inteligencia-artificial")
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        with patch("platform_core.admin.AIProviderManager.test_provider_connection") as connection:
            response = self.client.post(
                "/admin/inteligencia-artificial/gemini/testar-conexao",
                data={"csrf_token": csrf_from(page)},
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["connection_state"], CONNECTION_AVAILABLE)
        self.assertEqual(response.json["connection_status"], "Conexão disponível")
        connection.assert_called_once_with("gemini")
        states = {item["id"]: item["connection_state"] for item in AIProviderManager().provider_statuses()}
        self.assertEqual(states["gemini"], CONNECTION_AVAILABLE)
        self.assertEqual(states["openai"], CONNECTION_NOT_TESTED)
        self.assertEqual(states["analysis_native"], CONNECTION_NATIVE_AVAILABLE)
        reload = self.client.get("/admin/inteligencia-artificial")
        self.assertIn("Conexão disponível", reload.get_data(as_text=True))
        audit = db.session.scalar(
            db.select(AuditLog).where(
                AuditLog.action == CONNECTION_AUDIT_ACTION,
                AuditLog.target_id == "gemini",
            )
        )
        self.assertEqual(audit.after_json["state"], CONNECTION_AVAILABLE)

    def test_connection_failure_is_persisted_per_provider_and_returns_json(self):
        page = self.client.get("/admin/inteligencia-artificial")
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        failure = AIProviderError("temporário", status_code=503, reason_class="unavailable")
        with patch("platform_core.admin.AIProviderManager.test_provider_connection", side_effect=failure):
            response = self.client.post(
                "/admin/inteligencia-artificial/openai/testar-conexao",
                data={"csrf_token": csrf_from(page)},
                headers=headers,
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["connection_state"], CONNECTION_FAILED)
        self.assertEqual(response.json["connection_status"], "Falha na conexão")
        states = {item["id"]: item["connection_state"] for item in AIProviderManager().provider_statuses()}
        self.assertEqual(states["openai"], CONNECTION_FAILED)
        self.assertEqual(states["gemini"], CONNECTION_NOT_TESTED)
        self.assertEqual(states["analysis_native"], CONNECTION_NATIVE_AVAILABLE)

    def test_native_connection_test_is_local_and_uses_native_available_status(self):
        page = self.client.get("/admin/inteligencia-artificial")
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        with patch("platform_core.admin.AIProviderManager.test_provider_connection") as connection:
            response = self.client.post(
                "/admin/inteligencia-artificial/analysis_native/testar-conexao",
                data={"csrf_token": csrf_from(page)},
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["connection_state"], CONNECTION_NATIVE_AVAILABLE)
        self.assertEqual(response.json["connection_status"], "Motor nativo disponível")
        connection.assert_called_once_with("analysis_native")


class AssistantAISettingsMigrationTests(unittest.TestCase):
    previous = "a6f2c9e41b07"
    revision = "b8c4d3e2f1a0"
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")

    def test_upgrade_creates_exactly_one_default_settings_record(self):
        with tempfile.TemporaryDirectory(prefix="assistant-ai-settings-migration-") as directory:
            app = create_app({
                "TESTING": True, "SECRET_KEY": "migration-test-only",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{Path(directory).as_posix()}/db.sqlite",
                "PLATFORM_DATA_DIR": directory,
            })
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.previous)
                upgrade(directory=self.migrations, revision=self.revision)
                with db.engine.connect() as connection:
                    row = connection.exec_driver_sql(
                        "SELECT id, strategy, primary_provider, enabled_providers FROM assistant_ai_settings"
                    ).one()
                    self.assertEqual(row[:3], (1, "single", "gemini"))
                    self.assertEqual(json.loads(row[3]), ["gemini"])
                    self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), self.revision)
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
