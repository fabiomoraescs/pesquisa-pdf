"""Contrato puro do catálogo central e da futura sidebar dinâmica."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from platform_core.tool_catalog import (
    ANALYTICS_INSTAGRAM_TOOL,
    IMPLEMENTATION_STATUSES,
    TOOL_DEFINITIONS,
    TOOL_GROUPS,
    administrative_catalog_entries,
    persistent_tool_seed_rows,
    standalone_tools_in_display_order,
    tools_in_display_order,
    validate_visibility_target,
    visible_navigation_groups,
    visible_standalone_tools,
)


class ToolCatalogDefinitionTests(unittest.TestCase):
    def test_catalog_ids_statuses_and_orders_are_stable(self):
        self.assertEqual(len({group.group_id for group in TOOL_GROUPS}), len(TOOL_GROUPS))
        self.assertEqual(len({tool.tool_id for tool in TOOL_DEFINITIONS}), len(TOOL_DEFINITIONS))
        self.assertEqual(
            [(tool.group_id, tool.tool_id) for tool in tools_in_display_order()],
            [
                ("research_quali", "pdf_scraper"),
                ("research_quali", "document_analysis"),
                ("research_quali", "qualitative_analysis"),
                ("research_quanti", "quantitative_analysis"),
                ("analytics", ANALYTICS_INSTAGRAM_TOOL),
                ("analytics", "ojs_seo"),
                ("analytics", "ojs_analytics"),
                (None, "bibliographic_search"),
            ],
        )
        self.assertTrue(all(tool.implementation_status in IMPLEMENTATION_STATUSES for tool in TOOL_DEFINITIONS))

    def test_available_tools_reference_real_persisted_tools_and_only_they_have_routes(self):
        available = [tool for tool in TOOL_DEFINITIONS if tool.implementation_status == "available"]
        unavailable = [tool for tool in TOOL_DEFINITIONS if tool.implementation_status != "available"]
        self.assertEqual(
            persistent_tool_seed_rows(),
            (
                ("pdf_scraper", "Raspagem por termos", "/"),
                ("document_analysis", "Raspagem estruturada", "/analise-documental"),
                ("qualitative_analysis", "Análise Documental", "/analise-qualitativa"),
                (ANALYTICS_INSTAGRAM_TOOL, "Instagram", "/analytics/instagram"),
            ),
        )
        self.assertTrue(all(tool.tool_code and tool.route for tool in available))
        self.assertTrue(all(tool.tool_code is None and tool.route is None for tool in unavailable))
        self.assertEqual(
            {tool.tool_id for tool in unavailable},
            {"quantitative_analysis", "ojs_seo", "ojs_analytics", "bibliographic_search"},
        )
        self.assertEqual([tool.tool_id for tool in standalone_tools_in_display_order()], ["bibliographic_search"])

    def test_catalog_definition_is_immutable(self):
        with self.assertRaises(Exception):
            TOOL_DEFINITIONS[0].name = "Outro nome"  # type: ignore[misc]
        self.assertEqual(TOOL_DEFINITIONS[0].name, "Raspagem por termos")

    def test_visibility_targets_accept_only_catalog_group_or_tool_ids(self):
        validate_visibility_target("group", "analytics")
        validate_visibility_target("tool", ANALYTICS_INSTAGRAM_TOOL)
        with self.assertRaisesRegex(ValueError, "inexistente"):
            validate_visibility_target("group", "unknown")
        with self.assertRaisesRegex(ValueError, "inválido"):
            validate_visibility_target("other", "analytics")


class VisibilityPolicyTests(unittest.TestCase):
    def group_ids(self, **kwargs):
        return [item.group.group_id for item in visible_navigation_groups(**kwargs)]

    def tool_ids(self, **kwargs):
        return [tool.tool_id for group in visible_navigation_groups(**kwargs) for tool in group.tools]

    def test_default_keeps_visible_future_entries_in_official_order_without_making_them_operational(self):
        self.assertEqual(self.group_ids(), ["research_quali", "research_quanti", "analytics"])
        self.assertEqual(
            self.tool_ids(),
            [
                "pdf_scraper", "document_analysis", "qualitative_analysis",
                "quantitative_analysis", ANALYTICS_INSTAGRAM_TOOL, "ojs_seo", "ojs_analytics",
            ],
        )
        self.assertEqual([tool.tool_id for tool in visible_standalone_tools()], ["bibliographic_search"])

    def test_hidden_group_removes_all_its_tools_without_mutating_individual_preferences(self):
        tools = {"pdf_scraper": False, "document_analysis": True}
        result = visible_navigation_groups(
            group_visibility={"research_quali": False}, tool_visibility=tools,
        )
        self.assertEqual([item.group.group_id for item in result], ["research_quanti", "analytics"])
        self.assertEqual(tools, {"pdf_scraper": False, "document_analysis": True})

        reactivated = visible_navigation_groups(
            group_visibility={"research_quali": True}, tool_visibility=tools,
        )
        quali = next(item for item in reactivated if item.group.group_id == "research_quali")
        self.assertEqual([tool.tool_id for tool in quali.tools], ["document_analysis", "qualitative_analysis"])

    def test_hidden_individual_tool_and_empty_group_disappear_completely(self):
        partial = visible_navigation_groups(tool_visibility={"document_analysis": False})
        quali = next(item for item in partial if item.group.group_id == "research_quali")
        self.assertEqual([tool.tool_id for tool in quali.tools], ["pdf_scraper", "qualitative_analysis"])

        empty = visible_navigation_groups(tool_visibility={
            "pdf_scraper": False,
            "document_analysis": False,
            "qualitative_analysis": False,
        })
        self.assertEqual([item.group.group_id for item in empty], ["research_quanti", "analytics"])

    def test_future_tools_are_render_candidates_but_remain_independent_from_availability(self):
        result = visible_navigation_groups(
            group_visibility={"research_quanti": True},
            tool_visibility={"quantitative_analysis": True, "ojs_seo": True},
        )
        self.assertEqual([item.group.group_id for item in result], ["research_quali", "research_quanti", "analytics"])
        self.assertIn("quantitative_analysis", [tool.tool_id for group in result for tool in group.tools])
        self.assertIn("ojs_seo", [tool.tool_id for group in result for tool in group.tools])

        self.assertEqual(visible_standalone_tools(tool_visibility={"bibliographic_search": False}), ())

    def test_multiple_hidden_items_keep_the_official_order_when_reactivated(self):
        hidden = visible_navigation_groups(
            group_visibility={"analytics": False},
            tool_visibility={"pdf_scraper": False},
        )
        self.assertEqual([item.group.group_id for item in hidden], ["research_quali", "research_quanti"])
        self.assertEqual([tool.tool_id for tool in hidden[0].tools], ["document_analysis", "qualitative_analysis"])
        self.assertEqual(
            self.tool_ids(),
            [
                "pdf_scraper", "document_analysis", "qualitative_analysis",
                "quantitative_analysis", ANALYTICS_INSTAGRAM_TOOL, "ojs_seo", "ojs_analytics",
            ],
        )

    def test_invalid_or_unknown_visibility_input_fails_closed_in_the_policy_layer(self):
        with self.assertRaisesRegex(ValueError, "grupo desconhecido"):
            visible_navigation_groups(group_visibility={"unknown": False})
        with self.assertRaisesRegex(ValueError, "deve ser booleana"):
            visible_navigation_groups(tool_visibility={"pdf_scraper": "false"})  # type: ignore[arg-type]


class CatalogCompatibilityTests(unittest.TestCase):
    def test_seed_rows_reference_only_implemented_tools(self):
        expected_codes = {code for code, _name, _route in persistent_tool_seed_rows()}
        self.assertEqual(expected_codes, {"pdf_scraper", "document_analysis", "qualitative_analysis", ANALYTICS_INSTAGRAM_TOOL})
        self.assertFalse(expected_codes & {"quantitative_analysis", "ojs_seo", "ojs_analytics", "bibliographic_search"})

    def test_administrative_adapter_preserves_existing_catalog_entries_and_order(self):
        persisted = (
            SimpleNamespace(id="pdf_scraper", name="Nome não canônico", active=True),
            SimpleNamespace(id="document_analysis", name="Nome não canônico", active=True),
            SimpleNamespace(id="qualitative_analysis", name="Nome não canônico", active=False),
            SimpleNamespace(id=ANALYTICS_INSTAGRAM_TOOL, name="Nome não canônico", active=True),
        )
        rows = administrative_catalog_entries(persisted)
        self.assertEqual(
            [row["name"] for row in rows[:8]],
            [
                "Raspagem por termos", "Raspagem estruturada", "Análise Documental",
                "Pesquisa Quanti", "Instagram", "OJS SEO", "OJS Analytics", "Busca Bibliográfica",
            ],
        )
        self.assertTrue(all(row["implemented"] is False for row in rows if row["id"] in {
            "quantitative_analysis", "ojs_seo", "ojs_analytics", "bibliographic_search",
        }))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
