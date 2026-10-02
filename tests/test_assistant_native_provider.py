"""Regressões determinísticas da Análysis IA, sem API ou SDK externo."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from flask_migrate import downgrade, upgrade

from app import create_app

import test_qualitative_automatic as automatic_fixture
import test_qualitative_context as context_fixture

from platform_core.assistant_ai_manager import AIProviderManager, AISettingsValidationError
from platform_core.assistant_document_retrieval import _evidence_text
from platform_core.assistant_native_intent import native_question_plan
from platform_core.assistant_native_provider import (
    AnalysisNativeProvider,
    DEFAULT_ANALYSIS_NATIVE_MODEL,
    EvidenceAnalyzer,
    NativeAnswerBuilder,
)
from platform_core.assistant_suggestions import _native_textual_unit
from platform_core.assistant_project_context import resolve_project_context
from platform_core.assistant_suggestions import contextual_suggestions, reset_suggestion_caches
from platform_core.extensions import db


class AnalysisNativeProviderTests(unittest.TestCase):
    setUp = context_fixture.QualitativeContextTests.setUp
    tearDown = context_fixture.QualitativeContextTests.tearDown
    fixture_pages = automatic_fixture.QualitativeAutomaticTests.fixture_pages

    def _enable_native(self):
        manager = AIProviderManager()
        manager.save_configuration({
            "strategy": "single", "primary_provider": "analysis_native",
            "enabled_providers": ["analysis_native"], "fallback_order": [],
            "analysis_native_model": DEFAULT_ANALYSIS_NATIVE_MODEL,
            "gemini_model": "", "openai_model": "", "anthropic_model": "",
        })
        db.session.commit()

    def _ask(self, question, *, context="qualitative", page=False):
        payload = {
            "question": question, "context": context,
            "reference": {"project_id": self.project.id, "analysis_id": self.analysis.id},
        }
        if page:
            payload["page_context"] = {"document_id": self.document.id, "current_page": 1}
        return self.client.post("/assistant/ask", json=payload, headers={"X-CSRFToken": self.csrf})

    def test_native_platform_help_uses_local_tutorial_and_never_builds_remote_provider(self):
        self._enable_native()
        with patch("platform_core.assistant_ai_manager.GeminiProvider") as gemini, patch(
            "platform_core.assistant_ai_manager.OpenAIProvider"
        ) as openai:
            response = self._ask("Qual a diferença entre busca lexical e semântica?", context="home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["context_source"], "analysis_native")
        self.assertTrue(response.json["trace"])
        self.assertIn("Documentação", response.json["trace"][0]["label"])
        gemini.assert_not_called()
        openai.assert_not_called()

    def test_native_locates_fictional_corpus_evidence_and_preserves_document_page(self):
        self._enable_native()
        self.fixture_pages([["A raça é discutida no documento A."], ["Outro documento trata de raça e classe."]])
        response = self._ask("Onde aparece raça no corpus?", context="qualitative")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Foram localizadas evidências documentais", response.json["answer"])
        self.assertTrue(response.json["evidence"])
        self.assertTrue(all(item["page_number"] == 1 for item in response.json["evidence"]))
        self.assertTrue(all(item["url"].endswith("/paginas/1") for item in response.json["evidence"]))

    def test_native_current_page_is_extractive_and_not_external_knowledge(self):
        self._enable_native()
        self.fixture_pages([["A passagem documental afirma algo específico sobre raça."]])
        response = self._ask("Explique esta página.", context="qualitative_reader", page=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Evidência:", response.json["answer"])
        self.assertNotIn("Leitura documental", response.json["answer"])
        self.assertIn("algo específico", response.json["answer"])
        self.assertEqual(response.json["trace"][0]["source_type"], "document")

    def test_native_definition_synthesizes_before_evidence_and_source(self):
        self._enable_native()
        self.fixture_pages([["Raça é aqui compreendida como uma construção social produzida historicamente."]])
        response = self._ask("Como raça é definida nesta página?", context="qualitative_reader", page=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("apresenta Raça como uma construção social", response.json["answer"])
        self.assertIn("Evidência:", response.json["answer"])
        self.assertIn("Fonte: texto.pdf, página 1.", response.json["answer"])

    def test_native_does_not_treat_a_title_as_documentary_evidence(self):
        self._enable_native()
        title = "(In)disposições racializadas e ensino de Sociologia"
        self.fixture_pages([[title]])
        response = self._ask("Explique esta página.", context="qualitative_reader", page=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("não contém uma unidade textual completa", response.json["answer"])
        self.assertNotIn(title, response.json["answer"])

    def test_native_analyzer_covers_explicit_documentary_intents(self):
        cases = {
            "Onde aparece raça?": "LOCATE",
            "Como raça é definida?": "DEFINE",
            "Quais características aparecem?": "CHARACTERIZE",
            "Como os conceitos se relacionam?": "RELATE",
            "Compare os documentos.": "COMPARE",
            "Resuma este documento.": "SUMMARIZE",
            "Qual argumento o autor sustenta?": "ARGUMENT",
            "Que exemplo aparece?": "EXEMPLIFY",
            "Contextualize este trecho no corpus.": "CONTEXTUALIZE",
            "Quantos documentos há no corpus?": "COUNT",
            "Qual a distribuição das ocorrências?": "DISTRIBUTE",
            "Há coocorrência entre os termos?": "COOCCURRENCE",
        }
        for question, intent in cases.items():
            with self.subTest(question=question):
                self.assertEqual(native_question_plan(question).intent, intent)

    def test_native_question_keeps_server_selected_page_source_while_preserving_intent(self):
        plan = native_question_plan("Como raça é definida nesta página?", allowed_tool_names=("read_current_page",))
        self.assertEqual((plan.intent, plan.tool_name, plan.scope), ("DEFINE", "read_current_page", "current_page"))

    def test_documentary_evidence_keeps_previous_current_and_next_paragraphs_when_available(self):
        page = "Parágrafo anterior com ressalva relevante.\n\nRaça é aqui compreendida como construção social.\n\nParágrafo posterior com continuidade analítica."
        self.fixture_pages([[page]])
        start = page.index("Raça")
        text, truncated = _evidence_text(self.analysis, {
            "document_id": self.document.id,
            "page_number": 1,
            "start_offset": start,
            "end_offset": start + len("Raça"),
        }, {})
        self.assertIn("Parágrafo anterior", text)
        self.assertIn("Raça é aqui compreendida", text)
        self.assertIn("Parágrafo posterior", text)
        self.assertFalse(truncated)

    def test_native_suggestion_anchor_is_a_real_textual_unit(self):
        self.assertEqual(
            _native_textual_unit("Raça é aqui compreendida como uma construção social."),
            "Raça",
        )

    def test_native_comparison_does_not_claim_convergence_from_one_document(self):
        result = {
            "evidence": [{
                "document_name": "Documento A.pdf", "page_number": 2,
                "text": "O texto relaciona raça e desigualdade social.",
            }],
            "retrieval": {"methods": ["lexical"]},
            "limitations": [],
        }
        answer = NativeAnswerBuilder().from_findings(EvidenceAnalyzer().analyze(result, "COMPARE"))
        self.assertIn("Não há evidência recuperada em mais de um documento", answer)
        self.assertIn("Fonte: Documento A.pdf, página 2.", answer)

    def test_native_counts_the_authorized_corpus_with_the_counting_tool(self):
        self._enable_native()
        self.fixture_pages([["Documento A."], ["Documento B.", "Segunda página."]])
        response = self._ask("Quantos documentos há no corpus?", context="qualitative")
        self.assertEqual(response.status_code, 200)
        self.assertIn("2 documento(s) e 3 página(s)", response.json["answer"])

    def test_native_unsupported_capability_is_controlled_and_never_falls_back(self):
        self._enable_native()
        response = self._ask("Escreva um poema sobre a lua.", context="home")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json["reason_class"], "unsupported_capability")

    def test_native_dynamic_suggestions_are_three_contextual_and_local(self):
        self._enable_native()
        self.fixture_pages([["Raça é apresentada como construção social neste texto."]])
        reset_suggestion_caches()
        context = resolve_project_context(
            self.user, {"analysis_id": self.analysis.id},
            page_context={"document_id": self.document.id, "current_page": 1},
        )
        with patch("platform_core.assistant_ai_manager.GeminiProvider") as gemini:
            outcome = contextual_suggestions(user=self.user, context_key="qualitative_reader", project_context=context)
        self.assertTrue(outcome.dynamic)
        self.assertEqual(len(outcome.suggestions), 3)
        self.assertEqual([item.scope for item in outcome.suggestions], ["current_page", "current_page", "current_document"])
        self.assertTrue(all(item.id for item in outcome.suggestions))
        self.assertNotIn("Raça apresentada construção social", " ".join(outcome.questions))
        gemini.assert_not_called()

    def test_native_intent_keeps_required_tool_authoritative(self):
        plan = native_question_plan("Explique a lua.", allowed_tool_names=("get_platform_help",))
        self.assertEqual(plan.tool_name, "get_platform_help")
        self.assertEqual(plan.operation, "PLATFORM_HELP")

    def test_native_status_and_local_connection_need_no_credential(self):
        self._enable_native()
        manager = AIProviderManager()
        status = next(item for item in manager.provider_statuses() if item["id"] == "analysis_native")
        self.assertEqual(status["display_name"], "Análysis IA")
        self.assertEqual(status["provider_type"], "native")
        self.assertIsNone(status["credential_configured"])
        self.assertIn("no_external_knowledge", status["capabilities"])
        self.assertTrue(status["enabled"])
        provider = manager.build_provider("analysis_native")
        self.assertIsInstance(provider, AnalysisNativeProvider)
        self.assertEqual(provider.model, DEFAULT_ANALYSIS_NATIVE_MODEL)
        manager.test_provider_connection("analysis_native")

    def test_native_can_be_primary_or_technical_fallback(self):
        manager = AIProviderManager()
        settings = manager.save_configuration({
            "strategy": "fallback", "primary_provider": "gemini",
            "enabled_providers": ["gemini", "analysis_native"], "fallback_order": ["analysis_native"],
            "analysis_native_model": "attempted-override", "gemini_model": "gemini-test",
            "openai_model": "", "anthropic_model": "",
        })
        self.assertEqual(settings.analysis_native_model, DEFAULT_ANALYSIS_NATIVE_MODEL)
        self.assertEqual(manager.provider_ids_for_turn(), ["gemini", "analysis_native"])


class DocChatPreparationTests(unittest.TestCase):
    setUp = context_fixture.QualitativeContextTests.setUp
    tearDown = context_fixture.QualitativeContextTests.tearDown

    def test_docchat_is_nullable_and_rejects_native_or_unknown_provider(self):
        manager = AIProviderManager()
        self.assertEqual(manager.get_docchat_ai_config(), {
            "provider": None, "model": None, "status": "not_configured",
        })
        for provider in ("analysis_native", "unknown"):
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(AISettingsValidationError, "elegível"):
                    manager.save_docchat_configuration({"docchat_provider": provider, "docchat_model": "x"})

    def test_docchat_configuration_is_independent_and_does_not_instantiate_provider(self):
        manager = AIProviderManager()
        with patch.object(manager, "build_provider") as build:
            settings = manager.save_docchat_configuration({"docchat_provider": "gemini", "docchat_model": "gemini-future"})
            self.assertEqual(settings.docchat_provider, "gemini")
            self.assertEqual(manager.get_docchat_ai_config()["status"], "configured")
        build.assert_not_called()


class NativeAndDocChatMigrationTests(unittest.TestCase):
    previous = "b8c4d3e2f1a0"
    revision = "c3d9e8a7f2b4"
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")

    def test_child_migration_adds_and_removes_native_and_docchat_columns(self):
        with tempfile.TemporaryDirectory(prefix="analysis-native-migration-") as directory:
            app = create_app({
                "TESTING": True, "SECRET_KEY": "native-migration-test-only",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{Path(directory).as_posix()}/db.sqlite",
                "PLATFORM_DATA_DIR": directory,
            })
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.previous)
                upgrade(directory=self.migrations, revision=self.revision)
                with db.engine.connect() as connection:
                    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(assistant_ai_settings)")}
                    self.assertTrue({"analysis_native_model", "docchat_provider", "docchat_model"}.issubset(columns))
                    row = connection.exec_driver_sql(
                        "SELECT analysis_native_model, docchat_provider, docchat_model FROM assistant_ai_settings"
                    ).one()
                    self.assertEqual(row, (DEFAULT_ANALYSIS_NATIVE_MODEL, None, None))
                downgrade(directory=self.migrations, revision=self.previous)
                with db.engine.connect() as connection:
                    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(assistant_ai_settings)")}
                    self.assertFalse({"analysis_native_model", "docchat_provider", "docchat_model"} & columns)
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
