"""Regressões da autocodificação Lexical/Semântica sem baixar modelos nos testes."""

import hashlib
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import numpy as np
from sqlalchemy import select

import test_qualitative_automatic as automatic_fixture
import test_qualitative_context as context_fixture
from platform_core.extensions import db
from platform_core.models import QualitativeCode, QualitativeCoding, QualitativeExcerpt, QualitativeRejection
from platform_core.qualitative_corpus import load_qualitative_manifest, qualitative_corpus_dir, qualitative_manifest_path
from platform_core.qualitative_expanded_search import (
    SEGMENTATION_VERSION, is_semantic_segment_eligible, search_semantic, semantic_segments,
)
from platform_core.qualitative_expanded_search import semantic_threshold


class FakeModel:
    def __init__(self, classifier):
        self.classifier = classifier
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append(tuple(texts))
        return np.asarray([self.classifier(text) for text in texts], dtype=np.float32)


class ExpandedAutomaticTests(unittest.TestCase):
    setUp = context_fixture.QualitativeContextTests.setUp
    tearDown = context_fixture.QualitativeContextTests.tearDown
    auto_url = automatic_fixture.QualitativeAutomaticTests.auto_url
    automatic = automatic_fixture.QualitativeAutomaticTests.automatic
    remove = automatic_fixture.QualitativeAutomaticTests.remove
    codings = automatic_fixture.QualitativeAutomaticTests.codings
    fixture_pages = automatic_fixture.QualitativeAutomaticTests.fixture_pages

    def test_lexical_portuguese_forms_single_code_exact_spans_and_no_prefix(self):
        self.fixture_pages([["professor professores professora professoras profissional"]])
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
                   side_effect=AssertionError("PDF/OCR não pode ser reaberto")):
            response = self.automatic(q="professor", mode="lexical")
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json["search"]["total"], 4)
        self.assertEqual(response.json["summary"]["created"], 4)
        self.assertEqual(db.session.query(QualitativeCode).count(), 1)
        self.assertEqual(db.session.scalar(select(QualitativeCode.name)), "professor")
        self.assertEqual({coding.origin for coding in self.codings()}, {"automatic_lexical"})
        quotes = {db.session.get(QualitativeExcerpt, coding.excerpt_id).quoted_text for coding in self.codings()}
        self.assertEqual(quotes, {"professor", "professores", "professora", "professoras"})
        again = self.automatic(q="professor", mode="lexical")
        self.assertEqual((again.json["summary"]["created"], again.json["summary"]["existing"]), (0, 4))
        self.assertEqual(db.session.query(QualitativeExcerpt).count(), 4)

    def test_lexical_raca_derivations_keep_query_code_and_concrete_offsets(self):
        forms = ("raça raças racial raciais racialmente racialização racializações "
                 "racializado racializada racializados racializadas racista racistas racismo")
        self.fixture_pages([[forms + " racional raciocínio raquete preconceito discriminação"]])
        response = self.automatic(q="raça", mode="lexical")
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json["search"]["total"], 14)
        self.assertEqual(db.session.scalar(select(QualitativeCode.name)), "raça")
        self.assertEqual([item["match_text"] for item in response.json["search"]["results"]], forms.split())
        for item in response.json["search"]["results"]:
            self.assertEqual(forms[item["start_offset"]:item["end_offset"]], item["match_text"])

    def test_lexical_irregular_plurals_phrase_order_and_case(self):
        self.fixture_pages([["relação racial; relações raciais; racial relação. ",
                             "discriminação discriminações. Professor professor PROFESSOR"]])
        phrase = self.automatic(q="relações raciais", mode="lexical")
        self.assertEqual(phrase.status_code, 201, phrase.json)
        self.assertEqual(phrase.json["search"]["total"], 2)
        self.assertEqual([item["match_type"] for item in phrase.json["search"]["results"]],
                         ["lexical", "literal"])
        plural = self.automatic(q="discriminação", mode="lexical")
        self.assertEqual(plural.json["search"]["total"], 2)
        case = self.automatic(q="Professor", mode="lexical", case_sensitive=True)
        self.assertEqual(case.json["search"]["total"], 1)
        insensitive = self.automatic(q="Professor", mode="lexical", case_sensitive=False)
        self.assertEqual(insensitive.json["search"]["total"], 3)

    def test_lexical_multiple_terms_scope_and_over_200_without_cap(self):
        first = " ".join(["professores"] * 230) + " relação racial"
        self.fixture_pages([[first], ["professor relação racial"], ["professora"]])
        current = self.automatic(q="professor; relação racial", mode="lexical", multiple_terms=True)
        self.assertEqual(current.status_code, 201, current.json)
        self.assertEqual([item["total"] for item in current.json["terms"]], [230, 1])
        self.assertEqual({code.name for code in db.session.scalars(select(QualitativeCode))},
                         {"professor", "relação racial"})
        project = self.automatic(q="professor; relação racial", mode="lexical",
                                 multiple_terms=True, scope="project")
        self.assertEqual([item["total"] for item in project.json["terms"]], [232, 2])
        self.assertEqual(project.json["summary"]["created"], 3)
        self.assertEqual(project.json["summary"]["existing"], 231)
        self.assertEqual(db.session.query(QualitativeCode).count(), 2)

    def test_lexical_contextual_rejection_is_local_and_optional(self):
        self.fixture_pages([["professores professoras"]])
        response = self.automatic(q="professor", mode="lexical", contextual_rejection_enabled=True)
        self.assertEqual(response.json["summary"]["created"], 2)
        self.assertEqual(self.remove(self.codings()[0]).status_code, 200)
        self.assertEqual(db.session.query(QualitativeRejection).count(), 1)
        repeated = self.automatic(q="professor", mode="lexical")
        self.assertEqual((repeated.json["summary"]["rejected"], repeated.json["summary"]["existing"]), (1, 1))
        literal = self.automatic(q="professores", mode="literal")
        self.assertEqual(literal.json["summary"]["rejected"], 0)
        self.assertEqual(self.remove(self.codings()[-1]).status_code, 200)

    def test_semantic_includes_literal_lexical_and_context_without_overlap(self):
        corpus = ("relação racial. relações raciais. "
                  "Pessoas negras sofrem barreiras institucionais. Maçãs verdes no pomar.")
        self.fixture_pages([[corpus]])
        model = FakeModel(lambda text: [0, 1] if "Maçãs" in text else [1, 0])
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            response = self.automatic(q="relação racial", mode="semantic")
            again = self.automatic(q="relação racial", mode="semantic")
        self.assertEqual(response.status_code, 201, response.json)
        matches = response.json["search"]["results"]
        self.assertEqual([item["match_type"] for item in matches], ["literal", "lexical", "semantic"])
        self.assertEqual(response.json["summary"]["created"], 3)
        self.assertEqual(again.json["summary"]["existing"], 3)
        self.assertEqual({coding.origin for coding in self.codings()}, {"automatic_semantic"})
        self.assertEqual(db.session.scalar(select(QualitativeCode.name)), "relação racial")
        self.assertEqual(db.session.query(QualitativeExcerpt).count(), 3)
        self.assertEqual(len(model.calls), 3)  # duas queries; segmentos codificados só na primeira busca

    def test_semantic_threshold_multiple_terms_scope_and_cache_invalidation(self):
        self.fixture_pages([["Pessoas negras sofrem barreiras. Maçãs verdes crescem."],
                            ["Há disparidade no acesso à renda."]])
        def classify(text):
            if text == "racismo" or "Pessoas negras" in text:
                return [1, 0, 0]
            if text == "desigualdade racial" or "disparidade" in text:
                return [0, 1, 0]
            return [0.699, 0, 0.715]  # similaridade abaixo do limiar 0.70
        model = FakeModel(classify)
        with (patch("platform_core.qualitative_expanded_search._model", return_value=model),
              patch.dict(os.environ, {"QUALITATIVE_SEMANTIC_THRESHOLD": "0.70"})):
            response = self.automatic(q="racismo; desigualdade racial", mode="semantic",
                                      multiple_terms=True, scope="project")
            repeated = self.automatic(q="racismo; desigualdade racial", mode="semantic",
                                      multiple_terms=True, scope="project")
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual([item["total"] for item in response.json["terms"]], [1, 1])
        self.assertEqual(repeated.json["summary"]["existing"], 2)
        self.assertEqual(db.session.query(QualitativeCode).count(), 2)
        self.assertEqual(len([call for call in model.calls if "Pessoas negras sofrem barreiras." in call]), 1)
        manifest = load_qualitative_manifest(self.analysis)
        page = manifest["documents"][0]["pages"][0]
        replacement = "Pessoas negras enfrentam outras barreiras."
        (qualitative_corpus_dir(self.analysis.id) / page["file"]).write_text(replacement, encoding="utf-8")
        page["sha256"] = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
        page["char_count"] = len(replacement)
        qualitative_manifest_path(self.analysis.id).write_text(json.dumps(manifest), encoding="utf-8")
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            changed = search_semantic(self.analysis, self.document.id, "racismo")
        self.assertEqual(changed["total"], 1)
        self.assertEqual(len([call for call in model.calls if "Pessoas negras enfrentam outras barreiras." in call]), 1)
        with (patch("platform_core.qualitative_expanded_search._model", return_value=model),
              patch("platform_core.qualitative_expanded_search.v3.MODELO_SEMANTICO", "novo-modelo-v2")):
            search_semantic(self.analysis, self.document.id, "racismo")
        self.assertEqual(len([call for call in model.calls if "Pessoas negras enfrentam outras barreiras." in call]), 2)

    def test_semantic_more_than_200_threshold_matches_and_model_error(self):
        self.fixture_pages([["Pessoas negras enfrentam barreiras. " * 220]])
        model = FakeModel(lambda _: [1, 0])
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            response = self.automatic(q="racismo", mode="semantic")
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json["search"]["total"], 220)
        self.assertEqual(response.json["summary"]["created"], 220)
        self.fixture_pages([["Outras pessoas enfrentam problemas novos."]])
        with patch("platform_core.qualitative_expanded_search.v3.carregar_modelo_semantico",
                   side_effect=RuntimeError("offline")):
            failed = self.automatic(q="novo", mode="semantic")
        self.assertEqual(failed.status_code, 503)
        self.assertIn("Nenhuma codificação foi salva", failed.json["error"])
        self.assertEqual(db.session.query(QualitativeCode).count(), 1)

    def test_semantic_progress_reports_real_pages_batches_cache_commit_and_error(self):
        self.fixture_pages([["O primeiro segmento contém uma ideia. " * 45,
                             "O segundo segmento contém outra ideia."],
                            ["O terceiro segmento contém uma ideia."]])
        model = FakeModel(lambda _: [1, 0])
        events = []
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            search_semantic(self.analysis, None, "conceito", progress_callback=events.append)
            encoded_before_cache = len(model.calls)
            cached_events = []
            search_semantic(self.analysis, None, "conceito", progress_callback=cached_events.append)
        self.assertEqual(len(model.calls) - encoded_before_cache, 1)  # só a query, páginas em cache
        self.assertEqual([event["completed"] for event in events if event["stage"] == "literal"],
                         [1, 2, 3])
        self.assertEqual([event["completed"] for event in events if event["stage"] == "lexical"],
                         [1, 2, 3])
        self.assertEqual([event["completed"] for event in cached_events
                          if event["stage"] == "semantic"][-1], 3)
        job_id = str(uuid4())
        url = f"/analise-qualitativa/bases/{self.analysis.id}/progresso-semantico/{job_id}"
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            response = self.automatic(q="conceito", mode="semantic", scope="project", progress_id=job_id)
        self.assertEqual(response.status_code, 201, response.json)
        state = self.client.get(url).json
        self.assertEqual((state["state"], state["percent"]), ("complete", 100))
        self.assertEqual(state["document"], "Documento 1.pdf")
        self.assertEqual(state["page_number"], 1)
        failed_id = str(uuid4())
        with patch("platform_core.qualitative_expanded_search.v3.carregar_modelo_semantico",
                   side_effect=RuntimeError("offline")):
            failed = self.automatic(q="outro conceito", mode="semantic", progress_id=failed_id)
        self.assertEqual(failed.status_code, 503)
        failed_state = self.client.get(url.replace(job_id, failed_id)).json
        self.assertEqual(failed_state["state"], "error")
        self.assertLess(failed_state["percent"], 100)
        self.assertEqual(self.client.get(url.replace(job_id, str(uuid4()))).status_code, 404)

    def test_semantic_scope_rejection_and_optional_recreation(self):
        self.fixture_pages([["Barreiras institucionais persistem. Texto desconexo."],
                            ["Barreiras institucionais reaparecem."]])
        model = FakeModel(lambda text: [0, 1] if "desconexo" in text else [1, 0])
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            current = self.automatic(q="iniquidade", mode="semantic", contextual_rejection_enabled=True)
            self.assertEqual(current.json["search"]["total"], 1)
            self.assertEqual(current.json["summary"]["created"], 1)
            first = self.codings()[0]
            self.assertEqual(self.remove(first).status_code, 200)
            rejected = self.automatic(q="iniquidade", mode="semantic")
            self.assertEqual(rejected.json["summary"]["rejected"], 1)
            self.assertEqual(rejected.json["summary"]["created"], 0)
            across = self.automatic(q="iniquidade", mode="semantic", scope="project")
            self.assertEqual(across.json["search"]["total"], 2)
            self.assertEqual(across.json["summary"]["created"], 1)
            other = self.automatic(q="exclusão", mode="semantic")
            self.assertEqual(other.json["summary"]["created"], 1)
            other_id = other.json["terms"][0]["code_id"]
            coding = db.session.scalar(select(QualitativeCoding).where(QualitativeCoding.code_id == other_id))
            self.assertFalse(coding.contextual_rejection_enabled)
            self.assertEqual(self.remove(coding).status_code, 200)
            self.assertEqual(db.session.query(QualitativeRejection).count(), 1)
            recreated = self.automatic(q="exclusão", mode="semantic")
            self.assertEqual(recreated.json["summary"]["created"], 1)

    def test_semantic_threshold_includes_boundary_and_no_result_creates_no_code(self):
        self.fixture_pages([["O primeiro tema apresenta um argumento. "
                             "O segundo tema apresenta outro argumento."]])
        model = FakeModel(lambda text: [4, 3] if "primeiro" in text else
                          ([3, 4] if "segundo" in text else [1, 0]))
        with (patch("platform_core.qualitative_expanded_search._model", return_value=model),
              patch.dict(os.environ, {"QUALITATIVE_SEMANTIC_THRESHOLD": "0.8"})):
            found = self.automatic(q="conceito", mode="semantic")
        self.assertEqual(found.status_code, 201, found.json)
        self.assertEqual(found.json["search"]["total"], 1)
        self.assertEqual(found.json["summary"]["created"], 1)
        with (patch("platform_core.qualitative_expanded_search._model", return_value=model),
              patch.dict(os.environ, {"QUALITATIVE_SEMANTIC_THRESHOLD": "0.9"})):
            none = self.automatic(q="outro conceito", mode="semantic")
        self.assertEqual(none.json["search"]["total"], 0)
        self.assertEqual(none.json["summary"]["created"], 0)
        self.assertEqual(db.session.query(QualitativeCode).count(), 1)

    def test_semantic_default_half_and_environment_override(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUALITATIVE_SEMANTIC_THRESHOLD", None)
            self.assertEqual(semantic_threshold(), 0.50)
        self.fixture_pages([["O primeiro trecho desenvolve uma ideia. "
                             "O segundo trecho desenvolve outra ideia. "
                             "O terceiro trecho desenvolve mais uma ideia."]])
        model = FakeModel(lambda text: [1, 0] if text == "conceito" else
                          ([0.5, 0.8660254] if "primeiro" in text else
                           [0.7, 0.7141428] if "segundo" in text else [0.49, 0.8717224]))
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            found = self.automatic(q="conceito", mode="semantic")
        self.assertEqual(found.json["search"]["total"], 2)
        with (patch("platform_core.qualitative_expanded_search._model", return_value=model),
              patch.dict(os.environ, {"QUALITATIVE_SEMANTIC_THRESHOLD": "0.70"})):
            override = self.automatic(q="conceito", mode="semantic")
        self.assertEqual(override.json["search"]["total"], 1)

    def test_semantic_multiple_terms_model_failure_writes_nothing(self):
        self.fixture_pages([["O tema inicial apresenta uma ideia. "
                             "O tema posterior apresenta outra ideia."]])
        class FailingModel(FakeModel):
            def encode(self, texts, **kwargs):
                if texts == ["segundo conceito"]:
                    raise RuntimeError("Falha de inferência")
                return super().encode(texts, **kwargs)
        model = FailingModel(lambda _: [1, 0])
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            failed = self.automatic(q="primeiro conceito; segundo conceito", mode="semantic",
                                    multiple_terms=True)
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(db.session.query(QualitativeCode).count(), 0)
        self.assertEqual(db.session.query(QualitativeExcerpt).count(), 0)
        self.assertEqual(db.session.query(QualitativeCoding).count(), 0)

    def test_segmentation_keeps_real_offsets_and_never_cuts_a_word(self):
        text = "Esta frase curta apresenta uma ideia.\n\n" + "palavra " * 90 + "FIM"
        segments = semantic_segments(text)
        self.assertGreater(len(segments), 2)
        for start, end in segments:
            self.assertTrue(text[start:end].strip())
            self.assertFalse(text[start].isspace())
            self.assertFalse(text[end - 1].isspace())
            self.assertTrue(end == len(text) or not text[end].isalpha())

    def test_semantic_eligibility_rejects_numeric_and_editorial_fragments(self):
        for fragment in ("127", "12", "2026", "12.", "--- 45 ---", "123–125",
                         "15/09/2026", "Raça", "Introdução", "Capítulo 4",
                         "Figura 2", "Editora X", "Cabo Gilberto\nPraça\nCabo\nPrimeiro",
                         "ab�cd de�fg hi�jk lm�no pq�rs tu�vw"):
            with self.subTest(fragment=fragment):
                self.assertFalse(is_semantic_segment_eligible(fragment))
                self.assertEqual(semantic_segments(fragment), [])
        for sentence in ("O racismo estrutura relações sociais.",
                         "Em 2022, as desigualdades raciais permaneceram elevadas.",
                         "As classificações raciais foram historicamente utilizadas para hierarquizar grupos sociais."):
            with self.subTest(sentence=sentence):
                self.assertTrue(is_semantic_segment_eligible(sentence))
                self.assertEqual(semantic_segments(sentence), [(0, len(sentence))])

    def test_numbers_never_load_model_or_create_coding_and_progress_completes(self):
        self.fixture_pages([["127", "12", "2026", "123–125", "15/09/2026"]])
        events = []
        with patch("platform_core.qualitative_expanded_search._model",
                   side_effect=AssertionError("números não podem gerar embedding")):
            found = search_semantic(self.analysis, self.document.id, "raça",
                                    progress_callback=events.append)
            saved = self.automatic(q="raça", mode="semantic")
        self.assertEqual((found["total"], saved.json["summary"]["created"]), (0, 0))
        self.assertEqual(db.session.query(QualitativeCoding).count(), 0)
        self.assertEqual([event["completed"] for event in events if event["stage"] == "semantic"][-1], 5)

    def test_catalogue_metadata_page_is_not_semantic_prose(self):
        catalog = ("FICHA CATALOGRÁFICA\n"
                   "As classificações raciais foram historicamente utilizadas.\n"
                   "ISBN 978-65-87600-90-1. CDD 301:371.3")
        self.assertEqual(semantic_segments(catalog), [])
        self.assertEqual(semantic_segments("As classificações raciais foram historicamente utilizadas."),
                         [(0, len("As classificações raciais foram historicamente utilizadas."))])

    def test_repeated_page_furniture_excluded_before_embedding_without_changing_offsets(self):
        pages = [f"BRUNO CAMARGOS E POLICIAIS MILITARES\n{number}\n"
                 "Grupos sociais foram historicamente hierarquizados.\n"
                 "Uma panela cozinha alimentos durante a manhã."
                 for number in (127, 128, 129, 130)]
        self.fixture_pages([pages])
        model = FakeModel(lambda text: [1, 0] if text == "raça" or "hierarquizados" in text else [0, 1])
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            found = search_semantic(self.analysis, self.document.id, "raça")
        self.assertEqual(found["total"], 4)
        self.assertTrue(all(item["match_text"] ==
                            "Grupos sociais foram historicamente hierarquizados." for item in found["results"]))
        for index, item in enumerate(found["results"]):
            self.assertEqual(pages[index][item["start_offset"]:item["end_offset"]], item["match_text"])
        embedded = [segment for call in model.calls for segment in call if segment != "raça"]
        self.assertFalse(any("BRUNO CAMARGOS" in segment or segment.strip().isdigit()
                             for segment in embedded))

    def test_changed_segmentation_version_does_not_reuse_old_page_embeddings(self):
        self.fixture_pages([["Grupos sociais foram historicamente hierarquizados."]])
        model = FakeModel(lambda _: [1, 0])
        with (patch("platform_core.qualitative_expanded_search._model", return_value=model),
              patch("platform_core.qualitative_expanded_search.SEGMENTATION_VERSION", "sentence-word-v1")):
            search_semantic(self.analysis, self.document.id, "raça")
        before = len([call for call in model.calls if "Grupos sociais foram historicamente hierarquizados." in call])
        self.assertEqual(before, 1)
        self.assertNotEqual(SEGMENTATION_VERSION, "sentence-word-v1")
        with patch("platform_core.qualitative_expanded_search._model", return_value=model):
            search_semantic(self.analysis, self.document.id, "raça")
            search_semantic(self.analysis, self.document.id, "raça")
        after = len([call for call in model.calls if "Grupos sociais foram historicamente hierarquizados." in call])
        self.assertEqual(after, 2)  # primeira v2 recalcula; segunda v2 usa o cache


if __name__ == "__main__":
    unittest.main()
