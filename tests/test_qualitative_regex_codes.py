"""Nomes vêm do match canônico; a Regex permanece na proveniência."""
import unittest
from unittest.mock import patch

import test_qualitative_automatic as fixtures
from platform_core.extensions import db
from platform_core.models import QualitativeCode, QualitativeCoding, QualitativeExcerpt, QualitativeRejection


class RegexCodeTests(unittest.TestCase):
    setUp = fixtures.QualitativeAutomaticTests.setUp
    tearDown = fixtures.QualitativeAutomaticTests.tearDown
    auto_url = fixtures.QualitativeAutomaticTests.auto_url
    automatic = fixtures.QualitativeAutomaticTests.automatic
    remove = fixtures.QualitativeAutomaticTests.remove
    codings = fixtures.QualitativeAutomaticTests.codings
    fixture_pages = fixtures.QualitativeAutomaticTests.fixture_pages

    def counts(self):
        return {c.name: db.session.query(QualitativeCoding).filter_by(code_id=c.id).count()
                for c in db.session.query(QualitativeCode)}

    def test_alternation_names_and_offsets_come_from_complete_matches_and_are_idempotent(self):
        self.fixture_pages([["raça\nraças\nraça"]])
        response = self.automatic(q="raç(a|as)", grep=True)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.counts(), {"raça": 2, "raças": 1})
        self.assertEqual([m["match_text"] for m in response.json["search"]["results"]], ["raça", "raças", "raça"])
        for coding in self.codings():
            excerpt = db.session.get(QualitativeExcerpt, coding.excerpt_id)
            self.assertEqual(db.session.get(QualitativeCode, coding.code_id).name, excerpt.quoted_text)
            self.assertEqual(coding.source_query, "raç(a|as)")
            self.assertEqual(coding.origin, "automatic_regex")
        ids = {c.id for c in self.codings()}
        repeated = self.automatic(q="raç(a|as)", grep=True)
        self.assertEqual(repeated.json["summary"]["created"], 0)
        self.assertEqual(repeated.json["summary"]["existing"], 3)
        self.assertEqual({c.id for c in self.codings()}, ids)
        self.assertEqual(self.counts(), {"raça": 2, "raças": 1})
        # O endpoint consultivo mantém o matching Regex anterior.
        consultative = self.client.get(self.auto_url.replace("/codificar", "/buscar"),
                                      query_string={"q": "raç(a|as)", "grep": "1"})
        self.assertEqual([m["match_text"] for m in consultative.json["results"]], ["raça"] * 3)

    def test_multiple_patterns_forms_and_repetition_keep_query_to_match_mapping(self):
        self.fixture_pages([["racismo racismos racista racistas racismo discriminação discriminatório"]])
        response = self.automatic(q=r"racis\w+; discrimin\w+", grep=True, multiple_terms=True)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.counts(), {"racismo": 2, "racismos": 1, "racista": 1, "racistas": 1,
                                        "discriminação": 1, "discriminatório": 1})
        for coding in self.codings():
            name = db.session.get(QualitativeCode, coding.code_id).name
            self.assertEqual(coding.source_query, r"racis\w+" if name.startswith("racis") else r"discrimin\w+")
        repeated = self.automatic(q=r"discrimin\w+;racis\w+", grep=True, multiple_terms=True)
        self.assertEqual(repeated.json["summary"]["created"], 0)
        self.assertEqual(repeated.json["summary"]["existing"], 7)

    def test_rejection_is_specific_to_occurrence_code_pattern_and_case(self):
        self.fixture_pages([["raça raças raça"]])
        self.automatic(q="raç(a|as)", grep=True, contextual_rejection_enabled=True)
        coding = next(c for c in self.codings() if db.session.get(QualitativeCode, c.code_id).name == "raça")
        self.remove(coding)
        rejection = db.session.query(QualitativeRejection).one()
        self.assertEqual(rejection.query, "raç(a|as)")
        repeated = self.automatic(q="raç(a|as)", grep=True, contextual_rejection_enabled=True)
        self.assertEqual(repeated.json["summary"]["created"], 0)
        self.assertEqual(repeated.json["summary"]["rejected"], 1)
        self.assertEqual(repeated.json["summary"]["existing"], 2)
        self.assertEqual(self.counts(), {"raça": 1, "raças": 1})
        changed_query = self.automatic(q="raças?", grep=True)
        self.assertEqual(changed_query.json["summary"]["created"], 1)

    def test_existing_normalized_name_and_search_case_are_preserved(self):
        self.fixture_pages([["Racismo racismo RACISMO"]])
        code = QualitativeCode(analysis_id=self.analysis.id, name="RACISMO", description="Preservar",
                               created_by_user_id=self.user.id, active=True)
        db.session.add(code); db.session.commit()
        sensitive = self.automatic(q=r"racis\w+", grep=True, case_sensitive=True)
        self.assertEqual(sensitive.json["summary"]["created"], 1)
        insensitive = self.automatic(q=r"racis\w+", grep=True)
        self.assertEqual(insensitive.json["summary"]["created"], 2)
        self.assertEqual(self.counts(), {"RACISMO": 3})
        self.assertEqual(code.description, "Preservar")
        self.assertEqual({c.code_id for c in self.codings()}, {code.id})

    def test_no_match_and_invalid_pattern_timeout_or_long_name_never_leave_partial_writes(self):
        self.fixture_pages([["raça " + "x" * 161]])
        self.assertEqual(self.automatic(q="inexistente.*", grep=True).json["summary"]["created"], 0)
        for query in ("raça;[", "raça;x+"):
            with self.subTest(query=query):
                response = self.automatic(q=query, grep=True, multiple_terms=True)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.counts(), {})
                self.assertEqual(db.session.query(QualitativeExcerpt).count(), 0)
                self.assertEqual(self.codings(), [])
        with patch("platform_core.qualitative_search.SEARCH_TIMEOUT_SECONDS", 0):
            response = self.automatic(q="raça", grep=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.counts(), {})
        self.assertEqual(self.codings(), [])
