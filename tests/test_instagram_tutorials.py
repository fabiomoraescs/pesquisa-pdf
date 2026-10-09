"""Contrato pedagógico e de reutilização do Info do Instagram."""

from __future__ import annotations

import unittest
from pathlib import Path

from platform_core.platform_help import load_help_registry, tutorial_for


ROOT = Path(__file__).resolve().parents[1]


def _tutorial_text(key: str) -> str:
    tutorial = tutorial_for(key)
    parts = [tutorial["title"], tutorial["summary"]]
    for section in tutorial["sections"]:
        parts.extend((section["title"], *section["keywords"], *section["body"]))
        for item in section["items"]:
            parts.extend((item["name"], item["description"]))
    return "\n".join(parts)


class InstagramTutorialContentTests(unittest.TestCase):
    def setUp(self):
        load_help_registry.cache_clear()

    def tearDown(self):
        load_help_registry.cache_clear()

    def test_all_main_areas_have_a_structured_pedagogical_tutorial(self):
        expected_sections = {
            "instagram_dashboard": {
                "finalidade-e-periodo", "indicadores-e-cobertura", "indicadores-de-desempenho",
                "comparacoes", "exemplo-de-leitura", "conexao-com-o-fluxo",
            },
            "instagram_taxonomies": {
                "finalidade", "dimensoes-e-categorias", "estrutura-inicial", "exemplo-e-criterios",
                "limites-e-proximo-passo",
            },
            "instagram_classifications": {
                "finalidade-e-fluxo", "preencher-dimensoes", "historico-e-reimportacao", "exemplo-pratico",
                "revisao-e-proximo-passo",
            },
            "instagram_import": {
                "finalidade-e-fluxo", "campos-e-identidade", "datas-e-timezone", "metricas-e-ausencias",
                "validacao-e-confirmacao", "historico-e-reimportacao", "exemplo-e-proximo-passo",
            },
            "instagram_comments": {
                "finalidade", "historico-texto-e-respostas", "campos-analiticos", "classificador-local",
                "revisao-humana-e-historico", "exemplo-e-proximo-passo",
            },
            "instagram_insights": {
                "finalidade", "metodo-e-cobertura", "suporte-e-rastreabilidade", "associacao-nao-e-causalidade",
                "mediana-n-e-cobertura", "diferencas-e-suporte", "familias-de-regras-e-evidencias",
                "dados-insuficientes-e-periodo", "runs-versoes-e-proximo-passo",
            },
            "instagram_reports": {
                "escopo-e-snapshot", "selecao-automatica", "leitura-do-periodo", "planilha-xlsx",
                "causalidade-e-seguranca",
            },
        }
        for key, expected in expected_sections.items():
            with self.subTest(tutorial=key):
                tutorial = tutorial_for(key)
                section_ids = {section["id"] for section in tutorial["sections"]}
                self.assertTrue(expected.issubset(section_ids))
                self.assertGreaterEqual(len(tutorial["sections"]), 7)

    def test_comment_and_insight_tutorials_explain_their_interpretive_limits(self):
        comments = _tutorial_text("instagram_comments")
        for marker in (
            "local-rule-based", "comment-local-rules-v1", "Não é IA generativa", "confidence",
            "Rationale", "revisão humana", "Reprocessar comentários", "versão textual",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, comments)

        insights = _tutorial_text("instagram_insights")
        for marker in (
            "Associação não é causalidade", "mediana", "n =", "Cobertura", "p-valor",
            "pontos percentuais", "Ver evidências", "dados insuficientes", "engine_version", "rule_version",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, insights)

    def test_import_tutorial_distinguishes_dates_absence_and_historical_observations(self):
        tutorial = _tutorial_text("instagram_import")
        for marker in (
            "published_at", "observed_at", "period_start", "period_end", "ISO 8601", "America/Maceio",
            "0 somente", "prévia", "nova observação", "hash repetido", "possibilidade de ler comentários",
            "não durante este diagnóstico",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, tutorial)

    def test_main_and_contextual_pages_reuse_the_shared_tutorial_component(self):
        templates = {
            "dashboard.html": "instagram_dashboard",
            "taxonomies.html": "instagram_taxonomies",
            "classifications.html": "instagram_classifications",
            "classification_edit.html": "instagram_classifications",
            "import_new.html": "instagram_import",
            "import_preview.html": "instagram_import",
            "import_complete.html": "instagram_import",
            "comments.html": "instagram_comments",
            "comment_detail.html": "instagram_comments",
            "comment_classification_preview.html": "instagram_comments",
            "comment_import_new.html": "instagram_comments",
            "comment_import_preview.html": "instagram_comments",
            "comment_import_complete.html": "instagram_comments",
            "insights.html": "instagram_insights",
            "insight_detail.html": "instagram_insights",
            "reports.html": "instagram_reports",
            "report_form.html": "instagram_reports",
            "report_detail.html": "instagram_reports",
        }
        for name, key in templates.items():
            with self.subTest(template=name):
                source = (ROOT / "templates" / "platform" / "instagram" / name).read_text(encoding="utf-8")
                self.assertIn("tutorial_button", source)
                self.assertIn("tutorial_dialog", source)
                self.assertIn("js/platform_tutorial.js", source)
                self.assertIn(f"platform_help_for('{key}')", source)


if __name__ == "__main__":
    unittest.main()
