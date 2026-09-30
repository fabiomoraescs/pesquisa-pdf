"""Sugestões documentais: escopo autorizado, validação e fallback sem rede."""

from __future__ import annotations

import json
import unittest

import test_qualitative_automatic as automatic_fixture
import test_qualitative_context as context_fixture

from platform_helpers import create_user
from platform_core.assistant_context import SUGGESTION_SCOPES, assistant_contextual_prompts
from platform_core.assistant_suggestions import (
    MAX_PAGE_CHARS,
    SUGGESTION_INSTRUCTION,
    contextual_suggestions,
    reset_suggestion_caches,
    suggestion_scope_for_id,
)
from platform_core.assistant_project_context import resolve_project_context
from platform_core.assistant_tool_policy import TOOL_MODE_REQUIRED, tool_policy_for_question, tool_policy_for_suggestion_scope
from platform_core.extensions import db
from platform_core.models import AssistantAISettings, Project, QualitativeCode, UserToolOverride


class SuggestionProvider:
    model = "fake-suggestions-model"

    def __init__(self, output):
        self.output = output
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"output_text": self.output}

    def continue_with_tool_outputs(self, **_kwargs):
        raise AssertionError("Sugestões não usam tools nem continuação.")


class ToolLoopProvider:
    model = "fake-tool-loop-model"

    def __init__(self, initial, final):
        self.initial = initial
        self.final = final
        self.generate_calls = []
        self.continue_calls = []

    def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        return self.initial

    def continue_with_tool_outputs(self, **kwargs):
        self.continue_calls.append(kwargs)
        return self.final


def dynamic_output():
    return json.dumps({"questions": [
        {"text": "Como raça é apresentada nesta página?", "anchors": ["raça"]},
        {"text": "Que análise o código Desigualdade orienta nesta passagem?", "anchors": ["Desigualdade"]},
        {"text": "Como raça se relaciona com os demais documentos?", "anchors": ["raça"]},
    ]}, ensure_ascii=False)


def scoped_dynamic_output():
    return json.dumps({"questions": [
        {"text": "Que concepção de raça aparece neste trecho?", "anchors": ["raça"]},
        {"text": "Que aspecto da desigualdade merece atenção?", "anchors": ["desigualdade"]},
        {"text": "Como esta discussão se articula com os outros textos?", "anchors": ["raça"]},
    ]}, ensure_ascii=False)


def functional_output(*anchors):
    """Saída fake ancorada em fatos estruturados, sem chamar provider real."""
    return json.dumps({"questions": [
        {"text": f"Como entendo {anchors[0]}?", "anchors": [anchors[0]]},
        {"text": f"Como utilizo {anchors[1]}?", "anchors": [anchors[1]]},
        {"text": f"Qual próximo passo com {anchors[2]}?", "anchors": [anchors[2]]},
    ]}, ensure_ascii=False)


