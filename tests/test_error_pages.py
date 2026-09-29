"""Páginas de erro não expõem detalhes internos nem duplicam o Assistente."""

import unittest
from pathlib import Path

from platform_helpers import create_user, isolated_platform, login


ROOT = Path(__file__).resolve().parents[1]


class ErrorPagesTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.app.config.update(PROPAGATE_EXCEPTIONS=False)

        def controlled_failure():
            raise RuntimeError("segredo interno de teste")

        self.app.add_url_rule("/erro-controlado-testes", "controlled_failure", controlled_failure)
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def assert_error_page(self, response, *, status, title, message):
        self.assertEqual(response.status_code, status)
        html = response.get_data(as_text=True)
        self.assertIn('class="platform-error-page"', html)
        self.assertIn('img/assistant/robot_assistente.png', html)
        self.assertIn('alt="Assistente Análysis"', html)
        self.assertIn(title, html)
        self.assertIn(message, html)
        self.assertIn('class="platform-error-robot"', html)
        self.assertNotIn('class="platform-assistant"', html)
        self.assertNotIn('data-assistant', html)
        return html

    def test_not_found_is_a_custom_page_with_404_status(self):
        response = self.client.get("/pagina-que-nao-existe")
        self.assert_error_page(
            response, status=404, title="Parece que esta página se perdeu.",
            message="Não encontramos o endereço que você tentou acessar.",
        )

    def test_forbidden_is_a_custom_page_with_403_status(self):
        user = create_user()
        login(self.client, user.email)
        response = self.client.get("/projetos/qualitativos")
        self.assert_error_page(
            response, status=403, title="Esta área não está disponível para você.",
            message="Seu usuário não possui permissão para acessar este conteúdo.",
        )

    def test_internal_error_is_safe_and_keeps_its_500_status(self):
        user = create_user()
        login(self.client, user.email)
        with self.assertLogs(self.app.logger, level="ERROR") as logs:
            response = self.client.get("/erro-controlado-testes")
        html = self.assert_error_page(
            response, status=500, title="Ops! Algo não saiu como esperado.",
            message="O Análysis encontrou um problema ao processar esta solicitação.",
        )
        self.assertNotIn("Traceback", html)
        self.assertNotIn("segredo interno de teste", html)
        self.assertIn("segredo interno de teste", "\n".join(logs.output))

    def test_error_presentation_has_a_smaller_heading_on_a_homogeneous_background(self):
        css = (ROOT / "static/css/platform.css").read_text(encoding="utf-8")
        card = css.split(".platform-error-card {", 1)[1].split("}", 1)[0]
        heading = css.split(".platform-error-card h1 {", 1)[1].split("}", 1)[0]
        self.assertIn("background: transparent", card)
        self.assertIn("border: 0", card)
        self.assertIn("box-shadow: none", card)
        self.assertIn("font-size: clamp(1.35rem, 2.4vw, 1.9rem)", heading)


if __name__ == "__main__":
    unittest.main()
