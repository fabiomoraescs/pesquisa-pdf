"""Política determinística de fontes obrigatórias do Assistente Análysis."""

import unittest

from platform_core.assistant_context import CONTEXTUAL_PROMPTS
from platform_core.assistant_tool_policy import TOOL_MODE_AUTO, TOOL_MODE_REQUIRED, tool_policy_for_question


PAGE_CONTEXT = {"page": {"current_page": 1, "document": {"id": "documento-atual"}}}


class AssistantToolPolicyTests(unittest.TestCase):
    def policy(self, question, context="fallback", project_context=None):
        return tool_policy_for_question(question, context_key=context, project_context=project_context)

    def test_every_contextual_onboarding_prompt_has_a_server_side_required_source(self):
        for context, prompts in CONTEXTUAL_PROMPTS.items():
            for prompt in prompts:
                with self.subTest(context=context, prompt=prompt):
                    policy = self.policy(prompt, context)
                    self.assertEqual(policy.tool_mode, TOOL_MODE_REQUIRED)
                    self.assertEqual(len(policy.allowed_tool_names), 1)

    def test_dashboard_onboarding_requires_platform_help(self):
        for prompt in CONTEXTUAL_PROMPTS["home"]:
            with self.subTest(prompt=prompt):
                policy = self.policy(prompt, "home")
                self.assertEqual(policy.category, "platform_help")
                self.assertEqual(policy.allowed_tool_names, ("get_platform_help",))

    def test_internal_question_categories_require_their_authorized_source(self):
        cases = (
            ("Qual a diferença entre busca lexical e semântica?", "home", None, "get_platform_help", False),
            ("Como funciona esta tela?", "fallback", None, "get_current_ui_context", False),
            ("O que já fiz neste projeto?", "project", {}, "get_project_context", False),
            ("Explique esta página.", "qualitative_reader", PAGE_CONTEXT, "read_current_page", True),
            ("Como raça é definida no corpus?", "qualitative", PAGE_CONTEXT, "search_corpus", True),
            ("Resuma o capítulo 1.", "qualitative_reader", PAGE_CONTEXT, "summarize_document", True),
            ("Faça uma síntese do corpus.", "qualitative", PAGE_CONTEXT, "summarize_corpus", True),
            ("Que tensão aparece nesta passagem sobre raça?", "qualitative_reader", PAGE_CONTEXT, "read_current_page", True),
            ("Como raça se relaciona com os demais documentos?", "qualitative_reader", PAGE_CONTEXT, "search_corpus", True),
        )
        for question, context, project_context, expected_tool, corpus_only in cases:
            with self.subTest(question=question):
                policy = self.policy(question, context, project_context)
                self.assertEqual(policy.tool_mode, TOOL_MODE_REQUIRED)
                self.assertEqual(policy.allowed_tool_names, (expected_tool,))
                self.assertEqual(policy.corpus_only, corpus_only)

    def test_unrelated_general_question_remains_auto(self):
        policy = self.policy("Como você organiza uma rotina de leitura?", "fallback")
        self.assertEqual(policy.tool_mode, TOOL_MODE_AUTO)
        self.assertEqual(policy.allowed_tool_names, ())


if __name__ == "__main__":
    unittest.main()
