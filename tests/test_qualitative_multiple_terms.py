"""Consultas múltiplas reutilizam a busca e a transação da autocodificação."""
import unittest
from unittest.mock import patch

import test_qualitative_automatic as fixtures
from platform_core.extensions import db
from platform_core.models import QualitativeCode, QualitativeCoding, QualitativeExcerpt, QualitativeRejection
from platform_core.qualitative_automatic import prepare_automatic_query
from platform_core.qualitative_search import QualitativeSearchError


class MultipleQueryTests(unittest.TestCase):
    def test_parser_preserves_phrases_ignores_empty_and_deduplicates_literal_case(self):
        cases = [
            ("racismo; discriminação", False, ["racismo", "discriminação"]),
            (" racismo ; preconceito racial ; discriminação ; ", False,
             ["racismo", "preconceito racial", "discriminação"]),
            ("racismo;;; discriminação;", False, ["racismo", "discriminação"]),
            ("racismo; Racismo; RACISMO", False, ["racismo"]),
            ("racismo; Racismo; RACISMO", True, ["racismo", "Racismo", "RACISMO"]),
            ("racismo  estrutural; preconceito racial", False, ["racismo  estrutural", "preconceito racial"]),
        ]
        for query, sensitive, expected in cases:
            with self.subTest(query=query, sensitive=sensitive):
                terms, _ = prepare_automatic_query(query, multiple_terms=True, case_sensitive=sensitive)
                self.assertEqual(terms, expected)

    def test_stable_identity_distinguishes_single_mode_and_preserves_regex_syntax(self):
        terms, key = prepare_automatic_query("Racismo; discriminação", multiple_terms=True)
        self.assertEqual(terms, ["Racismo", "discriminação"])
        self.assertEqual(key, prepare_automatic_query("DISCRIMINAÇÃO; racismo; racismo", multiple_terms=True)[1])
        self.assertNotEqual(key, prepare_automatic_query("Racismo; discriminação", multiple_terms=True, case_sensitive=True)[1])
        self.assertNotEqual(key, prepare_automatic_query(key)[1])  # namespace não colide com consulta simples
        self.assertLessEqual(len(key), 200)
        self.assertEqual(prepare_automatic_query(" A ; B "), (["A ; B"], "A ; B"))
        patterns, source = prepare_automatic_query(r"\D;\d;\D", multiple_terms=True, grep=True)
        self.assertEqual(patterns, [r"\D", r"\d"])
        self.assertEqual(source, prepare_automatic_query(r"\d;\D", multiple_terms=True, grep=True)[1])
        # Expansão Unicode na identidade nunca excede a coluna de proveniência.
        self.assertLessEqual(len(prepare_automatic_query("ß" * 195 + ";a", multiple_terms=True)[1]), 200)

    def test_separator_rule_matches_scraping_only_for_multiple_literals(self):
        from app import _termos_digitados_tem_separador_invalido
        for query in ("racismo, discriminação", "racismo. discriminação",
                      "racismo, preconceito racial", "racismo. preconceito racial",
                      "racismo, discriminação, preconceito racial",
                      "racismo, preconceito racial, discriminação",
                      "racismo. preconceito racial. discriminação",
                      "racialização; racismo, preconceito racial, discriminação"):
            with self.subTest(warning=query), self.assertRaisesRegex(QualitativeSearchError, "ponto e vírgula"):
                prepare_automatic_query(query, multiple_terms=True)
        punctuated = ["Hoje, a pesquisa continua", "Hoje, eu estudo, com atenção.",
                      "Dr. Silva. Uma pesquisa", "Uma frase. Outra frase.",
                      "art. 5. direitos", "raça, classe e gênero", "Dr. Silva",
                      "Pesquisa de campo, com entrevistas", "Ele escreve, depois revisa"]
        for query in punctuated:
            with self.subTest(query=query):
                self.assertTrue(_termos_digitados_tem_separador_invalido(query))
                with self.assertRaisesRegex(QualitativeSearchError, "ponto e vírgula"):
                    prepare_automatic_query(query, multiple_terms=True)
                self.assertEqual(prepare_automatic_query(query)[0], [query])
        self.assertEqual(prepare_automatic_query("raça; classe", multiple_terms=True)[0], ["raça", "classe"])
        for pattern in (r"racis.*;discrimin.{1,3}", r"a,b,c", r"a. b. c", r"\D;\d"):
            with self.subTest(regex=pattern):
                self.assertTrue(prepare_automatic_query(pattern, multiple_terms=True, grep=True)[0])
        with self.assertRaisesRegex(QualitativeSearchError, "ao menos um"):
            prepare_automatic_query("; ; ;", multiple_terms=True)

    def test_two_term_warning_does_not_apply_to_regex_or_single_query(self):
        for query in ("racismo, discriminação", "racismo. discriminação",
                      "racismo, preconceito racial", "racismo. preconceito racial"):
            with self.subTest(query=query):
                self.assertEqual(prepare_automatic_query(query), ([query], query))
                self.assertEqual(prepare_automatic_query(query, multiple_terms=True, grep=True)[0], [query])


