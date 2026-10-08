"""Fase 8: motor determinístico, runs e evidências de Insights Instagram."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, inspect, select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_insights import (
    INSTAGRAM_INSIGHTS_VERSION,
    build_insight_drafts,
    generate_insights,
)
from platform_core.instagram_lifecycle import delete_instagram_project_permanently
from platform_core.instagram_taxonomy import classify_media, create_initial_taxonomy, taxonomy_dimensions
from platform_core.models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramComment,
    InstagramCommentAssessment,
    InstagramCommentTextVersion,
    InstagramInsightEvidence,
    InstagramInsightFinding,
    InstagramInsightRun,
    InstagramMedia,
    InstagramMediaObservation,
    PlanTool,
)
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


UTC = timezone.utc
REFERENCE = datetime(2026, 10, 9, 13, tzinfo=UTC)


class InstagramInsightsTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.app.config["PRESENTATION_TIMEZONE"] = "America/Maceio"
        self.client = self.app.test_client()
        self.user = create_user()
        self.other = create_user("Outra pessoa", "outra@example.org")
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        self.project = AnalyticsProject(owner_user_id=self.user.id, name="Insights da revista")
        self.other_project = AnalyticsProject(owner_user_id=self.other.id, name="Projeto alheio")
        db.session.add_all((self.project, self.other_project))
        db.session.flush()
        self.account = InstagramAccount(
            analytics_project_id=self.project.id,
            username="revista",
            username_normalized="revista",
        )
        db.session.add(self.account)
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def analytics_run(self, when: datetime = REFERENCE) -> AnalyticsRun:
        run = AnalyticsRun(
            analytics_project_id=self.project.id,
            source_kind="csv",
            status="completed",
            started_at=when,
            completed_at=when,
        )
        db.session.add(run)
        db.session.flush()
        return run

    def media(self, external_id: str, *, media_type: str = "REEL", days_before: int = 1) -> InstagramMedia:
        item = InstagramMedia(
            instagram_account_id=self.account.id,
            external_id=external_id,
            caption_original=f"Legenda {external_id}",
            media_type_raw=media_type,
            permalink=f"https://instagram.example.test/p/{external_id}",
            published_at=REFERENCE - timedelta(days=days_before),
        )
        db.session.add(item)
        db.session.flush()
        return item

    def observe(self, run: AnalyticsRun, media: InstagramMedia, *, reach: int | None,
                interactions: int | None = 0, when: datetime = REFERENCE, **extra) -> InstagramMediaObservation:
        observation = InstagramMediaObservation(
            instagram_media_id=media.id,
            analytics_run_id=run.id,
            observed_at=when,
            reach=reach,
            interactions=interactions,
            **extra,
        )
        db.session.add(observation)
        db.session.flush()
        return observation

    def classify_objective(self, media: InstagramMedia, category_key: str) -> None:
        dimensions = taxonomy_dimensions(self.project.id, active_only=True)
        dimension = next(item for item in dimensions if item.key == "communicational-objective")
        category = next(item for item in dimension.categories if item.key == category_key)
        classify_media(self.project, media.id, {dimension.id: (category.id,)}, self.user)

    def comment(self, media: InstagramMedia, index: int, *, sentiment: str = "negative",
                intent: str = "question", needs_response: bool | None = True,
                risk: str = "high", topic: str | None = "acesso") -> InstagramComment:
        comment = InstagramComment(
            instagram_media_id=media.id,
            external_id=f"comment-{index}",
            original_text=f"Comentário de teste {index}",
            published_at=REFERENCE,
        )
        db.session.add(comment)
        db.session.flush()
        version = InstagramCommentTextVersion(
            instagram_comment_id=comment.id,
            text=comment.original_text,
            content_hash=f"{index + 1:064x}",
            first_observed_at=REFERENCE,
            is_original=True,
        )
        db.session.add(version)
        db.session.flush()
        assessment = InstagramCommentAssessment(
            instagram_comment_id=comment.id,
            instagram_comment_text_version_id=version.id,
            source="manual",
            reviewer_user_id=self.user.id,
            sentiment=sentiment,
            intent=intent,
            needs_response=needs_response,
            risk=risk,
            topic=topic,
            topic_normalized=topic.casefold() if topic else None,
        )
        db.session.add(assessment)
        db.session.flush()
        return comment

    def prepare_editorial_comparison(self, *, group_reach: int = 1000,
                                     comparison_reach: int = 100) -> tuple[AnalyticsRun, tuple[InstagramMedia, ...]]:
        run = self.analytics_run()
        create_initial_taxonomy(self.project)
        media = tuple(self.media(f"post-{index}") for index in range(6))
        for index, item in enumerate(media):
            self.observe(run, item, reach=group_reach if index < 3 else comparison_reach,
                         interactions=100 if index < 3 else 10)
            self.classify_objective(item, "inform" if index < 3 else "promote")
        db.session.commit()
        return run, media

    def test_editorial_performance_uses_last_observation_and_records_evidence(self):
        old_run, media = self.prepare_editorial_comparison(group_reach=100, comparison_reach=50)
        newest = self.analytics_run(REFERENCE + timedelta(days=1))
        for item in media[:3]:
            self.observe(newest, item, reach=1000, interactions=100, when=REFERENCE + timedelta(days=1))
        db.session.commit()

        reference, rows, facts, drafts = build_insight_drafts(self.project, "all", "America/Maceio")
        self.assertEqual(reference.replace(tzinfo=UTC), REFERENCE + timedelta(days=1))
        self.assertEqual(len(rows), 6)
        self.assertFalse(facts)
        finding = next(item for item in drafts if (
            item.rule_id == "editorial_metric_difference"
            and item.group_label == "Objetivo comunicacional: Informar"
            and item.metric_key == "reach"
        ))
        self.assertEqual((finding.group_value, finding.comparison_value), (1000.0, 50.0))
        self.assertEqual((finding.group_n, finding.comparison_n), (3, 3))
        self.assertEqual(finding.coverage, 1)
        self.assertIn("não causal", finding.summary)
        _, _, _, repeated = build_insight_drafts(self.project, "all", "America/Maceio")
        self.assertEqual(
            [(item.rule_id, item.metric_key, item.group_label, item.title) for item in drafts],
            [(item.rule_id, item.metric_key, item.group_label, item.title) for item in repeated],
        )

        run = generate_insights(self.project, "all", "America/Maceio")
        persisted = db.session.get(InstagramInsightRun, run.id)
        self.assertEqual(persisted.engine_version, INSTAGRAM_INSIGHTS_VERSION)
        self.assertEqual(persisted.parameters_json["latest_observation_per_media"], True)
        stored = db.session.scalar(select(InstagramInsightFinding).where(
            InstagramInsightFinding.instagram_insight_run_id == run.id,
            InstagramInsightFinding.metric_key == "reach",
            InstagramInsightFinding.group_label == "Objetivo comunicacional: Informar",
        ))
        self.assertIsNotNone(stored)
        self.assertGreater(db.session.scalar(select(func.count()).select_from(InstagramInsightEvidence).where(
            InstagramInsightEvidence.instagram_insight_finding_id == stored.id
        )), 0)

    def test_media_type_comparison_uses_median_and_a_single_latest_observation_per_media(self):
        older = self.analytics_run(REFERENCE - timedelta(days=2))
        newest = self.analytics_run()
        media = tuple(
            self.media(f"type-{index}", media_type="REEL" if index < 3 else "CAROUSEL_ALBUM")
            for index in range(6)
        )
        for index, item in enumerate(media):
            self.observe(older, item, reach=10, when=REFERENCE - timedelta(days=2))
            self.observe(newest, item, reach=1000 if index < 3 else 100)
        db.session.commit()

        _, rows, _, drafts = build_insight_drafts(self.project, "all", "America/Maceio")
        self.assertEqual(len(rows), 6)
        finding = next(item for item in drafts if (
            item.rule_id == "media_type_metric_difference"
            and item.group_label == "Tipo de mídia: Reels"
            and item.metric_key == "reach"
        ))
        self.assertEqual((finding.group_value, finding.comparison_value), (1000.0, 100.0))
        self.assertEqual((finding.group_n, finding.comparison_n), (3, 3))

    def test_thresholds_coverage_zero_and_small_groups_do_not_create_performance_finding(self):
        run = self.analytics_run()
        create_initial_taxonomy(self.project)
        media = tuple(self.media(f"small-{index}") for index in range(4))
        for index, item in enumerate(media):
            self.observe(run, item, reach=1000 if index < 2 else 0)
            self.classify_objective(item, "inform" if index < 2 else "promote")
        db.session.commit()
        _, _, _, drafts = build_insight_drafts(self.project, "all", "America/Maceio")
        self.assertFalse(any(item.rule_id == "editorial_metric_difference" for item in drafts))

        # Aumenta a população, mas deixa a métrica desconhecida: cobertura baixa também bloqueia.
        for index in range(4, 10):
            item = self.media(f"unknown-{index}")
            self.observe(run, item, reach=None)
            self.classify_objective(item, "inform" if index < 7 else "promote")
        db.session.commit()
        _, _, _, drafts = build_insight_drafts(self.project, "all", "America/Maceio")
        self.assertFalse(any(item.rule_id == "editorial_metric_difference" for item in drafts))

    def test_reception_rules_use_human_priority_and_topic_evidence(self):
        run = self.analytics_run()
        media = self.media("commented")
        self.observe(run, media, reach=100)
        for index in range(6):
            self.comment(media, index, topic="acesso")
        for index in range(6, 10):
            self.comment(media, index, sentiment="positive", intent="praise", needs_response=False,
                         risk="none", topic="elogio")
        db.session.commit()
        _, _, _, drafts = build_insight_drafts(self.project, "all", "America/Maceio")
        rule_ids = {item.rule_id for item in drafts}
        self.assertTrue({
            "comment_sentiment_concentration", "comment_question_concentration",
            "comment_response_need", "comment_risk_concentration", "comment_topic_recurrence",
        }.issubset(rule_ids))
        topic = next(item for item in drafts if item.rule_id == "comment_topic_recurrence")
        self.assertEqual((topic.group_n, topic.comparison_n), (6, 4))
        self.assertTrue(any(item.source_type == "comment_assessment" for item in topic.evidence))

        first = db.session.scalar(select(InstagramComment).where(InstagramComment.external_id == "comment-0"))
        version = first.text_versions[0]
        db.session.add(InstagramCommentAssessment(
            instagram_comment_id=first.id,
            instagram_comment_text_version_id=version.id,
            source="human_review",
            reviewer_user_id=self.user.id,
            sentiment="positive",
        ))
        db.session.commit()
        _, _, facts, _ = build_insight_drafts(self.project, "all", "America/Maceio")
        effective = next(item.assessment for item in facts if item.comment.id == first.id)
        self.assertEqual((effective.source, effective.sentiment), ("human_review", "positive"))

    def test_content_reception_compares_evaluable_comment_populations_including_zero_percent(self):
        run = self.analytics_run()
        create_initial_taxonomy(self.project)
        media = tuple(self.media(f"reception-{index}") for index in range(6))
        for index, item in enumerate(media):
            self.observe(run, item, reach=100)
            is_inform = index < 3
            self.classify_objective(item, "inform" if is_inform else "promote")
            self.comment(
                item, 30 + index,
                sentiment="negative" if is_inform else "positive",
                intent="question" if is_inform else "praise",
                needs_response=is_inform,
                risk="high" if is_inform else "none",
                topic="acesso" if is_inform else "elogio",
            )
        db.session.commit()

        _, _, _, drafts = build_insight_drafts(self.project, "all", "America/Maceio")
        finding = next(item for item in drafts if (
            item.rule_id == "editorial_reception_difference"
            and item.group_label == "Objetivo comunicacional: Informar"
            and "sentimento negativo" in item.title.casefold()
        ))
        self.assertEqual((finding.group_value, finding.comparison_value), (100.0, 0.0))
        self.assertEqual((finding.group_n, finding.comparison_n), (3, 3))
        self.assertEqual(finding.absolute_difference, 100.0)
        self.assertEqual(finding.details["target_comparison_comments"], 0)

    def test_repeated_runs_are_historical_and_rollback_leaves_no_partial_run(self):
        self.prepare_editorial_comparison()
        first = generate_insights(self.project, "30d", "America/Maceio")
        second = generate_insights(self.project, "30d", "America/Maceio")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightRun)), 2)
        self.assertGreater(db.session.scalar(select(func.count()).select_from(InstagramInsightFinding)), 0)

        before = db.session.scalar(select(func.count()).select_from(InstagramInsightRun))
        with patch("platform_core.instagram_insights.db.session.commit", side_effect=RuntimeError("controlled")):
            with self.assertRaisesRegex(Exception, "nenhuma execução parcial"):
                generate_insights(self.project, "all", "America/Maceio")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightRun)), before)

    def test_routes_csrf_ownership_archived_read_only_and_evidence_detail(self):
        self.prepare_editorial_comparison()
        base = f"/analytics/instagram/projects/{self.project.id}/insights"
        page = self.client.get(base)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Ainda não há insights gerados", html)
        self.assertIn('aria-current="page">Insights</a>', html)
        self.assertIn("Como interpretar os Insights", html)
        self.assertEqual(self.client.post(base + "/generate", data={"period": "30d"}).status_code, 400)
        generated = self.client.post(base + "/generate", data={
            "csrf_token": csrf_from(page), "period": "30d",
        })
        self.assertEqual(generated.status_code, 302)
        self.assertIn("#instagram-insights-content", generated.headers["Location"])
        run = db.session.scalar(select(InstagramInsightRun).order_by(InstagramInsightRun.generated_at.desc()))
        finding = db.session.scalar(select(InstagramInsightFinding).where(
            InstagramInsightFinding.instagram_insight_run_id == run.id
        ))
        detail = self.client.get(base + f"/{finding.id}")
        detail_html = detail.get_data(as_text=True)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Evidências", detail_html)
        self.assertIn('platform-action-button', detail_html)
        self.assertIn('aria-label="Abrir publicação"', detail_html)
        self.assertIn('title="Abrir publicação"', detail_html)
        self.assertNotIn(">Abrir publicação</a>", detail_html)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.other.email)
        self.assertEqual(self.client.get(base).status_code, 404)
        self.assertEqual(self.client.get(base + f"/{finding.id}").status_code, 404)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client)
        self.project.status = "archived"
        self.project.archived_at = REFERENCE
        db.session.commit()
        self.assertEqual(self.client.get(base).status_code, 200)
        archived = self.client.get(base)
        self.assertEqual(self.client.post(base + "/generate", data={
            "csrf_token": csrf_from(archived), "period": "30d",
        }).status_code, 404)

    def test_permanent_deletion_removes_runs_findings_and_evidence(self):
        self.prepare_editorial_comparison()
        generate_insights(self.project, "all", "America/Maceio")
        self.project.status = "archived"
        self.project.archived_at = REFERENCE
        db.session.commit()
        delete_instagram_project_permanently(self.project, self.user, "deletar")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightRun)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightFinding)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightEvidence)), 0)


class InstagramInsightsMigrationTests(unittest.TestCase):
    def test_insights_migration_is_reversible_child_of_phase_seven_head(self):
        from app import create_app

        migrations = str(Path(__file__).resolve().parents[1] / "migrations")
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-insights-migration-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "insights-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=migrations, revision="f6a7b8c9d0e1")
                self.assertNotIn("instagram_insight_runs", inspect(db.engine).get_table_names())
                upgrade(directory=migrations, revision="a8b9c0d1e2f3")
                inspector = inspect(db.engine)
                expected = {
                    "instagram_insight_runs", "instagram_insight_findings", "instagram_insight_evidence",
                }
                self.assertTrue(expected.issubset(inspector.get_table_names()))
                self.assertIn(
                    "uq_instagram_insight_evidence_finding_source_role",
                    {item["name"] for item in inspector.get_unique_constraints("instagram_insight_evidence")},
                )
                downgrade(directory=migrations, revision="f6a7b8c9d0e1")
                self.assertTrue(expected.isdisjoint(inspect(db.engine).get_table_names()))
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
