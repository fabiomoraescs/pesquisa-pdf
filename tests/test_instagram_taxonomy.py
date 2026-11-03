"""Taxonomias editoriais manuais, históricas e isoladas por projeto."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, inspect, select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_dashboard import build_instagram_dashboard
from platform_core.instagram_taxonomy import (
    DEFAULT_TAXONOMY_VERSION,
    TaxonomyMetricInput,
    TaxonomyValidationError,
    classification_states,
    classify_media,
    create_category,
    create_initial_taxonomy,
    current_classifications,
    set_category_active,
    taxonomy_dashboard_data,
    taxonomy_dimensions,
)
from platform_core.models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramMedia,
    InstagramMediaClassification,
    InstagramMediaObservation,
    PlanTool,
)
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


UTC = timezone.utc
PREVIOUS = "d4e8f0a1b2c3"
REVISION = "e5f6a7b8c9d0"
MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


class InstagramTaxonomyTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.user = create_user()
        self.other = create_user("Outra pessoa", "outra-taxonomy@example.org")
        db.session.add(PlanTool(plan_id="research_quali", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        self.project = AnalyticsProject(owner_user_id=self.user.id, name="Instagram da revista")
        db.session.add(self.project)
        db.session.flush()
        self.account = InstagramAccount(
            analytics_project_id=self.project.id,
            username="@revista_exemplo",
            username_normalized="revista_exemplo",
        )
        db.session.add(self.account)
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def media(self, external_id: str, *, offset: int = 1) -> InstagramMedia:
        item = InstagramMedia(
            instagram_account_id=self.account.id,
            external_id=external_id,
            caption_original=f"Legenda {external_id}",
            media_type_raw="REEL",
            published_at=datetime(2026, 10, offset, 12, tzinfo=UTC),
        )
        db.session.add(item)
        db.session.flush()
        return item

    def dimensions(self):
        return {dimension.key: dimension for dimension in taxonomy_dimensions(self.project.id)}

    def defaults(self):
        create_initial_taxonomy(self.project)
        db.session.commit()
        return self.dimensions()

    def selected(self, **values):
        return {
            dimension.id: values.get(dimension.key, ())
            for dimension in taxonomy_dimensions(self.project.id, active_only=True)
        }

    def test_initial_taxonomy_is_explicit_complete_and_idempotent(self):
        self.assertEqual(taxonomy_dimensions(self.project.id), ())
        first = self.defaults()
        self.assertEqual(set(first), {
            "communicational-objective", "editorial-type", "call-to-action", "main-theme",
        })
        self.assertEqual(first["call-to-action"].selection_mode, "multi")
        self.assertEqual(first["main-theme"].categories, [])
        self.assertEqual([category.name for category in first["communicational-objective"].categories], [
            "Informar", "Educar / Orientar", "Divulgar / Promover", "Mobilizar / Convidar",
            "Relacionar / Engajar", "Institucional", "Outro",
        ])
        create_initial_taxonomy(self.project)
        db.session.commit()
        second = taxonomy_dimensions(self.project.id)
        self.assertEqual(len(second), 4)
        self.assertEqual(sum(len(dimension.categories) for dimension in second), 25)
        self.assertEqual(DEFAULT_TAXONOMY_VERSION, "instagram-taxonomy-default-v1")

    def test_single_and_multiple_selection_preserve_history_without_duplicates(self):
        dimensions = self.defaults()
        media = self.media("post-1")
        objective = dimensions["communicational-objective"]
        editorial = dimensions["editorial-type"]
        cta = dimensions["call-to-action"]
        inform = next(category for category in objective.categories if category.key == "inform")
        educate = next(category for category in objective.categories if category.key == "educate-orient")
        news = next(category for category in editorial.categories if category.key == "news-notice")
        comment = next(category for category in cta.categories if category.key == "comment")
        share = next(category for category in cta.categories if category.key == "share")

        classify_media(self.project, media.id, self.selected(
            **{"communicational-objective": (inform.id,), "editorial-type": (news.id,),
               "call-to-action": (comment.id, share.id)}
        ), self.user)
        db.session.commit()
        classify_media(self.project, media.id, self.selected(
            **{"communicational-objective": (educate.id,), "editorial-type": (news.id,),
               "call-to-action": (comment.id,)}
        ), self.user)
        db.session.commit()

        objective_history = db.session.scalars(select(InstagramMediaClassification).where(
            InstagramMediaClassification.instagram_media_id == media.id,
            InstagramMediaClassification.instagram_taxonomy_dimension_id == objective.id,
        )).all()
        self.assertEqual(len(objective_history), 2)
        self.assertEqual(sum(item.superseded_at is None for item in objective_history), 1)
        self.assertEqual(current_classifications(self.project.id, media_ids=(media.id,), dimension_id=objective.id)[0].category.id, educate.id)
        current_cta = current_classifications(self.project.id, media_ids=(media.id,), dimension_id=cta.id)
        self.assertEqual([item.category.id for item in current_cta], [comment.id])
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaClassification)), 5)

    def test_status_and_inactive_categories_follow_domain_rules(self):
        dimensions = self.defaults()
        media = self.media("post-2")
        objective = dimensions["communicational-objective"]
        editorial = dimensions["editorial-type"]
        inform = objective.categories[0]
        news = editorial.categories[0]
        states = classification_states(self.project, (media.id,))
        self.assertEqual(states[media.id].status, "unclassified")
        classify_media(self.project, media.id, self.selected(**{"communicational-objective": (inform.id,)}), self.user)
        db.session.commit()
        self.assertEqual(classification_states(self.project, (media.id,))[media.id].status, "partial")
        classify_media(self.project, media.id, self.selected(
            **{"communicational-objective": (inform.id,), "editorial-type": (news.id,)}
        ), self.user)
        db.session.commit()
        self.assertEqual(classification_states(self.project, (media.id,))[media.id].status, "classified")
        set_category_active(self.project, news.id, active=False)
        db.session.commit()
        with self.assertRaises(TaxonomyValidationError):
            classify_media(self.project, media.id, self.selected(
                **{"communicational-objective": (inform.id,), "editorial-type": (news.id,)}
            ), self.user)
        self.assertEqual(current_classifications(self.project.id, media_ids=(media.id,), dimension_id=editorial.id)[0].category.id, news.id)

    def test_category_must_belong_to_its_dimension_and_media_to_its_project(self):
        dimensions = self.defaults()
        media = self.media("post-3")
        wrong_category = dimensions["editorial-type"].categories[0]
        with self.assertRaises(TaxonomyValidationError):
            classify_media(self.project, media.id, self.selected(
                **{"communicational-objective": (wrong_category.id,)}
            ), self.user)

        other_project = AnalyticsProject(owner_user_id=self.other.id, name="Outro Instagram")
        db.session.add(other_project)
        db.session.flush()
        other_account = InstagramAccount(
            analytics_project_id=other_project.id, username="@outro", username_normalized="outro"
        )
        db.session.add(other_account)
        db.session.flush()
        other_media = InstagramMedia(instagram_account_id=other_account.id, external_id="other-post")
        db.session.add(other_media)
        db.session.commit()
        with self.assertRaises(TaxonomyValidationError):
            classify_media(self.project, other_media.id, {}, self.user)

    def test_dashboard_uses_current_categories_and_respects_null_and_multi_overlap(self):
        dimensions = self.defaults()
        objective = dimensions["communicational-objective"]
        cta = dimensions["call-to-action"]
        inform = objective.categories[0]
        educate = objective.categories[1]
        comment = cta.categories[0]
        share = cta.categories[1]
        first, second, third = self.media("first"), self.media("second"), self.media("third")
        classify_media(self.project, first.id, self.selected(
            **{"communicational-objective": (inform.id,), "call-to-action": (comment.id,)}
        ), self.user)
        classify_media(self.project, second.id, self.selected(
            **{"communicational-objective": (educate.id,), "call-to-action": (comment.id, share.id)}
        ), self.user)
        db.session.commit()
        values = {
            first.id: TaxonomyMetricInput(first.id, {"reach": 1000, "interactions": 100}, {"interaction_rate_by_reach": 10.0}),
            second.id: TaxonomyMetricInput(second.id, {"reach": None, "interactions": 30}, {"interaction_rate_by_reach": None}),
            third.id: TaxonomyMetricInput(third.id, {"reach": 500, "interactions": 0}, {"interaction_rate_by_reach": 0.0}),
        }
        single = taxonomy_dashboard_data(self.project, values.values(), selected_dimension_id=objective.id)
        by_name = {item.name: item for item in single.comparisons}
        self.assertEqual(by_name["Informar"].reach.median_value, 1000)
        self.assertEqual(by_name["Educar / Orientar"].reach.known, 0)
        self.assertEqual((by_name["Não classificado"].publications, by_name["Não classificado"].interaction_rate_by_reach.median_value), (1, 0.0))
        multi = taxonomy_dashboard_data(self.project, values.values(), selected_dimension_id=cta.id)
        multi_by_name = {item.name: item for item in multi.comparisons}
        self.assertTrue(multi.is_multi)
        self.assertEqual(multi_by_name["Comentar"].publications, 2)
        self.assertEqual(multi_by_name["Compartilhar"].publications, 1)
        self.assertNotIn("Não classificado", multi_by_name)

    def test_reimported_observation_does_not_erase_classification(self):
        dimensions = self.defaults()
        media = self.media("post-history")
        inform = dimensions["communicational-objective"].categories[0]
        classify_media(self.project, media.id, self.selected(
            **{"communicational-objective": (inform.id,)}
        ), self.user)
        first_run = AnalyticsRun(analytics_project_id=self.project.id, status="completed")
        second_run = AnalyticsRun(analytics_project_id=self.project.id, status="completed")
        db.session.add_all((first_run, second_run))
        db.session.flush()
        db.session.add_all((
            InstagramMediaObservation(instagram_media_id=media.id, analytics_run_id=first_run.id,
                                      observed_at=datetime(2026, 10, 1, tzinfo=UTC), reach=1000),
            InstagramMediaObservation(instagram_media_id=media.id, analytics_run_id=second_run.id,
                                      observed_at=datetime(2026, 10, 2, tzinfo=UTC), reach=1500),
        ))
        db.session.commit()
        self.assertEqual(len(current_classifications(self.project.id, media_ids=(media.id,))), 1)
        dashboard = build_instagram_dashboard(self.project, "all", "America/Maceio")
        self.assertEqual(dashboard.metrics[0].value, 1500)
        self.assertEqual(dashboard.taxonomy.comparisons[0].name, "Informar")

    def test_dashboard_renders_taxonomy_analytics_and_rejects_foreign_dimension(self):
        dimensions = self.defaults()
        media = self.media("dashboard-post")
        inform = dimensions["communicational-objective"].categories[0]
        classify_media(self.project, media.id, self.selected(
            **{"communicational-objective": (inform.id,)}
        ), self.user)
        run = AnalyticsRun(analytics_project_id=self.project.id, status="completed")
        db.session.add(run)
        db.session.flush()
        db.session.add(InstagramMediaObservation(
            instagram_media_id=media.id, analytics_run_id=run.id,
            observed_at=datetime(2026, 10, 4, tzinfo=UTC), reach=1000, interactions=100,
        ))
        db.session.commit()
        dashboard_url = f"/analytics/instagram/projects/{self.project.id}/dashboard"
        html = self.client.get(
            dashboard_url + f"?period=all&taxonomy_dimension={dimensions['communicational-objective'].id}"
        ).get_data(as_text=True)
        for text in (
            "Desempenho por classificação editorial", "Objetivo comunicacional", "Informar",
            "Taxa mediana de interação por alcance por categoria",
            "instagram-chart-taxonomy-interaction-rate",
        ):
            self.assertIn(text, html)
        self.assertEqual(self.client.get(dashboard_url + "?taxonomy_dimension=not-a-dimension").status_code, 400)

    def test_routes_require_ownership_csrf_and_block_mutations_when_archived(self):
        project_url = f"/analytics/instagram/projects/{self.project.id}"
        detail = self.client.get(project_url).get_data(as_text=True)
        self.assertIn("Taxonomias", detail)
        self.assertIn("Classificação de conteúdos", detail)
        taxonomy_url = f"{project_url}/taxonomies"
        self.assertIn("Criar estrutura inicial", self.client.get(taxonomy_url).get_data(as_text=True))
        self.assertEqual(self.client.post(f"{taxonomy_url}/initial").status_code, 400)
        page = self.client.get(taxonomy_url)
        created = self.client.post(f"{taxonomy_url}/initial", data={"csrf_token": csrf_from(page)})
        self.assertEqual(created.status_code, 302)
        self.assertIn("Objetivo comunicacional", self.client.get(taxonomy_url).get_data(as_text=True))
        page = self.client.get(taxonomy_url)
        created_dimension = self.client.post(f"{taxonomy_url}/dimensions", data={
            "csrf_token": csrf_from(page), "name": "Formato de convite", "selection_mode": "multi",
        })
        self.assertEqual(created_dimension.status_code, 302)
        custom_dimension = next(
            item for item in taxonomy_dimensions(self.project.id) if item.key == "formato-de-convite"
        )
        page = self.client.get(taxonomy_url)
        created_category = self.client.post(
            f"{taxonomy_url}/dimensions/{custom_dimension.id}/categories", data={
                "csrf_token": csrf_from(page), "name": "Convite aberto",
            },
        )
        self.assertEqual(created_category.status_code, 302)
        self.assertEqual(taxonomy_dimensions(self.project.id)[-1].categories[0].name, "Convite aberto")
        media = self.media("route-post")
        db.session.commit()
        classification_url = f"{project_url}/classifications/{media.id}"
        self.assertEqual(self.client.get(classification_url).status_code, 200)
        self.assertEqual(self.client.post(classification_url).status_code, 400)
        self.project.status = "archived"
        self.project.archived_at = datetime(2026, 10, 3, tzinfo=UTC)
        db.session.commit()
        self.assertEqual(self.client.get(taxonomy_url).status_code, 200)
        self.assertEqual(self.client.get(classification_url).status_code, 200)
        archived_post = self.client.post(f"{taxonomy_url}/initial", data={"csrf_token": csrf_from(self.client.get(taxonomy_url))})
        self.assertEqual(archived_post.status_code, 404)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.other.email)
        self.assertEqual(self.client.get(taxonomy_url).status_code, 404)
        self.assertEqual(self.client.get(f"{project_url}/classifications").status_code, 404)


class InstagramTaxonomyMigrationTests(unittest.TestCase):
    def test_incremental_migration_creates_only_taxonomy_tables(self):
        from app import create_app

        with tempfile.TemporaryDirectory(prefix="pesquisapdf-taxonomy-migration-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "taxonomy-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=MIGRATIONS, revision=PREVIOUS)
                before = set(inspect(db.engine).get_table_names())
                self.assertNotIn("instagram_taxonomy_dimensions", before)
                upgrade(directory=MIGRATIONS, revision=REVISION)
                inspector = inspect(db.engine)
                expected = {
                    "instagram_taxonomy_dimensions", "instagram_taxonomy_categories",
                    "instagram_media_classifications",
                }
                self.assertTrue(expected.issubset(set(inspector.get_table_names())))
                self.assertIn(
                    "uq_instagram_taxonomy_dimension_project_key",
                    {item["name"] for item in inspector.get_unique_constraints("instagram_taxonomy_dimensions")},
                )
                self.assertIn(
                    "ix_instagram_media_classifications_media_dimension_current",
                    {item["name"] for item in inspector.get_indexes("instagram_media_classifications")},
                )
                downgrade(directory=MIGRATIONS, revision=PREVIOUS)
                self.assertTrue(expected.isdisjoint(set(inspect(db.engine).get_table_names())))
                self.assertIn("instagram_media", set(inspect(db.engine).get_table_names()))
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
