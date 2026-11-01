"""Persistência isolada do domínio OJS SEO, sem HTTP nem rotas Flask."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

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
from platform_core.ojs_oai_client import OaiRecord
from platform_core.services import replace_grant, seed_platform


UTC = timezone.utc
PREVIOUS_REVISION = "1b2c3d4e5f60"
REVISION = "2c3d4e5f6071"
DURABLE_WORKER_REVISION = "3d4e5f607182"
ALLOCATION_REVISION = "4e5f60718293"
CURRENT_OJS_HEAD = "91a2b3c4d5e6"
MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")
OJS_TABLES = {
    "ojs_journals",
    "ojs_seo_audit_runs",
    "ojs_articles",
    "ojs_article_observations",
}
TEST_TEMP_ROOT = r"C:\Temp"


@contextmanager
def isolated_ojs_platform():
    """Banco e armazenamento de teste em NTFS, fora do OneDrive."""
    with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-seo-", dir=TEST_TEMP_ROOT) as data_dir:
        app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "PLATFORM_DATA_DIR": data_dir,
            "SECRET_KEY": "ojs-seo-test-only-secret-not-for-production",
        })
        with app.app_context():
            db.create_all()
            seed_platform()
            yield app
            db.session.remove()
            db.drop_all()


def create_test_user(name: str, email: str) -> User:
    user = User(name=name, email=email, role="user", status="active")
    user.set_password("senha-de-teste-segura-123")
    db.session.add(user)
    db.session.flush()
    replace_grant(user, "research_quali", "admin_courtesy", "active")
    db.session.commit()
    return user


class OjsSeoPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_ojs_platform()
        self.app = self.scope.__enter__()
        self.user = create_test_user(name="Pesquisadora OJS", email="ojs@example.org")
        self.other_user = create_test_user(name="Outra pesquisadora", email="outra-ojs@example.org")

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    @staticmethod
    def moment(day: int) -> datetime:
        return datetime(2026, 10, day, 12, tzinfo=UTC)

    def create_journal(self, owner: User | None = None, *, endpoint: str = "https://journal.example.test/oai") -> OjsJournal:
        owner = owner or self.user
        project = AnalyticsProject(
            owner_user_id=owner.id,
            module_key="ojs_seo",
            name=f"Projeto OJS {owner.id[:8]}",
            description="Periódico de teste",
        )
        journal = OjsJournal(
            analytics_project=project,
            public_url="https://journal.example.test/index",
            oai_endpoint_url=endpoint,
            oai_endpoint_validated_at=self.moment(1),
            repository_name="Revista de teste",
            observed_ojs_version="3.5.0.5",
            version_evidence="Identify e evidência pública",
            last_identified_at=self.moment(1),
            discovery_status="identified",
        )
        db.session.add(journal)
        db.session.commit()
        return journal

    def create_run(self, journal: OjsJournal, *, status: str = "completed", day: int = 2) -> OjsSeoAuditRun:
        moment = self.moment(day)
        is_running = status == "running"
        run = OjsSeoAuditRun(
            journal_id=journal.id,
            status=status,
            started_at=moment,
            completed_at=None if is_running else moment,
            claim_token="claim-test-token" if is_running else None,
            claimed_at=moment if is_running else None,
            lease_expires_at=self.moment(day + 1) if is_running else None,
            processor_version="ojs-oai-v1",
            parameters_json={"metadata_prefix": "oai_dc"},
            metrics_json={"active_records": 1},
            warnings_json=[],
            errors_json=[],
            page_count=1,
            record_count=1,
            doi_count=1,
            response_bytes=1024,
        )
        db.session.add(run)
        db.session.commit()
        return run

    def create_article(self, journal: OjsJournal, *, identifier: str = "oai:test:article-1") -> OjsArticle:
        article = OjsArticle(
            journal_id=journal.id,
            oai_identifier=identifier,
            record_status="active",
            title="Título normalizado",
            titles_json=["Título normalizado", "Normalized title"],
            creators_json=["Ana", "Bruno"],
            subjects_json=["SEO", "OJS"],
            descriptions_json=["Resumo em português", "Abstract"],
            identifiers_json=["10.1000/example", "https://journal.example.test/article/1"],
            dates_json=["2026-10-01"],
            languages_json=["pt-BR", "en"],
            types_json=["Article"],
            doi="10.1000/example",
            dois_json=["10.1000/example"],
            oai_datestamp="2026-10-01",
            first_observed_at=self.moment(2),
            last_observed_at=self.moment(2),
            metadata_hash="a" * 64,
        )
        db.session.add(article)
        db.session.commit()
        return article

    def rejects(self, item) -> None:
        db.session.add(item)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_journal_is_one_to_one_with_analytics_project_and_endpoint_is_not_global(self):
        first = self.create_journal()
        self.assertEqual("ojs_seo", first.analytics_project.module_key)
        self.assertEqual(first.id, first.analytics_project.ojs_journal.id)
        self.assertEqual("identified", first.discovery_status)

        self.rejects(OjsJournal(
            analytics_project_id=first.analytics_project_id,
            public_url="https://another.example.test/index",
            discovery_status="pending",
        ))

        second = self.create_journal(self.other_user, endpoint=first.oai_endpoint_url)
        self.assertEqual(first.oai_endpoint_url, second.oai_endpoint_url)
        self.assertNotEqual(first.analytics_project.owner_user_id, second.analytics_project.owner_user_id)

    def test_article_identity_is_unique_per_journal_and_preserves_multivalue_dc(self):
        first = self.create_journal()
        first_article = self.create_article(first)
        self.assertEqual(["Ana", "Bruno"], first_article.creators_json)
        self.assertEqual(["Título normalizado", "Normalized title"], first_article.titles_json)
        self.assertEqual(["SEO", "OJS"], first_article.subjects_json)
        self.assertEqual("10.1000/example", first_article.doi)

        self.rejects(OjsArticle(journal_id=first.id, oai_identifier=first_article.oai_identifier))

        second = self.create_journal(self.other_user)
        second_article = self.create_article(second, identifier=first_article.oai_identifier)
        self.assertEqual(first_article.oai_identifier, second_article.oai_identifier)
        self.assertNotEqual(first_article.journal_id, second_article.journal_id)

    def test_deleted_records_and_client_normalized_metadata_are_persistable(self):
        journal = self.create_journal()
        normalized = OaiRecord(
            identifier="oai:test:from-client",
            datestamp="2026-10-03",
            set_specs=("articles",),
            metadata_present=True,
            fields={
                "title": ("Título do cliente",), "creator": ("Autora", "Coautora"),
                "subject": ("OJS",), "description": ("Resumo",),
                "identifier": ("10.5555/from-client",), "date": ("2026-10-03",),
                "language": ("pt-BR",), "type": ("Article",),
            },
            dois=("10.5555/from-client",),
        )
        article = OjsArticle(
            journal_id=journal.id,
            oai_identifier=normalized.identifier,
            record_status="active",
            title=normalized.fields["title"][0],
            titles_json=list(normalized.fields["title"]),
            creators_json=list(normalized.fields["creator"]),
            subjects_json=list(normalized.fields["subject"]),
            descriptions_json=list(normalized.fields["description"]),
            identifiers_json=list(normalized.fields["identifier"]),
            dates_json=list(normalized.fields["date"]),
            languages_json=list(normalized.fields["language"]),
            types_json=list(normalized.fields["type"]),
            doi=normalized.dois[0],
            dois_json=list(normalized.dois),
            oai_datestamp=normalized.datestamp,
            metadata_hash="b" * 64,
        )
        tombstone = OjsArticle(
            journal_id=journal.id,
            oai_identifier="oai:test:deleted",
            record_status="deleted",
            identifiers_json=[],
            titles_json=[], creators_json=[], subjects_json=[], descriptions_json=[], dates_json=[],
            languages_json=[], types_json=[], dois_json=[],
        )
        db.session.add_all((article, tombstone))
        db.session.commit()
        self.assertEqual(["Autora", "Coautora"], article.creators_json)
        self.assertEqual("deleted", tombstone.record_status)
        self.assertIsNone(tombstone.title)

    def test_audit_history_observations_and_same_journal_integrity(self):
        journal = self.create_journal()
        article = self.create_article(journal)
        first_run = self.create_run(journal, day=2)
        second_run = self.create_run(journal, status="partial", day=9)
        first_observation = OjsArticleObservation(
            journal_id=journal.id,
            article_id=article.id,
            audit_run_id=first_run.id,
            record_status="active",
            dublin_core_json={"title": ["Título normalizado"]},
            doi_observed="10.1000/example",
            dois_observed_json=["10.1000/example"],
            oai_datestamp="2026-10-02",
            metadata_hash="c" * 64,
            observed_at=self.moment(2),
        )
        second_observation = OjsArticleObservation(
            journal_id=journal.id,
            article_id=article.id,
            audit_run_id=second_run.id,
            record_status="deleted",
            dublin_core_json={},
            dois_observed_json=[],
            oai_datestamp="2026-10-09",
            metadata_hash="d" * 64,
            observed_at=self.moment(9),
        )
        db.session.add_all((first_observation, second_observation))
        db.session.commit()
        self.assertEqual(2, len(article.observations))
        self.assertEqual("partial", second_run.status)

        self.rejects(OjsArticleObservation(
            journal_id=journal.id, article_id=article.id, audit_run_id=first_run.id,
            record_status="active", dublin_core_json={}, dois_observed_json=[], observed_at=self.moment(3),
        ))

        other = self.create_journal(self.other_user)
        self.rejects(OjsArticleObservation(
            journal_id=other.id, article_id=article.id, audit_run_id=first_run.id,
            record_status="active", dublin_core_json={}, dois_observed_json=[], observed_at=self.moment(3),
        ))

    def test_run_states_and_foreign_keys_are_enforced(self):
        journal = self.create_journal()
        run = self.create_run(journal, status="running")
        self.assertEqual("running", db.session.get(OjsSeoAuditRun, run.id).status)
        self.rejects(OjsSeoAuditRun(journal_id=journal.id, status="unknown"))
        self.rejects(OjsSeoAuditRun(journal_id="missing-journal", status="pending"))
        self.assertEqual([], db.session.execute(text("PRAGMA foreign_key_check")).all())


class OjsSeoMigrationTests(unittest.TestCase):
    def _app(self, root: str):
        database = Path(root) / "ojs-seo-migration.sqlite3"
        return create_app({
            "TESTING": True,
            "SECRET_KEY": "ojs-seo-migration-test-only",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "PLATFORM_DATA_DIR": root,
        })

    def test_upgrade_and_downgrade_are_restricted_to_ojs_schema(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-seo-migration-", dir=TEST_TEMP_ROOT) as root:
            app = self._app(root)
            with app.app_context():
                upgrade(directory=MIGRATIONS, revision=PREVIOUS_REVISION)
                user = User(name="Migração OJS", email="migration-ojs@example.org", role="user", status="active")
                user.set_password("senha-de-teste-segura-123")
                db.session.add(user)
                db.session.flush()
                project = AnalyticsProject(
                    owner_user_id=user.id, module_key="instagram", name="Instagram preservado", description=""
                )
                db.session.add(project)
                db.session.commit()
                project_id = project.id
                before_columns = {
                    table: tuple(column["name"] for column in inspect(db.engine).get_columns(table))
                    for table in ("analytics_projects", "commercial_trials", "instagram_accounts")
                }

                try:
                    upgrade(directory=MIGRATIONS, revision=REVISION)
                    db.session.remove()
                    inspector = inspect(db.engine)
                    self.assertTrue(OJS_TABLES.issubset(set(inspector.get_table_names())))
                    after_columns = {
                        table: tuple(column["name"] for column in inspector.get_columns(table))
                        for table in before_columns
                    }
                    self.assertEqual(before_columns, after_columns)
                    self.assertIsNotNone(db.session.get(AnalyticsProject, project_id))
                    with db.engine.connect() as connection:
                        self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                        self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                        self.assertEqual(
                            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                            REVISION,
                        )

                    downgrade(directory=MIGRATIONS, revision=PREVIOUS_REVISION)
                    db.session.remove()
                    self.assertFalse(OJS_TABLES & set(inspect(db.engine).get_table_names()))
                    self.assertIsNotNone(db.session.get(AnalyticsProject, project_id))
                    with db.engine.connect() as connection:
                        self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                        self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])

                    config = Config()
                    config.set_main_option("script_location", MIGRATIONS)
                    self.assertEqual(ScriptDirectory.from_config(config).get_heads(), [CURRENT_OJS_HEAD])
                finally:
                    db.session.remove()
                    db.engine.dispose()

    def test_durable_claim_migration_upgrades_legacy_running_without_touching_other_domains(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-worker-migration-", dir=TEST_TEMP_ROOT) as root:
            app = self._app(root)
            with app.app_context():
                upgrade(directory=MIGRATIONS, revision=REVISION)
                user = User(name="Worker OJS", email="worker-ojs-migration@example.org", role="user", status="active")
                user.set_password("senha-de-teste-segura-123")
                db.session.add(user)
                db.session.commit()
                project = AnalyticsProject(
                    owner_user_id=user.id, module_key="ojs_seo", name="Periódico Worker", description=""
                )
                journal = OjsJournal(
                    analytics_project=project,
                    public_url="https://journal.example.test/index",
                    oai_endpoint_url="https://journal.example.test/oai",
                    discovery_status="pending",
                )
                db.session.add_all((project, journal))
                db.session.commit()
                # O modelo atual já contém as colunas da revisão sucessora;
                # inserimos deliberadamente com SQL da revisão 2c para simular
                # uma execução ``running`` que existia antes dos claims.
                run_id = str(uuid4())
                db.session.execute(text(
                    "INSERT INTO ojs_seo_audit_runs "
                    "(id, journal_id, status, created_at, processor_version, parameters_json, "
                    "metrics_json, warnings_json, errors_json) "
                    "VALUES (:id, :journal_id, 'running', :created_at, 'ojs-oai-v1', "
                    "'{}', '{}', '[]', '[]')"
                ), {
                    "id": run_id,
                    "journal_id": journal.id,
                    "created_at": datetime(2026, 10, 1, 12, tzinfo=UTC),
                })
                db.session.commit()
                before_columns = {
                    table: tuple(column["name"] for column in inspect(db.engine).get_columns(table))
                    for table in ("analytics_projects", "commercial_trials", "instagram_accounts")
                }

                try:
                    upgrade(directory=MIGRATIONS, revision=DURABLE_WORKER_REVISION)
                    db.session.remove()
                    inspector = inspect(db.engine)
                    run_columns = {column["name"] for column in inspector.get_columns("ojs_seo_audit_runs")}
                    self.assertTrue({"claim_token", "claimed_at", "lease_expires_at", "attempt_count"}.issubset(run_columns))
                    self.assertEqual(before_columns, {
                        table: tuple(column["name"] for column in inspector.get_columns(table))
                        for table in before_columns
                    })
                    migrated = db.session.get(OjsSeoAuditRun, run_id)
                    self.assertEqual(("failed", "worker_claim_schema_upgrade"), (
                        migrated.status, migrated.completion_reason,
                    ))
                    index_names = {item["name"] for item in inspector.get_indexes("ojs_seo_audit_runs")}
                    self.assertIn("uq_ojs_audit_runs_one_running_journal", index_names)
                    with db.engine.connect() as connection:
                        self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                        self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                        self.assertEqual(
                            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                            DURABLE_WORKER_REVISION,
                        )

                    downgrade(directory=MIGRATIONS, revision=REVISION)
                    db.session.remove()
                    downgraded_columns = {column["name"] for column in inspect(db.engine).get_columns("ojs_seo_audit_runs")}
                    self.assertFalse({"claim_token", "claimed_at", "lease_expires_at", "attempt_count"} & downgraded_columns)
                    with db.engine.connect() as connection:
                        self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                        self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                finally:
                    db.session.remove()
                    db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
