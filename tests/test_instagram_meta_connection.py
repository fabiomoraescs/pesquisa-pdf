"""Facebook Login, Pages e diagnóstico Graph API sem ingestão canônica."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlparse
import unittest

from cryptography.fernet import Fernet
from flask_migrate import downgrade, upgrade
from sqlalchemy import func, inspect, select, text

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_meta import (
    CAPABILITY_METRICS,
    FACEBOOK_LOGIN_SCOPES,
    FacebookLoginProvider,
    INSTAGRAM_LOGIN_SCOPES,
    InstagramLoginCandidate,
    InstagramLoginProvider,
    InstagramMetaConfigurationError,
    InstagramMetaError,
    InstagramMetaSettings,
    persist_validated_connection,
    run_capability_test,
)
from platform_core.models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramAccountSnapshot,
    InstagramApiConnection,
    InstagramComment,
    InstagramMedia,
    InstagramMediaObservation,
    PlanTool,
    User,
)
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


class FakeMetaClient:
    def __init__(self, settings, *, profile=None, metric_error=None):
        self.settings = settings
        self.profile_payload = profile or {
            "id": "ig-42", "username": "revista_reels", "name": "Revista Reels",
            "followers_count": 12, "media_count": 3, "account_type": "BUSINESS",
        }
        self.profile_error = None
        self.discovery_error = None
        self.account_insight_error = None
        self.metric_error = metric_error
        self.metric_errors = {}
        self.metric_errors_by_media = {}
        self.comment_error = None
        self.comment_errors = {}
        self.sample_media_payload = [
            {"id": "reel-1", "media_type": "REEL"},
            {"id": "image-1", "media_type": "IMAGE"},
        ]
        self.comments_by_media = {
            "reel-1": [{"id": "comment-reel-1"}],
            "image-1": [{"id": "comment-image-1"}],
        }
        self.calls = []

    def _candidate(self, *, page_id="page-42", page_name="Página Reels", profile=None, page_token="page-token-very-secret-42"):
        return {
            "page_id": page_id,
            "page_name": page_name,
            "page_access_token": page_token,
            "instagram_profile": profile or self.profile_payload,
        }

    @property
    def candidates(self):
        return getattr(self, "_candidates", [self._candidate()])

    @candidates.setter
    def candidates(self, value):
        self._candidates = value

    def authorization_url(self, state):
        return "https://www.facebook.com/v26.0/dialog/oauth?state=" + state + "&scope=" + ",".join(FACEBOOK_LOGIN_SCOPES)

    def exchange_code(self, code):
        self.calls.append(("exchange", code))
        if code == "bad-code":
            raise InstagramMetaError("reconnect_required", "A autorização do Facebook expirou.")
        return {"access_token": "user-token-very-secret-42", "scope": ",".join(FACEBOOK_LOGIN_SCOPES)}

    def discover_professional_accounts(self, user_token):
        self.calls.append(("pages", user_token))
        if self.discovery_error is not None:
            raise self.discovery_error
        from platform_core.instagram_meta import FacebookPageCandidate
        return [FacebookPageCandidate(**candidate) for candidate in self.candidates]

    def resolve_connection_candidates(self, user_token):
        return self.discover_professional_accounts(user_token)

    def instagram_profile(self, account_id, page_token):
        self.calls.append(("profile", account_id, page_token))
        if self.profile_error is not None:
            raise self.profile_error
        return self.profile_payload

    def sample_media(self, account_id, page_token):
        self.calls.append(("sample", account_id, page_token))
        return self.sample_media_payload

    def sample_comments(self, media_id, page_token):
        self.calls.append(("comments", media_id, page_token))
        error = self.comment_errors.get(media_id, self.comment_error)
        if error is not None:
            raise error
        return self.comments_by_media.get(media_id, [])

    def media_metric(self, media_id, metric, page_token):
        self.calls.append(("metric", media_id, metric, page_token))
        error = self.metric_errors_by_media.get((media_id, metric))
        if error is not None:
            raise error
        error = self.metric_errors.get(metric)
        if error is not None:
            raise error
        if self.metric_error and metric == self.metric_error:
            raise InstagramMetaError("unsupported_metric", "Métrica não suportada.")
        return {"data": [{"period": "day", "values": [{"end_time": "2026-10-09T00:00:00+0000"}]}]}

    def account_insights(self, account_id, page_token):
        self.calls.append(("account_insights", account_id, page_token))
        if self.account_insight_error is not None:
            raise self.account_insight_error
        return {"data": [{"period": "day", "values": [{"end_time": "2026-10-09T00:00:00+0000"}]}]}


class FakeInstagramLoginClient:
    def __init__(self, settings, *, profile=None):
        self.settings = settings
        self.profile_payload = profile or {
            "id": "ig-42", "username": "revista_reels", "name": "Revista Reels",
            "media_count": 3, "account_type": "CREATOR",
        }
        self.profile_error = None
        self.comment_error = None
        self.comment_errors = {}
        self.metric_errors = {}
        self.metrics = {
            "reach": 100,
            "saved": 4,
            "shares": 2,
            "total_interactions": 17,
            "follows": 1,
            "views": 50,
        }
        self.account_reach = 500
        self.sample_media_payload = [
            {"id": f"media-{index}", "media_product_type": "REEL" if index % 2 else "FEED"}
            for index in range(1, 11)
        ]
        self.comments_by_media = {
            "media-1": [{"id": "comment-1"}],
            "media-2": [{"id": "comment-2"}],
            "media-3": [{"id": "comment-3"}],
        }
        self.calls = []

    def authorization_url(self, state):
        return "https://www.instagram.com/oauth/authorize?state=" + state + "&scope=" + ",".join(INSTAGRAM_LOGIN_SCOPES)

    def exchange_code(self, code):
        self.calls.append(("exchange", code))
        return {"access_token": "instagram-token-very-secret-42"}

    def resolve_connection_candidates(self, access_token):
        self.calls.append(("profile", access_token))
        if self.profile_error is not None:
            raise self.profile_error
        return [InstagramLoginCandidate(
            instagram_access_token=access_token,
            instagram_profile=self.profile_payload,
        )]

    def instagram_profile(self, account_id, access_token):
        self.calls.append(("capability_profile", account_id, access_token))
        if self.profile_error is not None:
            raise self.profile_error
        return self.profile_payload

    def sample_media(self, account_id, access_token):
        self.calls.append(("sample", account_id, access_token))
        return self.sample_media_payload

    def sample_comments(self, media_id, access_token):
        self.calls.append(("comments", media_id, access_token))
        error = self.comment_errors.get(media_id, self.comment_error)
        if error is not None:
            raise error
        return self.comments_by_media.get(media_id, [])

    def media_metric(self, media_id, metric, access_token):
        self.calls.append(("metric", media_id, metric, access_token))
        error = self.metric_errors.get(metric)
        if error is not None:
            raise error
        value = self.metrics.get(metric)
        values = [] if value is None else [{"value": value}]
        return {"data": [{"name": metric, "values": values}]}

    def account_insights(self, account_id, access_token):
        self.calls.append(("account_insights", account_id, access_token))
        values = [] if self.account_reach is None else [{"value": self.account_reach}]
        return {"data": [{"name": "reach", "values": values}]}


class InstagramMetaConnectionTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.app.config.update({
            "PESQUISAPDF_META_APP_ID": "app-id-for-tests",
            "PESQUISAPDF_META_APP_SECRET": "app-secret-for-tests",
            "PESQUISAPDF_META_INSTAGRAM_APP_ID": "instagram-app-id-for-tests",
            "PESQUISAPDF_META_INSTAGRAM_APP_SECRET": "instagram-app-secret-for-tests",
            "PESQUISAPDF_META_REDIRECT_URI": "https://analysis.example/analytics/instagram/imports/meta/callback",
            "PESQUISAPDF_META_API_VERSION": "v26.0",
            "PESQUISAPDF_META_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        })
        self.client = self.app.test_client()
        self.user = create_user()
        self.other = create_user("Outra pessoa", "outra-meta@example.org")
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        self.project = AnalyticsProject(owner_user_id=self.user.id, module_key="instagram", name="Meta")
        self.other_project = AnalyticsProject(owner_user_id=self.other.id, module_key="instagram", name="Outro")
        db.session.add_all((self.project, self.other_project))
        db.session.commit()
        self.fake = FakeMetaClient(InstagramMetaSettings.from_mapping(self.app.config))
        self.instagram_fake = FakeInstagramLoginClient(InstagramMetaSettings.from_mapping(self.app.config))
        self.app.extensions["instagram_meta_client_factory"] = (
            lambda settings, provider: self.instagram_fake if provider == "instagram_login" else self.fake
        )
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def _import_url(self, project=None):
        return f"/analytics/instagram/projects/{(project or self.project).id}/imports/new"

    def _connect(self, provider="facebook_login", project=None):
        project = project or self.project
        page = self.client.get(self._import_url(project))
        suffix = "" if provider == "facebook_login" else f"/{provider}"
        response = self.client.post(
            f"/analytics/instagram/projects/{project.id}/imports/meta/connect{suffix}",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlparse(response.headers["Location"]).query)["state"][0]

    def _callback(self, state, code="valid-code"):
        return self.client.get(
            "/analytics/instagram/imports/meta/callback",
            query_string={"state": state, "code": code}, follow_redirects=False,
        )

    def test_connect_uses_state_once_and_persists_only_encrypted_connection(self):
        state = self._connect()
        response = self._callback(state)
        self.assertEqual(response.status_code, 302)
        connection = db.session.scalar(select(InstagramApiConnection))
        account = db.session.scalar(select(InstagramAccount))
        self.assertEqual(connection.status, "connected")
        self.assertEqual(connection.provider, "facebook_login")
        self.assertEqual(connection.provider_account_external_id, "ig-42")
        self.assertEqual(connection.provider_username, "revista_reels")
        self.assertEqual(connection.identity_binding, "exact")
        self.assertEqual(connection.scopes_json, list(FACEBOOK_LOGIN_SCOPES))
        self.assertEqual(account.external_id, "ig-42")
        self.assertNotIn("user-token-very-secret-42", connection.encrypted_access_token)
        self.assertNotIn("page-token-very-secret-42", connection.encrypted_access_token)
        self.assertNotIn("page-token-very-secret-42", repr(connection))
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramComment)), 0)
        self.assertEqual(self._callback(state).status_code, 400)

    def test_authorization_url_uses_primary_app_id_and_exact_facebook_scopes(self):
        provider = FacebookLoginProvider(InstagramMetaSettings.from_mapping(self.app.config))
        query = parse_qs(urlparse(provider.authorization_url("state-safe")).query)
        self.assertEqual(FACEBOOK_LOGIN_SCOPES, (
            "instagram_basic",
            "instagram_manage_insights",
            "instagram_manage_comments",
            "pages_read_engagement",
            "pages_show_list",
        ))
        self.assertEqual(urlparse(provider.authorization_url("state-safe")).netloc, "www.facebook.com")
        self.assertEqual(query["client_id"], [self.app.config["PESQUISAPDF_META_APP_ID"]])
        self.assertEqual(query["redirect_uri"], [self.app.config["PESQUISAPDF_META_REDIRECT_URI"]])
        self.assertEqual(query["state"], ["state-safe"])
        self.assertEqual(query["scope"][0].split(","), list(FACEBOOK_LOGIN_SCOPES))
        self.assertNotIn("config_id", query)

    def test_instagram_authorization_uses_its_own_credentials_and_insights_scope(self):
        provider = InstagramLoginProvider(InstagramMetaSettings.from_mapping(self.app.config))
        authorization_url = provider.authorization_url("instagram-state-safe")
        query = parse_qs(urlparse(authorization_url).query)
        self.assertEqual(INSTAGRAM_LOGIN_SCOPES, (
            "instagram_business_basic",
            "instagram_business_manage_comments",
            "instagram_business_manage_insights",
        ))
        self.assertEqual(urlparse(authorization_url).netloc, "www.instagram.com")
        self.assertEqual(query["client_id"], [self.app.config["PESQUISAPDF_META_INSTAGRAM_APP_ID"]])
        self.assertEqual(query["scope"][0].split(","), list(INSTAGRAM_LOGIN_SCOPES))
        self.assertNotEqual(query["client_id"], [self.app.config["PESQUISAPDF_META_APP_ID"]])

    def test_comment_and_reply_probes_remain_provider_aware(self):
        settings = InstagramMetaSettings.from_mapping(self.app.config)
        providers = (
            (FacebookLoginProvider(settings), "graph.facebook.com"),
            (InstagramLoginProvider(settings), "graph.instagram.com"),
        )
        for provider, host in providers:
            calls = []
            provider._request_json = lambda method, url, **kwargs: calls.append((method, url, kwargs)) or {"data": []}
            with self.subTest(provider=provider.key):
                self.assertEqual(provider.sample_comments("media-42", "secret-token"), [])
                self.assertEqual(provider.sample_comment_replies("comment-42", "secret-token"), [])
                self.assertEqual(len(calls), 2)
                self.assertEqual(urlparse(calls[0][1]).netloc, host)
                self.assertIn("/media-42/comments?", calls[0][1])
                self.assertIn("/comment-42/replies?", calls[1][1])
                self.assertEqual(parse_qs(urlparse(calls[0][1]).query)["fields"], ["id"])
                self.assertNotIn("secret-token", calls[0][1])

    def test_direct_insights_use_the_instagram_graph_host_and_configured_version(self):
        provider = InstagramLoginProvider(InstagramMetaSettings.from_mapping(self.app.config))
        calls = []
        provider._request_json = lambda method, url, **kwargs: calls.append((method, url, kwargs)) or {"data": []}

        provider.media_metrics("media-42", ("reach", "saved"), "secret-token")
        provider.account_insights("ig-42", "secret-token")

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(urlparse(call[1]).netloc == "graph.instagram.com" for call in calls))
        self.assertTrue(all(f"/{self.app.config['PESQUISAPDF_META_API_VERSION']}/" in call[1] for call in calls))
        self.assertEqual(parse_qs(urlparse(calls[0][1]).query)["metric"], ["reach,saved"])
        self.assertEqual(parse_qs(urlparse(calls[1][1]).query)["metric"], ["reach"])
        self.assertNotIn("secret-token", calls[0][1])

    def test_import_page_exposes_both_providers_and_keeps_manual_import_available(self):
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Conectar diretamente ao Instagram", body)
        self.assertIn("Conectar via Facebook", body)
        self.assertIn("Importação manual", body)
        self.assertIn("Arquivo CSV ou XLSX", body)

        self.app.config["PESQUISAPDF_META_APP_ID"] = ""
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Conectar Instagram</button>", body)
        self.assertIn("Não configurado neste ambiente", body)
        self.assertIn("Arquivo CSV ou XLSX", body)

        self.app.config["PESQUISAPDF_META_INSTAGRAM_APP_ID"] = ""
        self.app.config["PESQUISAPDF_META_INSTAGRAM_APP_SECRET"] = ""
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Arquivo CSV ou XLSX", body)
        self.assertEqual(body.count("Não configurado neste ambiente"), 2)

    def test_instagram_callback_uses_provider_saved_in_state_not_a_query_parameter(self):
        state = self._connect("instagram_login")
        with self.client.session_transaction() as session:
            self.assertEqual(session["instagram_meta_oauth_state"]["provider"], "instagram_login")
        response = self.client.get(
            "/analytics/instagram/imports/meta/callback",
            query_string={"state": state, "code": "instagram-valid", "provider": "facebook_login"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        connection = db.session.scalar(select(InstagramApiConnection))
        self.assertEqual(connection.provider, "instagram_login")
        self.assertNotIn("instagram-token-very-secret-42", connection.encrypted_access_token)
        self.assertEqual([call[0] for call in self.instagram_fake.calls], ["exchange", "profile"])

    def test_direct_capability_probes_its_own_insights_without_persisting_analytics(self):
        self._callback(self._connect("instagram_login"), code="instagram-valid")
        connection = db.session.scalar(select(InstagramApiConnection))
        validated_at = connection.last_validated_at
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(connection)
        result = connection.capability_result_json
        self.assertEqual(result["provider"], "instagram_login")
        self.assertEqual(result["profile"]["status"], "supported")
        self.assertEqual(result["account_metrics"]["status"], "supported")
        self.assertEqual(result["media_metrics"]["reach"]["status"], "supported")
        self.assertEqual(result["media_metrics"]["views"]["status"], "supported")
        self.assertEqual(result["comments"], {
            "status": "supported",
            "observed_count": 3,
            "sampled_media_count": 3,
            "successful_probe_count": 3,
        })
        self.assertEqual(connection.last_validated_at, validated_at)
        self.assertIsNotNone(connection.last_capability_test_at)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramComment)), 0)

    def test_direct_diagnosis_is_humanized_without_exposing_internal_status_codes(self):
        self._callback(self._connect("instagram_login"), code="instagram-valid")
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Diagnóstico da conexão", body)
        self.assertIn("10 conteúdos verificados", body)
        self.assertIn("Comentários", body)
        self.assertIn("3 comentários encontrados na amostra", body)
        self.assertIn("Conexão validada em", body)
        self.assertIn("Dados verificados em", body)
        self.assertIn("Feed · Reels", body)
        self.assertIn("Alcance: Disponível", body)
        self.assertIn("Dados agregados da conta: Disponível.", body)
        self.assertIn("Verificar dados disponíveis", body)
        self.assertNotIn("not_available_for_provider", body)
        self.assertNotIn("permission_denied", body)
        self.assertNotIn("conteúdo(s)", body)
        self.assertNotIn("comentário(s)", body)
        self.assertNotIn("Última validação", body)
        self.assertNotIn("Última verificação", body)
        self.assertNotIn(">feed<", body)
        self.assertNotIn(">reel<", body)

    def test_comment_capability_distinguishes_empty_sample_and_permission_without_breaking_connection(self):
        self._callback(self._connect("instagram_login"), code="instagram-valid")
        self.instagram_fake.comments_by_media = {}
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        result = db.session.scalar(select(InstagramApiConnection)).capability_result_json["comments"]
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["observed_count"], 0)
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Nenhum comentário encontrado na amostra", body)

        connection = db.session.scalar(select(InstagramApiConnection))
        connection.scopes_json = ["instagram_business_basic"]
        self.instagram_fake.comment_error = InstagramMetaError("permission_denied", "nunca-expor")
        db.session.commit()
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(connection)
        self.assertEqual(connection.status, "connected")
        self.assertEqual(connection.capability_result_json["comments"]["status"], "permission_denied")
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Comentários</dt><dd class=\"col-sm-8\">Sem permissão", body)
        self.assertIn("Para verificar comentários, reconecte a conta e autorize o acesso solicitado.", body)
        self.assertIn("Reconectar</button>", body)
        self.assertNotIn("nunca-expor", body)

    def test_comment_capability_distinguishes_unavailable_temporary_and_missing_media(self):
        self._callback(self._connect("instagram_login"), code="instagram-valid")
        connection = db.session.scalar(select(InstagramApiConnection))
        page = self.client.get(self._import_url())

        self.instagram_fake.comment_error = InstagramMetaError("unsupported_metric", "nunca-expor")
        self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        self.assertEqual(connection.capability_result_json["comments"]["status"], "not_available_for_provider")

        self.instagram_fake.comment_error = InstagramMetaError("temporary", "nunca-expor")
        page = self.client.get(self._import_url())
        self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        self.assertEqual(connection.capability_result_json["comments"]["status"], "temporary_error")

        self.instagram_fake.comment_error = None
        self.instagram_fake.sample_media_payload = []
        page = self.client.get(self._import_url())
        self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        self.assertEqual(connection.capability_result_json["comments"]["status"], "not_observed")
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Não foi possível verificar", body)
        self.assertNotIn("not_available_for_provider", body)
        self.assertNotIn("temporary_error", body)

    def test_provider_switch_validates_identity_before_replacing_a_connection(self):
        self._callback(self._connect("instagram_login"), code="instagram-valid")
        connection = db.session.scalar(select(InstagramApiConnection))
        original_token = connection.encrypted_access_token
        self.assertEqual(connection.provider, "instagram_login")

        self.fake.profile_payload = {"id": "ig-other", "username": "outra_conta"}
        self._callback(self._connect("facebook_login"))
        db.session.refresh(connection)
        self.assertEqual(connection.provider, "instagram_login")
        self.assertEqual(connection.encrypted_access_token, original_token)

        self.fake.profile_payload = {
            "id": "ig-42", "username": "revista_reels", "name": "Revista Reels", "account_type": "BUSINESS",
        }
        self._callback(self._connect("facebook_login"))
        db.session.refresh(connection)
        self.assertEqual(connection.provider, "facebook_login")
        self.assertNotEqual(connection.encrypted_access_token, original_token)
        self.assertEqual(connection.capability_result_json, {})

        self._callback(self._connect("instagram_login"), code="instagram-valid-again")
        db.session.refresh(connection)
        self.assertEqual(connection.provider, "instagram_login")
        self.assertEqual(connection.provider_account_external_id, "ig-42")
        self.assertEqual(connection.scopes_json, list(INSTAGRAM_LOGIN_SCOPES))

    def test_code_exchange_uses_the_graph_api_without_exposing_a_token(self):
        provider = FacebookLoginProvider(InstagramMetaSettings.from_mapping(self.app.config))
        calls = []

        def request_json(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return {"access_token": "user-token-very-secret-42"}

        provider._request_json = request_json
        payload = provider.exchange_code("authorization-code-very-secret")
        self.assertEqual(payload["access_token"], "user-token-very-secret-42")
        self.assertEqual(len(calls), 1)
        method, url, kwargs = calls[0]
        query = parse_qs(urlparse(url).query)
        self.assertEqual(method, "GET")
        self.assertEqual(urlparse(url).netloc, "graph.facebook.com")
        self.assertEqual(query["client_id"], [self.app.config["PESQUISAPDF_META_APP_ID"]])
        self.assertEqual(query["redirect_uri"], [self.app.config["PESQUISAPDF_META_REDIRECT_URI"]])
        self.assertEqual(query["code"], ["authorization-code-very-secret"])
        self.assertNotIn("access_token", kwargs)

    def test_graph_page_discovery_paginates_and_distinguishes_missing_resources(self):
        provider = FacebookLoginProvider(InstagramMetaSettings.from_mapping(self.app.config))
        page_responses = iter((
            {"data": [{"id": "page-a", "name": "Página A", "access_token": "page-token-a", "instagram_business_account": {"id": "ig-a", "username": "conta_a"}}], "paging": {"cursors": {"after": "cursor-a"}}},
            {"data": [{"id": "page-b", "name": "Página B", "access_token": "page-token-b", "instagram_business_account": {"id": "ig-b", "username": "conta_b"}}]},
        ))
        requests = []

        def request_json(method, url, **kwargs):
            requests.append((method, url, kwargs))
            if "/me/accounts?" in url:
                return next(page_responses)
            if "/debug_token?" in url:
                return {"data": {"is_valid": True, "app_id": self.app.config["PESQUISAPDF_META_APP_ID"], "granular_scopes": []}}
            raise AssertionError(url)

        provider._request_json = request_json
        candidates = provider.discover_professional_accounts("user-token-very-secret-42")
        self.assertEqual([candidate.external_id for candidate in candidates], ["ig-a", "ig-b"])
        self.assertEqual(len(requests), 3)
        self.assertIn("/me/accounts?", requests[0][1])
        self.assertIn("instagram_business_account", requests[0][1])
        self.assertEqual(requests[0][2]["access_token"], "user-token-very-secret-42")
        self.assertIn("/debug_token?", requests[-1][1])
        self.assertEqual(
            requests[-1][2]["access_token"],
            f'{self.app.config["PESQUISAPDF_META_APP_ID"]}|{self.app.config["PESQUISAPDF_META_APP_SECRET"]}',
        )

        provider._request_json = lambda _method, url, **_kwargs: (
            {"data": []} if "/me/accounts?" in url
            else {"data": {"is_valid": True, "app_id": self.app.config["PESQUISAPDF_META_APP_ID"], "granular_scopes": []}}
        )
        with self.assertRaisesRegex(InstagramMetaError, "Página") as missing_pages:
            provider.discover_professional_accounts("user-token-very-secret-42")
        self.assertEqual(missing_pages.exception.category, "no_linked_instagram_account")

        provider._request_json = lambda _method, url, **_kwargs: (
            {"data": [{"id": "page-only", "name": "Sem Instagram", "access_token": "page-token"}]}
            if "/me/accounts?" in url
            else {"data": {"is_valid": True, "app_id": self.app.config["PESQUISAPDF_META_APP_ID"], "granular_scopes": []}}
        )
        with self.assertRaisesRegex(InstagramMetaError, "Página") as missing_instagram:
            provider.discover_professional_accounts("user-token-very-secret-42")
        self.assertEqual(missing_instagram.exception.category, "no_linked_instagram_account")

    def test_page_discovery_recovers_authorized_granular_page_omitted_by_me_accounts(self):
        provider = FacebookLoginProvider(InstagramMetaSettings.from_mapping(self.app.config))
        requests = []

        def page(page_id, instagram_id):
            return {
                "id": page_id,
                "name": f"Página {page_id}",
                "access_token": f"page-token-{page_id}",
                "instagram_business_account": {"id": instagram_id, "username": f"conta_{instagram_id.lower()}"},
            }

        def request_json(method, url, **kwargs):
            requests.append((method, url, kwargs))
            path = urlparse(url).path
            if path.endswith("/me/accounts"):
                return {"data": [page("A", "IG-A"), page("B", "IG-B")]}
            if path.endswith("/debug_token"):
                return {"data": {
                    "is_valid": True,
                    "app_id": self.app.config["PESQUISAPDF_META_APP_ID"],
                    "granular_scopes": [
                        {"scope": "pages_show_list", "target_ids": ["A", "B", "C", "D", "E"]},
                        {"scope": "pages_read_engagement", "target_ids": ["A", "B", "C", "D", "E"]},
                        {"scope": "instagram_basic", "target_ids": ["IG-NÃO-É-PAGE"]},
                    ],
                }}
            if path.endswith("/C"):
                return page("C", "IG-C")
            if path.endswith("/D"):
                return {"id": "D", "name": "Página D", "access_token": "page-token-D"}
            if path.endswith("/E"):
                raise InstagramMetaError("permission_denied", "page-token-E nunca deve aparecer")
            raise AssertionError(url)

        provider._request_json = request_json
        candidates = provider.discover_professional_accounts("user-token-very-secret-42")
        self.assertEqual([candidate.page_id for candidate in candidates], ["A", "B", "C"])
        self.assertEqual([candidate.external_id for candidate in candidates], ["IG-A", "IG-B", "IG-C"])
        direct_paths = [urlparse(url).path for _method, url, _kwargs in requests if "/me/accounts" not in url and "/debug_token" not in url]
        self.assertEqual(direct_paths, ["/v26.0/C", "/v26.0/D", "/v26.0/E"])
        self.assertNotIn("IG-NÃO-É-PAGE", direct_paths)
        self.assertEqual(candidates[-1].page_access_token, "page-token-C")
        self.assertNotIn("page-token-C", repr(candidates[-1]))
        self.assertNotIn("user-token-very-secret-42", repr(candidates))
        self.assertNotIn(self.app.config["PESQUISAPDF_META_APP_SECRET"], repr(candidates))

    def test_page_discovery_deduplicates_professional_account_and_keeps_primary_page(self):
        provider = FacebookLoginProvider(InstagramMetaSettings.from_mapping(self.app.config))

        def request_json(_method, url, **_kwargs):
            if "/me/accounts?" in url:
                return {"data": [
                    {"id": "A", "name": "Página A", "access_token": "page-token-A", "instagram_business_account": {"id": "IG-A", "username": "conta_a"}},
                    {"id": "B", "name": "Página B", "access_token": "page-token-B", "instagram_business_account": {"id": "IG-A", "username": "conta_a"}},
                ]}
            return {"data": {"is_valid": True, "app_id": self.app.config["PESQUISAPDF_META_APP_ID"], "granular_scopes": []}}

        provider._request_json = request_json
        candidates = provider.discover_professional_accounts("user-token-very-secret-42")
        self.assertEqual([(candidate.page_id, candidate.external_id) for candidate in candidates], [("A", "IG-A")])

    def test_page_discovery_keeps_primary_candidates_when_introspection_is_temporarily_unavailable(self):
        provider = FacebookLoginProvider(InstagramMetaSettings.from_mapping(self.app.config))

        def request_json(_method, url, **_kwargs):
            if "/me/accounts?" in url:
                return {"data": [{"id": "A", "name": "Página A", "access_token": "page-token-A", "instagram_business_account": {"id": "IG-A", "username": "conta_a"}}]}
            raise InstagramMetaError("temporary", "segredo-nunca-publico")

        provider._request_json = request_json
        candidates = provider.discover_professional_accounts("user-token-very-secret-42")
        self.assertEqual([candidate.page_id for candidate in candidates], ["A"])

    def test_page_discovery_ignores_wrong_app_targets_and_rejects_invalid_token_without_primary_page(self):
        provider = FacebookLoginProvider(InstagramMetaSettings.from_mapping(self.app.config))
        direct_requests = []

        def wrong_app(_method, url, **_kwargs):
            if "/me/accounts?" in url:
                return {"data": [{"id": "A", "name": "Página A", "access_token": "page-token-A", "instagram_business_account": {"id": "IG-A", "username": "conta_a"}}]}
            if "/debug_token?" in url:
                return {"data": {"is_valid": True, "app_id": "another-app", "granular_scopes": [{"scope": "pages_show_list", "target_ids": ["C"]}]}}
            direct_requests.append(url)
            raise AssertionError(url)

        provider._request_json = wrong_app
        self.assertEqual([candidate.page_id for candidate in provider.discover_professional_accounts("user-token-very-secret-42")], ["A"])
        self.assertEqual(direct_requests, [])

        def invalid_token(_method, url, **_kwargs):
            if "/me/accounts?" in url:
                return {"data": []}
            return {"data": {"is_valid": False, "app_id": self.app.config["PESQUISAPDF_META_APP_ID"]}}

        provider._request_json = invalid_token
        with self.assertRaises(InstagramMetaError) as invalid:
            provider.discover_professional_accounts("user-token-very-secret-42")
        self.assertEqual(invalid.exception.category, "reconnect_required")

    def test_multiple_professional_accounts_require_explicit_selection_before_persisting(self):
        self.fake.candidates = [
            self.fake._candidate(page_id="page-a", page_name="Página A", profile={"id": "ig-a", "username": "conta_a"}),
            self.fake._candidate(page_id="page-b", page_name="Página B", profile={"id": "ig-b", "username": "conta_b"}),
        ]
        callback = self._callback(self._connect())
        self.assertEqual(callback.status_code, 302)
        self.assertIn("/imports/meta/select", callback.headers["Location"])
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))
        selection_page = self.client.get(callback.headers["Location"])
        body = selection_page.get_data(as_text=True)
        self.assertIn("@conta_a", body)
        self.assertIn("Página B", body)
        self.assertNotIn("user-token-very-secret-42", body)
        self.assertNotIn("page-token-very-secret-42", body)
        self.assertNotIn("user-token-very-secret-42", callback.headers["Location"])
        self.assertNotIn("page-token-very-secret-42", callback.headers["Location"])
        response = self.client.post(
            callback.headers["Location"],
            data={"csrf_token": csrf_from(selection_page), "selection_token": parse_qs(urlparse(callback.headers["Location"]).query)["selection_token"][0], "candidate_index": "1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.scalar(select(InstagramAccount)).external_id, "ig-b")

    def test_callback_rejects_missing_state_cross_project_and_missing_page_link(self):
        self.assertEqual(self.client.get("/analytics/instagram/imports/meta/callback", query_string={"code": "x"}).status_code, 400)
        state = self._connect()
        with self.client.session_transaction() as session:
            session["instagram_meta_oauth_state"]["project_id"] = self.other_project.id
            session.modified = True
        self.assertEqual(self._callback(state).status_code, 404)
        self.fake.discovery_error = InstagramMetaError("no_linked_instagram_account", "sem vínculo")
        state = self._connect()
        response = self._callback(state)
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))

    def test_discovery_reports_zero_managed_pages_without_persisting_connection(self):
        self.fake.discovery_error = InstagramMetaError("no_managed_pages", "nenhuma Página")
        response = self._callback(self._connect(), "valid-code")
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))

    def test_reconnect_does_not_replace_existing_credential_when_identity_fails(self):
        state = self._connect()
        self._callback(state)
        original = db.session.scalar(select(InstagramApiConnection)).encrypted_access_token
        self.fake.candidates = [self.fake._candidate(profile={"id": "different-identity", "username": "outra"})]
        state = self._connect()
        response = self._callback(state)
        self.assertEqual(response.status_code, 302)
        connection = db.session.scalar(select(InstagramApiConnection))
        self.assertEqual(connection.encrypted_access_token, original)
        self.assertEqual(connection.status, "connected")

    def test_existing_external_id_resolves_matching_account_without_ambiguous_selection(self):
        self._callback(self._connect())
        self.fake.candidates = [
            self.fake._candidate(page_id="page-other", profile={"id": "ig-other", "username": "outra"}),
            self.fake._candidate(page_id="page-match", profile=self.fake.profile_payload),
        ]
        response = self._callback(self._connect())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("/imports/meta/select", response.headers["Location"])
        self.assertEqual(db.session.scalar(select(InstagramAccount)).external_id, "ig-42")

    def test_existing_account_without_external_id_is_completed_only_after_identity_validation(self):
        account = InstagramAccount(
            analytics_project=self.project, username="revista_antiga", username_normalized="revista_antiga",
            display_name="",
        )
        db.session.add(account)
        db.session.commit()
        connection = persist_validated_connection(
            self.project,
            page_access_token="page-token-very-secret-42",
            profile={"id": "ig-42", "username": "revista_reels", "account_type": "CREATOR"},
            granted_scopes=FACEBOOK_LOGIN_SCOPES,
            settings=InstagramMetaSettings.from_mapping(self.app.config),
        )
        db.session.commit()
        self.assertEqual(connection.instagram_account.external_id, "ig-42")
        self.assertEqual(connection.instagram_account.username, "revista_reels")
        self.assertEqual(connection.instagram_account.account_type, "CREATOR")

    def test_wrong_encryption_key_marks_integration_unavailable_without_losing_connection(self):
        self._callback(self._connect())
        connection = db.session.scalar(select(InstagramApiConnection))
        previous_ciphertext = connection.encrypted_access_token
        wrong_settings = InstagramMetaSettings(
            facebook_app_id="app-id-for-tests", facebook_app_secret="app-secret-for-tests",
            instagram_app_id="instagram-app-id-for-tests", instagram_app_secret="instagram-app-secret-for-tests",
            redirect_uri="https://analysis.example/callback", api_version="v26.0",
            token_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        with self.assertRaises(InstagramMetaConfigurationError):
            run_capability_test(connection, wrong_settings, self.fake)
        self.assertEqual(connection.status, "connected")
        self.assertEqual(connection.encrypted_access_token, previous_ciphertext)

    def test_capability_test_is_limited_and_never_persists_analytics_data(self):
        self._callback(self._connect())
        self.fake.metric_error = "views"
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        connection = db.session.scalar(select(InstagramApiConnection))
        result = connection.capability_result_json
        self.assertEqual(result["sample"]["limit"], 10)
        self.assertEqual(result["sample"]["media_types"], ["image", "reel"])
        self.assertEqual(result["media_metrics"]["reach"]["status"], "supported")
        self.assertEqual(result["media_metrics"]["views"]["status"], "unsupported")
        self.assertEqual(result["media_metrics"]["reach"]["temporal_by_media_type"]["reel"]["temporal_semantics"], "periodized")
        self.assertEqual(result["account_metrics"]["status"], "supported")
        self.assertNotIn("user-token-very-secret-42", repr(result))
        self.assertNotIn("page-token-very-secret-42", repr(result))
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot)), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramComment)), 0)

    def test_capability_records_partial_metric_states_without_global_flash(self):
        self._callback(self._connect())
        self.fake.metric_errors = {
            "saved": InstagramMetaError("permission_denied", "segredo-nunca-publico"),
            "shares": InstagramMetaError("unsupported_metric", "segredo-nunca-publico"),
            "total_interactions": InstagramMetaError("temporary", "segredo-nunca-publico"),
            "views": InstagramMetaError("not_applicable", "segredo-nunca-publico"),
        }
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Verificação dos dados disponíveis concluída", body)
        self.assertNotIn("Esta métrica não é disponibilizada pela API", body)
        result = db.session.scalar(select(InstagramApiConnection)).capability_result_json["media_metrics"]
        self.assertEqual(result["saved"]["status"], "permission_denied")
        self.assertEqual(result["shares"]["status"], "unsupported")
        self.assertEqual(result["total_interactions"]["status"], "temporary_error")
        self.assertEqual(result["follows"]["status"], "supported")
        self.assertEqual(result["views"]["status"], "not_applicable")

    def test_partial_metric_failure_logs_safe_probe_context_without_tokens(self):
        self._callback(self._connect())
        self.fake.metric_errors["views"] = InstagramMetaError(
            "unsupported_metric",
            "page-token-very-secret-42 Authorization: Bearer user-token-very-secret-42",
            http_status=400,
            meta_code=100,
            meta_subcode=33,
            meta_type="OAuthException",
        )
        page = self.client.get(self._import_url())
        with self.assertLogs("platform_core.instagram_meta", level="WARNING") as captured:
            response = self.client.post(
                f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
                data={"csrf_token": csrf_from(page)}, follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Verificação dos dados disponíveis concluída", body)
        self.assertNotIn("Esta métrica não é disponibilizada pela API", body)
        entries = "\n".join(captured.output)
        self.assertIn("provider=facebook_login", entries)
        self.assertIn("probe=media_metric", entries)
        self.assertIn("metric=views", entries)
        self.assertIn("media_type=reel", entries)
        self.assertIn("http_status=400", entries)
        self.assertIn("meta_code=100", entries)
        self.assertIn("meta_subcode=33", entries)
        self.assertIn("meta_type=OAuthException", entries)
        self.assertIn("classification=unsupported_metric", entries)
        self.assertNotIn("page-token-very-secret-42", entries)
        self.assertNotIn("user-token-very-secret-42", entries)
        self.assertNotIn("Authorization", entries)
        result = db.session.scalar(select(InstagramApiConnection)).capability_result_json
        self.assertEqual(result["media_metrics"]["views"]["status"], "unsupported")

    def test_structural_capability_failure_logs_safe_profile_context(self):
        self._callback(self._connect())
        self.fake.profile_error = InstagramMetaError(
            "reconnect_required",
            "access_token=page-token-very-secret-42 client_secret=app-secret-for-tests",
            http_status=401,
            meta_code=190,
            meta_subcode=463,
            meta_type="OAuthException",
        )
        page = self.client.get(self._import_url())
        with self.assertLogs("platform_core.instagram_meta", level="WARNING") as captured:
            response = self.client.post(
                f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
                data={"csrf_token": csrf_from(page)}, follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Reconecte sua conta do Instagram", body)
        entries = "\n".join(captured.output)
        self.assertIn("provider=facebook_login", entries)
        self.assertIn("probe=profile_identity", entries)
        self.assertIn("http_status=401", entries)
        self.assertIn("meta_code=190", entries)
        self.assertIn("meta_subcode=463", entries)
        self.assertIn("meta_type=OAuthException", entries)
        self.assertIn("classification=reconnect_required", entries)
        self.assertNotIn("page-token-very-secret-42", entries)
        self.assertNotIn("app-secret-for-tests", entries)
        connection = db.session.scalar(select(InstagramApiConnection))
        self.assertEqual(connection.status, "needs_reauth")
        self.assertEqual(connection.last_error_category, "reconnect_required")

    def test_facebook_profile_identity_uses_minimal_ig_user_request_and_page_token(self):
        settings = InstagramMetaSettings.from_mapping(self.app.config)
        provider = FacebookLoginProvider(settings)
        requests = []

        def fake_request_json(method, url, *, access_token=None, form=None, operation="generic"):
            requests.append((method, url, access_token, form, operation))
            fields = parse_qs(urlparse(url).query).get("fields")
            if fields != ["id,username"]:
                raise InstagramMetaError("unknown", "Campo opcional indisponível.", http_status=400, meta_code=100)
            return {"id": "ig-42", "username": "revista_renomeada"}

        provider._request_json = fake_request_json
        legacy_url = (
            f"https://graph.facebook.com/{settings.api_version}/ig-42?"
            "fields=id%2Cusername%2Cname%2Caccount_type"
        )
        with self.assertRaises(InstagramMetaError) as legacy_error:
            fake_request_json("GET", legacy_url, access_token="page-token-very-secret-42")
        self.assertEqual(legacy_error.exception.http_status, 400)
        self.assertEqual(legacy_error.exception.meta_code, 100)

        profile = provider.instagram_profile("ig-42", "page-token-very-secret-42")

        self.assertEqual(profile, {"id": "ig-42", "username": "revista_renomeada"})
        self.assertEqual(len(requests), 2)
        method, url, token, form, operation = requests[-1]
        parsed = urlparse(url)
        self.assertEqual(method, "GET")
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "graph.facebook.com")
        self.assertEqual(parsed.path, f"/{settings.api_version}/ig-42")
        self.assertEqual(parse_qs(parsed.query), {"fields": ["id,username"]})
        self.assertEqual(token, "page-token-very-secret-42")
        self.assertIsNone(form)
        self.assertEqual(operation, "profile_identity")

    def test_profile_identity_accepts_username_change_when_official_id_is_unchanged(self):
        self._callback(self._connect())
        self.fake.profile_payload = {"id": "ig-42", "username": "revista_renomeada"}
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Verificação dos dados disponíveis concluída", response.get_data(as_text=True))
        connection = db.session.scalar(select(InstagramApiConnection))
        self.assertEqual(connection.status, "connected")
        self.assertEqual(connection.provider_account_external_id, "ig-42")
        self.assertEqual(connection.capability_result_json["profile"]["status"], "supported")

    def test_profile_identity_rejects_different_official_id(self):
        self._callback(self._connect())
        self.fake.profile_payload = {"id": "ig-other", "username": "outra_conta"}
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("A conta autenticada não corresponde à conta vinculada a este projeto.", response.get_data(as_text=True))
        connection = db.session.scalar(select(InstagramApiConnection))
        self.assertEqual(connection.status, "error")
        self.assertEqual(connection.last_error_category, "identity_mismatch")

    def test_capability_succeeds_when_all_performance_metrics_are_individually_unavailable(self):
        self._callback(self._connect())
        self.fake.metric_errors = {
            metric: InstagramMetaError("unsupported_metric", "segredo-nunca-publico")
            for metric in CAPABILITY_METRICS
        }
        self.fake.account_insight_error = InstagramMetaError("unsupported_metric", "segredo-nunca-publico")
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Verificação dos dados disponíveis concluída", body)
        self.assertNotIn("Esta métrica não é disponibilizada pela API", body)
        connection = db.session.scalar(select(InstagramApiConnection))
        self.assertEqual(connection.status, "connected")
        self.assertTrue(all(
            metric["status"] == "unsupported"
            for metric in connection.capability_result_json["media_metrics"].values()
        ))
        self.assertEqual(connection.capability_result_json["account_metrics"]["status"], "unsupported")

    def test_metric_can_succeed_on_reel_after_image_is_not_applicable(self):
        self._callback(self._connect())
        self.fake.sample_media_payload = [
            {"id": "image-1", "media_type": "IMAGE"},
            {"id": "reel-1", "media_type": "REEL"},
        ]
        self.fake.metric_errors_by_media[("image-1", "views")] = InstagramMetaError(
            "not_applicable", "segredo-nunca-publico"
        )
        page = self.client.get(self._import_url())
        self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        views = db.session.scalar(select(InstagramApiConnection)).capability_result_json["media_metrics"]["views"]
        self.assertEqual(views["by_media_type"]["image"], "not_applicable")
        self.assertEqual(views["by_media_type"]["reel"], "supported")
        self.assertEqual(views["status"], "supported")

    def test_missing_media_in_sample_is_not_observed_not_unsupported(self):
        self._callback(self._connect())
        self.fake.sample_media_payload = []
        page = self.client.get(self._import_url())
        self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=False,
        )
        metrics = db.session.scalar(select(InstagramApiConnection)).capability_result_json["media_metrics"]
        self.assertTrue(all(result["status"] == "not_observed" for result in metrics.values()))

    def test_invalid_token_in_metric_probe_remains_a_structural_capability_failure(self):
        self._callback(self._connect())
        self.fake.metric_errors["reach"] = InstagramMetaError("reconnect_required", "segredo-nunca-publico")
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Reconecte sua conta do Instagram", body)
        connection = db.session.scalar(select(InstagramApiConnection))
        self.assertEqual(connection.status, "needs_reauth")
        self.assertEqual(connection.last_error_category, "reconnect_required")

    def test_account_insight_error_is_recorded_without_aborting_media_diagnosis(self):
        self._callback(self._connect())
        self.fake.account_insight_error = InstagramMetaError("temporary", "segredo-nunca-publico")
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        connection = db.session.scalar(select(InstagramApiConnection))
        result = connection.capability_result_json
        self.assertEqual(result["media_metrics"]["reach"]["status"], "supported")
        self.assertEqual(result["account_metrics"]["status"], "temporary_error")

    def test_capability_error_is_sanitized_before_html_and_connection_records(self):
        self._callback(self._connect())
        self.fake.profile_error = InstagramMetaError(
            "unknown", "page-token-very-secret-42 authorization-code-very-secret"
        )
        page = self.client.get(self._import_url())
        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/capabilities",
            data={"csrf_token": csrf_from(page)}, follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        connection = db.session.scalar(select(InstagramApiConnection))
        self.assertNotIn("page-token-very-secret-42", body)
        self.assertNotIn("authorization-code-very-secret", body)
        self.assertEqual(connection.last_error_category, "unknown")

    def test_import_page_exposes_connected_needs_reauth_and_safe_capability_summary(self):
        self._callback(self._connect())
        connection = db.session.scalar(select(InstagramApiConnection))
        connection.capability_result_json = {
            "sample": {"observed_count": 2, "media_types": ["image", "reel"]},
            "media_metrics": {"reach": {"status": "supported", "by_media_type": {"reel": "supported"}}},
            "account_metrics": {"status": "supported", "temporal_semantics": "periodized"},
        }
        db.session.commit()
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Instagram conectado", body)
        self.assertIn("Verificar dados disponíveis", body)
        self.assertIn("Alcance", body)
        self.assertNotIn("supported", body)
        connection.status = "needs_reauth"
        db.session.commit()
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Reconexão necessária", body)
        self.assertIn("Reconectar</button>", body)

    def test_disconnect_is_post_csrf_and_preserves_account_and_history(self):
        self._callback(self._connect())
        url = f"/analytics/instagram/projects/{self.project.id}/imports/meta/disconnect"
        confirmation = self.client.get(url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn("Desconectar", confirmation.get_data(as_text=True))
        self.assertEqual(self.client.post(url, data={"confirm": "yes"}).status_code, 400)
        response = self.client.post(url, data={"csrf_token": csrf_from(confirmation), "confirm": "yes"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        connection = db.session.scalar(select(InstagramApiConnection))
        self.assertEqual(connection.status, "revoked")
        self.assertIsNone(connection.encrypted_access_token)
        self.assertIsNotNone(db.session.scalar(select(InstagramAccount)))

    def test_archived_project_exposes_api_state_only_for_consultation(self):
        self.project.status = "archived"
        self.project.archived_at = datetime.now(timezone.utc)
        db.session.commit()
        body = self.client.get(self._import_url()).get_data(as_text=True)
        self.assertIn("Projeto arquivado: conexão disponível somente para consulta.", body)
        self.assertNotIn("Conectar Instagram</button>", body)
        self.assertNotIn("Validar e visualizar prévia</button>", body)
        # A rota precisa alcançar o guard de lifecycle; o CSRF é coberto pelos
        # demais POSTs desta suíte com token válido.
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.assertEqual(self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/imports/meta/connect",
        ).status_code, 404)
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.project.status = "active"
        self.project.archived_at = None
        self.app.config["PESQUISAPDF_META_APP_ID"] = ""
        db.session.commit()
        page = self.client.get(self._import_url())
        body = page.get_data(as_text=True)
        self.assertIn("Conectar diretamente ao Instagram", body)
        self.assertIn("Conectar via Facebook", body)
        self.assertIn("Não configurado neste ambiente", body)
        self.assertIn("Arquivo CSV ou XLSX", body)
        self.assertIn("Validar e visualizar prévia</button>", body)

    def test_reconciliation_requires_explicit_confirmation_and_preserves_manual_identity(self):
        manual_account = InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="teste_manual_fabio",
            display_name="Revista Reels",
        )
        db.session.add(manual_account)
        db.session.commit()

        callback = self._callback(self._connect("instagram_login"), code="instagram-valid")
        self.assertEqual(callback.status_code, 302)
        self.assertIn("/imports/meta/reconcile", callback.headers["Location"])
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))
        db.session.refresh(manual_account)
        self.assertEqual(manual_account.external_id, "teste_manual_fabio")
        self.assertEqual(manual_account.connection_status, "not_connected")
        confirmation = self.client.get(callback.headers["Location"])
        body = confirmation.get_data(as_text=True)
        self.assertIn("Confirmar vínculo da conta", body)
        self.assertIn("@revista_reels", body)
        self.assertNotIn("teste_manual_fabio", body)
        self.assertNotIn("ig-42", body)
        self.assertNotIn("instagram-token-very-secret-42", body)
        response = self.client.post(
            callback.headers["Location"],
            data={
                "csrf_token": csrf_from(confirmation),
                "reconciliation_token": parse_qs(urlparse(callback.headers["Location"]).query)["reconciliation_token"][0],
                "reconciliation_action": "confirm",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        connection = db.session.scalar(select(InstagramApiConnection))
        db.session.refresh(manual_account)
        self.assertEqual(manual_account.external_id, "teste_manual_fabio")
        self.assertEqual(manual_account.connection_status, "connected")
        self.assertEqual(connection.provider_account_external_id, "ig-42")
        self.assertEqual(connection.provider_username, "revista_reels")
        self.assertEqual(connection.identity_binding, "reconciled")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 0)
        self.assertEqual(self.client.post(
            callback.headers["Location"],
            data={
                "csrf_token": csrf_from(self.client.get(self._import_url())),
                "reconciliation_token": parse_qs(urlparse(callback.headers["Location"]).query)["reconciliation_token"][0],
                "reconciliation_action": "confirm",
            },
            follow_redirects=False,
        ).status_code, 302)

    def test_reconciliation_cancel_consumes_state_without_persisting_a_connection(self):
        manual_account = InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="manual-historical-42",
            display_name="Revista Reels",
        )
        db.session.add(manual_account)
        db.session.commit()

        callback = self._callback(self._connect("instagram_login"), code="instagram-valid")
        confirmation_url = callback.headers["Location"]
        confirmation = self.client.get(confirmation_url)
        token = parse_qs(urlparse(confirmation_url).query)["reconciliation_token"][0]
        response = self.client.post(
            confirmation_url,
            data={
                "csrf_token": csrf_from(confirmation),
                "reconciliation_token": token,
                "reconciliation_action": "cancel",
            },
            follow_redirects=True,
        )
        self.assertIn("A vinculação da conta foi cancelada. Nenhuma alteração foi feita.", response.get_data(as_text=True))
        db.session.refresh(manual_account)
        self.assertEqual(manual_account.external_id, "manual-historical-42")
        self.assertEqual(manual_account.connection_status, "not_connected")
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun)), 0)
        self.assertEqual(self.client.get(confirmation_url, follow_redirects=False).status_code, 302)

    def test_reconciliation_expiry_blocks_confirmation_without_persisting(self):
        from platform_core.instagram_meta import pending_meta_reconciliations

        db.session.add(InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="manual-historical-42",
            display_name="",
        ))
        db.session.commit()
        callback = self._callback(self._connect("instagram_login"), code="instagram-valid")
        confirmation_url = callback.headers["Location"]
        token = parse_qs(urlparse(confirmation_url).query)["reconciliation_token"][0]
        pending_meta_reconciliations._items[token].expires_at = 0
        page = self.client.get(self._import_url())
        response = self.client.post(
            confirmation_url,
            data={
                "csrf_token": csrf_from(page),
                "reconciliation_token": token,
                "reconciliation_action": "confirm",
            },
            follow_redirects=True,
        )
        self.assertIn("A confirmação expirou. Conecte a conta novamente.", response.get_data(as_text=True))
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))

    def test_project_conflict_has_a_specific_safe_message(self):
        db.session.add_all((
            InstagramAccount(
                analytics_project=self.project,
                username="revista_reels",
                username_normalized="revista_reels",
                external_id="manual-historical-42",
                display_name="",
            ),
            InstagramAccount(
                analytics_project=self.other_project,
                username="outra_conta",
                username_normalized="outra_conta",
                external_id="ig-42",
                display_name="",
            ),
        ))
        db.session.commit()

        response = self._callback(self._connect("instagram_login"), code="instagram-valid")
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.headers["Location"], follow_redirects=True)
        body = page.get_data(as_text=True)
        self.assertIn("Esta conta do Instagram já está vinculada a outro projeto.", body)
        self.assertNotIn("Outro", body)
        self.assertIsNone(db.session.scalar(
            select(InstagramApiConnection).join(InstagramAccount).where(
                InstagramAccount.analytics_project_id == self.project.id
            )
        ))

    def test_facebook_selection_reports_provider_identity_conflict_in_another_project(self):
        manual_account = InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="manual-historical-42",
            display_name="",
        )
        other_account = InstagramAccount(
            analytics_project=self.other_project,
            username="outra_conta",
            username_normalized="outra_conta",
            external_id="other-manual-42",
            display_name="",
        )
        db.session.add_all((manual_account, other_account))
        db.session.flush()
        db.session.add(InstagramApiConnection(
            instagram_account=other_account,
            provider="facebook_login",
            provider_account_external_id="ig-42",
            provider_username="outra_conta",
            identity_binding="exact",
            status="connected",
            api_version="v26.0",
            scopes_json=list(FACEBOOK_LOGIN_SCOPES),
        ))
        db.session.commit()
        self.fake.candidates = [
            self.fake._candidate(
                page_id="page-gepe",
                page_name="GEPE",
                profile={"id": "ig-gepe", "username": "gepe.ufal"},
            ),
            self.fake._candidate(
                page_id="page-reels",
                page_name="Página Reels",
                profile={"id": "ig-42", "username": "revista_reels"},
            ),
        ]

        callback = self._callback(self._connect("facebook_login"))
        selection_url = callback.headers["Location"]
        selection = self.client.get(selection_url)
        response = self.client.post(
            selection_url,
            data={
                "csrf_token": csrf_from(selection),
                "selection_token": parse_qs(urlparse(selection_url).query)["selection_token"][0],
                "candidate_index": "1",
            },
            follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        self.assertIn("Esta conta do Instagram já está vinculada a outro projeto.", body)
        self.assertNotIn("other-manual-42", body)
        self.assertIsNone(db.session.scalar(
            select(InstagramApiConnection).join(InstagramAccount).where(
                InstagramAccount.analytics_project_id == self.project.id
            )
        ))

    def test_facebook_multi_account_selection_stages_reconciliation_before_persisting(self):
        manual_account = InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="teste_manual_fabio",
            display_name="Revista Reels",
        )
        db.session.add(manual_account)
        db.session.commit()
        self.fake.candidates = [
            self.fake._candidate(
                page_id="page-gepe",
                page_name="GEPE",
                profile={"id": "ig-gepe", "username": "gepe.ufal"},
            ),
            self.fake._candidate(
                page_id="page-reels",
                page_name="Página Reels",
                profile={"id": "ig-42", "username": "revista_reels"},
            ),
        ]

        callback = self._callback(self._connect("facebook_login"))
        self.assertIn("/imports/meta/select", callback.headers["Location"])
        selection_url = callback.headers["Location"]
        selection = self.client.get(selection_url)
        response = self.client.post(
            selection_url,
            data={
                "csrf_token": csrf_from(selection),
                "selection_token": parse_qs(urlparse(selection_url).query)["selection_token"][0],
                "candidate_index": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/imports/meta/reconcile", response.headers["Location"])
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))
        db.session.refresh(manual_account)
        self.assertEqual(manual_account.external_id, "teste_manual_fabio")
        self.assertEqual(manual_account.connection_status, "not_connected")
        confirmation = self.client.get(response.headers["Location"])
        cancelled = self.client.post(
            response.headers["Location"],
            data={
                "csrf_token": csrf_from(confirmation),
                "reconciliation_token": parse_qs(urlparse(response.headers["Location"]).query)["reconciliation_token"][0],
                "reconciliation_action": "cancel",
            },
            follow_redirects=False,
        )
        self.assertEqual(cancelled.status_code, 302)
        db.session.refresh(manual_account)
        self.assertEqual(manual_account.external_id, "teste_manual_fabio")
        self.assertEqual(manual_account.connection_status, "not_connected")
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))

    def test_reconciliation_cancel_preserves_a_preexisting_connection_status(self):
        manual_account = InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="manual-historical-42",
            display_name="",
            connection_status="connected",
        )
        db.session.add(manual_account)
        db.session.flush()
        previous_connection = InstagramApiConnection(
            instagram_account=manual_account,
            provider="facebook_login",
            provider_account_external_id="ig-42",
            provider_username="revista_reels",
            identity_binding="exact",
            status="connected",
            api_version="v26.0",
            scopes_json=list(FACEBOOK_LOGIN_SCOPES),
        )
        db.session.add(previous_connection)
        db.session.commit()

        callback = self._callback(self._connect("instagram_login"), code="instagram-valid")
        confirmation_url = callback.headers["Location"]
        confirmation = self.client.get(confirmation_url)
        response = self.client.post(
            confirmation_url,
            data={
                "csrf_token": csrf_from(confirmation),
                "reconciliation_token": parse_qs(urlparse(confirmation_url).query)["reconciliation_token"][0],
                "reconciliation_action": "cancel",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(manual_account)
        self.assertEqual(manual_account.connection_status, "connected")
        self.assertEqual(manual_account.api_connection.id, previous_connection.id)
        self.assertEqual(manual_account.api_connection.status, "connected")

    def test_legacy_exact_connection_with_manual_identity_still_requires_confirmation(self):
        manual_account = InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="manual-historical-42",
            display_name="Revista Reels",
        )
        db.session.add(manual_account)
        db.session.flush()
        db.session.add(InstagramApiConnection(
            instagram_account=manual_account,
            provider="facebook_login",
            provider_account_external_id="ig-42",
            provider_username="revista_reels",
            identity_binding="exact",
            status="revoked",
            api_version="v26.0",
            scopes_json=[],
        ))
        db.session.commit()

        callback = self._callback(self._connect("instagram_login"), code="instagram-valid")
        self.assertEqual(callback.status_code, 302)
        self.assertIn("/imports/meta/reconcile", callback.headers["Location"])
        db.session.refresh(manual_account)
        self.assertEqual(manual_account.connection_status, "not_connected")
        self.assertEqual(manual_account.api_connection.identity_binding, "exact")

    def test_reconciliation_is_blocked_for_different_username_or_provider_identity_in_other_project(self):
        manual_account = InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="manual-historical-42",
            display_name="Revista Reels",
        )
        db.session.add(manual_account)
        db.session.commit()
        self.instagram_fake.profile_payload = {"id": "ig-42", "username": "outra_conta"}
        response = self._callback(self._connect("instagram_login"), code="instagram-valid")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("/imports/meta/reconcile", response.headers["Location"])
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))

        self.instagram_fake.profile_payload = {"id": "ig-42", "username": "revista_reels"}
        other_account = InstagramAccount(
            analytics_project=self.other_project,
            username="outra_conta",
            username_normalized="outra_conta",
            external_id="ig-42",
            display_name="",
        )
        db.session.add(other_account)
        db.session.commit()
        response = self._callback(self._connect("instagram_login"), code="instagram-valid")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("/imports/meta/reconcile", response.headers["Location"])
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))

    def test_reconciliation_nonce_requires_csrf_owner_and_an_active_project(self):
        db.session.add(InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="manual-historical-42",
            display_name="",
        ))
        db.session.commit()
        callback = self._callback(self._connect("instagram_login"), code="instagram-valid")
        confirmation_url = callback.headers["Location"]
        token = parse_qs(urlparse(confirmation_url).query)["reconciliation_token"][0]
        self.assertEqual(self.client.post(
            confirmation_url, data={"reconciliation_token": token}, follow_redirects=False,
        ).status_code, 400)

        logout_page = self.client.get(self._import_url())
        self.client.post("/logout", data={"csrf_token": csrf_from(logout_page)})
        login(self.client, self.other.email)
        self.assertEqual(self.client.get(confirmation_url).status_code, 404)
        logout_page = self.client.get("/")
        self.client.post("/logout", data={"csrf_token": csrf_from(logout_page)})
        login(self.client, self.user.email)

        self.project.status = "archived"
        self.project.archived_at = datetime.now(timezone.utc)
        db.session.commit()
        self.assertEqual(self.client.get(confirmation_url).status_code, 404)
        self.assertIsNone(db.session.scalar(select(InstagramApiConnection)))

    def test_reconciled_provider_identity_allows_later_username_update(self):
        account = InstagramAccount(
            analytics_project=self.project,
            username="revista_reels",
            username_normalized="revista_reels",
            external_id="manual-historical-42",
            display_name="",
        )
        db.session.add(account)
        db.session.commit()
        settings = InstagramMetaSettings.from_mapping(self.app.config)
        persist_validated_connection(
            self.project,
            access_token="instagram-token-very-secret-42",
            profile={"id": "ig-42", "username": "revista_reels"},
            granted_scopes=INSTAGRAM_LOGIN_SCOPES,
            settings=settings,
            provider="instagram_login",
            reconciliation_confirmed=True,
        )
        persist_validated_connection(
            self.project,
            access_token="instagram-token-very-secret-42-updated",
            profile={"id": "ig-42", "username": "revista_reels_novo"},
            granted_scopes=INSTAGRAM_LOGIN_SCOPES,
            settings=settings,
            provider="instagram_login",
        )
        db.session.commit()
        db.session.refresh(account)
        self.assertEqual(account.external_id, "manual-historical-42")
        self.assertEqual(account.username, "revista_reels_novo")
        self.assertEqual(account.api_connection.provider_account_external_id, "ig-42")
        self.assertEqual(account.api_connection.identity_binding, "reconciled")


class InstagramMetaConnectionMigrationTests(unittest.TestCase):
    def test_meta_connection_migrations_are_reversible_through_provider_identity_head(self):
        from app import create_app

        migrations = str(Path(__file__).resolve().parents[1] / "migrations")
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-meta-connection-migration-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "meta-connection-migration-test",
            })
            with app.app_context():
                upgrade(directory=migrations, revision="f3a4b5c6d7e")
                self.assertNotIn("instagram_api_connections", inspect(db.engine).get_table_names())
                upgrade(directory=migrations, revision="a9b8c7d6e5f4")
                self.assertIn("instagram_api_connections", inspect(db.engine).get_table_names())
                user = User(name="Migration Meta", email="migration-meta@example.org", password_hash="x")
                db.session.add(user)
                db.session.flush()
                project = AnalyticsProject(owner_user_id=user.id, module_key="instagram", name="Migration Meta")
                db.session.add(project)
                db.session.flush()
                account = InstagramAccount(
                    analytics_project=project, username="conta", username_normalized="conta",
                    external_id="legacy-meta-id", display_name="", connection_status="connected",
                )
                db.session.add(account)
                db.session.flush()
                db.session.execute(text(
                    "INSERT INTO instagram_api_connections "
                    "(id, instagram_account_id, provider, status, scopes_json, api_version, capability_result_json, created_at, updated_at) "
                    f"VALUES ('connection-meta', '{account.id}', 'instagram_login', 'connected', '[]', 'v26.0', '{{}}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ))
                db.session.commit()
                upgrade(directory=migrations, revision="b0c9d8e7f6a5")
                self.assertIn("instagram_api_connections", inspect(db.engine).get_table_names())
                self.assertEqual(db.session.execute(text(
                    "SELECT provider FROM instagram_api_connections WHERE id = 'connection-meta'"
                )).scalar_one(), "facebook_login")
                upgrade(directory=migrations, revision="c1d0e9f8a7b6")
                db.session.execute(text(
                    "UPDATE instagram_api_connections SET provider = 'instagram_login' "
                    "WHERE id = 'connection-meta'"
                ))
                db.session.commit()
                self.assertEqual(db.session.execute(text(
                    "SELECT provider FROM instagram_api_connections WHERE id = 'connection-meta'"
                )).scalar_one(), "instagram_login")
                upgrade(directory=migrations, revision="d2e1f0a9b8c7")
                columns = {column["name"] for column in inspect(db.engine).get_columns("instagram_api_connections")}
                self.assertTrue({
                    "provider_account_external_id", "provider_username", "identity_binding"
                }.issubset(columns))
                self.assertEqual(db.session.execute(text(
                    "SELECT identity_binding FROM instagram_api_connections WHERE id = 'connection-meta'"
                )).scalar_one(), "exact")
                self.assertEqual(db.session.execute(text(
                    "SELECT provider_account_external_id FROM instagram_api_connections WHERE id = 'connection-meta'"
                )).scalar_one(), "legacy-meta-id")
                downgrade(directory=migrations, revision="c1d0e9f8a7b6")
                columns = {column["name"] for column in inspect(db.engine).get_columns("instagram_api_connections")}
                self.assertNotIn("provider_account_external_id", columns)
                downgrade(directory=migrations, revision="b0c9d8e7f6a5")
                self.assertEqual(db.session.execute(text(
                    "SELECT provider FROM instagram_api_connections WHERE id = 'connection-meta'"
                )).scalar_one(), "facebook_login")
                downgrade(directory=migrations, revision="a9b8c7d6e5f4")
                self.assertIn("instagram_api_connections", inspect(db.engine).get_table_names())
                self.assertEqual(db.session.execute(text(
                    "SELECT provider FROM instagram_api_connections WHERE id = 'connection-meta'"
                )).scalar_one(), "instagram_login")
                downgrade(directory=migrations, revision="f3a4b5c6d7e")
                self.assertNotIn("instagram_api_connections", inspect(db.engine).get_table_names())
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
