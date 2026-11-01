from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import socket
import tempfile
import unittest

from sqlalchemy import event, func, select

from app import create_app
from platform_core.extensions import db
from platform_core.models import (
    AnalyticsProject,
    OjsArticle,
    OjsArticleObservation,
    OjsJournal,
    OjsSeoAuditRun,
    User,
)
from platform_core.ojs_oai_client import (
    HttpResponse,
    OaiCollectionResult,
    OaiDeletedRecord,
    OaiIssue,
    OaiPmhClient,
    OaiRecord,
)
from platform_core.ojs_seo_persistence import (
    OjsPersistenceConflictError,
    OjsPersistenceValidationError,
    persist_oai_collection,
)
from platform_core.services import seed_platform


UTC = timezone.utc
TEST_TEMP_ROOT = r"C:\Temp"
FIXTURES = Path(__file__).parent / "fixtures" / "ojs_oai"


def public_resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


class _Transport:
    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, _endpoint, _params, _config):
        return self.responses.pop(0)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def active(
    identifier: str,
    *,
    title: tuple[str, ...] = ("Título inicial",),
    creators: tuple[str, ...] = ("Ana",),
    subjects: tuple[str, ...] = ("OJS",),
    description: tuple[str, ...] = ("Resumo",),
    identifiers: tuple[str, ...] = ("10.1000/inicial",),
    dates: tuple[str, ...] = ("2026-10-01",),
    languages: tuple[str, ...] = ("pt-BR",),
    types: tuple[str, ...] = ("Article",),
    dois: tuple[str, ...] = ("10.1000/inicial",),
    datestamp: str | None = "2026-10-01",
) -> OaiRecord:
    return OaiRecord(
        identifier=identifier,
        datestamp=datestamp,
        set_specs=("articles",),
        metadata_present=True,
        fields={
            "title": title,
            "creator": creators,
            "subject": subjects,
            "description": description,
            "identifier": identifiers,
            "date": dates,
            "language": languages,
            "type": types,
        },
        dois=dois,
    )


def deleted(identifier: str, *, datestamp: str | None = "2026-10-02") -> OaiDeletedRecord:
    return OaiDeletedRecord(identifier=identifier, datestamp=datestamp, set_specs=("articles",))


def collection(
    *,
    status: str = "completed",
    records: tuple[OaiRecord, ...] = (),
    deleted_records: tuple[OaiDeletedRecord, ...] = (),
    duplicates: tuple[str, ...] = (),
    reason: str | None = None,
    warnings: tuple[OaiIssue, ...] = (),
    errors: tuple[OaiIssue, ...] = (),
) -> OaiCollectionResult:
    identifiers = tuple(record.identifier for record in (*records, *deleted_records))
    return OaiCollectionResult(
        status=status,
        completion_reason=reason,
        pages_fetched=1 if status != "failed" else 0,
        total_response_bytes=512 if status != "failed" else 0,
        records=records,
        deleted_records=deleted_records,
        unique_identifiers=tuple(dict.fromkeys(identifiers)),
        duplicate_identifiers=duplicates,
        warnings=warnings,
        errors=errors,
        elapsed_seconds=0.25,
    )


class OjsSeoCollectionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.scope = tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-seo-collection-", dir=TEST_TEMP_ROOT)
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "ojs-seo-collection-test-only",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "PLATFORM_DATA_DIR": self.scope.name,
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        seed_platform()
        self.user = self.create_user("Pesquisadora", "ojs-persist@example.org")
        self.other_user = self.create_user("Outra pesquisadora", "ojs-persist-other@example.org")

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.scope.cleanup()

    @staticmethod
    def moment(day: int) -> datetime:
        return datetime(2026, 10, day, 12, tzinfo=UTC)

    def create_user(self, name: str, email: str) -> User:
        user = User(name=name, email=email, role="user", status="active")
        user.set_password("senha-de-teste-segura-123")
        db.session.add(user)
        db.session.commit()
        return user

    def create_journal(self, owner: User | None = None, *, endpoint: str = "https://journal.example.test/oai") -> OjsJournal:
        owner = owner or self.user
        project = AnalyticsProject(
            owner_user_id=owner.id,
            module_key="ojs_seo",
            name=f"Projeto OJS {owner.id[:8]}",
            description="",
        )
        journal = OjsJournal(
            analytics_project=project,
            public_url="https://journal.example.test/index",
            oai_endpoint_url=endpoint,
            discovery_status="identified",
        )
        db.session.add(journal)
        db.session.commit()
        return journal

    def create_run(self, journal: OjsJournal) -> OjsSeoAuditRun:
        run = OjsSeoAuditRun(
            journal_id=journal.id,
            status="pending",
            processor_version="ojs-oai-v1",
            parameters_json={"metadata_prefix": "oai_dc"},
        )
        db.session.add(run)
        db.session.commit()
        return run

    def persist(self, journal: OjsJournal, run: OjsSeoAuditRun, result: OaiCollectionResult, *, day: int):
        summary = persist_oai_collection(db.session, journal, run, result, observed_at=self.moment(day))
        db.session.commit()
        return summary

    def article(self, journal: OjsJournal, identifier: str) -> OjsArticle:
        return db.session.scalar(select(OjsArticle).where(
            OjsArticle.journal_id == journal.id,
            OjsArticle.oai_identifier == identifier,
        ))

    def test_first_collection_persists_article_observation_and_indicators(self):
        journal = self.create_journal()
        run = self.create_run(journal)
        result = collection(
            records=(active("oai:test:one"), active("oai:test:two", description=(), dois=(), identifiers=())),
            warnings=(OaiIssue("partial_description", "Campo opcional ausente."),),
        )

        summary = self.persist(journal, run, result, day=2)
        article = self.article(journal, "oai:test:one")
        stored_run = db.session.get(OjsSeoAuditRun, run.id)
        observations = db.session.scalars(select(OjsArticleObservation).where(
            OjsArticleObservation.audit_run_id == run.id
        )).all()

        self.assertEqual((2, 2, 2), (summary.article_count_created, summary.observation_count_created, summary.observed_identifier_count))
        self.assertEqual("active", article.record_status)
        self.assertEqual(["Ana"], article.creators_json)
        self.assertEqual("completed", stored_run.status)
        self.assertEqual(2, stored_run.record_count)
        self.assertEqual(1, stored_run.doi_count)
        self.assertEqual({"title": 2, "description": 1}, {
            key: stored_run.metrics_json["coverage"][key] for key in ("title", "description")
        })
        self.assertEqual([{ "code": "partial_description", "message": "Campo opcional ausente."}], stored_run.warnings_json)
        self.assertEqual(2, len(observations))

    def test_two_audits_preserve_history_and_newer_metadata_becomes_current(self):
        journal = self.create_journal()
        first = self.create_run(journal)
        second = self.create_run(journal)
        identifier = "oai:test:history"
        self.persist(journal, first, collection(records=(active(identifier, title=("Título antigo",)),)), day=2)
        self.persist(journal, second, collection(records=(active(
            identifier,
            title=("Título novo",),
            creators=("Ana", "Bruno"),
            subjects=("OJS", "SEO"),
            dois=("10.1000/novo",),
            identifiers=("10.1000/novo",),
        ),)), day=9)

        article = self.article(journal, identifier)
        history = db.session.scalars(select(OjsArticleObservation).where(
            OjsArticleObservation.article_id == article.id
        ).order_by(OjsArticleObservation.observed_at)).all()
        self.assertEqual("Título novo", article.title)
        self.assertEqual(["Ana", "Bruno"], article.creators_json)
        self.assertEqual(["OJS", "SEO"], article.subjects_json)
        self.assertEqual("10.1000/novo", article.doi)
        self.assertEqual(["Título antigo"], history[0].dublin_core_json["title"])
        self.assertEqual(["Título novo"], history[1].dublin_core_json["title"])

    def test_removal_of_doi_tombstone_and_reappearance_are_historical(self):
        journal = self.create_journal()
        identifier = "oai:test:reappears"
        first, tombstone, reappeared = (self.create_run(journal) for _ in range(3))
        self.persist(journal, first, collection(records=(active(identifier),)), day=2)
        self.persist(journal, tombstone, collection(deleted_records=(deleted(identifier),)), day=4)
        article = self.article(journal, identifier)
        self.assertEqual(("deleted", None, []), (article.record_status, article.doi, article.dois_json))
        self.persist(journal, reappeared, collection(records=(active(
            identifier, dois=(), identifiers=("https://journal.example.test/article/1",)
        ),)), day=8)

        article = self.article(journal, identifier)
        history = db.session.scalars(select(OjsArticleObservation).where(
            OjsArticleObservation.article_id == article.id
        ).order_by(OjsArticleObservation.observed_at)).all()
        self.assertEqual(("active", None, []), (article.record_status, article.doi, article.dois_json))
        self.assertEqual(["active", "deleted", "active"], [item.record_status for item in history])

    def test_partial_and_complete_absences_never_mark_prior_articles_deleted(self):
        journal = self.create_journal()
        first, partial, complete = (self.create_run(journal) for _ in range(3))
        one, two = "oai:test:one", "oai:test:two"
        self.persist(journal, first, collection(records=(active(one), active(two))), day=2)
        self.persist(journal, partial, collection(status="partial", records=(active(one),), reason="page_limit_reached"), day=4)
        self.persist(journal, complete, collection(records=(active(one, title=("Título confirmado",)),)), day=8)

        two_article = self.article(journal, two)
        partial_observed = db.session.scalar(select(func.count()).select_from(OjsArticleObservation).where(
            OjsArticleObservation.article_id == two_article.id,
            OjsArticleObservation.audit_run_id == partial.id,
        ))
        complete_observed = db.session.scalar(select(func.count()).select_from(OjsArticleObservation).where(
            OjsArticleObservation.article_id == two_article.id,
            OjsArticleObservation.audit_run_id == complete.id,
        ))
        self.assertEqual("active", two_article.record_status)
        self.assertEqual((0, 0), (partial_observed, complete_observed))
        self.assertEqual("partial", db.session.get(OjsSeoAuditRun, partial.id).status)

    def test_duplicate_input_is_collapsed_and_identifiers_are_scoped_to_each_journal(self):
        journal = self.create_journal()
        same_record = active("oai:test:duplicate")
        run = self.create_run(journal)
        summary = self.persist(journal, run, collection(
            records=(same_record, same_record), duplicates=(same_record.identifier,)
        ), day=2)
        self.assertEqual((1, 1), (summary.observed_identifier_count, summary.observation_count_created))
        self.assertEqual(1, db.session.get(OjsSeoAuditRun, run.id).duplicate_identifier_count)

        other_journal = self.create_journal(self.other_user)
        other_run = self.create_run(other_journal)
        self.persist(other_journal, other_run, collection(records=(active(same_record.identifier),)), day=3)
        self.assertEqual(2, db.session.scalar(select(func.count()).select_from(OjsArticle).where(
            OjsArticle.oai_identifier == same_record.identifier
        )))

    def test_repeated_same_run_is_idempotent_and_divergent_reuse_is_rejected(self):
        journal = self.create_journal()
        run = self.create_run(journal)
        initial = collection(records=(active("oai:test:idempotent"),))
        first = self.persist(journal, run, initial, day=2)
        second = persist_oai_collection(db.session, journal, run, initial, observed_at=self.moment(2))
        db.session.commit()
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(OjsArticleObservation)))
        with self.assertRaises(OjsPersistenceConflictError):
            persist_oai_collection(db.session, journal, run, collection(
                records=(active("oai:test:idempotent", title=("Resultado divergente",)),)
            ), observed_at=self.moment(2))

    def test_failed_collection_persists_terminal_diagnostics_without_articles(self):
        journal = self.create_journal()
        run = self.create_run(journal)
        failed = collection(
            status="failed",
            reason="http_429",
            errors=(OaiIssue("http_429", "O servidor recusou temporariamente a coleta."),),
        )
        self.persist(journal, run, failed, day=2)
        stored_run = db.session.get(OjsSeoAuditRun, run.id)
        self.assertEqual(("failed", 0, 0), (stored_run.status, stored_run.record_count, stored_run.page_count))
        self.assertEqual("http_429", stored_run.errors_json[0]["code"])
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsArticle)))

    def test_older_audit_is_recorded_without_overwriting_newer_current_state(self):
        journal = self.create_journal()
        new_run = self.create_run(journal)
        old_run = self.create_run(journal)
        identifier = "oai:test:chronology"
        self.persist(journal, new_run, collection(records=(active(identifier, title=("Mais recente",)),)), day=9)
        self.persist(journal, old_run, collection(records=(active(identifier, title=("Mais antigo",)),)), day=2)
        article = self.article(journal, identifier)
        history_count = db.session.scalar(select(func.count()).select_from(OjsArticleObservation).where(
            OjsArticleObservation.article_id == article.id
        ))
        self.assertEqual("Mais recente", article.title)
        self.assertEqual(2, history_count)
        self.assertEqual(self.moment(2), article.first_observed_at.replace(tzinfo=UTC))
        self.assertEqual(self.moment(9), article.last_observed_at.replace(tzinfo=UTC))

    def test_scope_errors_and_write_failure_roll_back_the_entire_collection(self):
        journal = self.create_journal()
        other_journal = self.create_journal(self.other_user)
        run = self.create_run(journal)
        with self.assertRaises(OjsPersistenceValidationError):
            persist_oai_collection(db.session, other_journal, run, collection(records=(active("oai:test:wrong"),)))

        session = db.session()

        def fail_when_flushing_new_rows(_session, _flush_context, _instances):
            if _session.new:
                raise RuntimeError("simulated persistence failure")

        event.listen(session, "before_flush", fail_when_flushing_new_rows)
        try:
            with self.assertRaises(RuntimeError):
                persist_oai_collection(session, journal, run, collection(records=(active("oai:test:rollback"),)))
        finally:
            event.remove(session, "before_flush", fail_when_flushing_new_rows)
        self.assertIsNone(session.get(OjsSeoAuditRun, run.id).started_at)
        session.rollback()
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsArticle)))
        self.assertEqual("pending", db.session.get(OjsSeoAuditRun, run.id).status)

    def test_persists_the_direct_normalized_result_from_the_oai_client(self):
        client = OaiPmhClient(
            "https://journal.example.test/oai",
            resolver=public_resolver,
            transport=_Transport([
                HttpResponse(200, {}, _fixture("list_records_page_1.xml")),
                HttpResponse(200, {}, _fixture("list_records_page_2.xml")),
            ]),
        )
        result = client.collect_oai_dc()
        journal = self.create_journal()
        run = self.create_run(journal)
        self.persist(journal, run, result, day=2)
        article = self.article(journal, "oai:test:article-1")
        tombstone = self.article(journal, "oai:test:deleted-1")
        self.assertEqual("10.1000/abc.1", article.doi)
        self.assertEqual("deleted", tombstone.record_status)
        self.assertEqual((3, 1), (
            db.session.get(OjsSeoAuditRun, run.id).record_count,
            db.session.get(OjsSeoAuditRun, run.id).deleted_record_count,
        ))


if __name__ == "__main__":
    unittest.main()
