"""Sincronização Meta nos mesmos modelos canônicos da importação manual."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import unittest

from cryptography.fernet import Fernet
from sqlalchemy import func, select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_meta import (
    FACEBOOK_LOGIN_SCOPES,
    INSTAGRAM_LOGIN_SCOPES,
    FacebookLoginProvider,
    InstagramMetaError,
    InstagramMetaSettings,
    persist_validated_connection,
)
from platform_core.instagram_meta_sync import (
    META_SYNC_SOURCE_KIND,
    META_SYNC_VERSION,
    InstagramMetaSyncError,
    resolve_publication_window,
    sync_meta_connection,
)
from platform_core.instagram_meta_sync_jobs import (
    META_SYNC_FAILED,
    META_SYNC_PENDING,
    META_SYNC_RUNNING,
    process_next_meta_sync_run,
    recover_stale_meta_sync_runs,
)
from platform_core.models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccountSnapshot,
    InstagramApiConnection,
    InstagramComment,
    InstagramMedia,
    InstagramMediaClassification,
    InstagramMediaObservation,
    InstagramTaxonomyCategory,
    InstagramTaxonomyDimension,
    PlanTool,
)
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


ROOT = Path(__file__).resolve().parents[1]


class FakeMetaSyncClient:
    def __init__(
        self, *, profile=None, media=None, metrics=None, metric_errors_by_media=None,
        batch_omissions_by_media=None, account_reach=900, profile_enrichment=None,
    ):
        self.profile = profile or {
            "id": "ig-official-42", "username": "revista_reels", "followers_count": 120,
        }
        self.media = media or [
            {
                "id": "media-reel-1", "media_type": "REEL", "caption": "Reel oficial",
                "permalink": "https://instagram.example/reel-1", "timestamp": "2026-10-01T12:00:00+00:00",
                "like_count": 7, "comments_count": 2,
            },
            {
                "id": "media-image-1", "media_type": "IMAGE", "media_product_type": "FEED", "caption": "Imagem oficial",
                "permalink": "https://instagram.example/image-1", "timestamp": "2026-10-02T12:00:00+00:00",
                "like_count": 0, "comments_count": 0,
            },
        ]
        self.metrics = metrics or {
            "reach": 100, "saved": 0, "shares": 4, "total_interactions": 13, "follows": 2,
        }
        self.metric_errors_by_media = metric_errors_by_media or {}
        self.batch_omissions_by_media = batch_omissions_by_media or {}
        self.account_reach = account_reach
        self.profile_enrichment = profile_enrichment
        self.calls = []

    def instagram_profile(self, account_id, token):
        self.calls.append(("profile", account_id, token))
        return self.profile

    def list_media(self, account_id, token):
        self.calls.append(("list_media", account_id, token))
        return self.media

    def instagram_profile_enrichment(self, account_id, token):
        self.calls.append(("profile_enrichment", account_id, token))
        if isinstance(self.profile_enrichment, Exception):
            raise self.profile_enrichment
        return self.profile_enrichment or {}

    def media_metric(self, media_id, metric, token):
        self.calls.append(("metric", media_id, metric, token))
        error = self.metric_errors_by_media.get((media_id, metric))
        if error is not None:
            raise error
        value = self.metrics.get(metric)
        if isinstance(value, Exception):
            raise value
        values = [] if value is None else [{"value": value}]
        return {"data": [{"name": metric, "values": values}]}

    def media_metrics(self, media_id, metrics, token):
        metrics = tuple(metrics)
        self.calls.append(("metric_batch", media_id, metrics, token))
        rows = []
        for metric in metrics:
            error = self.metric_errors_by_media.get((media_id, metric))
            if error is not None:
                raise error
            if metric in self.batch_omissions_by_media.get(media_id, set()):
                continue
            error = self.metrics.get(metric)
            if isinstance(error, Exception):
                raise error
            values = [] if error is None else [{"value": error}]
            rows.append({"name": metric, "values": values})
        return {"data": rows}

    def account_insights(self, account_id, token):
        self.calls.append(("account_insights", account_id, token))
        if isinstance(self.account_reach, Exception):
            raise self.account_reach
        values = [] if self.account_reach is None else [{"value": self.account_reach}]
        return {"data": [{"name": "reach", "values": values}]}


class InstagramMetaSyncTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.app.config.update({
            "PESQUISAPDF_META_APP_ID": "app-id-for-tests",
            "PESQUISAPDF_META_APP_SECRET": "app-secret-for-tests",
            "PESQUISAPDF_META_INSTAGRAM_APP_ID": "instagram-app-id-for-tests",
            "PESQUISAPDF_META_INSTAGRAM_APP_SECRET": "instagram-app-secret-for-tests",
            "PESQUISAPDF_META_REDIRECT_URI": "https://analysis.example/meta/callback",
            "PESQUISAPDF_META_API_VERSION": "v26.0",
            "PESQUISAPDF_META_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
            "PRESENTATION_TIMEZONE": "America/Maceio",
        })
        self.settings = InstagramMetaSettings.from_mapping(self.app.config)
        self.client = self.app.test_client()
        self.user = create_user()
        self.other_user = create_user("Outra", "outra-sync@example.org")
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        self.project = AnalyticsProject(owner_user_id=self.user.id, module_key="instagram", name="Sync Meta")
        self.other_project = AnalyticsProject(owner_user_id=self.other_user.id, module_key="instagram", name="Outro")
        db.session.add_all((self.project, self.other_project))
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def _connection(self, *, provider="facebook_login", profile=None):
        profile = profile or {
            "id": "ig-official-42", "username": "revista_reels", "followers_count": 120,
        }
        connection = persist_validated_connection(
            self.project,
            access_token="token-very-secret-for-tests",
            profile=profile,
            granted_scopes=FACEBOOK_LOGIN_SCOPES if provider == "facebook_login" else INSTAGRAM_LOGIN_SCOPES,
            settings=self.settings,
            provider=provider,
        )
        db.session.commit()
        return connection

    def _sync_url(self, project=None):
        return f"/analytics/instagram/projects/{(project or self.project).id}/imports/meta/sync"

    def test_facebook_full_sync_persists_only_canonical_models_and_keeps_zero(self):
        connection = self._connection()
        fake = FakeMetaSyncClient()

        result = sync_meta_connection(self.project, connection, self.settings, fake)

        run = db.session.get(AnalyticsRun, result.run_id)
        self.assertEqual(run.source_kind, META_SYNC_SOURCE_KIND)
        self.assertEqual(run.processor_version, META_SYNC_VERSION)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.record_count, 2)
        self.assertIsNone(run.period_start)
        self.assertIsNone(run.period_end)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 2)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation)), 2)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramComment)), 0)
        image = db.session.scalar(select(InstagramMedia).where(InstagramMedia.external_id == "media-image-1"))
        observation = db.session.scalar(select(InstagramMediaObservation).where(
            InstagramMediaObservation.instagram_media_id == image.id
        ))
        self.assertEqual(observation.saves, 0)
        self.assertEqual(observation.likes, 0)
        self.assertEqual(observation.comments_count, 0)
        self.assertEqual(observation.reach, 100)
        self.assertEqual(observation.follows_generated, 2)
        self.assertIsNone(observation.impressions)
        self.assertIsNone(observation.plays)
        self.assertFalse(any(call[0] == "metric" and call[2] == "views" for call in fake.calls))
        self.assertEqual(image.media_type_raw, "IMAGE")
        snapshot = db.session.scalar(select(InstagramAccountSnapshot))
        self.assertEqual(snapshot.followers_count, 120)
        self.assertEqual(snapshot.account_reach, 900)
        self.assertIsNone(snapshot.new_followers)
        self.assertNotIn("token-very-secret-for-tests", str(run.parameters_json))

    def test_direct_sync_persists_confirmed_insights_and_keeps_views_unmapped(self):
        profile = {"id": "ig-official-42", "username": "revista_reels", "followers_count": 120}
        connection = self._connection(provider="instagram_login", profile=profile)
        fake = FakeMetaSyncClient(profile=profile)

        result = sync_meta_connection(self.project, connection, self.settings, fake)

        observations = db.session.scalars(select(InstagramMediaObservation).where(
            InstagramMediaObservation.analytics_run_id == result.run_id
        )).all()
        self.assertEqual(sorted(observation.likes for observation in observations), [0, 7])
        self.assertEqual(sorted(observation.comments_count for observation in observations), [0, 2])
        self.assertTrue(all(observation.reach == 100 for observation in observations))
        self.assertTrue(all(observation.saves == 0 for observation in observations))
        self.assertTrue(all(observation.shares == 4 for observation in observations))
        self.assertTrue(all(observation.interactions == 13 for observation in observations))
        self.assertTrue(all(observation.follows_generated == 2 for observation in observations))
        self.assertTrue(all(observation.impressions is None and observation.plays is None for observation in observations))
        snapshot = db.session.scalar(select(InstagramAccountSnapshot).where(
            InstagramAccountSnapshot.analytics_run_id == result.run_id
        ))
        self.assertEqual(snapshot.followers_count, 120)
        self.assertEqual(snapshot.account_reach, 900)
        self.assertTrue(any(call[0] == "metric" for call in fake.calls))

    def test_direct_sync_with_a_legacy_token_does_not_probe_ungranted_insights(self):
        profile = {"id": "ig-official-42", "username": "revista_reels", "followers_count": 120}
        connection = self._connection(provider="instagram_login", profile=profile)
        connection.scopes_json = ["instagram_business_basic", "instagram_business_manage_comments"]
        db.session.commit()
        fake = FakeMetaSyncClient(profile=profile)

        result = sync_meta_connection(self.project, connection, self.settings, fake)

        observations = db.session.scalars(select(InstagramMediaObservation).where(
            InstagramMediaObservation.analytics_run_id == result.run_id
        )).all()
        self.assertEqual(sorted(observation.likes for observation in observations), [0, 7])
        self.assertEqual(sorted(observation.comments_count for observation in observations), [0, 2])
        self.assertTrue(all(observation.reach is None for observation in observations))
        self.assertFalse(any(call[0] in {"metric", "metric_batch", "account_insights"} for call in fake.calls))

    def test_direct_sync_caches_only_an_explicitly_unsupported_metric_per_media_type(self):
        profile = {"id": "ig-official-42", "username": "revista_reels", "followers_count": 120}
        connection = self._connection(provider="instagram_login", profile=profile)
        reels = [
            {
                "id": f"direct-reel-{index}", "media_type": "REEL", "caption": "Reel",
                "permalink": f"https://instagram.example/direct-reel-{index}",
                "timestamp": "2026-10-01T12:00:00+00:00", "like_count": 0, "comments_count": 0,
            }
            for index in range(3)
        ]
        unsupported = InstagramMetaError("unsupported_metric", "Follows indisponível para Reels.")
        fake = FakeMetaSyncClient(
            profile=profile,
            media=reels,
            metric_errors_by_media={(row["id"], "follows"): unsupported for row in reels},
        )

        sync_meta_connection(self.project, connection, self.settings, fake)

        follows_probes = [
            call for call in fake.calls if call[0] == "metric" and call[2] == "follows"
        ]
        self.assertEqual(len(follows_probes), 1)

    def test_profile_followers_enrichment_is_optional_and_never_breaks_snapshot(self):
        profile = {"id": "ig-official-42", "username": "revista_reels"}
        connection = self._connection(profile=profile)
        fake = FakeMetaSyncClient(profile=profile, profile_enrichment={"followers_count": 321})

        result = sync_meta_connection(self.project, connection, self.settings, fake)

        snapshot = db.session.scalar(select(InstagramAccountSnapshot).where(
            InstagramAccountSnapshot.analytics_run_id == result.run_id
        ))
        self.assertEqual(snapshot.followers_count, 321)
        self.assertTrue(any(call[0] == "profile_enrichment" for call in fake.calls))

        second = FakeMetaSyncClient(
            profile=profile,
            profile_enrichment=InstagramMetaError("unsupported_metric", "Contagem indisponível."),
        )
        result = sync_meta_connection(self.project, connection, self.settings, second)
        snapshot = db.session.scalar(select(InstagramAccountSnapshot).where(
            InstagramAccountSnapshot.analytics_run_id == result.run_id
        ))
        self.assertIsNone(snapshot.followers_count)

    def test_second_sync_reuses_official_media_and_preserves_manual_classification(self):
        connection = self._connection()
        account = self.project.instagram_account
        existing = InstagramMedia(
            instagram_account=account,
            external_id="media-reel-1",
            caption_original="Legenda manual preservada",
            published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            media_type_raw="IMAGE",
        )
        dimension = InstagramTaxonomyDimension(
            analytics_project=self.project, key="tema", name="Tema", selection_mode="single", position=0,
        )
        db.session.add_all((existing, dimension))
        db.session.flush()
        category = InstagramTaxonomyCategory(
            dimension=dimension, key="educacao", name="Educação", position=0,
        )
        db.session.add(category)
        db.session.flush()
        classification = InstagramMediaClassification(
            instagram_media=existing, dimension=dimension, category=category,
            source="manual", assigned_by_user_id=self.user.id,
        )
        db.session.add(classification)
        db.session.commit()
        fake = FakeMetaSyncClient()

        first = sync_meta_connection(self.project, connection, self.settings, fake)
        second = sync_meta_connection(self.project, connection, self.settings, fake)

        self.assertEqual(first.media_created, 1)
        self.assertEqual(first.media_reused, 1)
        self.assertEqual(second.media_created, 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 2)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 2)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation)), 4)
        db.session.refresh(existing)
        self.assertEqual(existing.caption_original, "Legenda manual preservada")
        self.assertEqual(existing.media_type_raw, "REEL")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaClassification)), 1)

    def test_partial_metrics_are_null_but_structural_errors_roll_back_every_analytic_write(self):
        connection = self._connection()
        partial = FakeMetaSyncClient(metrics={
            "reach": 10,
            "saved": InstagramMetaError("unsupported_metric", "Sem métrica"),
            "shares": 1,
            "total_interactions": 2,
            "follows": 0,
        })
        result = sync_meta_connection(self.project, connection, self.settings, partial)
        observation = db.session.scalar(select(InstagramMediaObservation).where(
            InstagramMediaObservation.analytics_run_id == result.run_id
        ))
        self.assertIsNone(observation.saves)
        self.assertEqual(observation.follows_generated, 0)
        self.assertGreater(result.partial_metric_count, 0)

        with self.assertRaises(RuntimeError):
            with patch.object(db.session, "flush", side_effect=[None, RuntimeError("falha forçada")]):
                sync_meta_connection(self.project, connection, self.settings, FakeMetaSyncClient())
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation)), 2)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot)), 1)

    def test_unsupported_metric_is_learned_once_per_provider_media_type_and_sync(self):
        connection = self._connection()
        reels = [
            {
                "id": f"reel-{index}", "media_type": "REEL", "caption": "Reel",
                "permalink": f"https://instagram.example/reel-{index}",
                "timestamp": "2026-10-01T12:00:00+00:00", "like_count": 0, "comments_count": 0,
            }
            for index in range(20)
        ]
        unsupported = InstagramMetaError("unsupported_metric", "Follows não disponível para Reels.")
        fake = FakeMetaSyncClient(
            media=reels,
            metric_errors_by_media={(row["id"], "follows"): unsupported for row in reels},
        )

        result = sync_meta_connection(self.project, connection, self.settings, fake)

        follows_probes = [
            call for call in fake.calls if call[0] == "metric" and call[2] == "follows"
        ]
        batches = [call for call in fake.calls if call[0] == "metric_batch"]
        self.assertEqual(result.observations_created, 20)
        self.assertEqual(len(follows_probes), 1)
        self.assertEqual(len(batches), 19)
        self.assertTrue(all("follows" not in call[2] for call in batches))

    def test_metric_capability_cache_isolated_by_media_type(self):
        connection = self._connection()
        reels = [
            {
                "id": f"reel-{index}", "media_type": "REEL", "caption": "Reel",
                "permalink": f"https://instagram.example/reel-{index}",
                "timestamp": "2026-10-01T12:00:00+00:00", "like_count": 0, "comments_count": 0,
            }
            for index in range(10)
        ]
        images = [
            {
                "id": f"image-{index}", "media_type": "IMAGE", "media_product_type": "FEED", "caption": "Imagem",
                "permalink": f"https://instagram.example/image-{index}",
                "timestamp": "2026-10-02T12:00:00+00:00", "like_count": 0, "comments_count": 0,
            }
            for index in range(10)
        ]
        unsupported = InstagramMetaError("unsupported_metric", "Follows não disponível para Reels.")
        fake = FakeMetaSyncClient(
            media=reels + images,
            metric_errors_by_media={(row["id"], "follows"): unsupported for row in reels},
        )

        sync_meta_connection(self.project, connection, self.settings, fake)

        reel_follows = [
            call for call in fake.calls if call[0] == "metric" and call[1].startswith("reel-") and call[2] == "follows"
        ]
        image_follows_batches = [
            call for call in fake.calls if call[0] == "metric_batch" and call[1].startswith("image-") and "follows" in call[2]
        ]
        image = db.session.scalar(select(InstagramMedia).where(InstagramMedia.external_id == "image-0"))
        image_observation = db.session.scalar(select(InstagramMediaObservation).where(
            InstagramMediaObservation.instagram_media_id == image.id
        ))
        self.assertEqual(len(reel_follows), 1)
        self.assertEqual(len(image_follows_batches), 9)
        self.assertEqual(image_observation.follows_generated, 2)

    def test_metric_batch_fallback_learns_late_unsupported_without_losing_valid_metrics(self):
        connection = self._connection()
        reels = [
            {
                "id": f"reel-{index}", "media_type": "REEL", "caption": "Reel",
                "permalink": f"https://instagram.example/reel-{index}",
                "timestamp": "2026-10-01T12:00:00+00:00", "like_count": 0, "comments_count": 0,
            }
            for index in range(3)
        ]
        fake = FakeMetaSyncClient(
            media=reels,
            metric_errors_by_media={
                ("reel-1", "follows"): InstagramMetaError("unsupported_metric", "Follows indisponível."),
            },
        )

        result = sync_meta_connection(self.project, connection, self.settings, fake)

        second_observation = db.session.scalar(select(InstagramMediaObservation).where(
            InstagramMediaObservation.analytics_run_id == result.run_id,
            InstagramMediaObservation.instagram_media.has(external_id="reel-1"),
        ))
        third_batches = [
            call for call in fake.calls if call[0] == "metric_batch" and call[1] == "reel-2"
        ]
        self.assertIsNone(second_observation.follows_generated)
        self.assertEqual(second_observation.reach, 100)
        self.assertEqual(third_batches[0][2], ("reach", "saved", "shares", "total_interactions"))

    def test_partial_metric_batch_falls_back_only_for_the_missing_metric(self):
        connection = self._connection()
        reels = [
            {
                "id": f"reel-{index}", "media_type": "REEL", "caption": "Reel",
                "permalink": f"https://instagram.example/reel-{index}",
                "timestamp": "2026-10-01T12:00:00+00:00", "like_count": 0, "comments_count": 0,
            }
            for index in range(3)
        ]
        fake = FakeMetaSyncClient(media=reels, batch_omissions_by_media={"reel-1": {"shares"}})

        result = sync_meta_connection(self.project, connection, self.settings, fake)

        second_observation = db.session.scalar(select(InstagramMediaObservation).where(
            InstagramMediaObservation.analytics_run_id == result.run_id,
            InstagramMediaObservation.instagram_media.has(external_id="reel-1"),
        ))
        share_probes = [
            call for call in fake.calls if call[0] == "metric" and call[2] == "shares"
        ]
        self.assertEqual(second_observation.shares, 4)
        self.assertEqual(len(share_probes), 2)  # descoberta inicial + fallback da resposta parcial

    def test_temporary_metric_error_does_not_become_unsupported_cache(self):
        connection = self._connection()
        reels = [
            {
                "id": f"reel-{index}", "media_type": "REEL", "caption": "Reel",
                "permalink": f"https://instagram.example/reel-{index}",
                "timestamp": "2026-10-01T12:00:00+00:00", "like_count": 0, "comments_count": 0,
            }
            for index in range(2)
        ]
        temporary = InstagramMetaError("temporary", "A API está temporariamente indisponível.")
        fake = FakeMetaSyncClient(
            media=reels,
            metric_errors_by_media={(row["id"], "follows"): temporary for row in reels},
        )

        sync_meta_connection(self.project, connection, self.settings, fake)

        follows_probes = [
            call for call in fake.calls if call[0] == "metric" and call[2] == "follows"
        ]
        self.assertEqual(len(follows_probes), 2)

    def test_sync_route_requires_csrf_owner_active_project_and_hides_tokens(self):
        connection = self._connection()
        fake = FakeMetaSyncClient()
        self.app.extensions["instagram_meta_client_factory"] = lambda _settings, _provider: fake
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        markup = page.get_data(as_text=True)
        self.assertIn("Sincronizar dados", markup)
        self.assertIn('<option value="last_90_days" selected>', markup)
        self.assertIn("Período da sincronização", markup)
        self.assertEqual(self.client.post(self._sync_url()).status_code, 400)
        success = self.client.post(self._sync_url(), data={"csrf_token": csrf_from(page)}, follow_redirects=True)
        self.assertEqual(success.status_code, 200)
        self.assertIn("Sincronização iniciada", success.get_data(as_text=True))
        run = db.session.scalar(select(AnalyticsRun).where(AnalyticsRun.analytics_project_id == self.project.id))
        self.assertEqual(run.status, META_SYNC_PENDING)
        self.assertEqual(fake.calls, [])

        logout_page = self.client.get("/")
        self.client.post("/logout", data={"csrf_token": csrf_from(logout_page)})
        login(self.client, self.other_user.email)
        self.assertEqual(self.client.post(self._sync_url(), data={"csrf_token": csrf_from(page)}).status_code, 404)
        logout_page = self.client.get("/")
        self.client.post("/logout", data={"csrf_token": csrf_from(logout_page)})
        login(self.client, self.user.email)
        self.project.status = "archived"
        self.project.archived_at = datetime.now(timezone.utc)
        db.session.commit()
        self.assertEqual(self.client.post(self._sync_url(), data={"csrf_token": csrf_from(page)}).status_code, 404)
        self.assertEqual(connection.status, "connected")

    def test_sync_post_is_short_enqueues_once_and_status_requires_ownership(self):
        self._connection()
        fake = FakeMetaSyncClient()
        self.app.extensions["instagram_meta_client_factory"] = lambda _settings, _provider: fake
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        data = {"csrf_token": csrf_from(page)}

        first = self.client.post(self._sync_url(), data=data, headers={"Accept": "application/json"})
        self.assertEqual(first.status_code, 202)
        payload = first.get_json()
        self.assertEqual(payload["status"], META_SYNC_PENDING)
        self.assertFalse(payload["reused_active_run"])
        self.assertEqual(fake.calls, [])

        second = self.client.post(self._sync_url(), data=data, headers={"Accept": "application/json"})
        self.assertEqual(second.status_code, 202)
        self.assertTrue(second.get_json()["reused_active_run"])
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 1)
        self.assertEqual(self.client.get(payload["status_url"]).get_json()["status"], META_SYNC_PENDING)

        logout_page = self.client.get("/")
        self.client.post("/logout", data={"csrf_token": csrf_from(logout_page)})
        login(self.client, self.other_user.email)
        self.assertEqual(self.client.get(payload["status_url"]).status_code, 404)

    def test_period_presets_are_resolved_once_in_presentation_timezone(self):
        now = datetime(2026, 10, 10, 15, tzinfo=timezone.utc)
        expected_starts = {
            "last_7_days": datetime(2026, 10, 4, 3, tzinfo=timezone.utc),
            "last_30_days": datetime(2026, 9, 11, 3, tzinfo=timezone.utc),
            "last_60_days": datetime(2026, 8, 12, 3, tzinfo=timezone.utc),
            "last_90_days": datetime(2026, 7, 13, 3, tzinfo=timezone.utc),
            "last_6_months": datetime(2026, 4, 10, 3, tzinfo=timezone.utc),
            "last_12_months": datetime(2025, 10, 10, 3, tzinfo=timezone.utc),
        }
        for preset, expected_start in expected_starts.items():
            window = resolve_publication_window(preset, None, None, "America/Maceio", now=now)
            self.assertEqual(window.published_from, expected_start)
            self.assertEqual(window.published_to, now)
            self.assertTrue(window.end_clamped_to_now)
        history = resolve_publication_window("all_history", None, None, "America/Maceio", now=now)
        self.assertIsNone(history.published_from)
        self.assertIsNone(history.published_to)
        future_end = resolve_publication_window(
            "custom", "2026-10-09", "2026-10-17", "America/Maceio", now=now,
        )
        self.assertEqual(future_end.published_to, now)
        self.assertTrue(future_end.end_clamped_to_now)

    def test_custom_period_validation_accepts_one_day_and_rejects_missing_or_malformed_values(self):
        now = datetime(2026, 10, 10, 15, tzinfo=timezone.utc)
        same_day = resolve_publication_window(
            "custom", "2026-10-09", "2026-10-09", "America/Maceio", now=now,
        )
        self.assertEqual(same_day.published_from, datetime(2026, 10, 9, 3, tzinfo=timezone.utc))
        self.assertEqual(same_day.published_to, datetime(2026, 10, 10, 2, 59, 59, 999999, tzinfo=timezone.utc))
        for start, end in ((None, "2026-10-09"), ("2026-10-09", None), ("09/10/2026", "2026-10-09")):
            with self.assertRaises(InstagramMetaSyncError):
                resolve_publication_window("custom", start, end, "America/Maceio", now=now)

    def test_custom_period_is_frozen_in_run_and_active_job_keeps_its_original_window(self):
        self._connection()
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        response = self.client.post(
            self._sync_url(),
            data={
                "csrf_token": csrf_from(page),
                "sync_period": "custom",
                "published_from": "2026-09-01",
                "published_to": "2026-09-30",
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 202)
        run = db.session.get(AnalyticsRun, response.get_json()["status_url"].split("/")[-2])
        self.assertEqual(run.parameters_json["period_preset"], "custom")
        self.assertEqual(run.parameters_json["presentation_timezone"], "America/Maceio")
        self.assertEqual(run.parameters_json["published_from"], "2026-09-01T03:00:00+00:00")
        self.assertEqual(run.parameters_json["published_to"], "2026-10-01T02:59:59.999999+00:00")
        active_markup = self.client.get(
            f"/analytics/instagram/projects/{self.project.id}/imports/new"
        ).get_data(as_text=True)
        self.assertIn("meta_sync_completed=", active_markup)
        self.assertIn("sync_period=custom", active_markup)
        self.assertIn("published_from=2026-09-01", active_markup)
        self.assertIn("published_to=2026-09-30", active_markup)

        reused = self.client.post(
            self._sync_url(),
            data={"csrf_token": csrf_from(page), "sync_period": "last_30_days"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(reused.status_code, 202)
        reused_payload = reused.get_json()
        self.assertTrue(reused_payload["reused_active_run"])
        self.assertIn("sync_period=custom", reused_payload["completion_url"])
        self.assertIn("published_from=2026-09-01", reused_payload["completion_url"])
        self.assertIn("published_to=2026-09-30", reused_payload["completion_url"])
        db.session.refresh(run)
        self.assertEqual(run.parameters_json["period_preset"], "custom")

    def test_sync_completion_url_and_reload_preserve_the_selected_period(self):
        self._connection(provider="instagram_login")
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        response = self.client.post(
            self._sync_url(),
            data={"csrf_token": csrf_from(page), "sync_period": "last_7_days"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 202)
        completion_url = response.get_json()["completion_url"]
        self.assertIn("sync_period=last_7_days", completion_url)
        html = self.client.get(completion_url).get_data(as_text=True)
        self.assertIn('<option value="last_7_days" selected>Últimos 7 dias</option>', html)

    def test_worker_filters_only_the_frozen_publication_window_and_still_adds_snapshot(self):
        self._connection()
        fake = FakeMetaSyncClient(media=[
            {
                "id": "outside-before", "media_type": "IMAGE", "timestamp": "2026-08-31T23:59:59+00:00",
                "like_count": 2, "comments_count": 1,
            },
            {
                "id": "inside-start", "media_type": "REEL", "timestamp": "2026-09-01T03:00:00+00:00",
                "like_count": 3, "comments_count": 0,
            },
            {
                "id": "inside-end", "media_type": "REEL", "timestamp": "2026-10-01T02:59:59.999999+00:00",
                "like_count": 4, "comments_count": 0,
            },
            {
                "id": "outside-after", "media_type": "IMAGE", "timestamp": "2026-10-01T03:00:00+00:00",
                "like_count": 5, "comments_count": 0,
            },
        ])
        self.app.extensions["instagram_meta_client_factory"] = lambda _settings, _provider: fake
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        response = self.client.post(
            self._sync_url(),
            data={
                "csrf_token": csrf_from(page), "sync_period": "custom",
                "published_from": "2026-09-01", "published_to": "2026-09-30",
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 202)
        run = process_next_meta_sync_run()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.record_count, 2)
        self.assertEqual(
            set(db.session.scalars(select(InstagramMedia.external_id))), {"inside-start", "inside-end"},
        )
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot)), 1)
        self.assertFalse(any(call[0] in {"metric", "metric_batch"} and call[1].startswith("outside") for call in fake.calls))

    def test_invalid_custom_period_is_rejected_without_creating_a_run(self):
        self._connection()
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        response = self.client.post(
            self._sync_url(),
            data={
                "csrf_token": csrf_from(page), "sync_period": "custom",
                "published_from": "2026-10-03", "published_to": "2026-10-02",
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("inicial", response.get_json()["message"])
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 0)

    def test_empty_window_keeps_existing_media_and_creates_the_account_snapshot(self):
        self._connection()
        existing = InstagramMedia(
            instagram_account=self.project.instagram_account,
            external_id="historical-media",
            caption_original="Histórico preservado",
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            media_type_raw="IMAGE",
        )
        db.session.add(existing)
        db.session.commit()
        fake = FakeMetaSyncClient(media=[{
            "id": "outside-window", "media_type": "IMAGE", "timestamp": "2026-08-01T12:00:00+00:00",
        }])
        self.app.extensions["instagram_meta_client_factory"] = lambda _settings, _provider: fake
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        response = self.client.post(
            self._sync_url(),
            data={
                "csrf_token": csrf_from(page), "sync_period": "custom",
                "published_from": "2026-09-01", "published_to": "2026-09-01",
            },
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 202)
        run = process_next_meta_sync_run()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.record_count, 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 1)
        self.assertEqual(db.session.get(InstagramMedia, existing.id).external_id, "historical-media")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot)), 1)

    def test_worker_completes_queued_run_and_page_reload_exposes_active_status(self):
        self._connection()
        fake = FakeMetaSyncClient()
        self.app.extensions["instagram_meta_client_factory"] = lambda _settings, _provider: fake
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        response = self.client.post(self._sync_url(), data={"csrf_token": csrf_from(page)}, headers={"Accept": "application/json"})
        payload = response.get_json()
        reload_page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        reload_markup = reload_page.get_data(as_text=True)
        self.assertIn(payload["status_url"], reload_markup)
        self.assertIn('id="instagram-meta-sync-contexto-processamento">@revista_reels</p>', reload_markup)
        self.assertNotIn("ig-official-42", reload_markup)

        run = process_next_meta_sync_run()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.record_count, 2)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 2)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation)), 2)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot)), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramComment)), 0)
        status = self.client.get(payload["status_url"]).get_json()
        self.assertEqual(status["status"], "completed")
        self.assertIn("2 conteúdos foram processados", status["message"])

    def test_worker_failure_marks_run_failed_without_partial_canonical_data_and_stale_running_recovers(self):
        self._connection()
        fake = FakeMetaSyncClient()
        fake.list_media = lambda *_args: (_ for _ in ()).throw(InstagramMetaError("unknown", "secret-in-error"))
        self.app.extensions["instagram_meta_client_factory"] = lambda _settings, _provider: fake
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        response = self.client.post(self._sync_url(), data={"csrf_token": csrf_from(page)}, headers={"Accept": "application/json"})
        run = process_next_meta_sync_run()
        self.assertEqual(run.status, META_SYNC_FAILED)
        self.assertNotIn("secret-in-error", run.error_message)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot)), 0)
        self.assertEqual(self.client.get(response.get_json()["status_url"]).get_json()["status"], META_SYNC_FAILED)

        run.status = META_SYNC_RUNNING
        run.completed_at = None
        run.updated_at = datetime.now(timezone.utc) - timedelta(hours=3)
        db.session.commit()
        self.assertEqual(recover_stale_meta_sync_runs(stale_seconds=7200), 1)
        stale = db.session.get(AnalyticsRun, run.id)
        self.assertEqual(stale.status, META_SYNC_FAILED)
        self.assertIn("interrompida", stale.error_message)

    def test_worker_revalidates_connection_and_archived_project_before_collecting(self):
        connection = self._connection()
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        self.client.post(self._sync_url(), data={"csrf_token": csrf_from(page)})
        connection.status = "revoked"
        db.session.commit()
        failed = process_next_meta_sync_run(client_factory=lambda *_args: FakeMetaSyncClient())
        self.assertEqual(failed.status, META_SYNC_FAILED)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 0)

        connection.status = "connected"
        db.session.commit()
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        self.client.post(self._sync_url(), data={"csrf_token": csrf_from(page)})
        connection.provider = "instagram_login"
        db.session.commit()
        failed = process_next_meta_sync_run(client_factory=lambda *_args: FakeMetaSyncClient())
        self.assertEqual(failed.status, META_SYNC_FAILED)

        connection.provider = "facebook_login"
        db.session.commit()
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        self.client.post(self._sync_url(), data={"csrf_token": csrf_from(page)})
        self.project.status = "archived"
        self.project.archived_at = datetime.now(timezone.utc)
        db.session.commit()
        failed = process_next_meta_sync_run(client_factory=lambda *_args: FakeMetaSyncClient())
        self.assertEqual(failed.status, META_SYNC_FAILED)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 0)

    def test_worker_uses_the_same_durable_pipeline_for_direct_instagram_login(self):
        profile = {"id": "ig-official-42", "username": "revista_reels", "followers_count": 120}
        self._connection(provider="instagram_login", profile=profile)
        fake = FakeMetaSyncClient(profile=profile)
        self.app.extensions["instagram_meta_client_factory"] = lambda _settings, _provider: fake
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        page_markup = page.get_data(as_text=True)
        self.assertIn('id="instagram-meta-sync-contexto-processamento">@revista_reels</p>', page_markup)
        self.assertNotIn("ig-official-42", page_markup)
        self.client.post(self._sync_url(), data={"csrf_token": csrf_from(page)})

        run = process_next_meta_sync_run()
        self.assertEqual(run.status, "completed")
        observation = db.session.scalar(select(InstagramMediaObservation))
        self.assertEqual(observation.reach, 100)
        self.assertEqual(observation.saves, 0)
        self.assertEqual(observation.shares, 4)
        self.assertEqual(observation.interactions, 13)
        self.assertEqual(observation.follows_generated, 2)
        self.assertIsNone(observation.impressions)
        self.assertIsNone(observation.plays)
        self.assertTrue(any(call[0] == "list_media" for call in fake.calls))

    def test_sync_form_reuses_shared_loading_overlay_and_keeps_capability_separate(self):
        template = (ROOT / "templates/platform/instagram/import_new.html").read_text(encoding="utf-8")
        temporal_partial = (ROOT / "templates/platform/instagram/_temporal_period.html").read_text(encoding="utf-8")
        partial = (ROOT / "templates/platform/_progress_overlay.html").read_text(encoding="utf-8")
        styles = (ROOT / "static/css/style.css").read_text(encoding="utf-8")
        qualitative_template = (ROOT / "templates/platform/qualitative_reader.html").read_text(encoding="utf-8")
        source = (ROOT / "static/js/platform_form_loading_overlay.js").read_text(encoding="utf-8")
        status_source = (ROOT / "static/js/instagram_meta_sync_status.js").read_text(encoding="utf-8")
        self.assertIn("platform/_progress_overlay.html", template)
        self.assertIn("data-platform-loading-overlay-form", template)
        self.assertIn("data-loading-overlay-target=\"instagram-meta-sync-overlay-processamento\"", template)
        self.assertIn("data-loading-overlay-submit", template)
        self.assertIn("progress_aria_label='Progresso da sincronização'", template)
        self.assertIn("progress_context=sync_progress_context", template)
        self.assertIn("('@' ~ meta_account.username)", template)
        self.assertIn("platform_form_loading_overlay.js", template)
        self.assertIn("instagram_meta_sync_status.js", template)
        self.assertIn("data-instagram-meta-sync-form", template)
        self.assertIn("data-instagram-meta-sync-status-url", template)
        self.assertIn("select_name='sync_period'", template)
        self.assertIn("sync_period_options", template)
        self.assertIn("data-meta-sync-custom-period", temporal_partial)
        self.assertIn("data-meta-sync-all-history-warning", temporal_partial)
        self.assertIn("A sincronização de todo o histórico pode levar mais tempo", temporal_partial)
        self.assertIn("progress_show_time|default(true)", partial)
        self.assertIn("progress_aria_label|default('Progresso da análise')", partial)
        self.assertIn("progress_context|default('')", partial)
        self.assertIn("{% if progress_context %}", partial)
        self.assertIn("loading-context", partial)
        self.assertIn(".loading-context", styles)
        self.assertIn("overflow-wrap: anywhere", styles)
        self.assertNotIn("progress_context", qualitative_template)
        self.assertIn("window.fetch(statusUrl", status_source)
        self.assertIn("window.setTimeout(poll, 3000)", status_source)
        self.assertIn("payload.status === 'completed'", status_source)
        self.assertIn("payload.status === 'failed'", status_source)
        self.assertIn("updatePeriodControls", status_source)
        self.assertIn("payload?.message", status_source)
        self.assertNotIn("data-platform-loading-overlay-form\"><input type=\"hidden\" name=\"csrf_token\" value=\"{{ csrf_token() }}\"><button class=\"btn btn-outline-primary btn-sm\" type=\"submit\">Verificar dados disponíveis", template)
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        result = subprocess.run(
            [node, "-e", "const assert = require('node:assert/strict');\n" + r"""
