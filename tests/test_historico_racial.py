"""Testes de isolamento da ferramenta histórico-racial."""

import unittest

from platform_helpers import create_project, create_user, isolated_platform, login


class TesteHistoricoRacial(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        create_user()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_rota_historico_racial_exibe_esqueleto_independente(self):
        with self.app.test_client() as cliente:
            login(cliente)
            redirecionamento = cliente.get("/historico-racial")
            self.assertEqual(redirecionamento.status_code, 302)
            projeto = create_project(cliente)
            resposta = cliente.get(f"/analise-documental/projetos/{projeto}")

        self.assertEqual(resposta.status_code, 200)
        pagina = resposta.get_data(as_text=True)
        self.assertIn("Análysis", pagina)
        self.assertIn("ferramentas para pesquisa", pagina)
        self.assertIn("Raspagem de dados", pagina)
        self.assertIn("Raspagem sistemática", pagina)
        self.assertIn("Processar PDFs", pagina)
        self.assertIn(f'action="/analise-documental/projetos/{projeto}/analisar"', pagina)

    def test_rota_principal_permanece_disponivel(self):
        with self.app.test_client() as cliente:
            login(cliente)
            resposta = cliente.get("/")

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Dashboard", resposta.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
