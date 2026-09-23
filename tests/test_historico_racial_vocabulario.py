"""Versões do vocabulário e isolamento da busca histórico-racial."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from platform_helpers import create_project, create_user, csrf_from, isolated_platform, login
from platform_core.vocabularies import project_store
from historico_racial.dictionaries import carregar_entidades
from historico_racial.occurrences import BuscadorLexical
from historico_racial.processor import ArquivoPDF, processar_documentos
from historico_racial.routes import PROGRESSOS_HR, RESULTADOS_HR
from historico_racial.vocabulary import (
    VocabularyStore, VocabularioError, entidades_pesquisaveis, hash_vocabulario,
)


def _pdf(texto: str) -> bytes:
    documento = pymupdf.open()
    pagina = documento.new_page()
    pagina.insert_text((50, 80), texto, fontsize=10)
    conteudo = documento.tobytes()
    documento.close()
    return conteudo


class TesteVocabularioHistoricoRacial(unittest.TestCase):
    def setUp(self):
        self.temporario = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporario.cleanup)
        self.store = VocabularyStore(Path(self.temporario.name))
        self.inicial = self.store.capturar_ativa()

    def _salvar(self, alterar, nota="Teste controlado"):
        rascunho = copy.deepcopy(self.store.capturar_ativa()["vocabulario"])
        alterar(rascunho)
        return self.store.salvar(self.store.capturar_ativa()["version"], rascunho, nota)

    @staticmethod
    def _buscar(snapshot, texto):
        return BuscadorLexical(entidades_pesquisaveis(snapshot["vocabulario"])).localizar(texto)

    def test_bootstrap_cria_primeira_versao_com_todo_o_seed(self):
        self.assertEqual(self.inicial["version"], "v1.0")
        self.assertEqual(self.inicial["counts"], {"grupos": 8, "entidades": 75, "variantes": 117})
        self.assertTrue(self.store.active.is_file())
        self.assertTrue((self.store.versions / "v1.0.json").is_file())
        self.assertTrue(all(g["ativo"] for g in self.inicial["vocabulario"]["grupos"].values()))
        self.assertTrue(all(e["ativo"] and all(v["ativo"] for v in e["variantes"])
                            for e in self.inicial["vocabulario"]["entidades"]))

    def test_seed_preserva_nomes_ids_formas_e_variantes_exatamente(self):
        seed = carregar_entidades()
        atual = self.inicial["vocabulario"]
        self.assertEqual({codigo: grupo["nome"] for codigo, grupo in atual["grupos"].items()}, seed["grupos"])
        self.assertEqual([e["id_entidade"] for e in atual["entidades"]], [e["id_entidade"] for e in seed["entidades"]])
        for persistida, original in zip(atual["entidades"], seed["entidades"], strict=True):
            self.assertEqual(persistida["forma_canonica"], original["forma_canonica"])
            self.assertEqual([v["texto"] for v in persistida["variantes"]], original["variantes"])

    def test_hash_deterministico_e_integridade_do_snapshot(self):
        vocabulario = self.inicial["vocabulario"]
        invertido = {"entidades": vocabulario["entidades"], "grupos": vocabulario["grupos"]}
        self.assertEqual(hash_vocabulario(vocabulario), hash_vocabulario(invertido))
        self.assertEqual(self.inicial["hash"], hash_vocabulario(vocabulario))
        self.assertEqual(len(self.inicial["hash"]), 64)
        corrompido = self.store.versions / "v1.0.json"
        dados = json.loads(corrompido.read_text(encoding="utf-8"))
        dados["vocabulario"]["grupos"]["A_classificacao_racial_brasileira"]["nome"] = "Corrompido"
        corrompido.write_text(json.dumps(dados), encoding="utf-8")
        with self.assertRaises(VocabularioError):
            self.store.carregar("v1.0")

    def test_versao_antiga_permanece_imutavel_e_historico_lista_ativa(self):
        arquivo = self.store.versions / "v1.0.json"
        bytes_antes = arquivo.read_bytes()
        criada = self._salvar(lambda v: v["grupos"]["A_classificacao_racial_brasileira"].update(ativo=False))
        self.assertEqual(criada["version"], "v1.1")
        self.assertEqual(criada["parent"], "v1.0")
        self.assertEqual(arquivo.read_bytes(), bytes_antes)
        self.assertEqual(self.store.carregar("v1.0"), self.inicial)
        self.assertEqual(self.store.capturar_ativa()["version"], "v1.1")
        self.assertEqual([v["version"] for v in self.store.listar_versoes()], ["v1.1", "v1.0"])
        self.assertTrue(self.store.listar_versoes()[0]["active"])
        with self.assertRaises(VocabularioError):
            self.store.salvar("v1.0", criada["vocabulario"])

    def test_snapshot_capturado_nao_muda_com_edicao_posterior(self):
        fixado = self.store.capturar_ativa()
        self._salvar(lambda v: v["grupos"]["A_classificacao_racial_brasileira"].update(ativo=False))
        self.assertEqual(fixado["version"], "v1.0")
        self.assertTrue(fixado["vocabulario"]["grupos"]["A_classificacao_racial_brasileira"]["ativo"])
        self.assertNotEqual(fixado["hash"], self.store.capturar_ativa()["hash"])

    def test_grupo_inativo_bloqueia_entidade_exclusiva(self):
        nova = self._salvar(lambda v: v["grupos"]["A_classificacao_racial_brasileira"].update(ativo=False))
        self.assertEqual(self._buscar(nova, "A população negra foi mencionada."), [])
        self.assertTrue(self._buscar(self.inicial, "A população negra foi mencionada."))

    def test_entidade_inativa_nao_e_pesquisada(self):
        nova = self._salvar(lambda v: next(e for e in v["entidades"] if e["id_entidade"] == "du_bois").update(ativo=False))
        self.assertFalse(any(c.id_entidade == "du_bois" for c in self._buscar(nova, "Du Bois foi citado.")))

    def test_variante_inativa_nao_e_pesquisada(self):
        def alterar(v):
            entidade = next(e for e in v["entidades"] if e["id_entidade"] == "du_bois")
            next(item for item in entidade["variantes"] if item["texto"] == "Du Bois")["ativo"] = False
        nova = self._salvar(alterar)
        self.assertFalse(any(c.id_entidade == "du_bois" for c in self._buscar(nova, "Du Bois foi citado.")))
        self.assertTrue(any(c.id_entidade == "du_bois" for c in self._buscar(nova, "W. E. B. Du Bois foi citado.")))

    def test_multigrupo_ativo_em_um_lado_sem_duplicar_e_inativo_em_ambos(self):
        codigo_a = "A_classificacao_racial_brasileira"
        codigo_b = "B_democracia_racial_sistema_racial_brasileiro"
        um_ativo = self._salvar(lambda v: v["grupos"][codigo_a].update(ativo=False))
        encontrados = [c for c in self._buscar(um_ativo, "A mestiçagem foi debatida.") if c.id_entidade == "mesticagem"]
        self.assertEqual(len(encontrados), 1)
        ambos_inativos = self._salvar(lambda v: v["grupos"][codigo_b].update(ativo=False))
        self.assertFalse(any(c.id_entidade == "mesticagem" for c in self._buscar(ambos_inativos, "A mestiçagem foi debatida.")))

    def test_adicionar_entidade_e_variante_em_grupo_existente(self):
        def alterar(v):
            v["entidades"].append({
                "id_entidade": "consciencia_racial", "forma_canonica": "consciência racial",
                "variantes": [{"texto": "consciência racial", "ativo": True}, {"texto": "consciência de raça", "ativo": True}],
                "tipo_entidade": "conceito", "grupo": ["A_classificacao_racial_brasileira"],
                "tradicao_intelectual": "", "pais_regiao": "", "observacoes": "", "ativo": True,
            })
        nova = self._salvar(alterar)
        achados = self._buscar(nova, "A consciência de raça apareceu no documento.")
        self.assertEqual([c.id_entidade for c in achados], ["consciencia_racial"])
        self.assertEqual(achados[0].entidade.forma_canonica, "consciência racial")

    def test_criar_grupo_e_associar_entidade_existente_sem_duplicacao(self):
        def alterar(v):
            v["grupos"]["identidade_consciencia_racial"] = {
                "id_grupo": "identidade_consciencia_racial", "nome": "Identidade e consciência racial",
                "descricao": "Novo grupo", "ativo": True,
            }
            entidade = next(e for e in v["entidades"] if e["id_entidade"] == "negro")
            entidade["grupo"].append("identidade_consciencia_racial")
        nova = self._salvar(alterar)
        self.assertEqual(nova["counts"]["grupos"], 9)
        self.assertEqual(nova["counts"]["entidades"], 75)
        self.assertEqual(len([c for c in self._buscar(nova, "O negro foi citado.") if c.id_entidade == "negro"]), 1)

    def test_entidade_de_grupo_novo_e_encontrada(self):
        def alterar(v):
            v["grupos"]["identidade_consciencia_racial"] = {
                "id_grupo": "identidade_consciencia_racial", "nome": "Identidade e consciência racial",
                "descricao": "", "ativo": True,
            }
            v["entidades"].append({
                "id_entidade": "orgulho_negro", "forma_canonica": "orgulho negro",
                "variantes": [{"texto": "orgulho negro", "ativo": True}],
                "tipo_entidade": "conceito", "grupo": ["identidade_consciencia_racial"],
                "tradicao_intelectual": "", "pais_regiao": "", "observacoes": "", "ativo": True,
            })
        nova = self._salvar(alterar)
        self.assertEqual([c.id_entidade for c in self._buscar(nova, "O orgulho negro foi citado.")], ["orgulho_negro", "negro"])

    def test_variante_adicionada_a_entidade_existente(self):
        def alterar(v):
            entidade = next(e for e in v["entidades"] if e["id_entidade"] == "du_bois")
            entidade["variantes"].append({"texto": "William Edward Burghardt Du Bois", "ativo": True})
        nova = self._salvar(alterar)
        achados = [c for c in self._buscar(nova, "William Edward Burghardt Du Bois foi citado.") if c.id_entidade == "du_bois"]
        self.assertEqual(len(achados), 1)
        self.assertEqual(achados[0].entidade.forma_canonica, "W. E. B. Du Bois")

    def test_grupos_ativos_em_ambos_nao_duplicam_multigrupo(self):
        achados = [c for c in self._buscar(self.inicial, "A mestiçagem foi debatida.") if c.id_entidade == "mesticagem"]
        self.assertEqual(len(achados), 1)

    def test_duplicidade_de_variante_id_e_forma_e_rejeitada(self):
        base = self.inicial["vocabulario"]
        duplicado = copy.deepcopy(base)
        duplicado["entidades"].append(copy.deepcopy(duplicado["entidades"][0]))
        with self.assertRaisesRegex(VocabularioError, "ID de entidade repetido"):
            self.store.salvar("v1.0", duplicado)
        duplicado = copy.deepcopy(base)
        item = next(e for e in duplicado["entidades"] if e["id_entidade"] == "du_bois")
        item["variantes"].append({"texto": "negro", "ativo": True})
        with self.assertRaisesRegex(VocabularioError, "Variante repetida"):
            self.store.salvar("v1.0", duplicado)
        duplicado = copy.deepcopy(base)
        item = next(e for e in duplicado["entidades"] if e["id_entidade"] == "du_bois")
        item["forma_canonica"] = "negro"
        with self.assertRaisesRegex(VocabularioError, "Forma canônica repetida"):
            self.store.salvar("v1.0", duplicado)

    def test_desativar_nao_exclui_e_remocao_e_rejeitada(self):
        base = self.inicial["vocabulario"]
        sem_grupo = copy.deepcopy(base)
        sem_grupo["grupos"].pop("A_classificacao_racial_brasileira")
        with self.assertRaises(VocabularioError):
            self.store.salvar("v1.0", sem_grupo)
        sem_entidade = copy.deepcopy(base)
        sem_entidade["entidades"] = [e for e in sem_entidade["entidades"] if e["id_entidade"] != "du_bois"]
        with self.assertRaises(VocabularioError):
            self.store.salvar("v1.0", sem_entidade)
        sem_variante = copy.deepcopy(base)
        next(e for e in sem_variante["entidades"] if e["id_entidade"] == "du_bois")["variantes"].pop()
        with self.assertRaises(VocabularioError):
            self.store.salvar("v1.0", sem_variante)

    def test_sem_alteracao_nao_cria_versao(self):
        with self.assertRaisesRegex(VocabularioError, "Nenhuma alteração"):
            self.store.salvar("v1.0", copy.deepcopy(self.inicial["vocabulario"]))
        self.assertEqual(self.store.capturar_ativa()["version"], "v1.0")

    def test_estado_e_grupo_invalidos_retorna_erro_controlado(self):
        invalido = copy.deepcopy(self.inicial["vocabulario"])
        invalido["entidades"][0]["ativo"] = "sim"
        with self.assertRaises(VocabularioError):
            self.store.salvar("v1.0", invalido)
        invalido = copy.deepcopy(self.inicial["vocabulario"])
        invalido["entidades"][0]["grupo"] = ["grupo_inexistente"]
        with self.assertRaises(VocabularioError):
            self.store.salvar("v1.0", invalido)

    def test_tentativa_de_sobrescrever_versao_e_rejeitada(self):
        with self.assertRaises(VocabularioError):
            self.store._criar_versao("v1.0", None, "Duplicada", self.inicial["vocabulario"])
        self.assertEqual(self.store.carregar("v1.0"), self.inicial)

    def test_multiplos_pdfs_mesmo_snapshot_e_resultado_registra_versao_hash(self):
        pdf_a = _pdf("Du Bois foi citado em debates sobre a população negra e outros temas sociais relevantes.")
        pdf_b = _pdf("Fanon foi citado em debates sobre democracia racial e outros temas sociais relevantes.")
        with tempfile.TemporaryDirectory() as pasta:
            a, b = Path(pasta) / "a.pdf", Path(pasta) / "b.pdf"
            a.write_bytes(pdf_a)
            b.write_bytes(pdf_b)
            resultado = processar_documentos([
                ArquivoPDF(a, "a.pdf", "a"), ArquivoPDF(b, "b.pdf", "b")
            ], vocabulario=self.inicial)
        self.assertEqual(resultado["total_pdfs"], 2)
        self.assertEqual(resultado["vocabulario_version"], "v1.0")
        self.assertEqual(resultado["vocabulario_hash"], self.inicial["hash"])
        self.assertEqual({e["arquivo_pdf"] for e in resultado["ocorrencias"]}, {"a.pdf", "b.pdf"})

    def test_job_em_execucao_preserva_snapshot_apos_nova_versao(self):
        from historico_racial import routes
        chamadas = []
        pdf = _pdf("Du Bois foi citado em debates sobre a população negra e outros temas sociais relevantes.")
        with isolated_platform() as test_app:
            create_user()
            with test_app.test_client() as cliente, patch.object(routes.EXECUTOR_HR, "submit", side_effect=lambda *args: chamadas.append(args)):
                login(cliente)
                project_id = create_project(cliente)
                path = f"/analise-documental/projetos/{project_id}"
                token = csrf_from(cliente.get(path))
                original = project_store(project_id).capturar_ativa()
                resposta = cliente.post(f"{path}/analisar", data={"pdfs": (io.BytesIO(pdf), "a.pdf"), "csrf_token": token}, content_type="multipart/form-data", headers={"X-Requested-With": "XMLHttpRequest"})
                self.assertEqual(resposta.status_code, 202)
                job_id = resposta.json["job_id"]
                self.assertEqual(PROGRESSOS_HR[job_id]["vocabulario_version"], "v1.0")
                self.assertEqual(PROGRESSOS_HR[job_id]["vocabulario_hash"], original["hash"])
                edited = copy.deepcopy(original["vocabulario"])
                next(e for e in edited["entidades"] if e["id_entidade"] == "du_bois")["ativo"] = False
                project_store(project_id).salvar("v1.0", edited)
                funcao, *args = chamadas[0]
                funcao(*args)
                self.assertEqual(RESULTADOS_HR[job_id]["vocabulario_version"], "v1.0")
                self.assertTrue(any(e["id_entidade"] == "du_bois" for e in RESULTADOS_HR[job_id]["ocorrencias"]))

    def test_rotas_principal_gerenciador_salvamento_resultado(self):
        with isolated_platform() as test_app:
            create_user()
            with test_app.test_client() as cliente:
                login(cliente)
                project_id = create_project(cliente)
                path = f"/analise-documental/projetos/{project_id}"
                inicio = cliente.get(path)
                gerente = cliente.get(f"{path}/vocabulario")
                self.assertEqual(inicio.status_code, 200)
                self.assertEqual(gerente.status_code, 200)
                self.assertIn("Versão ativa:", inicio.get_data(as_text=True))
                self.assertIn("Gerenciar vocabulário", inicio.get_data(as_text=True))
                self.assertIn("Histórico de versões", gerente.get_data(as_text=True))
                rascunho = copy.deepcopy(project_store(project_id).capturar_ativa()["vocabulario"])
                rascunho["grupos"]["A_classificacao_racial_brasileira"]["ativo"] = False
                token = csrf_from(gerente)
                salvo = cliente.post(f"{path}/vocabulario/versoes", json={"base_version": "v1.0", "vocabulario": rascunho, "nota": "Teste da rota"}, headers={"X-CSRFToken": token})
                self.assertEqual(salvo.status_code, 201)
                self.assertEqual(salvo.json["version"], "v1.1")
                self.assertIn("reprocessados", salvo.json["aviso"])
                obsoleto = cliente.post(f"{path}/vocabulario/versoes", json={"base_version": "v1.0", "vocabulario": rascunho}, headers={"X-CSRFToken": token})
                self.assertEqual(obsoleto.status_code, 409)

    def test_resultado_da_rota_mostra_versao_utilizada_nao_ativa_atual(self):
        job_id = "00000000-0000-4000-8000-000000000001"
        with isolated_platform() as test_app:
            user = create_user()
            with test_app.test_client() as cliente:
                login(cliente)
                project_id = create_project(cliente)
                original = project_store(project_id).capturar_ativa()
                RESULTADOS_HR[job_id] = {
                    "documentos": [], "ocorrencias": [], "grupos": {}, "erros": [],
                    "total_pdfs": 1, "total_ocorrencias": 0, "entidades_distintas": 0,
                    "vocabulario_version": "v1.0", "vocabulario_hash": original["hash"],
                    "project_id": project_id, "owner_user_id": user.id, "library_names": ["Relações raciais"],
                }
                self.addCleanup(lambda: RESULTADOS_HR.pop(job_id, None))
                edited = copy.deepcopy(original["vocabulario"])
                edited["grupos"]["A_classificacao_racial_brasileira"]["ativo"] = False
                project_store(project_id).salvar("v1.0", edited)
                resposta = cliente.get(f"/analise-documental/projetos/{project_id}/resultado/{job_id}")
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Vocabulário utilizado: <strong>v1.0</strong>", resposta.get_data(as_text=True))
        self.assertIn(original["hash"], resposta.get_data(as_text=True))

    def test_ocr_registra_idioma_e_preserva_fallback(self):
        from historico_racial import pdf as modulo_pdf
        pdf = _pdf("Uma página de controle com texto longo para verificar a seleção do idioma OCR sem alterar o reconhecedor.")
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "ocr.pdf"
            caminho.write_bytes(pdf)
            with patch.object(modulo_pdf.v1, "pagina_precisa_ocr", return_value=True), \
                 patch.object(modulo_pdf.v1, "configurar_tesseract", return_value="tesseract"), \
                 patch.object(modulo_pdf.v1.pytesseract, "get_languages", return_value=["eng", "por", "osd"]), \
                 patch.object(modulo_pdf.v1, "ocr_pagina", return_value=[{"texto": "Du Bois foi citado em um texto histórico longo sobre a população negra."}]) as ocr:
                resultado = processar_documentos([ArquivoPDF(caminho, "ocr.pdf", "ocr")], vocabulario=self.inicial)
                self.assertEqual(resultado["documentos"][0]["ocr_idioma_utilizado"], "por+eng")
                self.assertEqual(ocr.call_args.args[1], "por+eng")
            with patch.object(modulo_pdf.v1.pytesseract, "get_languages", return_value=["eng", "osd"]):
                self.assertEqual(modulo_pdf.v1.escolher_idioma_ocr(), "eng")
            with patch.object(modulo_pdf.v1.pytesseract, "get_languages", return_value=["por", "osd"]):
                self.assertEqual(modulo_pdf.v1.escolher_idioma_ocr(), "por")


if __name__ == "__main__":
    unittest.main()
