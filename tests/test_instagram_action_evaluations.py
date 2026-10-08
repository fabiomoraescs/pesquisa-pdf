"""Fase 8.4: acompanhamento observacional de ações/testes Instagram."""

from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, inspect, select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_action_evaluations import (
    InstagramActionEvaluationDuplicateError,
    InstagramActionEvaluationError,
    create_action_evaluation,
    eligible_followup_runs,
    preview_action_evaluation,
    update_action_evaluation,
)
from platform_core.instagram_actions import InstagramActionError, delete_recommendation_action
from platform_core.instagram_lifecycle import delete_instagram_project_permanently
from platform_core.models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramInsightFinding,
    InstagramInsightEvidence,
    InstagramInsightRun,
    InstagramMedia,
    InstagramMediaObservation,
    InstagramRecommendation,
    InstagramRecommendationAction,
    InstagramRecommendationActionEvaluation,
    PlanTool,
)
from platform_core.instagram_recommendations import generate_recommendations
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


UTC = timezone.utc
COMPLETED = datetime(2026, 1, 15, 12, tzinfo=UTC)


class InstagramActionEvaluationsTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.user = create_user()
        self.other = create_user("Outra pessoa", "outra@example.org")
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        self.project = AnalyticsProject(owner_user_id=self.user.id, name="Aprendizado")
        self.other_project = AnalyticsProject(owner_user_id=self.other.id, name="Outro")
        db.session.add_all((self.project, self.other_project))
        db.session.flush()
        self.account = InstagramAccount(
            analytics_project_id=self.project.id, username="aprendizado", username_normalized="aprendizado",
        )
        db.session.add(self.account)
        db.session.flush()
        self.media = InstagramMedia(instagram_account_id=self.account.id, external_id="post-1")
        db.session.add(self.media)
        self.insight_run = InstagramInsightRun(
            analytics_project_id=self.project.id, period_key="30d", period_start=datetime(2025, 12, 1, tzinfo=UTC),
            period_end=datetime(2025, 12, 30, tzinfo=UTC), engine_version="instagram-insights-v1",
            parameters_json={}, publication_count=8, comment_count=0, finding_count=1,
            generated_at=datetime(2025, 12, 31, tzinfo=UTC),
        )
        db.session.add(self.insight_run)
        db.session.flush()
        finding = InstagramInsightFinding(
            instagram_insight_run_id=self.insight_run.id, rule_id="editorial_metric_difference",
            rule_version="editorial_metric_difference-v1", engine_version="instagram-insights-v1",
            kind="performance", title="Salvamentos educativos", summary="Teste descritivo.",
            metric_key="saves", metric_version="instagram-derived-v1", group_label="Educativos",
            comparison_label="Demais", group_value=42, comparison_value=31, absolute_difference=11,
            relative_difference=None, difference_unit="value", group_n=8, comparison_n=8, coverage=.9,
            support_level="consistent", support_explanation="Cobertura suficiente",
            details_json={"metric_label": "Salvamentos"},
        )
        self.finding = finding
        db.session.add(finding)
        db.session.flush()
        db.session.add(InstagramInsightEvidence(
            instagram_insight_finding_id=finding.id,
            source_type="media",
            source_id=self.media.id,
            role="principal",
            details_json={"label": "Post de referência"},
        ))
        db.session.commit()
        self.recommendation = generate_recommendations(self.project, self.insight_run).recommendations[0]
        self.action = self.make_action()
        self.before_run = self.make_run("before", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 14, tzinfo=UTC))
        self.overlap_run = self.make_run("overlap", datetime(2026, 1, 10, tzinfo=UTC), datetime(2026, 1, 20, tzinfo=UTC))
        self.followup_run = self.make_run("followup", datetime(2026, 1, 16, tzinfo=UTC), datetime(2026, 2, 15, tzinfo=UTC))
        db.session.add(InstagramMediaObservation(
            instagram_media_id=self.media.id, analytics_run_id=self.followup_run.id,
            observed_at=datetime(2026, 2, 15, tzinfo=UTC), saves=51, reach=1000, interactions=50,
        ))
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def make_action(self, **overrides):
        values = dict(
            analytics_project_id=self.project.id, instagram_recommendation_id=self.recommendation.id,
            created_by_user_id=self.user.id, action_kind="test", title="Mais conteúdos educativos",
            description="", objective="Registrar novo período.", hypothesis="Hipótese sem causalidade.",
            target_metric="saves", baseline_value=42, status="completed",
            result_summary="Resultado operacional registrado.", outcome="inconclusive", completed_at=COMPLETED,
        )
        values.update(overrides)
        action = InstagramRecommendationAction(**values)
        db.session.add(action)
        db.session.commit()
        return action

    def make_run(self, source_hash, start, end):
        run = AnalyticsRun(
            analytics_project_id=self.project.id, source_kind="csv", status="completed",
            period_start=start, period_end=end, started_at=end, completed_at=end,
            processor_version="instagram-import-v1", source_hash=source_hash,
        )
        db.session.add(run)
        db.session.flush()
        return run

    @staticmethod
    def form_values(**overrides):
        values = {"assessment": "consistent", "learning_summary": "O período posterior foi maior, com cautela metodológica."}
        values.update(overrides)
        return values

    def test_eligible_runs_exclude_prior_overlap_unknown_and_other_project(self):
        unknown = AnalyticsRun(analytics_project_id=self.project.id, source_kind="csv", status="completed")
        other = AnalyticsRun(
            analytics_project_id=self.other_project.id, source_kind="csv", status="completed",
            period_start=COMPLETED + timedelta(days=1), period_end=COMPLETED + timedelta(days=3),
        )
        db.session.add_all((unknown, other))
        db.session.commit()
        self.assertEqual([run.id for run in eligible_followup_runs(self.project, self.action)], [self.followup_run.id])
        with self.assertRaisesRegex(InstagramActionEvaluationError, "não pertence"):
            preview_action_evaluation(self.project, self.action, other.id)

    def test_preview_uses_exact_selected_run_median_and_preserves_zero_null(self):
        preview = preview_action_evaluation(self.project, self.action, self.followup_run.id)
        self.assertEqual(preview.metric_key, "saves")
        self.assertEqual(preview.baseline_value, 42)
        self.assertEqual(preview.followup_value, 51)
        self.assertEqual(preview.absolute_change, 9)
        self.assertAlmostEqual(preview.relative_change, 9 / 42)
        self.assertEqual(preview.followup_sample_size, 1)
        self.assertEqual(preview.followup_population_size, 1)
        self.assertEqual(preview.followup_coverage, 1)
        zero_action = self.make_action(title="Zero", baseline_value=0)
        zero_preview = preview_action_evaluation(self.project, zero_action, self.followup_run.id)
        self.assertEqual(zero_preview.baseline_value, 0)
        self.assertEqual(zero_preview.absolute_change, 51)
        self.assertIsNone(zero_preview.relative_change)
        self.assertEqual(zero_preview.relative_display, "Não disponível")
        null_action = self.make_action(title="Nulo", baseline_value=None)
        null_preview = preview_action_evaluation(self.project, null_action, self.followup_run.id)
        self.assertIsNone(null_preview.absolute_change)
        self.assertIsNone(null_preview.relative_change)
        self.assertEqual(null_preview.relative_display, "Não disponível")
        missing_run = self.make_run(
            "missing-followup",
            datetime(2026, 2, 16, tzinfo=UTC),
            datetime(2026, 3, 15, tzinfo=UTC),
        )
        db.session.add(InstagramMediaObservation(
            instagram_media_id=self.media.id,
            analytics_run_id=missing_run.id,
            observed_at=datetime(2026, 3, 15, tzinfo=UTC),
            saves=None,
        ))
        db.session.commit()
        missing_preview = preview_action_evaluation(self.project, self.action, missing_run.id)
        self.assertIsNone(missing_preview.followup_value)
        self.assertIsNone(missing_preview.relative_change)
        self.assertEqual(missing_preview.relative_display, "Não disponível")

    def test_unsupported_or_missing_metric_is_not_evaluable(self):
        unsupported = self.make_action(title="Sem métrica", target_metric="comment_proportion")
        preview = preview_action_evaluation(self.project, unsupported, self.followup_run.id)
        self.assertFalse(preview.evaluable)
        self.assertEqual(preview.metric_key, "comment_proportion")
        with self.assertRaisesRegex(InstagramActionEvaluationError, "Não avaliável"):
            create_action_evaluation(self.project, unsupported, self.followup_run.id, self.form_values(), actor_id=self.user.id)
        evaluation = create_action_evaluation(
            self.project, unsupported, self.followup_run.id,
            self.form_values(assessment="not_evaluable"), actor_id=self.user.id,
        )
        self.assertEqual(evaluation.assessment, "not_evaluable")
        self.assertIsNone(evaluation.followup_value)

    def test_create_relation_duplicate_and_immutable_quantitative_snapshot(self):
        evaluation = create_action_evaluation(
            self.project, self.action, self.followup_run.id, self.form_values(), actor_id=self.user.id,
        )
        self.assertEqual(evaluation.action.id, self.action.id)
        self.assertEqual(evaluation.analytics_run.id, self.followup_run.id)
        self.assertEqual(evaluation.evaluation_version, "instagram-action-evaluation-v1")
        self.assertEqual(len(self.action.evaluations), 1)
        snapshot = (evaluation.metric_key, evaluation.baseline_value, evaluation.followup_value, evaluation.absolute_change, evaluation.relative_change)
        update_action_evaluation(self.project, evaluation, self.form_values(assessment="inconclusive", learning_summary="Leitura revista."))
        self.assertEqual(snapshot, (evaluation.metric_key, evaluation.baseline_value, evaluation.followup_value, evaluation.absolute_change, evaluation.relative_change))
        with self.assertRaises(InstagramActionEvaluationDuplicateError):
            create_action_evaluation(self.project, self.action, self.followup_run.id, self.form_values(), actor_id=self.user.id)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationActionEvaluation)), 1)

    def test_delete_action_with_evaluation_is_blocked_without_removing_history_or_origins(self):
        evaluation = create_action_evaluation(
            self.project, self.action, self.followup_run.id, self.form_values(), actor_id=self.user.id,
        )
        with self.assertRaisesRegex(InstagramActionError, "possui acompanhamento"):
            delete_recommendation_action(self.project, self.action, "deletar")

        self.assertIsNotNone(db.session.get(InstagramRecommendationAction, self.action.id))
        self.assertIsNotNone(db.session.get(InstagramRecommendationActionEvaluation, evaluation.id))
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendation)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightFinding)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightEvidence)), 1)

        base = f"/analytics/instagram/projects/{self.project.id}/insights"
        action_delete_url = base + f"/actions/{self.action.id}/delete"
        page = self.client.get(base)
        self.assertNotIn(action_delete_url, page.get_data(as_text=True))
        rejected = self.client.post(action_delete_url, data={
            "csrf_token": csrf_from(page), "confirmation": "deletar",
        })
        self.assertEqual(rejected.status_code, 302)
        self.assertIn("possui acompanhamento registrado", self.client.get(rejected.headers["Location"]).get_data(as_text=True))
        self.assertIsNotNone(db.session.get(InstagramRecommendationActionEvaluation, evaluation.id))

    def test_noncompleted_archived_foreign_or_ineligible_runs_are_rejected(self):
        planned = self.make_action(title="Planejada", status="planned", result_summary=None, outcome=None, completed_at=None)
        self.assertEqual(eligible_followup_runs(self.project, planned), ())
        with self.assertRaisesRegex(InstagramActionEvaluationError, "após a conclusão"):
            preview_action_evaluation(self.project, planned, self.followup_run.id)
        with self.assertRaisesRegex(InstagramActionEvaluationError, "não é elegível"):
            preview_action_evaluation(self.project, self.action, self.overlap_run.id)
        self.project.status = "archived"
        self.project.archived_at = COMPLETED
        db.session.commit()
        with self.assertRaisesRegex(InstagramActionEvaluationError, "arquivados"):
            create_action_evaluation(self.project, self.action, self.followup_run.id, self.form_values(), actor_id=self.user.id)

    def test_routes_preview_create_detail_security_csrf_and_archived_read_only(self):
        base = f"/analytics/instagram/projects/{self.project.id}/insights/actions/{self.action.id}"
        detail = self.client.get(base)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Acompanhamento e aprendizado", detail.get_data(as_text=True))
        self.assertIn("Avaliar novo período", detail.get_data(as_text=True))
        new_url = base + "/evaluations/new"
        preview = self.client.get(new_url + f"?analytics_run_id={self.followup_run.id}")
        self.assertEqual(preview.status_code, 200)
        html = preview.get_data(as_text=True)
        self.assertIn("Prévia observacional", html)
        self.assertIn('data-partial-target="#instagram-evaluation-form-content"', html)
        self.assertIn("51", html)
        create_url = base + "/evaluations"
        self.assertEqual(self.client.post(create_url, data={**self.form_values(), "analytics_run_id": self.followup_run.id}).status_code, 400)
        created = self.client.post(create_url, data={
            **self.form_values(), "analytics_run_id": self.followup_run.id, "csrf_token": csrf_from(preview),
        })
        self.assertEqual(created.status_code, 302)
        detail_page = self.client.get(created.headers["Location"])
        self.assertIn("Snapshot do período posterior", detail_page.get_data(as_text=True))
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.other.email)
        self.assertEqual(self.client.get(base).status_code, 404)
        self.assertEqual(self.client.get(new_url).status_code, 404)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client)
        self.project.status = "archived"
        self.project.archived_at = COMPLETED
        db.session.commit()
        self.assertEqual(self.client.get(created.headers["Location"]).status_code, 200)
        self.assertEqual(self.client.get(new_url).status_code, 404)

    def test_lifecycle_removes_evaluations_before_actions_and_runs(self):
        create_action_evaluation(self.project, self.action, self.followup_run.id, self.form_values(), actor_id=self.user.id)
        self.project.status = "archived"
        self.project.archived_at = COMPLETED
        db.session.commit()
        delete_instagram_project_permanently(self.project, self.user, "deletar")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationActionEvaluation)), 0)

    def test_insights_tutorial_explains_followup_learning_and_noncausality(self):
        catalog = json.loads(
            (Path(__file__).resolve().parents[1] / "resources" / "platform_tutorials.json").read_text(encoding="utf-8")
        )
        tutorial = next(item for item in catalog["tutorials"] if item["key"] == "instagram_insights")
        section = next(
            item for item in tutorial["sections"]
            if item["id"] == "acompanhar-acao-ou-teste-em-novo-periodo"
        )
        text = " ".join(section["body"])
        self.assertIn("baseline 42", text)
        self.assertIn("follow-up 51", text)
        self.assertIn("não demonstra", text)
        self.assertIn("cobertura", text)


class InstagramActionEvaluationsMigrationTests(unittest.TestCase):
    def test_evaluation_migration_is_reversible_child_of_actions_head(self):
        from app import create_app

        migrations = str(Path(__file__).resolve().parents[1] / "migrations")
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-evaluations-migration-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root, "SECRET_KEY": "evaluations-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=migrations, revision="c0d1e2f3a4b5")
                self.assertNotIn("instagram_recommendation_action_evaluations", inspect(db.engine).get_table_names())
                upgrade(directory=migrations, revision="d1e2f3a4b5c6")
                inspector = inspect(db.engine)
                self.assertIn("instagram_recommendation_action_evaluations", inspector.get_table_names())
                self.assertIn("uq_instagram_action_evaluation_action_run", {
                    item["name"] for item in inspector.get_unique_constraints("instagram_recommendation_action_evaluations")
                })
                self.assertIn("ck_instagram_action_evaluation_assessment", {
                    item["name"] for item in inspector.get_check_constraints("instagram_recommendation_action_evaluations")
                })
                downgrade(directory=migrations, revision="c0d1e2f3a4b5")
                self.assertNotIn("instagram_recommendation_action_evaluations", inspect(db.engine).get_table_names())
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
