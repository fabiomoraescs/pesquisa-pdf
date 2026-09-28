"""Regressão SQLite com biblioteca já referenciada, sem tocar volumes reais."""
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask_migrate import downgrade, upgrade

from app import create_app
from platform_core.extensions import db
from platform_core.models import Project, ProjectLibrary, User, VocabularyLibrary


PREVIOUS = "c73a91e24f08"
REVISION = "d84b02f35a19"
INDEX = "ix_vocabulary_libraries_owner_user_id"
MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


class ReferencedPrivateLibraryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="private-library-migration-")
        self.addCleanup(self.directory.cleanup)
        app = create_app({
            "TESTING": True, "SECRET_KEY": "migration-test-only",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{Path(self.directory.name).as_posix()}/db.sqlite",
            "PLATFORM_DATA_DIR": self.directory.name,
        })
        context = app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(lambda: db.engine.dispose())
        self.addCleanup(db.session.remove)
        # Parte diretamente do schema anterior; não passa pelo upgrade novo para semear.
        upgrade(directory=MIGRATIONS, revision=PREVIOUS)
        with db.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            connection.execute(User.__table__.insert().values(
                id="owner", name="Pesquisador", email="migration@example.org", password_hash="test-only"))
            connection.execute(Project.__table__.insert().values(
                id="project", owner_user_id="owner", name="Projeto existente"))
            connection.execute(VocabularyLibrary.__table__.insert().values(
                id="global", name="Biblioteca existente", snapshot_json={"terms": ["Escola"]},
                content_hash="a" * 64, counts_json={"terms": 1}))
            connection.execute(ProjectLibrary.__table__.insert().values(
                project_id="project", library_id="global", source_hash="a" * 64))
        self.before = self.snapshot()
        self.statements = []

        def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            self.statements.append(statement)

        sa.event.listen(db.engine, "before_cursor_execute", record_sql)
        self.addCleanup(lambda: sa.event.remove(db.engine, "before_cursor_execute", record_sql))

    def snapshot(self):
        with db.engine.connect() as connection:
            return {table: connection.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id").all()
                    for table in ("users", "projects")} | {
                "libraries": connection.exec_driver_sql(
                    "SELECT id, name, description, active, status, version, snapshot_json, "
                    "content_hash, counts_json, created_at, updated_at FROM vocabulary_libraries ORDER BY id").all(),
                "links": connection.exec_driver_sql("SELECT * FROM project_libraries").all(),
            }

    def assert_integrity(self):
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
        self.assertEqual(self.snapshot(), self.before)

    def assert_upgraded(self):
        inspector = sa.inspect(db.engine)
        column = next(c for c in inspector.get_columns("vocabulary_libraries") if c["name"] == "owner_user_id")
        self.assertTrue(column["nullable"])
        self.assertIn(INDEX, {i["name"] for i in inspector.get_indexes("vocabulary_libraries")})
        fk = next(f for f in inspector.get_foreign_keys("vocabulary_libraries")
                  if f["constrained_columns"] == ["owner_user_id"])
        self.assertEqual(fk["referred_table"], "users")
        self.assertEqual(fk["referred_columns"], ["id"])
        with db.engine.connect() as connection:
            # SQLite PRAGMA/SQLAlchemy não refletem nomes de FKs inline.
            schema = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'vocabulary_libraries'").scalar()
            self.assertIn("CONSTRAINT fk_vocabulary_libraries_owner REFERENCES users (id)", schema)
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), REVISION)
            self.assertIsNone(connection.exec_driver_sql("SELECT owner_user_id FROM vocabulary_libraries").scalar())
        self.assert_integrity()

    def test_upgrade_and_downgrade_preserve_referenced_data_without_rebuilding(self):
        inspector = sa.inspect(db.engine)
        incoming = [(table, fk["constrained_columns"]) for table in inspector.get_table_names()
                    for fk in inspector.get_foreign_keys(table) if fk["referred_table"] == "vocabulary_libraries"]
        self.assertEqual(incoming, [("project_libraries", ["library_id"])])
        upgrade(directory=MIGRATIONS, revision=REVISION)
        self.assert_upgraded()
        # FK nova e FK existente realmente bloqueiam operações inválidas.
        for sql in (
            "UPDATE vocabulary_libraries SET owner_user_id = 'missing'",
            "DELETE FROM vocabulary_libraries WHERE id = 'global'",
        ):
            with self.assertRaises(sa.exc.IntegrityError):
                with db.engine.begin() as connection:
                    connection.exec_driver_sql(sql)
        with db.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE vocabulary_libraries SET owner_user_id = 'owner'")
            connection.exec_driver_sql("UPDATE vocabulary_libraries SET owner_user_id = NULL")
        downgrade(directory=MIGRATIONS, revision=PREVIOUS)
        inspector = sa.inspect(db.engine)
        self.assertNotIn("owner_user_id", {c["name"] for c in inspector.get_columns("vocabulary_libraries")})
        self.assertNotIn(INDEX, {i["name"] for i in inspector.get_indexes("vocabulary_libraries")})
        self.assert_integrity()
        upgrade(directory=MIGRATIONS, revision=REVISION)
        self.assert_upgraded()
        ddl = "\n".join(self.statements).upper()
        self.assertIn("ALTER TABLE VOCABULARY_LIBRARIES ADD COLUMN", ddl)
        self.assertIn("ALTER TABLE VOCABULARY_LIBRARIES DROP COLUMN", ddl)
        self.assertNotIn("DROP TABLE", ddl)
        self.assertNotIn("CREATE TABLE", ddl)
        self.assertNotIn("FOREIGN_KEYS=OFF", ddl.replace(" ", ""))

    def test_recovery_from_equivalent_failed_batch_keeps_data_and_artifacts(self):
        # Reproduz exatamente a implementação que falhou, com o mesmo engine/PRAGMA.
        with self.assertRaisesRegex(sa.exc.IntegrityError, "FOREIGN KEY constraint failed"):
            with db.engine.begin() as connection:
                operations = Operations(MigrationContext.configure(connection))
                with operations.batch_alter_table("vocabulary_libraries") as batch:
                    batch.add_column(sa.Column("owner_user_id", sa.String(36), nullable=True))
                    batch.create_foreign_key("fk_vocabulary_libraries_owner", "users", ["owner_user_id"], ["id"])
                    batch.create_index(INDEX, ["owner_user_id"])
        self.assert_integrity()
        inspector = sa.inspect(db.engine)
        self.assertNotIn("owner_user_id", {c["name"] for c in inspector.get_columns("vocabulary_libraries")})
        self.assertNotIn(INDEX, {i["name"] for i in inspector.get_indexes("vocabulary_libraries")})
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), PREVIOUS)
            artifacts = connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'").all()
            for name, _ in artifacts:
                self.assertEqual(name, "_alembic_tmp_vocabulary_libraries")
                self.assertEqual(connection.exec_driver_sql(f'SELECT COUNT(*) FROM "{name}"').scalar(), 0)
            print(f"SQLite {connection.exec_driver_sql('SELECT sqlite_version()').scalar()}: "
                  f"artefatos após falha = {[name for name, _ in artifacts]}")
        self.statements.clear()
        upgrade(directory=MIGRATIONS, revision=REVISION)
        self.assert_upgraded()
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'").all(), artifacts)
        self.assertNotIn("DROP TABLE", "\n".join(self.statements).upper())

    def test_downgrade_refuses_to_make_private_libraries_global(self):
        upgrade(directory=MIGRATIONS, revision=REVISION)
        with db.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE vocabulary_libraries SET owner_user_id = 'owner'")
        with self.assertRaises(SystemExit):
            downgrade(directory=MIGRATIONS, revision=PREVIOUS)
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT owner_user_id FROM vocabulary_libraries").scalar(), "owner")
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), REVISION)
        self.assertIn(INDEX, {i["name"] for i in sa.inspect(db.engine).get_indexes("vocabulary_libraries")})
        self.assert_integrity()

    def test_old_sqlite_refuses_downgrade_before_altering_schema(self):
        upgrade(directory=MIGRATIONS, revision=REVISION)
        migration = import_module("migrations.versions.d84b02f35a19_bibliotecas_privadas")
        self.statements.clear()
        with db.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with patch.object(connection.dialect, "server_version_info", (3, 34, 1)):
                    with self.assertRaisesRegex(RuntimeError, "SQLite >= 3.35"):
                        migration.downgrade()
        self.assertNotIn("DROP", "\n".join(self.statements).upper())
        self.assert_upgraded()

    def test_native_ddl_failure_rolls_back_without_partial_column_or_index(self):
        def fail_on(fragment):
            def listener(_conn, _cursor, statement, _parameters, _context, _executemany):
                if fragment in statement:
                    raise RuntimeError("Falha de DDL simulada")
            return listener

        # Falha no índice não deixa a coluna parcialmente adicionada.
        fail_index = fail_on("CREATE INDEX " + INDEX)
        sa.event.listen(db.engine, "before_cursor_execute", fail_index)
        try:
            with self.assertRaises(SystemExit):
                upgrade(directory=MIGRATIONS, revision=REVISION)
        finally:
            sa.event.remove(db.engine, "before_cursor_execute", fail_index)
        self.assertNotIn("owner_user_id", {c["name"] for c in sa.inspect(db.engine).get_columns("vocabulary_libraries")})
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), PREVIOUS)
        self.assert_integrity()
        upgrade(directory=MIGRATIONS, revision=REVISION)
        # Falha no DROP COLUMN não deixa o índice parcialmente removido.
        fail_column = fail_on("DROP COLUMN owner_user_id")
        sa.event.listen(db.engine, "before_cursor_execute", fail_column)
        try:
            with self.assertRaises(SystemExit):
                downgrade(directory=MIGRATIONS, revision=PREVIOUS)
        finally:
            sa.event.remove(db.engine, "before_cursor_execute", fail_column)
        self.assert_upgraded()
