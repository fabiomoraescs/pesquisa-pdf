"""Testes da configuração externa, sem executar análise de PDFs."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

import yaml

from historico_racial.dictionaries import (
    ARQUIVOS,
    CONFIG_DIR,
    ConfiguracaoInvalidaError,
    carregar_categorias,
    carregar_configuracoes,
    carregar_entidades,
    carregar_ibr,
    carregar_schema,
    carregar_sugestoes,
)
from historico_racial.entities import listar_entidades
from historico_racial.schemas import (
    CODIFICACAO,
    COOCORRENCIAS,
    DOCUMENTOS,
    OCORRENCIAS,
    SCHEMA_VERSION,
    obter_esquema,
)


class TesteConfiguracaoHistoricoRacial(unittest.TestCase):
    def test_cinco_yamls_carregam_sem_executar_analise(self):
        configuracoes = carregar_configuracoes()
        self.assertEqual(set(configuracoes), {"schema", "entities", "categories", "suggestions", "ibr"})
        self.assertTrue(carregar_categorias()["tipos_entidade"])
        self.assertTrue(carregar_sugestoes()["classificacoes_sugeridas"])

    def test_schema_version_e_quatro_estruturas(self):
        esquema = carregar_schema()
        self.assertEqual(esquema["schema_version"], "0.1")
        self.assertEqual(SCHEMA_VERSION, "0.1")
        self.assertEqual(obter_esquema(), esquema)
        self.assertEqual(
            set(esquema["estruturas"]),
            {DOCUMENTOS, OCORRENCIAS, CODIFICACAO, COOCORRENCIAS},
        )

    def test_entidades_possuem_ids_unicos_e_variantes_na_mesma_entidade(self):
        entidades = listar_entidades()
        ids = [entidade.id_entidade for entidade in entidades]
        self.assertEqual(len(ids), len(set(ids)))
        por_id = {entidade.id_entidade: entidade for entidade in entidades}
        self.assertEqual(por_id["du_bois"].forma_canonica, "W. E. B. Du Bois")
        self.assertIn("Du Bois", por_id["du_bois"].variantes)
        self.assertNotIn("du_bois_curto", por_id)
        self.assertEqual(sum("Du Bois" in entidade.variantes for entidade in entidades), 1)
        self.assertIn("Kwame Ture", por_id["stokely_carmichael"].variantes)
        self.assertEqual(len(por_id["mesticagem"].grupo), 2)
        with self.assertRaises(FrozenInstanceError):
            por_id["du_bois"].forma_canonica = "outra"

    def test_autores_e_categorias_raciais_basicas(self):
        entidades = listar_entidades()
        por_id = {entidade.id_entidade: entidade for entidade in entidades}
        for identificador in ("malcolm_x", "frantz_fanon", "florestan_fernandes"):
            self.assertIn(identificador, por_id)
        variantes = {variante for entidade in entidades for variante in entidade.variantes}
        for forma in (
            "negro", "negros", "negra", "negras", "preto", "pretos", "preta",
            "pretas", "pardo", "pardos", "parda", "pardas", "mulato", "mulatos",
            "mulata", "mestiço", "mestiços", "mestiça", "mestiças", "branco",
            "brancos", "branca", "brancas", "homem de cor", "homens de cor",
            "gente de cor", "população negra", "raça negra",
        ):
            self.assertIn(forma, variantes)

    def test_ibr_registra_b1_a_b5_sem_calculo(self):
        ibr = carregar_ibr()
        self.assertEqual(set(ibr["criterios"]), {"B1", "B2", "B3", "B4", "B5"})
        self.assertEqual((ibr["minimo"], ibr["maximo"]), (0, 5))
        self.assertIn("análise humana", ibr["observacao"])

    def test_configuracao_ausente_ou_yaml_invalido_gera_erro_controlado(self):
        with tempfile.TemporaryDirectory() as temporario:
            with self.assertRaisesRegex(ConfiguracaoInvalidaError, "ausente"):
                carregar_schema(temporario)
            caminho = Path(temporario) / "schema.yml"
            caminho.write_text("schema_version: [", encoding="utf-8")
            with self.assertRaisesRegex(ConfiguracaoInvalidaError, "Não foi possível carregar"):
                carregar_schema(temporario)

    def test_id_duplicado_em_configuracao_e_rejeitado(self):
        with tempfile.TemporaryDirectory() as temporario:
            destino = Path(temporario)
            for nome in ARQUIVOS:
                shutil.copyfile(CONFIG_DIR / nome, destino / nome)
            caminho = destino / "entities.yml"
            dados = yaml.safe_load(caminho.read_text(encoding="utf-8"))
            dados["entidades"].append(dict(dados["entidades"][0]))
            caminho.write_text(yaml.safe_dump(dados, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfiguracaoInvalidaError, "ID de entidade repetido"):
                carregar_entidades(destino)

    def test_variante_de_outra_entidade_e_rejeitada(self):
        with tempfile.TemporaryDirectory() as temporario:
            destino = Path(temporario)
            for nome in ARQUIVOS:
                shutil.copyfile(CONFIG_DIR / nome, destino / nome)
            caminho = destino / "entities.yml"
            dados = yaml.safe_load(caminho.read_text(encoding="utf-8"))
            dados["entidades"][1]["variantes"].append("negro")
            caminho.write_text(yaml.safe_dump(dados, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfiguracaoInvalidaError, "Variante repetida"):
                carregar_entidades(destino)


if __name__ == "__main__":
    unittest.main()
