"""Testes da etapa lexical histórico-racial, independentes de internet/OCR real."""

from __future__ import annotations

import io
from contextlib import contextmanager
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pymupdf

from platform_helpers import create_project, create_user, csrf_from, isolated_platform, login
from historico_racial.context import construir_paragrafos, obter_contexto
from historico_racial.entities import Entidade, listar_entidades
from historico_racial.occurrences import BuscadorLexical
from historico_racial.pdf import PDFInvalidoError, contar_paginas
from historico_racial.processor import ArquivoPDF, ProcessamentoError, processar_documentos
from historico_racial.routes import RESULTADOS_HR


@contextmanager
def client_with_project():
    with isolated_platform() as test_app:
        create_user()
        with test_app.test_client() as client:
            login(client)
            project_id = create_project(client)
            path = f"/analise-documental/projetos/{project_id}"
            token = csrf_from(client.get(path))
            yield client, path, token


def pdf_controlado(paragrafos: list[str]) -> bytes:
    documento = pymupdf.open()
    pagina = documento.new_page(width=595, height=842)
    for indice, paragrafo in enumerate(paragrafos):
        pagina.insert_text((50, 80 + 100 * indice), paragrafo, fontsize=10)
    conteudo = documento.tobytes()
    documento.close()
    return conteudo


class TesteBuscaLexicalHistoricoRacial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.buscador = BuscadorLexical(listar_entidades())

    def test_du_bois_curto_e_completo_sao_a_mesma_entidade(self):
        curtos = self.buscador.localizar("Du Bois foi citado.")
        completos = self.buscador.localizar("W. E. B. Du Bois foi citado.")
        self.assertEqual([item.id_entidade for item in curtos], ["du_bois"])
        self.assertEqual([item.id_entidade for item in completos], ["du_bois"])
        self.assertEqual(curtos[0].termo_encontrado, "Du Bois")
        self.assertEqual(completos[0].termo_encontrado, "W. E. B. Du Bois")

    def test_grafia_original_e_acentos_sao_preservados(self):
        encontrados = self.buscador.localizar("NEGRITUDE, Négritude e negritude.")
        formas = [item.forma_original_no_texto for item in encontrados if item.id_entidade == "negritude"]
        self.assertEqual(formas, ["NEGRITUDE", "Négritude", "negritude"])

    def test_categoria_racial_e_limites_de_palavra(self):
        encontrados = self.buscador.localizar("negro; Marx; Marxismo; supermarxiano.")
        ids = [item.id_entidade for item in encontrados]
        self.assertIn("negro", ids)
        self.assertEqual(ids.count("karl_marx"), 1)
        self.assertNotIn("supermarxiano", [item.forma_original_no_texto for item in encontrados])

    def test_mesticagem_multigrupo_permanece_uma_entidade(self):
        encontrados = self.buscador.localizar("A mestiçagem foi debatida.")
        candidatos = [item for item in encontrados if item.id_entidade == "mesticagem"]
        self.assertEqual(len(candidatos), 1)
        self.assertEqual(len(candidatos[0].entidade.grupo), 2)

    def test_pdf_sem_entidades_retorna_lista_vazia(self):
        self.assertEqual(self.buscador.localizar("Um texto inteiramente fictício sobre nuvens luminosas."), [])

    def test_flexoes_simples_somente_quando_ativadas_para_nova_execucao(self):
        entidade = Entidade("trabalhador", "trabalhador", ("trabalhador",), "conceito", ("G",))
        texto = "As trabalhadoras aparecem no documento."
        self.assertEqual(BuscadorLexical((entidade,)).localizar(texto), [])
        encontrados = BuscadorLexical((entidade,), incluir_morfologia=True).localizar(texto)
        self.assertEqual(len(encontrados), 1)
        self.assertEqual(encontrados[0].termo_encontrado, "trabalhador")
        self.assertEqual(encontrados[0].forma_original_no_texto, "trabalhadoras")

    def test_contexto_anterior_atual_posterior_e_bordas(self):
        paragrafos = construir_paragrafos([{
            "pagina_pdf": 1,
            "blocos": ["Primeiro parágrafo.", "Du Bois aparece aqui.", "Último parágrafo."],
        }])
        meio = obter_contexto(paragrafos, 1)
        self.assertEqual(meio["trecho_anterior"], "Primeiro parágrafo.")
        self.assertEqual(meio["trecho_ocorrencia"], "Du Bois aparece aqui.")
        self.assertEqual(meio["trecho_posterior"], "Último parágrafo.")
        self.assertEqual(meio["contexto_completo"], "Primeiro parágrafo.\n\nDu Bois aparece aqui.\n\nÚltimo parágrafo.")
        self.assertEqual(obter_contexto(paragrafos, 0)["trecho_anterior"], "")
        self.assertEqual(obter_contexto(paragrafos, 2)["trecho_posterior"], "")


