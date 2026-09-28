"""Contrato linguístico compartilhado das ferramentas ativas da plataforma."""

import unittest

from analyzer import v1, v2, v3
from analyzer.lexical_family import find_lexical_spans, morphological_relation, word_relation
from historico_racial.entities import Entidade
from historico_racial.occurrences import BuscadorLexical
import varredura_pdf_v1


class LexicalFamilyTests(unittest.TestCase):
    def test_raca_family_and_morphological_boundary(self):
        forms = ("raça", "raças", "racial", "raciais", "racialmente", "racialização",
                 "racializações", "racializado", "racializada", "racializados",
                 "racializadas", "racista", "racistas", "racismo")
        self.assertEqual({word for word in forms if word_relation("raça", word)}, set(forms))
        self.assertEqual({word for word in forms if word_relation("raca", word)}, set(forms))
        self.assertEqual({word for word in forms if morphological_relation("raça", word)},
                         {"raça", "raças"})
        for false_positive in ("racional", "raciocínio", "raquete", "preconceito",
                               "discriminação", "segregação", "identidade", "etnia"):
            self.assertIsNone(word_relation("raça", false_positive))

    def test_other_families_and_phrase_adjacency(self):
        for query, candidate in (("professor", "professoras"),
                                 ("educação", "educacional"),
                                 ("educação", "educacionais"),
                                 ("colonial", "colonialismo"),
                                 ("colonial", "colonialista"),
                                 ("colonial", "colonização"),
                                 ("colonial", "colonizado"),
                                 ("racial", "racialização")):
            with self.subTest(query=query, candidate=candidate):
                self.assertIsNotNone(word_relation(query, candidate))
        text = "relação racializada; racializada relação; relação antiga racializada"
        self.assertEqual([text[start:end] for start, end, _ in
                          find_lexical_spans(text, "relação racial")], ["relação racializada"])

    def test_tools_share_same_recognition_without_changing_historical_explicit_only(self):
        text = "raça raças racial racismo racional"
        shared = [text[start:end] for start, end, _ in find_lexical_spans(text, "raça")]
        self.assertEqual(shared, ["raça", "raças", "racial", "racismo"])
        self.assertEqual(v1.contar_ocorrencias(text, "raça"), len(shared))
        self.assertEqual(v2.contar_ocorrencias(text, "raça"), len(shared))
        self.assertEqual(varredura_pdf_v1.contar_ocorrencias(text, "raça"), len(shared))
        self.assertEqual(v3._tipo_lexical("racismo", "raça"), ("Lexical", "racismo"))
        entity = Entidade("raca", "raça", ("raça",), "conceito", ("teste",))
        historic = BuscadorLexical((entity,)).localizar(text)
        self.assertEqual([item.forma_original_no_texto for item in historic], ["raça"])
        morphological = BuscadorLexical((entity,), incluir_morfologia=True).localizar(text)
        self.assertEqual([item.forma_original_no_texto for item in morphological], ["raça", "raças"])
        structured = BuscadorLexical((entity,), incluir_familia_lexical=True).localizar(text)
        self.assertEqual([item.forma_original_no_texto for item in structured], shared)


if __name__ == "__main__":
    unittest.main()
