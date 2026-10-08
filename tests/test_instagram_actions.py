"""Fase 8.3: ações/testes derivados de recomendações Instagram."""

from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_actions import (
    InstagramActionError,
    InstagramActionPersistenceError,
    action_prefill,
    create_recommendation_action,
    delete_recommendation_action,
    parse_planned_datetime,
    transition_recommendation_action,
)
from platform_core.instagram_lifecycle import delete_instagram_project_permanently
from platform_core.instagram_recommendations import generate_recommendations
from platform_core.models import (
    AnalyticsProject,
    InstagramInsightEvidence,
    InstagramInsightFinding,
    InstagramInsightRun,
    InstagramRecommendation,
    InstagramRecommendationAction,
    PlanTool,
)
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


UTC = timezone.utc
WHEN = datetime(2026, 10, 7, 15, tzinfo=UTC)


class InstagramActionsTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.user = create_user()
        self.other = create_user("Outra pessoa", "outra@example.org")
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        self.project = AnalyticsProject(owner_user_id=self.user.id, name="Ações")
        self.other_project = AnalyticsProject(owner_user_id=self.other.id, name="Outro projeto")
        db.session.add_all((self.project, self.other_project))
        db.session.flush()
        self.insight_run = InstagramInsightRun(
            analytics_project_id=self.project.id,
            period_key="30d", engine_version="instagram-insights-v1",
            parameters_json={}, publication_count=12, comment_count=0,
            finding_count=1, generated_at=WHEN,
        )
        db.session.add(self.insight_run)
        db.session.flush()
        self.finding = InstagramInsightFinding(
            instagram_insight_run_id=self.insight_run.id,
            rule_id="editorial_metric_difference", rule_version="editorial_metric_difference-v1",
            engine_version="instagram-insights-v1", kind="performance",
            title="Objetivo comunicacional: Educar", summary="Achado descritivo de teste.",
            metric_key="saves", metric_version="instagram-derived-v1",
            group_label="Objetivo comunicacional: Educar", comparison_label="demais conteúdos",
            group_value=0.0, comparison_value=20.0, absolute_difference=20.0,
            relative_difference=None, difference_unit="value", group_n=8, comparison_n=8,
            coverage=.95, support_level="consistent", support_explanation="Teste",
            details_json={"metric_label": "Salvamentos"},
        )
        db.session.add(self.finding)
        db.session.flush()
        self.evidence = InstagramInsightEvidence(
            instagram_insight_finding_id=self.finding.id,
            source_type="media",
            source_id="action-fixture-media",
            role="principal",
            details_json={"label": "Evidência de origem"},
        )
        db.session.add(self.evidence)
        db.session.commit()
        self.recommendation = generate_recommendations(self.project, self.insight_run).recommendations[0]
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def values(self, **overrides):
        values = {
            "title": "Acompanhar conteúdos educativos",
            "description": "Registro operacional de teste.",
            "objective": "Acompanhar a métrica registrada no próximo ciclo.",
            "hypothesis": "A intervenção pode coincidir com mudança na métrica, sem provar causalidade.",
            "target_metric": "saves",
            "baseline_value": "0",
            "target_value": "",
            "success_criterion": "Registrar o resultado com cautela.",
            "planned_start_at": None,
            "planned_end_at": None,
        }
        values.update(overrides)
        return values

    def test_prefill_is_deterministic_and_preserves_zero_baseline(self):
        prefill = action_prefill(self.recommendation, "test")
        self.assertEqual(prefill.target_metric, "saves")
        self.assertEqual(prefill.baseline_value, 0.0)
        self.assertIn("não demonstra causalidade", prefill.hypothesis)
        self.assertIn("Acompanhar", prefill.objective)

    def test_service_rejects_invalid_kind_or_foreign_recommendation_and_keeps_null_baseline(self):
        with self.assertRaisesRegex(InstagramActionError, "Tipo de ação"):
            create_recommendation_action(
                self.project, self.recommendation, "standalone", self.values(), actor_id=self.user.id,
            )
        with self.assertRaisesRegex(InstagramActionError, "não pertence"):
            create_recommendation_action(
                self.other_project, self.recommendation, "action",
                self.values(hypothesis=""), actor_id=self.other.id,
            )
        self.finding.group_value = None
        db.session.flush()
        self.assertIsNone(action_prefill(self.recommendation, "action").baseline_value)

    def test_action_and_test_are_one_to_many_from_recommendation(self):
        action = create_recommendation_action(
            self.project, self.recommendation, "action", self.values(hypothesis=""), actor_id=self.user.id,
        )
        test = create_recommendation_action(
            self.project, self.recommendation, "test", self.values(title="Teste educativo"), actor_id=self.user.id,
        )
        self.assertEqual(action.analytics_project_id, self.project.id)
        self.assertEqual(test.instagram_recommendation_id, self.recommendation.id)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationAction)), 2)
        self.assertEqual({item.action_kind for item in self.recommendation.actions}, {"action", "test"})

    def test_dates_statuses_and_outcome_validate_without_overwriting_history(self):
        start = parse_planned_datetime("2026-10-10T09:00", "America/Maceio")
        end = parse_planned_datetime("2026-10-11T09:00", "America/Maceio")
        action = create_recommendation_action(
            self.project, self.recommendation, "test",
            self.values(planned_start_at=start, planned_end_at=end), actor_id=self.user.id,
        )
        self.assertEqual(action.status, "planned")
        self.assertLess(action.planned_start_at, action.planned_end_at)
        transition_recommendation_action(action, "in_progress")
        transition_recommendation_action(action, "completed", {
            "result_summary": "Não foi possível isolar outros fatores.",
            "observed_result": "0", "outcome": "inconclusive",
        })
        self.assertEqual(action.status, "completed")
        self.assertEqual(action.observed_result, 0.0)
        self.assertEqual(action.outcome, "inconclusive")
        self.assertIsNotNone(action.completed_at)
        first_summary = action.result_summary
        with self.assertRaisesRegex(InstagramActionError, "não permitida"):
            transition_recommendation_action(action, "planned")
        self.assertEqual(action.result_summary, first_summary)
        with self.assertRaisesRegex(InstagramActionError, "anterior"):
            create_recommendation_action(
                self.project, self.recommendation, "action",
                self.values(hypothesis="", planned_start_at=end, planned_end_at=start), actor_id=self.user.id,
            )

    def test_cancel_transitions_and_null_observed_result_are_valid(self):
        planned = create_recommendation_action(
            self.project, self.recommendation, "action", self.values(hypothesis=""), actor_id=self.user.id,
        )
        transition_recommendation_action(planned, "cancelled")
        self.assertEqual(planned.status, "cancelled")
        in_progress = create_recommendation_action(
            self.project, self.recommendation, "test", self.values(), actor_id=self.user.id,
        )
        transition_recommendation_action(in_progress, "in_progress")
        transition_recommendation_action(in_progress, "cancelled")
        self.assertEqual(in_progress.status, "cancelled")
        completed = create_recommendation_action(
            self.project, self.recommendation, "test", self.values(), actor_id=self.user.id,
        )
        transition_recommendation_action(completed, "in_progress")
        transition_recommendation_action(completed, "completed", {
            "result_summary": "A métrica não estava disponível no ciclo.",
            "outcome": "not_applicable",
        })
        self.assertIsNone(completed.observed_result)

    def test_persistence_failure_rolls_back_the_new_action(self):
        with patch.object(db.session, "commit", side_effect=SQLAlchemyError("falha controlada")):
            with self.assertRaises(InstagramActionPersistenceError):
                create_recommendation_action(
                    self.project, self.recommendation, "action",
                    self.values(hypothesis=""), actor_id=self.user.id,
                )
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationAction)), 0)

    def test_delete_actions_without_evaluations_allows_every_status_and_preserves_origins(self):
        planned_action = create_recommendation_action(
            self.project, self.recommendation, "action", self.values(hypothesis=""), actor_id=self.user.id,
        )
        planned_test = create_recommendation_action(
            self.project, self.recommendation, "test", self.values(title="Teste planejado"), actor_id=self.user.id,
        )
        in_progress = create_recommendation_action(
            self.project, self.recommendation, "action", self.values(title="Ação em andamento", hypothesis=""), actor_id=self.user.id,
        )
        transition_recommendation_action(in_progress, "in_progress")
        completed = create_recommendation_action(
            self.project, self.recommendation, "test", self.values(title="Teste concluído"), actor_id=self.user.id,
        )
        transition_recommendation_action(completed, "in_progress")
        transition_recommendation_action(completed, "completed", {
            "result_summary": "Registro concluído sem inferência causal.",
            "outcome": "inconclusive",
        })
        cancelled = create_recommendation_action(
            self.project, self.recommendation, "action", self.values(title="Ação cancelada", hypothesis=""), actor_id=self.user.id,
        )
        transition_recommendation_action(cancelled, "cancelled")

        for action in (planned_action, planned_test, in_progress, completed, cancelled):
            delete_recommendation_action(self.project, action, "deletar")
            self.assertIsNone(db.session.get(InstagramRecommendationAction, action.id))

        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendation)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightFinding)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightEvidence)), 1)

    def test_delete_action_rejects_a_project_mismatch(self):
        action = create_recommendation_action(
            self.project, self.recommendation, "action", self.values(hypothesis=""), actor_id=self.user.id,
        )
        with self.assertRaisesRegex(InstagramActionError, "não pertence"):
            delete_recommendation_action(self.other_project, action, "deletar")
        self.assertIsNotNone(db.session.get(InstagramRecommendationAction, action.id))

    def test_templates_cover_all_recommendation_families_without_generating_targets(self):
        for recommendation_type in (
            "content_mix", "media_format", "information_clarity", "community_response",
            "editorial_strategy", "risk_review", "measurement_quality",
        ):
            self.recommendation.recommendation_type = recommendation_type
            prefill = action_prefill(self.recommendation, "test")
            self.assertTrue(prefill.title)
            self.assertTrue(prefill.objective)
            self.assertEqual(prefill.target_metric, "saves")

    def test_insights_tutorial_explains_actions_tests_and_non_causality(self):
        catalog = json.loads((Path(__file__).resolve().parents[1] / "resources" / "platform_tutorials.json").read_text(encoding="utf-8"))
        tutorial = next(item for item in catalog["tutorials"] if item["key"] == "instagram_insights")
        section = next(item for item in tutorial["sections"] if item["id"] == "acoes-e-testes-derivados")
        self.assertIn("Ação/Teste → Recomendação → Achado → Evidência", " ".join(section["body"]))
        self.assertIn("não prova causalidade", " ".join(section["body"]))
        self.assertIn("Cancelar preserva", " ".join(section["body"]))
        self.assertIn("Excluir remove definitivamente", " ".join(section["body"]))
        self.assertIn("digitar “deletar”", " ".join(section["body"]))
        self.assertIn("digitar “deletar”", " ".join(section["body"]))

    def test_routes_csrf_ownership_archived_and_traceability(self):
        base = f"/analytics/instagram/projects/{self.project.id}/insights"
        page = self.client.get(base)
        html = page.get_data(as_text=True)
        self.assertIn('title="Transformar em ação"', html)
        self.assertIn('aria-label="Transformar em ação"', html)
        self.assertIn('title="Criar teste"', html)
        self.assertIn('aria-label="Criar teste"', html)
        self.assertNotIn(">Transformar em ação</a>", html)
        self.assertNotIn(">Criar teste</a>", html)
        self.assertIn("Ações/Testes", html)
        self.assertIn("Ainda não há ações ou testes derivados", html)
        action_new_url = base + f"/recommendations/{self.recommendation.id}/actions/action/new"
        action_form = self.client.get(action_new_url)
        self.assertEqual(action_form.status_code, 200)
        self.assertIn("Tipo", action_form.get_data(as_text=True))
        self.assertIn("Limitações para leitura", action_form.get_data(as_text=True))
        self.assertEqual(self.client.post(action_new_url, data={
            **self.values(hypothesis=""), "csrf_token": csrf_from(action_form),
        }).status_code, 302)
        actions_html = self.client.get(base).get_data(as_text=True)
        actions_section = actions_html[actions_html.index('id="instagram-actions"'):]
        self.assertIn('data-partial-update data-partial-target="#instagram-actions"', actions_html)
        self.assertIn('name="action_kind" data-auto-submit', actions_html)
        self.assertIn('name="action_status" data-auto-submit', actions_html)
        self.assertNotIn('onchange="this.form.submit()"', actions_section)
        self.assertIn('<noscript><button class="btn btn-outline-primary btn-sm" type="submit">Atualizar</button></noscript>', actions_section)
        self.assertIn('js/platform_auto_submit.js', actions_html)
        new_url = base + f"/recommendations/{self.recommendation.id}/actions/test/new"
        form = self.client.get(new_url)
        self.assertEqual(form.status_code, 200)
        self.assertIn("Recomendação de origem", form.get_data(as_text=True))
        self.assertEqual(self.client.post(new_url, data=self.values()).status_code, 400)
        posted = self.client.post(new_url, data={**self.values(), "csrf_token": csrf_from(form)})
        self.assertEqual(posted.status_code, 302)
        action = db.session.scalar(select(InstagramRecommendationAction))
        detail_url = base + f"/actions/{action.id}"
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Rastreabilidade", detail.get_data(as_text=True))
        self.assertIn(self.finding.title, detail.get_data(as_text=True))
        started = self.client.post(detail_url + "/start", data={"csrf_token": csrf_from(detail)})
        self.assertEqual(started.status_code, 302)
        completion_page = self.client.get(detail_url)
        completed = self.client.post(detail_url + "/complete", data={
            "csrf_token": csrf_from(completion_page),
            "result_summary": "Resultado inconclusivo por período curto.",
            "observed_result": "0", "outcome": "inconclusive",
        })
        self.assertEqual(completed.status_code, 302)

        filtered = self.client.get(base + "?period=30d&action_kind=action&action_status=completed")
        filtered_html = filtered.get_data(as_text=True)
        self.assertIn('<input type="hidden" name="period" value="30d">', filtered_html)
        self.assertIn('<option value="action" selected>Tipo: Ação</option>', filtered_html)
        self.assertIn('<option value="completed" selected>Status: Concluída</option>', filtered_html)
        empty_filtered_html = self.client.get(
            base + "?period=30d&action_kind=test&action_status=completed"
        ).get_data(as_text=True)
        self.assertIn('name="action_kind" data-auto-submit', empty_filtered_html)
        self.assertIn('<option value="test" selected>Tipo: Teste</option>', empty_filtered_html)
        self.assertIn('Nenhuma ação ou teste corresponde aos filtros selecionados.', empty_filtered_html)

        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.other.email)
        self.assertEqual(self.client.get(detail_url).status_code, 404)
        self.assertEqual(self.client.get(new_url).status_code, 404)
        self.assertEqual(self.client.get(base + "/recommendations/00000000-0000-0000-0000-000000000000/actions/action/new").status_code, 404)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client)
        self.project.status = "archived"
        self.project.archived_at = WHEN
        db.session.commit()
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        self.assertEqual(self.client.get(new_url).status_code, 404)
        self.assertEqual(self.client.post(
            detail_url + "/start", data={"csrf_token": csrf_from(self.client.get(detail_url))},
        ).status_code, 404)

    def test_delete_route_uses_global_confirmation_before_post_and_preserves_filters(self):
        action = create_recommendation_action(
            self.project, self.recommendation, "test", self.values(title="Teste a excluir"), actor_id=self.user.id,
        )
        base = f"/analytics/instagram/projects/{self.project.id}/insights"
        page = self.client.get(base + "?period=30d&action_kind=test&action_status=planned")
        html = page.get_data(as_text=True)
        delete_url = base + f"/actions/{action.id}/delete"
        self.assertIn(delete_url, html)
        self.assertIn('title="Excluir teste"', html)
        self.assertIn('aria-label="Excluir teste"', html)
        self.assertIn("btn-outline-danger btn-sm platform-action-button", html)
        self.assertLess(html.index(f"/actions/{action.id}\""), html.index(delete_url))
        self.assertNotIn("onsubmit=\"return confirm(", html)

        confirmation_url = delete_url + "?period=30d&action_kind=test&action_status=planned"
        confirmation = self.client.get(confirmation_url)
        self.assertEqual(confirmation.status_code, 200)
        confirmation_html = confirmation.get_data(as_text=True)
        self.assertIn("Confirmar exclusão permanente", confirmation_html)
        self.assertIn("Você está prestes a excluir permanentemente este teste.", confirmation_html)
        self.assertIn(action.title, confirmation_html)
        self.assertIn('data-delete-confirm="final-delete-button"', confirmation_html)
        self.assertIn("Digite <strong>deletar</strong> para confirmar", confirmation_html)
        self.assertIn("Excluir permanentemente", confirmation_html)
        self.assertIn("Cancelar", confirmation_html)
        self.assertIsNotNone(db.session.get(InstagramRecommendationAction, action.id))
        self.assertEqual(self.client.post(confirmation_url, data={}).status_code, 400)

        invalid = self.client.post(confirmation_url, data={
            "csrf_token": csrf_from(confirmation), "confirmation": "excluir",
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('Digite exatamente &#34;deletar&#34;', invalid.get_data(as_text=True))
        self.assertIsNotNone(db.session.get(InstagramRecommendationAction, action.id))

        deleted = self.client.post(confirmation_url, data={
            "csrf_token": csrf_from(confirmation), "confirmation": "deletar",
        })
        self.assertEqual(deleted.status_code, 302)
        self.assertIn("period=30d", deleted.headers["Location"])
        self.assertIn("action_kind=test", deleted.headers["Location"])
        self.assertIn("action_status=planned", deleted.headers["Location"])
        self.assertIn("#instagram-actions", deleted.headers["Location"])
        self.assertIsNone(db.session.get(InstagramRecommendationAction, action.id))
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendation)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightFinding)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramInsightEvidence)), 1)
        self.assertIn(
            "Ainda não há ações ou testes derivados das recomendações deste projeto.",
            self.client.get(base).get_data(as_text=True),
        )

        protected_action = create_recommendation_action(
            self.project, self.recommendation, "action", self.values(hypothesis=""), actor_id=self.user.id,
        )
        protected_url = base + f"/actions/{protected_action.id}/delete"
        action_confirmation = self.client.get(protected_url)
        self.assertEqual(action_confirmation.status_code, 200)
        self.assertIn(
            "Você está prestes a excluir permanentemente esta ação.",
            action_confirmation.get_data(as_text=True),
        )
        self.assertIsNotNone(db.session.get(InstagramRecommendationAction, protected_action.id))
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.other.email)
        self.assertEqual(
            self.client.post(protected_url, data={"csrf_token": csrf_from(self.client.get("/"))}).status_code,
            404,
        )
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client)
        self.project.status = "archived"
        self.project.archived_at = WHEN
        db.session.commit()
        self.assertEqual(
            self.client.post(protected_url, data={"csrf_token": csrf_from(self.client.get(base))}).status_code,
            404,
        )

    def test_insights_template_reuses_distinct_global_icons_for_action_and_test(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "platform" / "instagram" / "insights.html"
        ).read_text(encoding="utf-8")
        self.assertIn("platform-action-button", template)
        self.assertIn("icon('ativar')", template)
        self.assertIn("icon('analytics')", template)
        self.assertIn("icon('lixeira')", template)
        self.assertIn("delete_recommendation_action_route", template)
        self.assertIn('title="Transformar em ação"', template)
        self.assertIn('title="Criar teste"', template)
        self.assertIn('data-partial-target="#instagram-actions"', template)

    def test_permanent_action_delete_reuses_the_shared_confirmation_component(self):
        root = Path(__file__).resolve().parents[1] / "templates" / "platform"
        for template_name in (
            "project_delete.html",
            "instagram/project_delete.html",
            "instagram/recommendation_action_delete.html",
        ):
            template = (root / template_name).read_text(encoding="utf-8")
            self.assertIn("platform/_permanent_delete_confirmation.html", template)

    def test_lifecycle_removes_actions_before_recommendation_chain(self):
        create_recommendation_action(
            self.project, self.recommendation, "action", self.values(hypothesis=""), actor_id=self.user.id,
        )
        self.project.status = "archived"
        self.project.archived_at = WHEN
        db.session.commit()
        delete_instagram_project_permanently(self.project, self.user, "deletar")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendationAction)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramRecommendation)), 0)


class InstagramActionsMigrationTests(unittest.TestCase):
    def test_actions_migration_is_reversible_child_of_recommendations_head(self):
        from app import create_app

        migrations = str(Path(__file__).resolve().parents[1] / "migrations")
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-actions-migration-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "actions-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=migrations, revision="b9cad1e2f3a4")
                self.assertNotIn("instagram_recommendation_actions", inspect(db.engine).get_table_names())
                upgrade(directory=migrations, revision="c0d1e2f3a4b5")
                inspector = inspect(db.engine)
                self.assertIn("instagram_recommendation_actions", inspector.get_table_names())
                self.assertIn(
                    "ck_instagram_action_status",
                    {item["name"] for item in inspector.get_check_constraints("instagram_recommendation_actions")},
                )
                self.assertIn(
                    "ck_instagram_action_completion_payload",
                    {item["name"] for item in inspector.get_check_constraints("instagram_recommendation_actions")},
                )
                downgrade(directory=migrations, revision="b9cad1e2f3a4")
                self.assertNotIn("instagram_recommendation_actions", inspect(db.engine).get_table_names())
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