class TesteProcessamentoHistoricoRacial(unittest.TestCase):
    def test_pdf_integracao_preserva_contexto_ids_e_ocr_desligado(self):
        pdf = pdf_controlado([
            "O crescimento urbano modificou profundamente a organização das cidades e das relações sociais.",
            "Du Bois participou de importantes debates sobre a população negra e a democracia racial brasileira.",
            "Os movimentos discutiam democracia racial e mestiçagem em um contexto histórico amplo.",
        ])
        with tempfile.TemporaryDirectory() as temporario:
            caminho = Path(temporario) / "controle.pdf"
            caminho.write_bytes(pdf)
            eventos = []
            resultado = processar_documentos([ArquivoPDF(caminho, "controle.pdf", "arquivo-1")], eventos.append)
        self.assertEqual(resultado["total_pdfs"], 1)
        self.assertGreater(resultado["total_ocorrencias"], 0)
        self.assertFalse(resultado["documentos"][0]["ocr_utilizado"])
        ids = [item["id_ocorrencia"] for item in resultado["ocorrencias"]]
        self.assertEqual(len(ids), len(set(ids)))
        for identificador in ids:
            UUID(identificador)
        du_bois = next(item for item in resultado["ocorrencias"] if item["id_entidade"] == "du_bois")
        self.assertEqual(du_bois["forma_original_no_texto"], "Du Bois")
        self.assertIn("O crescimento urbano", du_bois["trecho_anterior"])
        self.assertIn("Du Bois participou", du_bois["trecho_ocorrencia"])
        self.assertIn("Os movimentos discutiam", du_bois["trecho_posterior"])
        self.assertEqual(du_bois["validar"], "pendente")
        self.assertEqual(du_bois["metodo_localizacao"], "lexical")
        percentuais = [evento["percentual"] for evento in eventos if evento.get("percentual") is not None]
        self.assertEqual(percentuais, sorted(percentuais))
        self.assertLessEqual(max(percentuais), 99)

    def test_ocr_existente_e_reutilizado_sem_reescrever_reconhecimento(self):
        pdf = pdf_controlado(["Página usada para testar o caminho controlado do OCR."])
        with tempfile.TemporaryDirectory() as temporario:
            caminho = Path(temporario) / "ocr.pdf"
            caminho.write_bytes(pdf)
            with patch("historico_racial.pdf.v1.pagina_precisa_ocr", return_value=True), \
                 patch("historico_racial.pdf.v1.configurar_tesseract", return_value="tesseract"), \
                 patch("historico_racial.pdf.v1.escolher_idioma_ocr", return_value="por+eng"), \
                 patch("historico_racial.pdf.v1.ocr_pagina", return_value=[{"texto": "Du Bois foi mencionado pela população negra."}]):
                resultado = processar_documentos([ArquivoPDF(caminho, "ocr.pdf", "arquivo-ocr")])
        self.assertTrue(resultado["documentos"][0]["ocr_utilizado"])
        self.assertIn("du_bois", {item["id_entidade"] for item in resultado["ocorrencias"]})

    def test_pdf_invalido_e_vazio_tem_erro_controlado(self):
        with tempfile.TemporaryDirectory() as temporario:
            caminho = Path(temporario) / "invalido.pdf"
            caminho.write_bytes(b"nao e pdf")
            with self.assertRaises(PDFInvalidoError):
                contar_paginas(caminho)
            with self.assertRaises(ProcessamentoError):
                processar_documentos([ArquivoPDF(caminho, "invalido.pdf", "arquivo-x")])

    def test_multiplos_pdfs_mantem_documentos_e_ocorrencias_separados(self):
        pdf_a = pdf_controlado(["Du Bois foi citado em debates sobre questões sociais e políticas no Brasil."])
        pdf_b = pdf_controlado(["Frantz Fanon foi citado em debates sobre questões sociais e políticas no Brasil."])
        with tempfile.TemporaryDirectory() as temporario:
            caminhos = [Path(temporario) / "um.pdf", Path(temporario) / "dois.pdf"]
            caminhos[0].write_bytes(pdf_a)
            caminhos[1].write_bytes(pdf_b)
            arquivos = [ArquivoPDF(caminho, caminho.name, f"arquivo-{indice}") for indice, caminho in enumerate(caminhos)]
            resultado = processar_documentos(arquivos)
        self.assertEqual(resultado["total_pdfs"], 2)
        self.assertEqual(len({item["id_documento"] for item in resultado["documentos"]}), 2)
        self.assertEqual({item["arquivo_pdf"] for item in resultado["ocorrencias"]}, {"um.pdf", "dois.pdf"})

    def test_pdf_legivel_sem_entidades_retorna_zero_ocorrencias(self):
        pdf = pdf_controlado([
            "Uma narrativa inteiramente fictícia descreve nuvens luminosas, jardins imaginários e viagens por lugares inventados."
        ])
        with tempfile.TemporaryDirectory() as temporario:
            caminho = Path(temporario) / "sem-entidades.pdf"
            caminho.write_bytes(pdf)
            resultado = processar_documentos([ArquivoPDF(caminho, caminho.name, "arquivo-vazio")])
        self.assertEqual(resultado["total_pdfs"], 1)
        self.assertEqual(resultado["ocorrencias"], [])

    def test_upload_job_polling_e_pagina_de_resultados(self):
        pdf = pdf_controlado([
            "O crescimento urbano modificou profundamente a organização das cidades e das relações sociais.",
            "Du Bois participou de importantes debates sobre a população negra e a democracia racial brasileira.",
            "Os movimentos discutiam democracia racial e mestiçagem em um contexto histórico amplo.",
        ])
        with client_with_project() as (cliente, path, token):
            resposta = cliente.post(
                f"{path}/analisar",
                data={"pdfs": (io.BytesIO(pdf), "corpus.pdf"), "csrf_token": token},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(resposta.status_code, 202)
            job_id = resposta.json["job_id"]
            UUID(job_id)
            for _ in range(100):
                progresso = cliente.get(resposta.json["progresso_url"])
                self.assertEqual(progresso.status_code, 200)
                if progresso.json["status"] != "processando":
                    break
                time.sleep(0.05)
            self.assertEqual(progresso.json["status"], "concluido")
            self.assertEqual(progresso.json["percentual"], 100)
            pagina = cliente.get(progresso.json["resultado_url"])
            self.assertEqual(pagina.status_code, 200)
            self.assertIn("Salvar base", pagina.get_data(as_text=True))
            pagina = cliente.post(progresso.json["resultado_url"], data={
                "csrf_token": csrf_from(pagina), "name": "Debates raciais",
            }, follow_redirects=True)
            conteudo = pagina.get_data(as_text=True)
            self.assertIn("Du Bois", conteudo)
            self.assertIn("Primeiras ocorrências", conteudo)
            self.assertIn("Contexto", conteudo)

    def test_upload_ausente_ou_extensao_invalida_e_rejeitado(self):
        with client_with_project() as (cliente, path, token):
            vazio = cliente.post(f"{path}/analisar", data={"csrf_token": token}, headers={"X-Requested-With": "XMLHttpRequest"})
            errado = cliente.post(
                f"{path}/analisar",
                data={"pdfs": (io.BytesIO(b"texto"), "termos.txt"), "csrf_token": token},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        self.assertEqual(vazio.status_code, 400)
        self.assertEqual(errado.status_code, 400)
        self.assertIn(".pdf", errado.json["erro"])

    def test_upload_pdf_corrompido_retorna_erro_no_job_sem_http_500(self):
        with client_with_project() as (cliente, path, token):
            resposta = cliente.post(
                f"{path}/analisar",
                data={"pdfs": (io.BytesIO(b"nao e pdf"), "corrompido.pdf"), "csrf_token": token},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(resposta.status_code, 202)
            for _ in range(100):
                progresso = cliente.get(resposta.json["progresso_url"])
                if progresso.json["status"] != "processando":
                    break
                time.sleep(0.05)
        self.assertEqual(progresso.status_code, 200)
        self.assertEqual(progresso.json["status"], "erro")
        self.assertIn("PDF", progresso.json["erro"])

    def test_upload_de_dois_pdfs_cria_dois_documentos_no_mesmo_job(self):
        pdf_a = pdf_controlado(["Du Bois foi citado em debates sobre questões sociais e políticas no Brasil."])
        pdf_b = pdf_controlado(["Frantz Fanon foi citado em debates sobre questões sociais e políticas no Brasil."])
        with client_with_project() as (cliente, path, token):
            resposta = cliente.post(
                f"{path}/analisar",
                data={"pdfs": [(io.BytesIO(pdf_a), "um.pdf"), (io.BytesIO(pdf_b), "dois.pdf")], "csrf_token": token},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(resposta.status_code, 202)
            for _ in range(100):
                progresso = cliente.get(resposta.json["progresso_url"])
                if progresso.json["status"] != "processando":
                    break
                time.sleep(0.05)
        self.assertEqual(progresso.json["status"], "concluido")
        resultado = RESULTADOS_HR[resposta.json["job_id"]]
        self.assertEqual(resultado["total_pdfs"], 2)
        self.assertEqual({item["arquivo_pdf"] for item in resultado["documentos"]}, {"um.pdf", "dois.pdf"})


if __name__ == "__main__":
    unittest.main()