class AssistantSuggestionsTests(unittest.TestCase):
    setUp = context_fixture.QualitativeContextTests.setUp
    tearDown = context_fixture.QualitativeContextTests.tearDown
    fixture_pages = automatic_fixture.QualitativeAutomaticTests.fixture_pages

    def setUp(self):
        context_fixture.QualitativeContextTests.setUp(self)
        reset_suggestion_caches()

    def tearDown(self):
        reset_suggestion_caches()
        context_fixture.QualitativeContextTests.tearDown(self)

    def request_payload(self):
        return {
            "context": "qualitative_reader",
            "page": "qualitative.page",
            "reference": {"project_id": self.project.id, "analysis_id": self.analysis.id},
            "page_context": {"document_id": self.document.id, "current_page": 1,
                             "selected_search_mode": "lexical"},
        }

    def authorized_context(self, *, page_number=1):
        return resolve_project_context(self.user, {
            "project_id": self.project.id, "analysis_id": self.analysis.id,
        }, page_context={"document_id": self.document.id, "current_page": page_number})

    def test_current_page_drives_three_validated_dynamic_questions_and_is_cached(self):
        page_text = "A discussão sobre raça e desigualdade orienta a leitura desta página. " * 100
        self.fixture_pages([[page_text]])
        code = QualitativeCode(analysis_id=self.analysis.id, name="Desigualdade", created_by_user_id=self.user.id)
        db.session.add(code)
        db.session.commit()
        provider = SuggestionProvider(dynamic_output())
        self.app.extensions["assistant_ai_provider"] = provider

        first = self.client.post("/assistant/suggestions", json=self.request_payload(),
                                 headers={"X-CSRFToken": self.csrf})
        second = self.client.post("/assistant/suggestions", json=self.request_payload(),
                                  headers={"X-CSRFToken": self.csrf})

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json["dynamic"])
        self.assertEqual(len(first.json["questions"]), 3)
        self.assertEqual(len(first.json["suggestions"]), 3)
        self.assertTrue(all(set(item) == {"id", "text"} and item["id"] for item in first.json["suggestions"]))
        self.assertNotIn("anchors", first.json)
        self.assertTrue(second.json["cached"])
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertEqual(call["tools"], [])
        self.assertEqual(call["tool_mode"], "auto")
        source = json.loads(call["input_items"][0]["content"])
        self.assertEqual(source["current_page"]["priority"], "highest")
        self.assertEqual(source["current_page"]["page_number"], 1)
        self.assertEqual(source["current_page"]["selected_search_mode"], "lexical")
        self.assertLessEqual(len(source["current_page"]["canonical_text"]), MAX_PAGE_CHARS)
        self.assertIn("raça", source["current_page"]["canonical_text"])
        self.assertIn("dado não confiável", SUGGESTION_INSTRUCTION)

        page_policy = tool_policy_for_question(first.json["questions"][0], context_key="qualitative_reader", project_context={
            "page": {"current_page": 1, "document": {"id": self.document.id}},
        })
        corpus_policy = tool_policy_for_question(first.json["questions"][2], context_key="qualitative_reader", project_context={
            "page": {"current_page": 1, "document": {"id": self.document.id}},
        })
        self.assertEqual(page_policy.tool_mode, TOOL_MODE_REQUIRED)
        self.assertEqual(page_policy.allowed_tool_names, ("read_current_page",))
        self.assertEqual(corpus_policy.allowed_tool_names, ("search_corpus",))

        db.session.add(QualitativeCode(analysis_id=self.analysis.id, name="Marcador novo",
                                       created_by_user_id=self.user.id))
        db.session.commit()
        refreshed = self.client.post("/assistant/suggestions", json=self.request_payload(),
                                     headers={"X-CSRFToken": self.csrf})
        self.assertTrue(refreshed.json["dynamic"])
        self.assertFalse(refreshed.json["cached"])
        self.assertEqual(len(provider.calls), 2)

    def test_base_without_open_page_uses_only_a_bounded_profile_sample(self):
        self.fixture_pages([["O documento apresenta raça como categoria de análise social. " * 80]])
        db.session.add(QualitativeCode(analysis_id=self.analysis.id, name="Desigualdade",
                                       created_by_user_id=self.user.id))
        db.session.commit()
        provider = SuggestionProvider(dynamic_output())
        self.app.extensions["assistant_ai_provider"] = provider
        response = self.client.post("/assistant/suggestions", json={
            "context": "qualitative", "reference": {
                "project_id": self.project.id, "analysis_id": self.analysis.id,
            },
        }, headers={"X-CSRFToken": self.csrf})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["dynamic"])
        source = json.loads(provider.calls[0]["input_items"][0]["content"])
        self.assertEqual(source["current_page"]["priority"], "profile_sample_only")
        self.assertEqual(source["current_page"]["canonical_text"], "")
        self.assertLessEqual(len(source["corpus_profile"]["profile_sample"]), 1_600)

    def test_valid_suggestion_id_forces_current_page_without_relying_on_its_wording(self):
        self.fixture_pages([["A concepção de raça é discutida com desigualdade nesta seção. " * 30]])
        db.session.add(QualitativeCode(analysis_id=self.analysis.id, name="Desigualdade",
                                       created_by_user_id=self.user.id))
        db.session.commit()
        self.app.extensions["assistant_ai_provider"] = SuggestionProvider(scoped_dynamic_output())
        suggestions = self.client.post("/assistant/suggestions", json=self.request_payload(),
                                       headers={"X-CSRFToken": self.csrf}).json["suggestions"]
        selected = suggestions[0]
        provider = ToolLoopProvider(
            {"output": [{"type": "function_call", "name": "read_current_page", "call_id": "page-call",
                         "arguments": "{}"}]},
            {"output_text": "Resposta baseada na página autorizada."},
        )
        self.app.extensions["assistant_ai_provider"] = provider

        response = self.client.post("/assistant/ask", json={
            **self.request_payload(), "question": selected["text"], "suggestion_id": selected["id"],
            # Campos arbitrários do browser não alteram a source policy.
            "scope": "corpus", "required_tool": "search_corpus",
        }, headers={"X-CSRFToken": self.csrf})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.generate_calls[0]["tool_mode"], "required")
        self.assertEqual(provider.generate_calls[0]["allowed_tool_names"], ("read_current_page",))
        self.assertEqual(len(provider.continue_calls), 1)

    def test_valid_connection_suggestion_forces_corpus_search(self):
        self.fixture_pages([
            ["A concepção de raça é discutida neste primeiro texto. " * 30],
            ["O outro texto também trata de raça e desigualdade. " * 30],
        ])
        db.session.add(QualitativeCode(analysis_id=self.analysis.id, name="Desigualdade",
                                       created_by_user_id=self.user.id))
        db.session.commit()
        self.app.extensions["assistant_ai_provider"] = SuggestionProvider(scoped_dynamic_output())
        suggestions = self.client.post("/assistant/suggestions", json=self.request_payload(),
                                       headers={"X-CSRFToken": self.csrf}).json["suggestions"]
        selected = suggestions[2]
        provider = ToolLoopProvider(
            {"output": [{"type": "function_call", "name": "search_corpus", "call_id": "corpus-call",
                         "arguments": json.dumps({"query": "raça", "scope": "analysis", "document_ids": None,
                                                   "retrieval_mode": "lexical"})}]},
            {"output_text": "Resposta baseada no corpus autorizado."},
        )
        self.app.extensions["assistant_ai_provider"] = provider

        response = self.client.post("/assistant/ask", json={
            **self.request_payload(), "question": selected["text"], "suggestion_id": selected["id"],
        }, headers={"X-CSRFToken": self.csrf})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.generate_calls[0]["tool_mode"], "required")
        self.assertEqual(provider.generate_calls[0]["allowed_tool_names"], ("search_corpus",))

    def test_forged_foreign_or_stale_suggestion_id_is_ignored(self):
        self.fixture_pages([["A concepção de raça é discutida com desigualdade nesta seção. " * 30,
                             "A segunda página preserva outro contexto sobre raça. " * 30]])
        db.session.add(QualitativeCode(analysis_id=self.analysis.id, name="Desigualdade",
                                       created_by_user_id=self.user.id))
        db.session.commit()
        self.app.extensions["assistant_ai_provider"] = SuggestionProvider(scoped_dynamic_output())
        selected = self.client.post("/assistant/suggestions", json=self.request_payload(),
                                    headers={"X-CSRFToken": self.csrf}).json["suggestions"][0]
        context = self.authorized_context()

        self.assertIsNone(suggestion_scope_for_id(
            user=self.user, context_key="qualitative_reader", project_context=context,
            suggestion_id="forged-id", question=selected["text"],
        ))
        other = create_user(name="Outro", email="outro@example.org")
        self.assertIsNone(suggestion_scope_for_id(
            user=other, context_key="qualitative_reader", project_context=context,
            suggestion_id=selected["id"], question=selected["text"],
        ))
        self.assertIsNone(suggestion_scope_for_id(
            user=self.user, context_key="qualitative_reader", project_context=self.authorized_context(page_number=2),
            suggestion_id=selected["id"], question=selected["text"],
        ))
        # O snapshot da sugestão também está amarrado à Base selecionada, não
        # apenas ao projeto que a contém.
        other_base_context = {
            **context,
            "analysis": {
                **context["analysis"],
                "selected": {
                    **context["analysis"]["selected"],
                    "id": "00000000-0000-0000-0000-000000000000",
                },
            },
        }
        self.assertIsNone(suggestion_scope_for_id(
            user=self.user, context_key="qualitative_reader", project_context=other_base_context,
            suggestion_id=selected["id"], question=selected["text"],
        ))
        db.session.add(QualitativeCode(analysis_id=self.analysis.id, name="Revisão posterior",
                                       created_by_user_id=self.user.id))
        db.session.commit()
        self.assertIsNone(suggestion_scope_for_id(
            user=self.user, context_key="qualitative_reader", project_context=context,
            suggestion_id=selected["id"], question=selected["text"],
        ))

    def test_manual_question_without_id_uses_textual_policy_fallback(self):
        context = {"page": {"current_page": 1, "document": {"id": self.document.id}}}
        self.assertIsNone(suggestion_scope_for_id(
            user=self.user, context_key="qualitative_reader", project_context=context,
            suggestion_id=None, question="Como raça é definida no corpus?",
        ))
        policy = tool_policy_for_question("Como raça é definida no corpus?", context_key="qualitative_reader",
                                          project_context=context)
        self.assertEqual(policy.allowed_tool_names, ("search_corpus",))

    def test_closed_scope_mapping_does_not_accept_a_browser_chosen_tool(self):
        page_context = {"page": {"current_page": 1, "document": {"id": self.document.id}}}
        self.assertEqual(tool_policy_for_suggestion_scope("current_page", project_context=page_context).allowed_tool_names,
                         ("read_current_page",))
        self.assertEqual(tool_policy_for_suggestion_scope("corpus", project_context=page_context).allowed_tool_names,
                         ("search_corpus",))
        self.assertIsNone(tool_policy_for_suggestion_scope("search_corpus", project_context=page_context))
        self.assertTrue({"current_page", "corpus"}.issubset(SUGGESTION_SCOPES))

    def test_invalid_structured_output_falls_back_to_the_existing_three_prompts(self):
        self.fixture_pages([["A discussão sobre raça oferece material suficiente para uma pergunta específica."]])
        provider = SuggestionProvider('{"questions":[{"text":"Isto não é uma pergunta","anchors":["raça"]}]}')
        self.app.extensions["assistant_ai_provider"] = provider

        response = self.client.post("/assistant/suggestions", json=self.request_payload(),
                                    headers={"X-CSRFToken": self.csrf})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["dynamic"])
        self.assertEqual(response.json["questions"], list(assistant_contextual_prompts("qualitative_reader")))
        self.assertEqual(len(provider.calls), 1)

    def test_dashboard_uses_authorized_structured_context_and_server_cache(self):
        provider = SuggestionProvider(functional_output(
            "Visão geral inicial do Análysis", "ferramentas autorizadas", "abrir ou criar projetos",
        ))
        self.app.extensions["assistant_ai_provider"] = provider

        response = self.client.post("/assistant/suggestions", json={"context": "home", "reference": {}},
                                    headers={"X-CSRFToken": self.csrf})
        cached = self.client.post("/assistant/suggestions", json={"context": "home", "reference": {}},
                                  headers={"X-CSRFToken": self.csrf})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["dynamic"])
        self.assertTrue(cached.json["cached"])
        self.assertEqual(len(response.json["suggestions"]), 3)
        self.assertEqual(len(provider.calls), 1)
        source = json.loads(provider.calls[0]["input_items"][0]["content"])
        self.assertEqual(source["grounding"], "structured_authorized_facts")
        self.assertTrue(source["current_state"]["authorized_access"]["access_active"])
        self.assertEqual(source["current_state"]["authorized_access"]["basis"], "current_access_grant")
        self.assertIn("qualitative_analysis", [item["id"] for item in source["current_state"]["authorized_access"]["tools"]])
        self.assertNotIn("email", json.dumps(source).casefold())
        self.assertNotIn("api_key", json.dumps(source).casefold())
        self.assertEqual(provider.calls[0]["tools"], [])

    def test_dashboard_rebuilds_available_tools_for_each_user(self):
        limited_user = create_user(name="Acesso limitado", email="acesso-limitado@example.org")
        db.session.add(UserToolOverride(
            user_id=limited_user.id, tool_id="qualitative_analysis", decision="deny",
        ))
        db.session.commit()
        provider = SuggestionProvider(functional_output(
            "Visão geral inicial do Análysis", "ferramentas autorizadas", "abrir ou criar projetos",
        ))
        self.app.extensions["assistant_ai_provider"] = provider

        full = contextual_suggestions(user=self.user, context_key="home", project_context=None)
        limited = contextual_suggestions(user=limited_user, context_key="home", project_context=None)

        self.assertTrue(full.dynamic)
        self.assertTrue(limited.dynamic)
        full_tools = {item["id"] for item in json.loads(provider.calls[0]["input_items"][0]["content"])
                      ["current_state"]["authorized_access"]["tools"]}
        limited_tools = {item["id"] for item in json.loads(provider.calls[1]["input_items"][0]["content"])
                         ["current_state"]["authorized_access"]["tools"]}
        self.assertIn("qualitative_analysis", full_tools)
        self.assertNotIn("qualitative_analysis", limited_tools)

    def test_project_listing_snapshot_invalidates_only_when_its_state_changes(self):
        provider = SuggestionProvider(functional_output(
            "Listagem e organização de projetos", "criar projeto", "organizar projetos",
        ))
        self.app.extensions["assistant_ai_provider"] = provider
        payload = {"context": "projects", "reference": {}}
        first = self.client.post("/assistant/suggestions", json=payload, headers={"X-CSRFToken": self.csrf})
        second = self.client.post("/assistant/suggestions", json=payload, headers={"X-CSRFToken": self.csrf})
        self.assertTrue(first.json["dynamic"])
        self.assertTrue(second.json["cached"])
        self.assertEqual(len(provider.calls), 1)

        db.session.add(Project(
            owner_user_id=self.user.id,
            name="Outro projeto", description="", scrape_type="qualitative", status="active",
        ))
        db.session.commit()
        refreshed = self.client.post("/assistant/suggestions", json=payload, headers={"X-CSRFToken": self.csrf})
        self.assertTrue(refreshed.json["dynamic"])
        self.assertFalse(refreshed.json["cached"])
        self.assertEqual(len(provider.calls), 2)
        state = json.loads(provider.calls[1]["input_items"][0]["content"])["current_state"]["projects"]
        self.assertEqual(state["total"], 2)

    def test_project_listing_supports_empty_and_populated_authorized_states(self):
        empty_user = create_user(name="Sem projetos", email="sem-projetos@example.org")
        provider = SuggestionProvider(functional_output(
            "Listagem e organização de projetos", "criar projeto", "organizar projetos",
        ))
        self.app.extensions["assistant_ai_provider"] = provider

        empty = contextual_suggestions(user=empty_user, context_key="projects", project_context=None)
        empty_state = json.loads(provider.calls[0]["input_items"][0]["content"])["current_state"]["projects"]
        self.assertTrue(empty.dynamic)
        self.assertEqual(empty_state["total"], 0)
        self.assertEqual(empty_state["by_status"], {})

        db.session.add(Project(
            owner_user_id=empty_user.id,
            name="Projeto autorizado", description="", scrape_type="systematic", status="active",
        ))
        db.session.commit()
        populated = contextual_suggestions(user=empty_user, context_key="projects", project_context=None)
        populated_state = json.loads(provider.calls[1]["input_items"][0]["content"])["current_state"]["projects"]
        self.assertTrue(populated.dynamic)
        self.assertFalse(populated.cached)
        self.assertEqual(populated_state["total"], 1)
        self.assertEqual(populated_state["by_status"], {"active": 1})

    def test_admin_ai_context_uses_persisted_settings_without_credential_state(self):
        admin = create_user(name="Admin", email="admin-suggestions@example.org", role="admin", plan="student")
        settings = db.session.get(AssistantAISettings, 1)
        settings.strategy = "fallback"
        settings.primary_provider = "gemini"
        settings.enabled_providers = ["gemini", "openai"]
        settings.fallback_order = ["openai"]
        db.session.commit()
        provider = SuggestionProvider(functional_output(
            "Configuração gerencial dos providers de IA", "revisar providers", "configurar modelos",
        ))
        self.app.extensions["assistant_ai_provider"] = provider

        outcome = contextual_suggestions(user=admin, context_key="admin_ai_settings", project_context=None)

        self.assertTrue(outcome.dynamic)
        source = json.loads(provider.calls[0]["input_items"][0]["content"])
        configuration = source["current_state"]["ai_configuration"]
        self.assertEqual(configuration["strategy"], "fallback")
        self.assertEqual(configuration["fallback_order"], ["openai"])
        serialized = json.dumps(source).casefold()
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("credential", serialized)

        cached = contextual_suggestions(user=admin, context_key="admin_ai_settings", project_context=None)
        self.assertTrue(cached.cached)
        settings.gemini_model = "modelo-de-teste-atualizado"
        db.session.commit()
        refreshed = contextual_suggestions(user=admin, context_key="admin_ai_settings", project_context=None)
        self.assertTrue(refreshed.dynamic)
        self.assertFalse(refreshed.cached)
        self.assertEqual(len(provider.calls), 2)

    def test_functional_suggestion_id_forces_server_chosen_platform_help(self):
        self.app.extensions["assistant_ai_provider"] = SuggestionProvider(functional_output(
            "Visão geral inicial do Análysis", "ferramentas autorizadas", "abrir ou criar projetos",
        ))
        selected = self.client.post("/assistant/suggestions", json={"context": "home", "reference": {}},
                                    headers={"X-CSRFToken": self.csrf}).json["suggestions"][0]
        provider = ToolLoopProvider(
            {"output": [{"type": "function_call", "name": "get_platform_help", "call_id": "platform-call",
                         "arguments": '{"topic":"plataforma"}'}]},
            {"output_text": "Resposta fundamentada na ajuda autorizada."},
        )
        self.app.extensions["assistant_ai_provider"] = provider

        response = self.client.post("/assistant/ask", json={
            "context": "home", "reference": {}, "question": selected["text"],
            "suggestion_id": selected["id"], "required_tool": "search_corpus",
        }, headers={"X-CSRFToken": self.csrf})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.generate_calls[0]["tool_mode"], TOOL_MODE_REQUIRED)
        self.assertEqual(provider.generate_calls[0]["allowed_tool_names"], ("get_platform_help",))
        self.assertEqual(len(provider.continue_calls), 1)

    def test_regular_user_cannot_generate_admin_configuration_suggestions(self):
        provider = SuggestionProvider(functional_output("área gerencial", "administrar recursos", "configurações"))
        self.app.extensions["assistant_ai_provider"] = provider

        response = self.client.post("/assistant/suggestions", json={"context": "admin_ai_settings", "reference": {}},
                                    headers={"X-CSRFToken": self.csrf})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["dynamic"])
        self.assertEqual(provider.calls, [])

    def test_unauthorized_or_malformed_page_context_keeps_fallback_and_never_leaks_corpus(self):
        provider = SuggestionProvider(dynamic_output())
        self.app.extensions["assistant_ai_provider"] = provider
        payload = self.request_payload()
        payload["page_context"] = {"document_id": "00000000-0000-0000-0000-000000000000", "current_page": 1}

        response = self.client.post("/assistant/suggestions", json=payload,
                                    headers={"X-CSRFToken": self.csrf})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["dynamic"])
        self.assertEqual(provider.calls, [])

    def test_suggestions_endpoint_requires_csrf_and_authentication(self):
        self.assertEqual(self.client.post("/assistant/suggestions", json=self.request_payload()).status_code, 400)
        self.assertEqual(self.client.post("/logout", data={"csrf_token": self.csrf}).status_code, 302)
        response = self.client.post("/assistant/suggestions", json={"context": "home"})
        # A proteção CSRF global pode rejeitar antes do login_required.
        self.assertIn(response.status_code, (302, 400, 401))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
