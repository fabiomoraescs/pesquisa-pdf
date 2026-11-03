"""Contrato administrativo da configuração dual da Integração Meta."""

from __future__ import annotations

import unittest

from cryptography.fernet import Fernet

from platform_helpers import create_user, csrf_from, isolated_platform, login


class AdminMetaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.admin = create_user("Administrador", "admin-meta@example.org", role="admin")
        login(self.client, self.admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_admin_sees_provider_statuses_and_env_names_without_secret_values(self):
        self.app.config.update({
            "PESQUISAPDF_META_APP_ID": "facebook-app-id-visible-nowhere",
            "PESQUISAPDF_META_APP_SECRET": "facebook-app-secret-must-not-render",
            "PESQUISAPDF_META_INSTAGRAM_APP_ID": "instagram-app-id-visible-nowhere",
            "PESQUISAPDF_META_INSTAGRAM_APP_SECRET": "instagram-app-secret-must-not-render",
            "PESQUISAPDF_META_REDIRECT_URI": "https://example.test/analytics/instagram/imports/meta/callback",
            "PESQUISAPDF_META_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        })
        body = self.client.get("/admin/integracao-meta").get_data(as_text=True)
        self.assertIn("Integração Meta", body)
        self.assertIn("Instagram Login direto", body)
        self.assertIn("Facebook Login", body)
        self.assertIn("PESQUISAPDF_META_INSTAGRAM_APP_ID", body)
        self.assertIn("PESQUISAPDF_META_APP_ID", body)
        self.assertIn("PESQUISAPDF_META_TOKEN_ENCRYPTION_KEY", body)
        self.assertEqual(body.count("Configurado"), 2)
        self.assertGreaterEqual(body.count("Configurada"), 2)
        for secret in (
            "facebook-app-id-visible-nowhere",
            "facebook-app-secret-must-not-render",
            "instagram-app-id-visible-nowhere",
            "instagram-app-secret-must-not-render",
        ):
            self.assertNotIn(secret, body)

    def test_partial_or_missing_provider_configuration_is_reported_independently(self):
        self.app.config.update({
            "PESQUISAPDF_META_APP_ID": "facebook-app",
            "PESQUISAPDF_META_APP_SECRET": "facebook-secret",
            "PESQUISAPDF_META_INSTAGRAM_APP_ID": "",
            "PESQUISAPDF_META_INSTAGRAM_APP_SECRET": "",
            "PESQUISAPDF_META_REDIRECT_URI": "https://example.test/callback",
            "PESQUISAPDF_META_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        })
        body = self.client.get("/admin/integracao-meta").get_data(as_text=True)
        self.assertIn("Instagram Login direto", body)
        self.assertIn("Facebook Login", body)
        self.assertIn("Não configurado", body)
        self.assertIn("Configurado", body)

    def test_common_user_cannot_read_infrastructure_status(self):
        user = create_user("Comum", "common-meta@example.org")
        admin_page = self.client.get("/admin/integracao-meta")
        self.client.post("/logout", data={"csrf_token": csrf_from(admin_page)})
        login(self.client, user.email)
        self.assertEqual(self.client.get("/admin/integracao-meta").status_code, 403)


if __name__ == "__main__":
    unittest.main()
