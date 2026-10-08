"""Fase 8.2: recomendações determinísticas derivadas exclusivamente de achados."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, inspect, select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_lifecycle import delete_instagram_project_permanently
from platform_core.instagram_recommendations import (
    INSTAGRAM_RECOMMENDATIONS_VERSION,
    build_recommendation_drafts,
    generate_recommendations,
)
from platform_core.models import (
    AnalyticsProject,
    InstagramInsightFinding,
    InstagramInsightRun,
    InstagramRecommendation,
    InstagramRecommendationFinding,
    InstagramRecommendationRun,
    PlanTool,
)
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


UTC = timezone.utc
WHEN = datetime(2026, 10, 7, 15, tzinfo=UTC)


class InstagramRecommendationsTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.user = create_user()
        self.other = create_user("Outra pessoa", "outra@example.org")
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        self.project = AnalyticsProject(owner_user_id=self.user.id, name="Recomendações")
        self.other_project = AnalyticsProject(owner_user_id=self.other.id, name="Outro projeto")
        db.session.add_all((self.project, self.other_project))
        db.session.flush()
        self.insight_run = InstagramInsightRun(
            analytics_project_id=self.project.id,
            period_key="30d",
            engine_version="instagram-insights-v1",
            parameters_json={}, publication_count=12, comment_count=12,
            finding_count=0, generated_at=WHEN,
        )
        db.session.add(self.insight_run)
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def finding(self, *, rule_id="editorial_metric_difference", group_label="Objetivo comunicacional: Educar",
                metric_key="saves", absolute=40.0, support="consistent", group_n=8,
                comparison_n=8, coverage=.95, title=None) -> InstagramInsightFinding:
        item = InstagramInsightFinding(
            instagram_insight_run_id=self.insight_run.id,
            rule_id=rule_id,
            rule_version=f"{rule_id}-v1",
            engine_version="instagram-insights-v1",
            kind="performance" if "metric" in rule_id else "reception",
            title=title or f"{group_label}: padrão de teste",
            summary="Achado descritivo de teste, sem causalidade.",
            metric_key=metric_key,
            group_label=group_label,
            comparison_label="demais conteúdos",
            group_value=100.0,
            comparison_value=60.0,
            absolute_difference=absolute,
            relative_difference=.66,
            difference_unit="value",
            group_n=group_n,
            comparison_n=comparison_n,
            coverage=coverage,
            support_level=support,
            support_explanation="Teste",
            details_json={"metric_label": {"saves": "Salvamentos", "shares": "Compartilhamentos"}.get(metric_key, metric_key)},
        )
        db.session.add(item)
        db.session.flush()
        self.insight_run.finding_count += 1
        return item

    def test_superior_performance_converges_and_is_idempotent(self):
        first = self.finding(metric_key="saves", absolute=40)
        second = self.finding(metric_key="shares", absolute=25)
        db.session.commit()

        recommendation_run = generate_recommendations(self.project, self.insight_run)
        recommendations = db.session.scalars(select(InstagramRecommendation)).all()
        self.assertEqual(recommendation_run.engine_version, INSTAGRAM_RECOMMENDATIONS_VERSION)
        self.assertEqual(len(recommendations), 1)
        recommendation = recommendations[0]
        self.assertEqual(recommendation.recommendation_type, "content_mix")
        self.assertIn("Considere testar", recommendation.summary)
        self.assertIn("não demonstram causalidade", recommendation.limitations_json[0])
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(InstagramRecommendationFinding)), 2,
        )
        self.assertEqual({link.instagram_insight_finding_id for link in recommendation.finding_links}, {first.id, second.id})

        repeated = generate_recommendations(self.project, self.insight_run)
        self.assertEqual(repeated.id, recommendation_run.id)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationRun)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendation)), 1)

    def test_new_insight_run_creates_new_historical_recommendation_run(self):
        self.finding()
        db.session.commit()
        first = generate_recommendations(self.project, self.insight_run)
        newer = InstagramInsightRun(
            analytics_project_id=self.project.id,
            period_key="30d",
            engine_version="instagram-insights-v1",
            parameters_json={}, publication_count=12, comment_count=12,
            finding_count=1, generated_at=datetime(2026, 10, 8, 15, tzinfo=UTC),
        )
        db.session.add(newer)
        db.session.flush()
        newer_finding = InstagramInsightFinding(
            instagram_insight_run_id=newer.id, rule_id="editorial_metric_difference",
            rule_version="editorial_metric_difference-v1", engine_version="instagram-insights-v1",
            kind="performance", title="Objetivo comunicacional: Educar: padrão novo",
            summary="Achado histórico novo.", metric_key="saves", group_label="Objetivo comunicacional: Educar",
            comparison_label="demais conteúdos", group_value=120.0, comparison_value=60.0,
            absolute_difference=60.0, relative_difference=1.0, difference_unit="value",
            group_n=8, comparison_n=8, coverage=.95, support_level="consistent",
            support_explanation="Teste", details_json={"metric_label": "Salvamentos"},
        )
        db.session.add(newer_finding)
        db.session.commit()
        second = generate_recommendations(self.project, newer)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationRun)), 2)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendation)), 2)

    def test_persistence_error_rolls_back_run_recommendations_and_links(self):
        self.finding()
        db.session.commit()
        with patch("platform_core.instagram_recommendations.db.session.commit", side_effect=RuntimeError("controlled")):
            with self.assertRaisesRegex(Exception, "nenhuma execução parcial"):
                generate_recommendations(self.project, self.insight_run)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationRun)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendation)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationFinding)), 0)

    def test_inferior_and_mixed_performance_are_cautious_and_not_contradictory(self):
        inferior = self.finding(metric_key="reach", absolute=-50.0, support="moderate")
        db.session.commit()
        drafts, _ = build_recommendation_drafts((inferior,))
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].rule_id, "performance_inferior")
        self.assertIn("Vale avaliar", drafts[0].summary)

        opposite = self.finding(metric_key="saves", absolute=20.0, support="moderate")
        db.session.commit()
        drafts, _ = build_recommendation_drafts((inferior, opposite))
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].rule_id, "performance_mixed_signal")
        self.assertIn("sinais distintos", drafts[0].summary)

    def test_exploratory_support_generates_measurement_quality_not_editorial_advice(self):
        exploratory = self.finding(support="exploratory", group_n=3, comparison_n=3, coverage=.70)
        db.session.commit()
        drafts, _ = build_recommendation_drafts((exploratory,))
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].recommendation_type, "measurement_quality")
        self.assertNotIn("maior presença", drafts[0].summary)

        insufficient_n = self.finding(support="exploratory", group_n=2, comparison_n=2, coverage=.95)
        db.session.commit()
        drafts, _ = build_recommendation_drafts((insufficient_n,))
        self.assertEqual(drafts, ())

    def test_reception_rules_generate_cautious_human_oriented_recommendations(self):
        findings = (
            self.finding(rule_id="comment_question_concentration", group_label="Perguntas", metric_key="comment_proportion"),
            self.finding(rule_id="comment_response_need", group_label="Necessidade de resposta", metric_key="comment_proportion"),
            self.finding(rule_id="comment_sentiment_concentration", group_label="Sentimento negativo", metric_key="comment_proportion"),
            self.finding(rule_id="comment_risk_concentration", group_label="Risco médio ou alto", metric_key="comment_proportion"),
            self.finding(rule_id="comment_topic_recurrence", group_label="acesso", metric_key="topic_recurrence"),
        )
        db.session.commit()
        drafts, _ = build_recommendation_drafts(findings)
        types = {item.recommendation_type for item in drafts}
        self.assertTrue({"information_clarity", "community_response", "risk_review"}.issubset(types))
        risk = next(item for item in drafts if item.recommendation_type == "risk_review")
        self.assertEqual(risk.priority, "high")
        self.assertIn("revisar", risk.summary)

    def test_routes_detail_csrf_ownership_archived_and_empty_state(self):
        base = f"/analytics/instagram/projects/{self.project.id}/insights"
        page = self.client.get(base)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Gere recomendações a partir", page.get_data(as_text=True))

        finding = self.finding()
        db.session.commit()
        page = self.client.get(base)
        html = page.get_data(as_text=True)
        self.assertIn("Gerar recomendações", html)
        self.assertIn('platform-action-button', html)
        self.assertIn('aria-label="Ver evidências"', html)
        self.assertIn('title="Ver evidências"', html)
        self.assertNotIn(">Ver evidências</a>", html)
        route = base + "/recommendations/generate"
        self.assertEqual(self.client.post(route, data={"insight_run_id": self.insight_run.id}).status_code, 400)
        generated = self.client.post(route, data={
            "csrf_token": csrf_from(page), "insight_run_id": self.insight_run.id,
        })
        self.assertEqual(generated.status_code, 302)
        self.assertIn("#instagram-recommendations", generated.headers["Location"])
        recommendation = db.session.scalar(select(InstagramRecommendation))
        page = self.client.get(base)
        html = page.get_data(as_text=True)
        self.assertIn(f'href="{base}/recommendations/{recommendation.id}"', html)
        self.assertIn('aria-label="Ver recomendação"', html)
        self.assertIn('title="Ver recomendação"', html)
        self.assertNotIn(">Ver recomendação</a>", html)
        detail = self.client.get(base + f"/recommendations/{recommendation.id}")
        detail_html = detail.get_data(as_text=True)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Achados de origem", detail_html)
        self.assertIn(finding.title, detail_html)
        self.assertIn(f'href="{base}/{finding.id}"', detail_html)
        self.assertIn('aria-label="Ver evidências"', detail_html)
        self.assertIn('title="Ver evidências"', detail_html)
        self.assertNotIn(">Ver evidências</a>", detail_html)

        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.other.email)
        self.assertEqual(self.client.get(base).status_code, 404)
        self.assertEqual(self.client.get(base + f"/recommendations/{recommendation.id}").status_code, 404)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client)
        self.project.status = "archived"
        self.project.archived_at = WHEN
        db.session.commit()
        archived = self.client.get(base)
        self.assertEqual(archived.status_code, 200)
        self.assertNotIn("Gerar recomendações", archived.get_data(as_text=True))
        self.assertEqual(self.client.post(route, data={
            "csrf_token": csrf_from(archived), "insight_run_id": self.insight_run.id,
        }).status_code, 404)

    def test_project_without_insight_run_only_offers_insight_generation(self):
        empty = AnalyticsProject(owner_user_id=self.user.id, name="Sem insights")
        db.session.add(empty)
        db.session.commit()
        page = self.client.get(f"/analytics/instagram/projects/{empty.id}/insights")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Gere primeiro os Insights do projeto", html)
        self.assertNotIn("insights/recommendations/generate", html)

    def test_permanent_deletion_removes_recommendation_chain_before_insights(self):
        self.finding()
        db.session.commit()
        generate_recommendations(self.project, self.insight_run)
        self.project.status = "archived"
        self.project.archived_at = WHEN
        db.session.commit()
        delete_instagram_project_permanently(self.project, self.user, "deletar")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationFinding)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendation)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationRun)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightRun)), 0)


class InstagramRecommendationsMigrationTests(unittest.TestCase):
    def test_recommendations_migration_is_reversible_child_of_insights_head(self):
        from app import create_app

        migrations = str(Path(__file__).resolve().parents[1] / "migrations")
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-recommendations-migration-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "recommendations-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=migrations, revision="a8b9c0d1e2f3")
                self.assertNotIn("instagram_recommendation_runs", inspect(db.engine).get_table_names())
                upgrade(directory=migrations, revision="b9cad1e2f3a4")
                inspector = inspect(db.engine)
                expected = {
                    "instagram_recommendation_runs", "instagram_recommendations",
                    "instagram_recommendation_findings",
                }
                self.assertTrue(expected.issubset(inspector.get_table_names()))
                self.assertIn(
                    "uq_instagram_recommendation_run_insight_engine",
                    {item["name"] for item in inspector.get_unique_constraints("instagram_recommendation_runs")},
                )
                downgrade(directory=migrations, revision="a8b9c0d1e2f3")
                self.assertTrue(expected.isdisjoint(inspect(db.engine).get_table_names()))
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