class MultipleAutomaticTests(unittest.TestCase):
    setUp = fixtures.QualitativeAutomaticTests.setUp
    tearDown = fixtures.QualitativeAutomaticTests.tearDown
    auto_url = fixtures.QualitativeAutomaticTests.auto_url
    automatic = fixtures.QualitativeAutomaticTests.automatic
    remove = fixtures.QualitativeAutomaticTests.remove
    codings = fixtures.QualitativeAutomaticTests.codings
    fixture_pages = fixtures.QualitativeAutomaticTests.fixture_pages

    def assert_no_writes(self):
        self.assertEqual(self.codings(), [])
        for model in (QualitativeExcerpt, QualitativeCode, QualitativeRejection):
            self.assertEqual(db.session.query(model).count(), 0)

    def test_two_terms_create_own_codes_and_never_cross_associate_matches(self):
        self.fixture_pages([["racismo " * 8 + "raça " * 12]])
        result = self.automatic(q=" racismo; raça; racismo ", multiple_terms=True)
        self.assertEqual(result.status_code, 201)
        self.assertEqual([item["term"] for item in result.json["terms"]], ["racismo", "raça"])
        self.assertEqual([item["total"] for item in result.json["terms"]], [8, 12])
        counts = {code.name: db.session.query(QualitativeCoding).filter_by(code_id=code.id).count()
                  for code in db.session.query(QualitativeCode)}
        self.assertEqual(counts, {"racismo": 8, "raça": 12})
        for coding in self.codings():
            code = db.session.get(QualitativeCode, coding.code_id)
            excerpt = db.session.get(QualitativeExcerpt, coding.excerpt_id)
            self.assertEqual(code.name, excerpt.quoted_text)
            self.assertEqual(code.name, coding.source_query)
        ids = {c.id for c in self.codings()}
        again = self.automatic(q="racismo; raça", multiple_terms=True)
        self.assertEqual(again.json["summary"]["existing"], 20)
        self.assertEqual(again.json["summary"]["created"], 0)
        self.assertEqual({c.id for c in self.codings()}, ids)
        import io
        from openpyxl import load_workbook
        report = f"/analise-qualitativa/bases/{self.analysis.id}/relatorio-codificacao"
        self.assertEqual(self.client.get(report).status_code, 200)
        sheet = load_workbook(io.BytesIO(self.client.get(report + ".xlsx").data)).active
        self.assertEqual(sheet.max_row, 21)
        for row in sheet.iter_rows(min_row=2, values_only=True):
            self.assertEqual(row[0], row[3])

    def test_no_matches_create_no_code_and_existing_name_is_reused(self):
        self.fixture_pages([["racismo preconceito  racial"]])
        empty = self.automatic(q="inexistente")
        self.assertEqual(empty.status_code, 201)
        self.assertEqual(empty.json["search"]["total"], 0)
        self.assert_no_writes()
        code = QualitativeCode(analysis_id=self.analysis.id, name="RACISMO", description="Manter",
                               created_by_user_id=self.user.id)
        db.session.add(code); db.session.commit()
        result = self.automatic(q="racismo; raça; preconceito  racial", multiple_terms=True)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json["terms"][0]["code_id"], code.id)
        self.assertEqual(result.json["terms"][1]["total"], 0)
        self.assertIsNone(result.json["terms"][1]["code_id"])
        self.assertEqual({c.name for c in db.session.query(QualitativeCode)}, {"RACISMO", "preconceito  racial"})
        self.assertEqual(code.description, "Manter")
        db.session.expire_all()
        self.assertEqual({c.source_query for c in self.codings()}, {"racismo", "preconceito  racial"})

    def test_inactive_code_or_long_name_rolls_back_entire_operation(self):
        self.fixture_pages([["raça racismo " + "x" * 161]])
        invalid_name = self.automatic(q="raça;" + "x" * 161, multiple_terms=True)
        self.assertEqual(invalid_name.status_code, 400)
        self.assertIn("160", invalid_name.json["error"])
        self.assert_no_writes()
        code = QualitativeCode(analysis_id=self.analysis.id, name="racismo", active=False,
                               created_by_user_id=self.user.id)
        db.session.add(code); db.session.commit()
        result = self.automatic(q="raça;racismo", multiple_terms=True)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(db.session.query(QualitativeCode).count(), 1)
        self.assertEqual(self.codings(), [])
        self.assertEqual(db.session.query(QualitativeExcerpt).count(), 0)

    def test_rejection_of_one_pattern_does_not_reject_another_pattern_for_same_code(self):
        self.fixture_pages([["racismo"]])
        query = r"racismo;racis\w+"
        self.automatic(q=query, multiple_terms=True, grep=True, contextual_rejection_enabled=True)
        removed = next(c for c in self.codings() if c.source_query == "racismo")
        self.remove(removed)
        again = self.automatic(q=query, multiple_terms=True, grep=True)
        self.assertEqual(again.json["summary"]["rejected"], 1)
        self.assertEqual(again.json["summary"]["existing"], 0)
        self.assertEqual(again.json["summary"]["created"], 1)
        self.assertEqual([c.source_query for c in self.codings()], [r"racis\w+"])

    def test_legacy_global_rejection_is_preserved_for_matching_code_and_operation(self):
        self.fixture_pages([["racismo raça"]])
        query = "racismo;raça"
        self.automatic(q=query, multiple_terms=True, contextual_rejection_enabled=True)
        coding = next(c for c in self.codings() if c.source_query == "racismo")
        coding.source_query = prepare_automatic_query(query, multiple_terms=True)[1]
        db.session.commit()
        self.remove(coding)
        repeated = self.automatic(q=query, multiple_terms=True)
        self.assertEqual(repeated.json["summary"]["rejected"], 1)
        self.assertEqual(repeated.json["summary"]["created"], 0)

    def test_off_keeps_semicolon_as_one_query_and_consultative_search_stays_read_only(self):
        self.fixture_pages([["raça; classe\nraça\nclasse"]])
        result = self.automatic(q="raça; classe")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json["search"]["total"], 1)
        self.assertEqual(self.codings()[0].source_query, "raça; classe")
        before = [c.id for c in self.codings()]
        found = self.client.get(self.auto_url.replace("/codificar", "/buscar"),
                                query_string={"q": "raça; classe", "multiple_terms": "1"})
        self.assertEqual(found.json["total"], 1)
        self.assertEqual([c.id for c in self.codings()], before)

    def test_three_terms_have_own_codes_unicode_phrases_and_stable_reordered_operation(self):
        self.fixture_pages([["Racismo estrutural, preconceito racial e discriminação. 😀"]])
        query = " Racismo estrutural ; preconceito racial ; discriminação ; ; RACISMO ESTRUTURAL"
        result = self.automatic(q=query, multiple_terms=True)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json["summary"]["terms"], 3)
        self.assertEqual(result.json["summary"]["created"], 3)
        self.assertEqual(result.json["search"]["total"], 3)
        self.assertEqual(db.session.query(QualitativeCode).count(), 3)
        self.assertEqual(len({c.code_id for c in self.codings()}), 3)
        source = {c.source_query for c in self.codings()}
        self.assertEqual(source, {"Racismo estrutural", "preconceito racial", "discriminação"})
        for coding in self.codings():
            self.assertEqual(db.session.get(QualitativeCode, coding.code_id).name, coding.source_query)
            self.assertEqual(db.session.get(QualitativeExcerpt, coding.excerpt_id).quoted_text, coding.source_query)
        self.assertEqual({e.quoted_text for e in db.session.query(QualitativeExcerpt)},
                         {"Racismo estrutural", "preconceito racial", "discriminação"})
        ids = {c.id for c in self.codings()}
        again = self.automatic(q="discriminação; racismo estrutural; preconceito racial", multiple_terms=True)
        self.assertEqual(again.json["summary"]["created"], 0)
        self.assertEqual(again.json["summary"]["existing"], 3)
        self.assertEqual({c.id for c in self.codings()}, ids)
        self.assertEqual({c.source_query for c in self.codings()}, source)

    def test_case_sensitive_applies_to_every_term(self):
        self.fixture_pages([["Raça raça RAÇA Classe classe"]])
        sensitive = self.automatic(q="raça;classe", multiple_terms=True, case_sensitive=True)
        insensitive = self.automatic(q="raça;classe", multiple_terms=True)
        self.assertEqual(sensitive.json["search"]["total"], 2)
        self.assertEqual(insensitive.json["search"]["total"], 5)

    def test_overlapping_but_different_intervals_are_not_dropped(self):
        self.fixture_pages([["racismo estrutural"]])
        result = self.automatic(q="racismo; racismo estrutural", multiple_terms=True)
        self.assertEqual(result.json["summary"]["created"], 2)
        self.assertEqual({e.quoted_text for e in db.session.query(QualitativeExcerpt)}, {"racismo", "racismo estrutural"})

    def test_regex_patterns_reuse_same_code_and_coding_for_identical_spans(self):
        self.fixture_pages([["raça RAÇA 7"]])
        result = self.automatic(q=r"raça; raç[a]; raça", multiple_terms=True, grep=True)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json["summary"]["terms"], 2)
        self.assertEqual(result.json["search"]["total"], 4)
        self.assertEqual(result.json["summary"]["created"], 2)
        self.assertEqual(db.session.query(QualitativeExcerpt).count(), 2)
        self.assertEqual(db.session.query(QualitativeCode).count(), 1)
        self.assertEqual(result.json["summary"]["existing"], 2)
        again = self.automatic(q=r"raç[a]; raça", multiple_terms=True, grep=True)
        self.assertEqual(again.json["summary"]["existing"], 4)
        distinct = self.automatic(q=r"\D;\d", multiple_terms=True, grep=True)
        self.assertEqual(distinct.json["summary"]["terms"], 2)
        self.assertEqual(distinct.json["search"]["total"], len("raça RAÇA 7"))

    def test_valid_regex_dot_and_comma_are_not_separator_errors(self):
        self.fixture_pages([["a,b,c racismo 123\nracismo, discriminação\nracismo. preconceito racial"]])
        result = self.automatic(q=r"a,b,c; racis.*?o; \d{1,3}; racismo, discriminação; racismo\. preconceito racial",
                                multiple_terms=True, grep=True)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json["search"]["total"], 7)
        # Dois padrões retornam exatamente "racismo, discriminação": mesmo vínculo.
        self.assertEqual(result.json["summary"]["created"], 6)
        self.assertEqual(result.json["summary"]["existing"], 1)

    def test_invalid_regex_after_successful_term_has_no_partial_persistence(self):
        self.fixture_pages([["raça"]])
        result = self.automatic(q="raça; [", multiple_terms=True, grep=True)
        self.assertEqual(result.status_code, 400)
        self.assertIn("Consulta “[”", result.json["error"])
        self.assert_no_writes()

    def test_timeout_after_successful_term_has_no_partial_persistence(self):
        self.fixture_pages([["raça\n" + "a" * 20000 + "!"]])
        with patch("platform_core.qualitative_search.SEARCH_TIMEOUT_SECONDS", .01):
            result = self.automatic(q="raça; (a+)+$", multiple_terms=True, grep=True)
        self.assertEqual(result.status_code, 400)
        self.assertIn("(a+)+$", result.json["error"])
        self.assertIn("não foi concluída", result.json["error"])
        self.assertNotIn("search", result.json)
        self.assert_no_writes()

    def test_separator_warning_happens_before_any_search_or_write(self):
        for query in ("racismo, discriminação", "racismo. discriminação",
                      "racismo, preconceito racial", "racismo. preconceito racial",
                      "racismo, discriminação, preconceito racial",
                      "racismo, preconceito racial, discriminação", "racismo. preconceito racial. discriminação", "; ; ;"):
            with self.subTest(query=query), patch("platform_core.qualitative_automatic.search_qualitative_document",
                                                 side_effect=AssertionError("Não executar busca")):
                result = self.automatic(q=query, multiple_terms=True)
                self.assertEqual(result.status_code, 400)
                self.assertIn(";", result.json["error"])
                if query != "; ; ;":
                    self.assertEqual(result.json["error"],
                        "Para pesquisar múltiplos termos, separe cada palavra ou expressão com ponto e vírgula (;). "
                        "Vírgulas e pontos não são usados como separadores de múltiplos termos.")
                self.assert_no_writes()

    def test_invalid_multiple_flag_is_rejected(self):
        for invalid in (None, 1, "false"):
            with self.subTest(value=invalid):
                self.assertEqual(self.automatic(multiple_terms=invalid).status_code, 400)
        self.assert_no_writes()

    def test_document_and_project_scopes_exceed_500_without_truncation(self):
        self.fixture_pages([["raça Ação 😀\n" * 125] * 2, ["raça Ação 😀\n" * 70]])
        document = self.automatic(q="raça; Ação 😀", multiple_terms=True)
        self.assertEqual(document.json["search"]["total"], 500)
        self.assertEqual(document.json["summary"]["created"], 500)
        project = self.automatic(q="Ação 😀; raça", multiple_terms=True, scope="project")
        self.assertEqual(project.json["search"]["total"], 640)
        self.assertEqual(project.json["summary"]["created"], 140)
        self.assertEqual(project.json["summary"]["existing"], 500)
        self.assertEqual(len(project.json["search"]["results"]), 640)
        self.assertEqual(len(self.codings()), 640)

    def test_multiple_rejection_off_removal_can_be_recreated(self):
        self.fixture_pages([["raça classe"]])
        self.automatic(q="raça; classe", multiple_terms=True)
        self.remove(self.codings()[0])
        self.assertEqual(db.session.query(QualitativeRejection).count(), 0)
        again = self.automatic(q="classe; raça", multiple_terms=True)
        self.assertEqual(again.json["summary"]["created"], 1)
        self.assertEqual(again.json["summary"]["existing"], 1)

    def test_multiple_rejection_on_survives_order_and_follows_its_term(self):
        self.fixture_pages([["raça classe"]])
        self.automatic(q="raça; classe", multiple_terms=True, contextual_rejection_enabled=True)
        removed = self.codings()[0]
        source = removed.source_query
        self.remove(removed)
        self.assertEqual(db.session.query(QualitativeRejection).one().query, source)
        again = self.automatic(q="CLASSE; RAÇA; raça", multiple_terms=True)
        self.assertEqual(again.json["summary"]["rejected"], 1)
        self.assertEqual(again.json["summary"]["created"], 0)
        other = self.automatic(q="classe; raça; inexistente", multiple_terms=True)
        self.assertEqual(other.json["summary"]["rejected"], 1)
        self.assertEqual(other.json["summary"]["created"], 0)
