"""Testes de isolamento da ferramenta histórico-racial."""

import unittest

from app import app


class TesteHistoricoRacial(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)

    def test_rota_historico_racial_exibe_esqueleto_independente(self):
        with app.test_client() as cliente:
            resposta = cliente.get("/historico-racial")

        self.assertEqual(resposta.status_code, 200)
        pagina = resposta.get_data(as_text=True)
        self.assertIn("Raspagem de Dados", pagina)
        self.assertIn("Análise histórico-racial", pagina)
        self.assertIn("Processamento em desenvolvimento", pagina)

    def test_rota_principal_permanece_disponivel(self):
        with app.test_client() as cliente:
            resposta = cliente.get("/")

        self.assertEqual(resposta.status_code, 200)


if __name__ == "__main__":
    unittest.main()
