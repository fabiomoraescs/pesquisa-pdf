"""Dashboard Instagram: última observação, cobertura, filtros e autorização."""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_dashboard import build_instagram_dashboard
from platform_core.models import (
    AnalyticsProject, AnalyticsRun, InstagramAccount, InstagramAccountSnapshot,
    InstagramMedia, InstagramMediaObservation, PlanTool,
)
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


UTC = timezone.utc
REFERENCE = datetime(2026, 10, 9, 13, tzinfo=UTC)  # 10:00 em America/Maceio


class InstagramDashboardTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.app.config["PRESENTATION_TIMEZONE"] = "America/Maceio"
        self.client = self.app.test_client()
        self.user = create_user()
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        self.project = AnalyticsProject(owner_user_id=self.user.id, name="Projeto da revista")
        db.session.add(self.project)
        db.session.flush()
        self.account = InstagramAccount(
            analytics_project_id=self.project.id, username="@revista_exemplo",
            username_normalized="revista_exemplo",
        )
        db.session.add(self.account)
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def run_at(self, when: datetime = REFERENCE, *, status: str = "completed") -> AnalyticsRun:
        run = AnalyticsRun(
            analytics_project_id=self.project.id, status=status,
            started_at=when, completed_at=when if status == "completed" else None,
        )
        db.session.add(run)
        db.session.flush()
        return run

    def media(self, external_id: str, *, days_before: int = 1, caption: str = "Publicação de teste",
              media_type: str = "REEL") -> InstagramMedia:
        media = InstagramMedia(
            instagram_account_id=self.account.id, external_id=external_id,
            published_at=REFERENCE - timedelta(days=days_before),
            caption_original=caption,
            media_type_raw=media_type, permalink=f"https://instagram.example/p/{external_id}/",
        )
        db.session.add(media)
        db.session.flush()
        return media

    def observe(self, run: AnalyticsRun, media: InstagramMedia, *, when: datetime,
                reach: int | None = None, interactions: int | None = None,
                plays: int | None = None, observation_id: str | None = None, **metrics):
        observation = InstagramMediaObservation(
            instagram_media_id=media.id, analytics_run_id=run.id, observed_at=when,
            reach=reach, interactions=interactions, plays=plays,
            **metrics,
        )
        if observation_id is not None:
            observation.id = observation_id
        db.session.add(observation)
        db.session.flush()
        return observation

    def summary(self, period="all", *, sort="recent"):
        db.session.commit()
        return build_instagram_dashboard(self.project, period, "America/Maceio", sort=sort)

    @staticmethod
    def metric(summary, field):
        return next(metric for metric in summary.metrics if metric.field == field)

    def test_latest_observation_replaces_old_value_for_one_and_two_media(self):
        first = self.run_at(REFERENCE - timedelta(days=7))
        second = self.run_at()
        post_a = self.media("post-a")
        self.observe(first, post_a, when=REFERENCE - timedelta(days=7), reach=1000)
        newest_a = self.observe(second, post_a, when=REFERENCE, reach=1500)
        summary = self.summary()
        self.assertEqual(summary.publication_count, 1)
        self.assertEqual(self.metric(summary, "reach").value, 1500)
        self.assertEqual(summary.rows[0].observation.id, newest_a.id)
        post_b = self.media("post-b")
        self.observe(first, post_b, when=REFERENCE - timedelta(days=7), reach=800)
        self.observe(second, post_b, when=REFERENCE, reach=1200)
        summary = self.summary()
        self.assertEqual(summary.publication_count, 2)
        self.assertEqual(self.metric(summary, "reach").value, 2700)
        self.assertEqual(sorted(summary.chart_data["reach"]["values"]), [1200, 1500])

    def test_ten_media_with_twenty_observations_still_count_as_ten(self):
        old_run = self.run_at(REFERENCE - timedelta(days=7))
        new_run = self.run_at()
        for index in range(10):
            post = self.media(f"post-{index:03d}")
            self.observe(old_run, post, when=REFERENCE - timedelta(days=7), reach=100 + index)
            self.observe(new_run, post, when=REFERENCE, reach=200 + index)
        summary = self.summary()
        self.assertEqual(summary.publication_count, 10)
        self.assertEqual(self.metric(summary, "reach").value, sum(range(200, 210)))
        self.assertEqual(len(summary.rows), 10)
        self.assertEqual(len(summary.chart_data["reach"]["values"]), 10)
        self.assertEqual(len(summary.ranking), 5)

    def test_null_is_unknown_and_zero_is_known(self):
        run = self.run_at()
        for index, value in enumerate((1000, None, 500)):
            self.observe(run, self.media(f"partial-{index}"), when=REFERENCE, reach=value,
                         plays=0 if index == 0 else None)
        summary = self.summary()
        reach = self.metric(summary, "reach")
        self.assertEqual((reach.value, reach.known, reach.publications, reach.partial), (1500, 2, 3, True))
        self.assertEqual((self.metric(summary, "plays").value, self.metric(summary, "plays").known), (0, 1))
        self.assertIsNone(self.metric(summary, "impressions").value)
        self.assertEqual(self.metric(summary, "impressions").display, "—")
        extra = self.media("zero")
        self.observe(run, extra, when=REFERENCE, reach=0)
        zero = self.metric(self.summary(), "reach")
        self.assertEqual((zero.value, zero.known, zero.publications), (1500, 3, 4))

    def test_filters_use_publication_date_relative_to_latest_observation(self):
        run = self.run_at()
        for days in (2, 10, 50, 200):
            self.observe(run, self.media(f"post-{days}", days_before=days), when=REFERENCE, reach=days)
        self.assertEqual([self.summary(period).publication_count for period in ("7d", "30d", "90d", "all")],
                         [1, 2, 3, 4])
        self.assertEqual(self.summary("30d").reference_at.replace(tzinfo=UTC), REFERENCE)
        with self.assertRaises(ValueError):
            self.summary("bad")

    def test_filter_boundary_uses_configured_local_clock(self):
        run = self.run_at()
        inside = self.media("at-boundary", days_before=7)
        outside = self.media("before-boundary", days_before=7)
        outside.published_at = REFERENCE - timedelta(days=7, seconds=1)
        self.observe(run, inside, when=REFERENCE, reach=100)
        self.observe(run, outside, when=REFERENCE, reach=200)
        summary = self.summary("7d")
        self.assertEqual(summary.publication_count, 1)
        self.assertEqual(summary.rows[0].media.external_id, "at-boundary")

    def test_followers_are_latest_stock_and_chronological_series(self):
        old_run = self.run_at(REFERENCE - timedelta(days=7))
        new_run = self.run_at()
        self.observe(new_run, self.media("post-a"), when=REFERENCE, reach=100)
        db.session.add_all((
            InstagramAccountSnapshot(instagram_account_id=self.account.id, analytics_run_id=old_run.id,
                                     observed_at=REFERENCE - timedelta(days=7), followers_count=8450),
            InstagramAccountSnapshot(instagram_account_id=self.account.id, analytics_run_id=new_run.id,
                                     observed_at=REFERENCE, followers_count=8675),
        ))
        summary = self.summary("7d")
        self.assertEqual(summary.followers_count, 8675)
        self.assertEqual([point.value for point in summary.followers_series], [8450, 8675])
        self.assertEqual(summary.chart_data["followers"]["values"], [8450, 8675])
        self.assertEqual(summary.chart_data["followers"]["labels"][-1], "09/10/2026 10:00")
        self.assertEqual(summary.followers_variation.absolute, 225)
        self.assertAlmostEqual(summary.followers_variation.percentage, 2.662721893491124)

    def test_derived_metrics_use_latest_observation_and_compare_content_types(self):
        old_run = self.run_at(REFERENCE - timedelta(days=7))
        new_run = self.run_at()
        reel = self.media("reel", media_type="REEL")
        carousel = self.media("carousel", media_type="CAROUSEL_ALBUM")
        image = self.media("image", media_type="IMAGE")
        self.observe(old_run, reel, when=REFERENCE - timedelta(days=7), reach=1000, interactions=10)
        self.observe(new_run, reel, when=REFERENCE, reach=1500, interactions=180,
                     impressions=1800, likes=90, comments_count=30, shares=20, saves=40)
        self.observe(new_run, carousel, when=REFERENCE, reach=1000, impressions=1300, interactions=100)
        self.observe(new_run, image, when=REFERENCE, reach=0, impressions=0, interactions=0)
        summary = self.summary()
        self.assertEqual(self.metric(summary, "reach").value, 2500)  # Fase 4: não soma a coleta antiga.
        rate = summary.performance_statistics["interaction_rate_by_reach"]
        self.assertEqual((rate.known, rate.publications), (2, 3))
        self.assertEqual(rate.median_value, 11.0)
        self.assertEqual(rate.mean_value, 11.0)
        self.assertEqual(summary.performance_statistics["reach"].median_value, 1000)
        self.assertAlmostEqual(summary.performance_statistics["reach"].mean_value, 2500 / 3)
        self.assertEqual(summary.performance_statistics["impressions"].median_value, 1300)
        self.assertEqual(summary.performance_statistics["interactions"].median_value, 100)
        reel_row = next(row for row in summary.rows if row.media.id == reel.id)
        self.assertEqual(reel_row.derived_metrics["interaction_rate_by_reach"], 12.0)
        self.assertEqual(reel_row.derived_metrics["saves_rate_by_reach"], 40 / 1500 * 100)
        by_type = {item.key: item for item in summary.type_comparisons}
        self.assertEqual((by_type["reel"].publications, by_type["reel"].reach.median_value), (1, 1500))
        self.assertEqual(by_type["carousel"].interaction_rate_by_reach.median_value, 10.0)
        self.assertEqual(by_type["image"].interaction_rate_by_reach.known, 0)
        self.assertEqual(summary.chart_data["type_interaction_rate"]["values"], [10.0, 12.0])
        self.assertEqual([row.media.id for row in self.summary(sort="interaction_rate").table_rows][:2],
                         [reel.id, carousel.id])

    def test_timestamp_tie_and_unfinished_run_do_not_change_latest(self):
        run = self.run_at()
        tied_run = self.run_at()
        pending = self.run_at(status="pending")
        post = self.media("post-a")
        self.observe(run, post, when=REFERENCE, reach=100,
                     observation_id="00000000-0000-0000-0000-000000000001")
        winning = self.observe(tied_run, post, when=REFERENCE, reach=150,
                               observation_id="00000000-0000-0000-0000-000000000002")
        self.observe(pending, post, when=REFERENCE + timedelta(days=1), reach=900)
        summary = self.summary()
        self.assertEqual(summary.rows[0].observation.id, winning.id)
        self.assertEqual(self.metric(summary, "reach").value, 150)

    def test_dashboard_html_empty_populated_and_archived(self):
        path = f"/analytics/instagram/projects/{self.project.id}/dashboard"
        detail = self.client.get(f"/analytics/instagram/projects/{self.project.id}").get_data(as_text=True)
        self.assertIn(f'href="{path}"', detail)
        empty = self.client.get(path)
        self.assertEqual(empty.status_code, 200)
        html = empty.get_data(as_text=True)
        self.assertIn("Ainda não há dados para exibir.", html)
        self.assertIn("Importar dados", html)
        self.assertNotIn('id="instagram-dashboard-chart-data"', html)
        run = self.run_at()
        self.observe(run, self.media("post-a", caption="Legenda visível"), when=REFERENCE,
                     reach=1500, interactions=73)
        db.session.commit()
        populated = self.client.get(path)
        self.assertEqual(populated.status_code, 200)
        html = populated.get_data(as_text=True)
        for text in ("Dashboard", "Dados atualizados até", "09/10/2026 10:00", "7 dias", "30 dias",
                     "90 dias", "Todo o histórico", "Publicações", "Alcance", "Dados parciais",
                     "Evolução de seguidores", "Alcance por publicação", "Interações por publicação",
                     "Publicações com maior alcance", "Legenda visível", "Indicadores de desempenho",
                     "Taxa mediana de interação por alcance", "Desempenho por tipo de conteúdo",
                     "Taxa de interação por alcance", "Curtidas por alcance",
                     "instagram-chart-type-interaction-rate"):
            self.assertIn(text, html)
        self.assertIn('aria-current="page"', html)
        self.assertNotIn('class="display-', html)
        match = re.search(r'<script id="instagram-dashboard-chart-data" type="application/json">(.*?)</script>', html)
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match.group(1))["reach"]["values"], [1500])
        self.assertEqual(self.client.get(path + "?period=invalid").status_code, 400)
        self.assertEqual(self.client.get(path + "?sort=invalid").status_code, 400)
        self.assertEqual(self.client.get(path + "?sort=interaction_rate").status_code, 200)
        self.project.status = "archived"
        self.project.archived_at = REFERENCE
        db.session.commit()
        self.assertEqual(self.client.get(path).status_code, 200)  # leitura histórica como no detalhe

    def test_period_without_publications_and_cross_user_access(self):
        run = self.run_at()
        self.observe(run, self.media("old", days_before=100), when=REFERENCE, reach=300)
        db.session.commit()
        path = f"/analytics/instagram/projects/{self.project.id}/dashboard"
        self.assertIn("Nenhuma publicação encontrada neste período.",
                      self.client.get(path + "?period=7d").get_data(as_text=True))
        self.assertEqual(self.client.get(path + "?period=all").status_code, 200)
        other = create_user("Outra", "outra-dashboard@example.org")
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        self.assertEqual(self.client.get(path).status_code, 302)
        login(self.client, other.email)
        self.assertEqual(self.client.get(path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
