"""Contratos puros dos recortes temporais e da paginação compartilhada."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from sqlalchemy import select

from platform_core.instagram_periods import (
    InstagramPeriodError,
    TEMPORAL_PERIOD_OPTIONS,
    effective_run_period,
    resolve_calendar_dates,
    resolve_local_period,
)
from platform_core.instagram_insights import generate_insights, latest_insight_run
from platform_core.pagination import PAGE_SIZE, paginate_sequence
from platform_core.extensions import db
from platform_core.models import AnalyticsProject, AnalyticsRun, PlanTool
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL
from platform_helpers import create_user, isolated_platform, login


class InstagramTemporalPeriodsTests(unittest.TestCase):
    def test_the_same_official_options_are_exposed_in_one_shared_vocabulary(self):
        self.assertEqual(
            tuple(key for key, _label in TEMPORAL_PERIOD_OPTIONS),
            ("7d", "30d", "60d", "90d", "6m", "12m", "custom", "all"),
        )

    def test_calendar_presets_resolve_to_concrete_inclusive_dates(self):
        now = datetime(2026, 10, 10, 15, tzinfo=timezone.utc)
        expected = {
            "7d": ("2026-10-04", "2026-10-10"),
            "30d": ("2026-09-11", "2026-10-10"),
            "60d": ("2026-08-12", "2026-10-10"),
            "90d": ("2026-07-13", "2026-10-10"),
            "6m": ("2026-04-10", "2026-10-10"),
            "12m": ("2025-10-10", "2026-10-10"),
        }
        for key, (start, end) in expected.items():
            period = resolve_calendar_dates(key, "America/Maceio", now=now)
            self.assertEqual((period[0].isoformat(), period[1].isoformat()), (start, end))
        self.assertIsNone(resolve_calendar_dates("all", "America/Maceio", now=now))

    def test_custom_period_is_inclusive_and_rejects_an_inverted_range(self):
        period = resolve_local_period(
            "custom", "America/Maceio", reference_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
            period_start="2026-10-09", period_end="2026-10-09",
        )
        self.assertEqual(period.start.isoformat(), "2026-10-09T03:00:00+00:00")
        self.assertEqual(period.end.isoformat(), "2026-10-10T02:59:59.999999+00:00")
        with self.assertRaises(InstagramPeriodError):
            resolve_calendar_dates(
                "custom", "America/Maceio", period_start="2026-10-10", period_end="2026-10-09",
            )

    def test_effective_run_period_distinguishes_columns_meta_history_and_legacy(self):
        columns = SimpleNamespace(
            source_kind="csv",
            period_start=datetime(2026, 10, 8, 3, tzinfo=timezone.utc),
            period_end=datetime(2026, 10, 14, 15, tzinfo=timezone.utc),
            parameters_json={},
        )
        meta = SimpleNamespace(
            source_kind="meta_api", period_start=None, period_end=None,
            parameters_json={
                "period_preset": "last_7_days",
                "published_from": "2026-10-08T03:00:00+00:00",
                "published_to": "2026-10-14T15:00:00+00:00",
                "presentation_timezone": "America/Maceio",
            },
        )
        all_history = SimpleNamespace(
            source_kind="meta_api", period_start=None, period_end=None,
            parameters_json={"period_preset": "all_history"},
        )
        legacy = SimpleNamespace(
            source_kind="meta_api", period_start=None, period_end=None, parameters_json={},
        )

        self.assertEqual((effective_run_period(columns).kind, effective_run_period(columns).source), ("bounded", "columns"))
        resolved_meta = effective_run_period(meta)
        self.assertEqual((resolved_meta.kind, resolved_meta.source, resolved_meta.start.isoformat()), (
            "bounded", "meta_parameters", "2026-10-08T03:00:00+00:00",
        ))
        self.assertEqual(effective_run_period(all_history).kind, "all_history")
        self.assertEqual(effective_run_period(legacy).kind, "unknown")

    def test_day_presets_use_inclusive_local_calendar_dates_not_rolling_hours(self):
        reference = datetime(2026, 10, 14, 15, 9, 29, tzinfo=timezone.utc)
        resolved = resolve_local_period("7d", "America/Maceio", reference_at=reference)
        self.assertEqual(resolved.start.isoformat(), "2026-10-08T03:00:00+00:00")
        self.assertEqual(resolved.end.isoformat(), "2026-10-14T15:09:29+00:00")

    def test_shared_temporal_control_marks_filter_updates_and_keeps_custom_safe(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "templates/platform/instagram/_temporal_period.html").read_text(encoding="utf-8")
        script = (root / "static/js/instagram_temporal_period.js").read_text(encoding="utf-8")
        auto_submit = (root / "static/js/platform_auto_submit.js").read_text(encoding="utf-8")
        self.assertIn("data-instagram-temporal-auto-submit", template)
        self.assertIn("Período: {% endif %}", template)
        self.assertIn("select.value !== 'custom'", script)
        self.assertIn("customDatesAreReady(control)", script)
        self.assertIn("form.dataset.resetPagination = 'true'", script)
        self.assertIn("/(?:^|_)page(?:_|$)/", auto_submit)

    def test_dark_pagination_uses_shared_high_contrast_text(self):
        css = (Path(__file__).resolve().parents[1] / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn('html[data-theme="dark"] .platform-pagination .page-link', css)
        self.assertIn("color: #fff", css)


class PlatformPaginationTests(unittest.TestCase):
    def test_twenty_items_are_not_paginated_and_twenty_one_items_are(self):
        one_page = paginate_sequence(tuple(range(PAGE_SIZE)), 1)
        self.assertEqual((one_page.total, one_page.pages, len(one_page.items)), (20, 1, 20))
        two_pages = paginate_sequence(tuple(range(PAGE_SIZE + 1)), 2)
        self.assertEqual((two_pages.total, two_pages.pages, two_pages.page), (21, 2, 2))
        self.assertEqual(two_pages.items, (20,))

    def test_compact_navigation_keeps_current_page_and_uses_ellipsis_for_long_lists(self):
        page = paginate_sequence(tuple(range(101)), 3)
        self.assertEqual(page.pages, 6)
        self.assertEqual(page.numbers, (1, 2, 3, 4, 5, 6))
        long_page = paginate_sequence(tuple(range(401)), 11)
        self.assertEqual(long_page.pages, 21)
        self.assertIn(None, long_page.numbers)
        self.assertIn(11, long_page.numbers)
        self.assertEqual((long_page.previous_page, long_page.next_page), (10, 12))


class InstagramProjectPaginationPresentationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.user = create_user()
        db.session.add(PlanTool(plan_id="research_quali", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        db.session.add_all(
            AnalyticsProject(
                owner_user_id=self.user.id,
                module_key="instagram",
                name=f"Projeto paginado {index:02d}",
            )
            for index in range(21)
        )
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_active_projects_are_paginated_server_side_at_twenty_items(self):
        first = self.client.get("/analytics/instagram")
        self.assertEqual(first.status_code, 200)
        first_html = first.get_data(as_text=True)
        self.assertEqual(first_html.count('<article class="platform-list-item"'), 20)
        self.assertIn('aria-current="page">1</span>', first_html)
        self.assertIn("page=2", first_html)

        second = self.client.get("/analytics/instagram?page=2")
        self.assertEqual(second.status_code, 200)
        second_html = second.get_data(as_text=True)
        self.assertEqual(second_html.count('<article class="platform-list-item"'), 1)
        self.assertIn('aria-current="page">2</span>', second_html)

    def test_meta_history_uses_the_frozen_period_parameters_without_rewriting_runs(self):
        project = db.session.scalar(
            select(AnalyticsProject).where(AnalyticsProject.owner_user_id == self.user.id)
        )
        run = AnalyticsRun(
            analytics_project_id=project.id,
            source_kind="meta_api",
            status="completed",
            completed_at=datetime(2026, 10, 10, 15, tzinfo=timezone.utc),
            parameters_json={
                "period_preset": "last_30_days",
                "published_from": "2026-09-11T03:00:00+00:00",
                "published_to": "2026-10-11T02:59:59.999999+00:00",
                "presentation_timezone": "America/Maceio",
            },
        )
        db.session.add(run)
        db.session.commit()

        html = self.client.get(f"/analytics/instagram/projects/{project.id}").get_data(as_text=True)
        self.assertIn("Últimos 30 dias · 11/09/2026 a 10/10/2026", html)
        self.assertIsNone(db.session.get(AnalyticsRun, run.id).period_start)
        self.assertIsNone(db.session.get(AnalyticsRun, run.id).period_end)

    def test_source_page_uses_the_same_history_for_direct_meta_and_manual_runs(self):
        project = db.session.scalar(
            select(AnalyticsProject).where(AnalyticsProject.owner_user_id == self.user.id)
        )
        db.session.add_all((
            AnalyticsRun(
                analytics_project_id=project.id, source_kind="meta_api", status="completed",
                completed_at=datetime(2026, 10, 14, 15, tzinfo=timezone.utc),
                parameters_json={
                    "provider": "instagram_login", "period_preset": "last_7_days",
                    "published_from": "2026-10-08T03:00:00+00:00",
                    "published_to": "2026-10-14T15:00:00+00:00",
                    "presentation_timezone": "America/Maceio",
                },
            ),
            AnalyticsRun(
                analytics_project_id=project.id, source_kind="csv", status="completed",
                period_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
                period_end=datetime(2026, 9, 2, tzinfo=timezone.utc),
                completed_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            ),
        ))
        db.session.commit()

        html = self.client.get(f"/analytics/instagram/projects/{project.id}/imports/new").get_data(as_text=True)
        self.assertIn("Histórico de importações e sincronizações", html)
        self.assertIn("Últimos 7 dias · 08/10/2026 a 14/10/2026", html)
        self.assertIn("CSV", html)

    def test_new_insight_presets_keep_their_requested_window_without_a_schema_change(self):
        project = db.session.scalar(
            select(AnalyticsProject).where(AnalyticsProject.owner_user_id == self.user.id)
        )
        run = generate_insights(project, "60d", "America/Maceio")

        self.assertEqual(run.period_key, "all")  # chave compatível com a constraint histórica
        self.assertEqual(run.parameters_json["requested_period_key"], "60d")
        self.assertEqual(run.parameters_json["persisted_period_key"], "all")
        self.assertEqual(
            latest_insight_run(project, "60d", timezone_name="America/Maceio").id,
            run.id,
        )


if __name__ == "__main__":
    unittest.main()
