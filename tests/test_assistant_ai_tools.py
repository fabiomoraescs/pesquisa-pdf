"""Fatos de acesso usados pelo Assistente, sem respostas prontas."""

import unittest

from platform_core.assistant_ai_tools import AssistantToolExecutor
from platform_core.extensions import db
from platform_core.models import Plan, PlanTool
from platform_core.services import replace_grant
from platform_helpers import create_user, isolated_platform


class AssistantPlatformHelpAccessTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    @staticmethod
    def _help(user):
        executor = AssistantToolExecutor(user=user, context_key="home", project_context=None, provider=object())
        result, sources = executor.execute("get_platform_help", {"topic": "plataforma"})
        return result, sources

    def test_platform_help_reports_only_each_users_actual_tools(self):
        user_a = create_user("Pessoa A", "a@example.org", plan="student")
        db.session.add(Plan(id="single-tool", name="Uma ferramenta", active=True))
        db.session.add(PlanTool(plan_id="single-tool", tool_id="pdf_scraper"))
        db.session.flush()
        user_b = create_user("Pessoa B", "b@example.org", plan="student")
        replace_grant(user_b, "single-tool", "test", "active")
        db.session.commit()

        help_a, sources_a = self._help(user_a)
        help_b, sources_b = self._help(user_b)

        self.assertEqual(sources_a, [])
        self.assertEqual(sources_b, [])
        self.assertEqual(help_a["access"]["access_basis"], "current_grant")
        self.assertEqual(help_a["access"]["plan"]["id"], "student")
        self.assertEqual(
            {tool["id"] for tool in help_a["access"]["available_tools"]},
            {"pdf_scraper", "document_analysis"},
        )
        self.assertEqual(help_b["access"]["plan"]["id"], "single-tool")
        self.assertEqual(
            {tool["id"] for tool in help_b["access"]["available_tools"]},
            {"pdf_scraper"},
        )
        self.assertNotIn("document_analysis", {tool["id"] for tool in help_b["access"]["available_tools"]})

    def test_platform_help_marks_administrator_bypass_without_a_plan_fact(self):
        admin = create_user("Admin", "admin@example.org", role="admin", plan="institutional")
        help_result, _sources = self._help(admin)
        self.assertEqual(help_result["access"]["access_basis"], "administrator_bypass")
        self.assertNotIn("plan", help_result["access"])
        self.assertTrue(help_result["access"]["available_tools"])


if __name__ == "__main__":
    unittest.main()