const classes = new Set();
const body = {classList:{toggle(){},add:name=>classes.add(name),remove:name=>classes.delete(name)}};
const attrs = {}, overlay = {hidden:true, attrs:{}, setAttribute(k,v){this.attrs[k]=v}};
const button = {disabled:false, attrs:{}, setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]}};
let submitHandler, pageshowHandler;
const form = {dataset:{loadingOverlayTarget:'instagram-meta-sync-overlay-processamento'},
  querySelector:selector=>selector==='[data-loading-overlay-submit]'?button:null,
  addEventListener:(name,handler)=>{if(name==='submit')submitHandler=handler;},
  checkValidity:()=>true,setAttribute(k,v){attrs[k]=v},removeAttribute(k){delete attrs[k]}};
const document = {querySelectorAll:()=>[form],getElementById:id=>id==='instagram-meta-sync-overlay-processamento'?overlay:null,body};
const window = {addEventListener:(name,handler)=>{if(name==='pageshow')pageshowHandler=handler;}};
""" + source + r"""
const first={prevented:false,preventDefault(){this.prevented=true}}; submitHandler(first);
assert.equal(first.prevented,false);assert.equal(overlay.hidden,false);assert.equal(overlay.attrs['aria-hidden'],'false');
assert.equal(button.disabled,true);assert.equal(button.attrs['aria-disabled'],'true');assert.equal(attrs['aria-busy'],'true');assert.equal(classes.has('is-processing'),true);
const repeated={prevented:false,preventDefault(){this.prevented=true}};submitHandler(repeated);assert.equal(repeated.prevented,true);
pageshowHandler();assert.equal(overlay.hidden,true);assert.equal(button.disabled,false);assert.equal(classes.has('is-processing'),false);assert.equal('aria-busy' in attrs,false);
"""],
            capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sync_invalid_connection_and_public_error_never_expose_token(self):
        connection = self._connection()
        connection.status = "revoked"
        db.session.commit()
        with self.assertRaises(InstagramMetaSyncError):
            sync_meta_connection(self.project, connection, self.settings, FakeMetaSyncClient())

        connection.status = "connected"
        db.session.commit()
        fake = FakeMetaSyncClient()
        fake.list_media = lambda *_args: (_ for _ in ()).throw(
            InstagramMetaError("unknown", "token-very-secret-for-tests")
        )
        self.app.extensions["instagram_meta_client_factory"] = lambda _settings, _provider: fake
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}/imports/new")
        response = self.client.post(
            self._sync_url(), data={"csrf_token": csrf_from(page)}, follow_redirects=True,
        )
        self.assertNotIn("token-very-secret-for-tests", response.get_data(as_text=True))
        failed = process_next_meta_sync_run()
        self.assertEqual(failed.status, META_SYNC_FAILED)
        self.assertNotIn("token-very-secret-for-tests", failed.error_message)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 1)


class FacebookMediaPaginationTests(unittest.TestCase):
    def test_list_media_uses_cursor_pagination_without_following_provider_urls(self):
        settings = InstagramMetaSettings(
            facebook_app_id="app", facebook_app_secret="secret", instagram_app_id="", instagram_app_secret="",
            redirect_uri="https://analysis.example/callback", api_version="v26.0", token_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        provider = FacebookLoginProvider(settings)
        responses = iter((
            {"data": [{"id": "one"}], "paging": {"cursors": {"after": "cursor-1"}}},
            {"data": [{"id": "two"}], "paging": {}},
        ))
        calls = []

        def request_json(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return next(responses)

        provider._request_json = request_json
        media = provider.list_media("ig-official-42", "page-token-very-secret")

        self.assertEqual([row["id"] for row in media], ["one", "two"])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("page-token-very-secret", calls[0][1])
        self.assertEqual(parse_qs(urlparse(calls[1][1]).query)["after"], ["cursor-1"])


if __name__ == "__main__":
    unittest.main()
