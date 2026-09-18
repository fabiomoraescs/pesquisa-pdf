"""Testes do fluxo observacional de jobs e progresso local."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import shutil
import tempfile
from uuid import UUID
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pymupdf

import app as aplicacao_web
from analyzer import v1, v2, v3


class ModeloMinimo:
    """Modelo determinístico: testa eventos sem alterar a rotina semântica."""

    def encode(self, textos, **_kwargs):
        return np.tile(np.asarray([[1.0, 0.0]]), (len(textos), 1))


class AnalisadorDeMultiplosArquivos:
    """Substituto sem lógica analítica para testar só a orquestração."""

    def __init__(self):
        self.planilhas = []

    def analisar_pdf(self, caminho, termos, configuracoes=None, progress_callback=None):
        if progress_callback is not None:
            progress_callback(
                {
                    "fase": "paginas",
                    "etapa": "Analisando páginas…",
                    "pagina_atual": 1,
                    "paginas_total": 1,
                }
            )
        return (
            [
                {
                    "ID livro": caminho.stem,
                    "Termo": termos[0]["termo"],
                    "_quantidade_no_registro": 1,
                }
            ],
            {"arquivo": caminho.name, "paginas_processadas_com_OCR": 0},
        )

    @staticmethod
    def dataframe_vazio():
        return pd.DataFrame(
            columns=["ID livro", "Termo", "_quantidade_no_registro"]
        )

    def salvar_excel_completo(self, caminho, *_args, **_kwargs):
        self.planilhas.append(caminho.name)


def blocos_controlados():
    campos = {
        "unidade": "",
        "capitulo": "",
        "secao": "",
        "subsecao": "",
        "metodo": "texto",
        "tipo_ocorrencia": "parágrafo",
    }
    return [
        {
            **campos,
            "texto": "São Paulo discute desigualdade regional e saneamento público.",
            "pagina": 1,
            "pagina_pdf": 1,
        },
        {
            **campos,
            "texto": "A migração provocada pela seca reorganizou o território.",
            "pagina": 2,
            "pagina_pdf": 2,
        },
    ]


class TesteProgresso(unittest.TestCase):
    def setUp(self):
        with aplicacao_web.PROGRESSOS_LOCK:
            aplicacao_web.PROGRESSOS.clear()
        with aplicacao_web.ANALISES_LOCK:
            aplicacao_web.ANALISES.clear()

    def tearDown(self):
        with aplicacao_web.PROGRESSOS_LOCK:
            aplicacao_web.PROGRESSOS.clear()
        with aplicacao_web.ANALISES_LOCK:
            aplicacao_web.ANALISES.clear()

    def test_endpoint_informa_processamento_monotono_e_conclusao(self):
        job_id = "f8f2a0bf-5d71-42d1-baf0-5c4a5f8e2ef1"
        aplicacao_web._novo_progresso(job_id, "v1", 1)
        relator = aplicacao_web.RelatorDeProgresso(job_id, "v1", None)

        relator(
            {
                "fase": "paginas",
                "etapa": "Analisando páginas…",
                "arquivo": "livro.pdf",
                "arquivo_indice": 1,
                "arquivos_total": 1,
                "pagina_atual": 4,
                "paginas_total": 10,
            }
        )
        primeiro = aplicacao_web._progresso_publico(job_id)
        relator(
            {
                "fase": "paginas",
                "etapa": "Analisando páginas…",
                "pagina_atual": 2,
                "paginas_total": 10,
            }
        )
        segundo = aplicacao_web._progresso_publico(job_id)
        self.assertEqual(primeiro["status"], "processando")
        self.assertEqual(primeiro["arquivo_atual"], "livro.pdf")
        self.assertGreater(primeiro["percentual"], 0)
        self.assertGreaterEqual(segundo["percentual"], primeiro["percentual"])

        relator.concluir("/resultado/" + job_id)
        with aplicacao_web.app.test_client() as cliente:
            resposta = cliente.get("/api/progresso/" + job_id)
        dados = resposta.get_json()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(dados["status"], "concluido")
        self.assertEqual(dados["percentual"], 100)
        self.assertEqual(dados["resultado_url"], "/resultado/" + job_id)

    def test_endpoint_trata_erro_e_job_inexistente_sem_traceback(self):
        job_id = "1e2dca18-4a86-4a15-bbb1-029d463d4b7f"
        aplicacao_web._novo_progresso(job_id, "v2", 1)
        aplicacao_web.RelatorDeProgresso(job_id, "v2", None).erro(
            "Não foi possível concluir a análise."
        )
        with aplicacao_web.app.test_client() as cliente:
            erro = cliente.get("/api/progresso/" + job_id)
            ausente = cliente.get("/api/progresso/inexistente")
        self.assertEqual(erro.status_code, 200)
        self.assertEqual(erro.get_json()["status"], "erro")
        self.assertNotIn("Traceback", erro.get_json()["erro"])
        self.assertEqual(ausente.status_code, 404)
        self.assertEqual(ausente.get_json()["erro"], "Análise não encontrada.")

    def test_preparacao_do_modelo_mantem_eta_indeterminada(self):
        job_id = "b49b9023-f7c3-482d-96e0-52aaeb6e26cd"
        aplicacao_web._novo_progresso(job_id, "v3", 1)
        relator = aplicacao_web.RelatorDeProgresso(
            job_id,
            "v3",
            {"incluir_lexical": False, "incluir_semantica": True},
        )
        relator(
            {
                "fase": "paginas",
                "etapa": "Analisando páginas…",
                "pagina_atual": 4,
                "paginas_total": 10,
            }
        )
        relator(
            {
                "fase": "preparando_modelo_semantico",
                "etapa": "Preparando modelo semântico…",
                "preparando_modelo": True,
            }
        )
        self.assertIsNone(aplicacao_web._progresso_publico(job_id)["eta_segundos"])

    def test_post_cria_job_uuid_e_retorna_endpoint_sem_executar_no_request(self):
        executor = MagicMock()
        with patch.object(aplicacao_web.EXECUTOR_ANALISES, "submit", executor):
            with aplicacao_web.app.test_client() as cliente:
                resposta = cliente.post(
                    "/",
                    data={
                        "versao": "v1",
                        "termos": "São Paulo",
                        "pdfs": (BytesIO(b"%PDF-1.4\n%teste"), "livro.pdf"),
                    },
                    headers={"X-Requested-With": "XMLHttpRequest"},
                    content_type="multipart/form-data",
                )
        dados = resposta.get_json()
        self.assertEqual(resposta.status_code, 202)
        UUID(dados["job_id"], version=4)
        self.assertEqual(dados["status"], "processando")
        self.assertEqual(dados["progresso_url"], "/api/progresso/" + dados["job_id"])
        executor.assert_called_once()
        shutil.rmtree(aplicacao_web.UPLOAD_DIR / dados["job_id"], ignore_errors=True)

    def test_callbacks_opcionais_nao_alteram_v1_v2_v3(self):
        termos = [{"termo": "São Paulo", "categoria": "TERMO INFORMADO"}]

        with patch("analyzer.v1.extrair_pdf", return_value=(blocos_controlados(), 2, 0, None)) as extrair:
            v1.analisar_pdf(Path("v1.pdf"), termos)
            extrair.assert_called_once_with(Path("v1.pdf"))

        with patch("analyzer.v2._analisar_pdf_referencia", return_value=([], {})) as referencia:
            v2.analisar_pdf(Path("v2.pdf"), termos)
            referencia.assert_called_once_with(Path("v2.pdf"), termos)

        configuracao = {
            "incluir_lexical": True,
            "incluir_semantica": False,
            "limiar_semantico": 0.70,
        }
        with patch("analyzer.v3.v1.extrair_pdf", return_value=(blocos_controlados(), 2, 0, None)) as extrair:
            v3.analisar_pdf(Path("v3.pdf"), termos, configuracao)
            extrair.assert_called_once_with(Path("v3.pdf"))

    def test_v1_v2_e_v3_publicam_somente_marcos_reais(self):
        termos = [{"termo": "São Paulo", "categoria": "TERMO INFORMADO"}]

        eventos_v1 = []
        with patch("analyzer.v1.extrair_pdf", return_value=(blocos_controlados(), 2, 0, None)):
            v1.analisar_pdf(Path("v1.pdf"), termos, progress_callback=eventos_v1.append)
        self.assertTrue(any(evento["fase"] == "busca_lexical" for evento in eventos_v1))

        eventos_v2 = []
        def referencia_v2_com_eventos(_caminho, _termos, progress_callback=None):
            progress_callback(
                {
                    "fase": "paginas",
                    "etapa": "Analisando páginas…",
                    "pagina_atual": 1,
                    "paginas_total": 2,
                }
            )
            progress_callback(
                {
                    "fase": "busca_lexical",
                    "etapa": "Executando busca lexical…",
                    "bloco_atual": 1,
                    "blocos_total": 1,
                }
            )
            return [], {}

        with patch("analyzer.v2._analisar_pdf_referencia", side_effect=referencia_v2_com_eventos):
            v2.analisar_pdf(Path("v2.pdf"), termos, progress_callback=eventos_v2.append)
        self.assertEqual([evento["fase"] for evento in eventos_v2], [
            "paginas", "busca_lexical", "analise_v2_concluida"
        ])

        eventos_v3 = []
        configuracao = {
            "incluir_lexical": True,
            "incluir_semantica": True,
            "limiar_semantico": 0.70,
        }
        with patch("analyzer.v3.v1.extrair_pdf", return_value=(blocos_controlados(), 2, 0, None)), patch(
            "analyzer.v3.carregar_modelo_semantico", return_value=ModeloMinimo()
        ):
            v3.analisar_pdf(
                Path("v3.pdf"), termos, configuracao, progress_callback=eventos_v3.append
            )
        fases = [evento["fase"] for evento in eventos_v3]
        self.assertIn("busca_lexical", fases)
        self.assertIn("preparando_modelo_semantico", fases)
        self.assertIn("modelo_semantico_carregado", fases)
        self.assertIn("codificando_blocos_semanticos", fases)

    def test_v3_respeita_modalidades_ativas_ao_emitir_eventos(self):
        termos = [{"termo": "São Paulo", "categoria": "TERMO INFORMADO"}]
        with patch("analyzer.v3.v1.extrair_pdf", return_value=(blocos_controlados(), 2, 0, None)), patch(
            "analyzer.v3.carregar_modelo_semantico", return_value=ModeloMinimo()
        ) as modelo:
            eventos_lexicais = []
            v3.analisar_pdf(
                Path("lexical.pdf"),
                termos,
                {"incluir_lexical": True, "incluir_semantica": False, "limiar_semantico": 0.70},
                progress_callback=eventos_lexicais.append,
            )
            self.assertIn("busca_lexical", [evento["fase"] for evento in eventos_lexicais])
            self.assertNotIn(
                "preparando_modelo_semantico",
                [evento["fase"] for evento in eventos_lexicais],
            )
            modelo.assert_not_called()

            eventos_semanticos = []
            v3.analisar_pdf(
                Path("semantica.pdf"),
                termos,
                {"incluir_lexical": False, "incluir_semantica": True, "limiar_semantico": 0.70},
                progress_callback=eventos_semanticos.append,
            )
            fases_semanticas = [evento["fase"] for evento in eventos_semanticos]
            self.assertNotIn("busca_lexical", fases_semanticas)
            self.assertIn("preparando_modelo_semantico", fases_semanticas)

    def test_v2_usa_paginas_globais_sem_reiniciar_percentual(self):
        job_id = "70d9bd36-bc28-4e04-af0b-dc72ae61c91b"
        aplicacao_web._novo_progresso(job_id, "v2", 2)
        relator = aplicacao_web.RelatorDeProgresso(job_id, "v2", None)
        eventos = [
            {
                "fase": "paginas", "etapa": "Analisando páginas…",
                "arquivo": "primeiro.pdf", "arquivo_indice": 1,
                "arquivos_total": 2, "pagina_atual": 25, "paginas_total": 50,
                "paginas_processadas_total": 25, "paginas_total_global": 100,
                "paginas_anteriores": 0, "paginas_arquivo": 50,
            },
            {
                "fase": "paginas", "etapa": "Analisando páginas…",
                "arquivo": "segundo.pdf", "arquivo_indice": 2,
                "arquivos_total": 2, "pagina_atual": 25, "paginas_total": 50,
                "paginas_processadas_total": 75, "paginas_total_global": 100,
                "paginas_anteriores": 50, "paginas_arquivo": 50,
            },
        ]
        percentuais = []
        for evento in eventos:
            relator(evento)
            publico = aplicacao_web._progresso_publico(job_id)
            percentuais.append(publico["percentual"])

        self.assertEqual(percentuais, [25, 75])
        self.assertEqual(publico["paginas_processadas_total"], 75)
        self.assertEqual(publico["paginas_total_global"], 100)

    def test_v2_pdf_controlado_emite_paginas_sem_alterar_resultados(self):
        termos = [{"termo": "São Paulo", "categoria": "TERMO INFORMADO"}]
        texto = (
            "São Paulo aparece em uma discussão sobre desigualdade regional, "
            "saneamento público e migração no Nordeste brasileiro. "
        ) * 2
        with tempfile.TemporaryDirectory() as diretorio:
            caminho = Path(diretorio) / "controle-v2.pdf"
            documento = pymupdf.open()
            for _ in range(2):
                pagina = documento.new_page()
                pagina.insert_text((72, 72), texto)
            documento.save(caminho)
            documento.close()

            sem_callback = v2.analisar_pdf(caminho, termos)
            eventos = []
            com_callback = v2.analisar_pdf(
                caminho, termos, progress_callback=eventos.append
            )

        paginas = [
            evento["pagina_atual"]
            for evento in eventos
            if evento["fase"] in {"paginas", "ocr"}
        ]
        self.assertEqual(sem_callback, com_callback)
        self.assertEqual(paginas, [1, 2])

    def test_v3_isolados_possuem_intervalos_reais_e_monotonos(self):
        casos = (
            (
                {"incluir_lexical": True, "incluir_semantica": False},
                [
                    {"fase": "paginas", "pagina_atual": 10, "paginas_total": 10},
                    {"fase": "busca_lexical", "bloco_atual": 5, "blocos_total": 10},
                    {"fase": "busca_lexical", "bloco_atual": 10, "blocos_total": 10},
                ],
            ),
            (
                {"incluir_lexical": False, "incluir_semantica": True},
                [
                    {"fase": "paginas", "pagina_atual": 10, "paginas_total": 10},
                    {"fase": "preparando_blocos_semanticos", "bloco_atual": 10, "blocos_total": 10},
                    {"fase": "codificando_blocos_semanticos", "bloco_atual": 16, "blocos_total": 32},
                    {"fase": "codificando_blocos_semanticos", "bloco_atual": 32, "blocos_total": 32},
                    {"fase": "comparando_semantica", "consulta_atual": 1, "consultas_total": 1},
                ],
            ),
        )
        for indice, (configuracao, eventos) in enumerate(casos, start=1):
            job_id = f"f36cf9b8-ae6f-4f2b-85fc-0d2a67112a0{indice}"
            aplicacao_web._novo_progresso(job_id, "v3", 1)
            relator = aplicacao_web.RelatorDeProgresso(job_id, "v3", configuracao)
            percentuais = []
            for evento in eventos:
                relator(evento)
                percentuais.append(aplicacao_web._progresso_publico(job_id)["percentual"])
            self.assertTrue(all(atual <= proximo for atual, proximo in zip(percentuais, percentuais[1:])))
            self.assertGreater(percentuais[-1], percentuais[0])

    def test_v3_semantica_publica_lotes_concluidos_sem_mudar_ordem(self):
        trechos = [
            {
                "texto": f"Trecho semântico {indice}",
                "pagina_inicial": 1,
                "pagina_final": 1,
                "pagina_inicial_pdf": 1,
                "pagina_final_pdf": 1,
                "indice_bloco_inicial": 0,
                "indice_bloco_final": 0,
            }
            for indice in range(65)
        ]
        termos = [{"termo": "São Paulo", "categoria": "TERMO INFORMADO"}]
        eventos = []
        with patch("analyzer.v3.carregar_modelo_semantico", return_value=ModeloMinimo()):
            sem_callback = v3._registros_semanticos(
                Path("semantica.pdf"), trechos, termos, 0.70
            )
            v3._registros_semanticos(
                Path("semantica.pdf"), trechos, termos, 0.70,
                progress_callback=eventos.append, progresso_por_lote=True,
            )
            com_callback = v3._registros_semanticos(
                Path("semantica.pdf"), trechos, termos, 0.70,
                progress_callback=lambda _evento: None, progresso_por_lote=True,
            )
        codificados = [
            evento["bloco_atual"]
            for evento in eventos
            if evento["fase"] == "codificando_blocos_semanticos"
        ]
        self.assertEqual(codificados, [0, 32, 64, 65])
        self.assertEqual(sem_callback, com_callback)

    def test_callbacks_preservam_registros_das_tres_versoes(self):
        termos = [{"termo": "São Paulo", "categoria": "TERMO INFORMADO"}]

        with patch("analyzer.v1.extrair_pdf", return_value=(blocos_controlados(), 2, 0, None)):
            sem_callback, _ = v1.analisar_pdf(Path("v1.pdf"), termos)
        with patch("analyzer.v1.extrair_pdf", return_value=(blocos_controlados(), 2, 0, None)):
            com_callback, _ = v1.analisar_pdf(Path("v1.pdf"), termos, progress_callback=lambda _evento: None)
        self.assertEqual(sem_callback, com_callback)

        registros_v2 = [{"ID livro": "v2", "Termo": "São Paulo"}]
        with patch("analyzer.v2._analisar_pdf_referencia", return_value=(registros_v2, {"arquivo": "v2.pdf"})):
            sem_callback_v2 = v2.analisar_pdf(Path("v2.pdf"), termos)
        with patch("analyzer.v2._analisar_pdf_referencia", return_value=(registros_v2, {"arquivo": "v2.pdf"})):
            com_callback_v2 = v2.analisar_pdf(Path("v2.pdf"), termos, progress_callback=lambda _evento: None)
        self.assertEqual(sem_callback_v2, com_callback_v2)

        configuracao = {"incluir_lexical": True, "incluir_semantica": True, "limiar_semantico": 0.70}
        with patch("analyzer.v3.v1.extrair_pdf", return_value=(blocos_controlados(), 2, 0, None)), patch(
            "analyzer.v3.carregar_modelo_semantico", return_value=ModeloMinimo()
        ):
            sem_callback_v3, _ = v3.analisar_pdf(Path("v3.pdf"), termos, configuracao)
        with patch("analyzer.v3.v1.extrair_pdf", return_value=(blocos_controlados(), 2, 0, None)), patch(
            "analyzer.v3.carregar_modelo_semantico", return_value=ModeloMinimo()
        ):
            com_callback_v3, _ = v3.analisar_pdf(
                Path("v3.pdf"), termos, configuracao, progress_callback=lambda _evento: None
            )
        self.assertEqual(sem_callback_v3, com_callback_v3)

    def test_frontend_mantem_polling_e_bloqueio_de_reenvio(self):
        with aplicacao_web.app.test_client() as cliente:
            html = cliente.get("/").get_data(as_text=True)
        self.assertIn("iniciarPolling", html)
        self.assertIn("X-Requested-With", html)
        self.assertIn("if (enviando)", html)
        self.assertIn("window.clearTimeout(temporizadorPolling)", html)
        self.assertIn("typeof progresso.etaSegundos === 'number'", html)
        self.assertIn("typeof percentual === 'number'", html)

    def test_worker_conclui_sem_expor_dados_analiticos_no_progresso(self):
        job_id = "8626e4bb-b6a7-4e86-8447-a30e6bc917e2"
        aplicacao_web._novo_progresso(job_id, "v1", 1)
        resultado = {
            "arquivos": [],
            "erros": [],
            "quantidade_pdfs": 1,
            "quantidade_termos": 1,
            "ocorrencias": None,
            "diagnosticos": None,
            "termos": [],
            "versao": "v1",
        }
        with patch("app.executar_analises", return_value=resultado), patch(
            "app.criar_dashboard", return_value={"indicadores": {}}
        ):
            aplicacao_web._executar_job(
                job_id,
                [Path("livro.pdf")],
                [{"termo": "Teste", "categoria": "TERMO INFORMADO"}],
                Path("saidas"),
                "v1",
                None,
                "/resultado/" + job_id,
            )
        publico = aplicacao_web._progresso_publico(job_id)
        self.assertEqual(publico["status"], "concluido")
        self.assertNotIn("ocorrencias", publico)
        self.assertNotIn("termos", publico)

    def test_multiplos_pdfs_identificam_livro_e_consolidado_nos_tres_metodos(self):
        from analyzer import common

        termos = [{"termo": "São Paulo", "categoria": "TERMO INFORMADO"}]
        configuracao_v3 = {
            "incluir_lexical": True,
            "incluir_semantica": False,
            "limiar_semantico": 0.70,
        }
        for versao in ("v1", "v2", "v3"):
            analisador = AnalisadorDeMultiplosArquivos()
            eventos = []
            with tempfile.TemporaryDirectory() as diretorio, patch(
                "analyzer.common.obter_analisador", return_value=analisador
            ):
                resultado = common.executar_analises(
                    [Path("primeiro.pdf"), Path("segundo.pdf")],
                    termos,
                    Path(diretorio),
                    versao,
                    configuracao_v3 if versao == "v3" else None,
                    progress_callback=eventos.append,
                )

            concluidos = [evento for evento in eventos if evento["fase"] == "pdf_concluido"]
            self.assertEqual([evento["arquivo_indice"] for evento in concluidos], [1, 2])
            self.assertEqual([evento["arquivo"] for evento in concluidos], ["primeiro.pdf", "segundo.pdf"])
            self.assertIn("gerando_consolidado", [evento["fase"] for evento in eventos])
            self.assertEqual(resultado["quantidade_pdfs"], 2)
            self.assertEqual(len(resultado["arquivos"]), 3)
            self.assertTrue(resultado["arquivos"][-1]["consolidado"])
            self.assertEqual(len(analisador.planilhas), 3)


if __name__ == "__main__":
    unittest.main()
