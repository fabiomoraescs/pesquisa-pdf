"""Upgrade SQLite com codings/memos reais referenciando códigos já existentes."""

import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa
from flask_migrate import downgrade, upgrade

from app import create_app
from platform_core.extensions import db
from platform_core.models import (Analysis, AnalysisDocument, Project, QualitativeCode,
                                  QualitativeCoding, QualitativeExcerpt, QualitativeMemo, Tool, User)
from platform_core.scraping_types import QUALITATIVE_TOOL


PREVIOUS = "d84b02f35a19"
REVISION = "a6f2c9e41b07"
MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


class QualitativeCodeColorMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="code-color-migration-")
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
        upgrade(directory=MIGRATIONS, revision=PREVIOUS)
        with db.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            connection.execute(User.__table__.insert().values(
                id="owner", name="Pesquisador", email="color-migration@example.org", password_hash="test-only"))
            if not connection.exec_driver_sql("SELECT 1 FROM tools WHERE id = ?", (QUALITATIVE_TOOL,)).scalar():
                connection.execute(Tool.__table__.insert().values(
                    id=QUALITATIVE_TOOL, name="Análise quali-dados", route="/analise-qualitativa"))
            connection.execute(Project.__table__.insert().values(
                id="project", owner_user_id="owner", name="Projeto existente", scrape_type="qualitative"))
            connection.execute(Analysis.__table__.insert().values(
                id="analysis", user_id="owner", project_id="project", name="Base existente",
                source_type="qualitative", tool_id=QUALITATIVE_TOOL, tool_version="manual-v1",
                status="concluida"))
            connection.execute(AnalysisDocument.__table__.insert().values(
                id="document", analysis_id="analysis", original_name="livro.pdf", stored_name="livro.pdf"))
            connection.execute(QualitativeCode.__table__.insert().values(
                id="code", analysis_id="analysis", name="Raça", normalized_name="raça",
                created_by_user_id="owner"))
            connection.execute(QualitativeExcerpt.__table__.insert().values(
                id="excerpt", analysis_id="analysis", document_id="document", page_number=1,
                start_offset=0, end_offset=4, quoted_text="Raça", page_text_hash="a" * 64,
                created_by_user_id="owner"))
            connection.execute(QualitativeCoding.__table__.insert().values(
                id="coding", analysis_id="analysis", excerpt_id="excerpt", code_id="code",
                created_by_user_id="owner", origin="manual"))
            connection.execute(QualitativeMemo.__table__.insert().values(
                id="memo", analysis_id="analysis", code_id="code", text="Nota existente",
                created_by_user_id="owner"))
        self.statements = []

        def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            self.statements.append(statement)

        sa.event.listen(db.engine, "before_cursor_execute", record_sql)
        self.addCleanup(lambda: sa.event.remove(db.engine, "before_cursor_execute", record_sql))

    def assert_integrity(self):
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            self.assertEqual(connection.exec_driver_sql(
                "SELECT c.id, c.name, k.id, k.code_id, k.excerpt_id, m.id, m.code_id "
                "FROM qualitative_codes c JOIN qualitative_codings k ON k.code_id = c.id "
                "JOIN qualitative_memos m ON m.code_id = c.id").one(),
                ("code", "Raça", "coding", "code", "excerpt", "memo", "code"))

    def test_native_upgrade_preserves_referenced_data_and_safe_downgrade(self):
        incoming = {(table, tuple(fk["constrained_columns"]))
                    for table in sa.inspect(db.engine).get_table_names()
                    for fk in sa.inspect(db.engine).get_foreign_keys(table)
                    if fk["referred_table"] == "qualitative_codes"}
        self.assertIn(("qualitative_codings", ("code_id", "analysis_id")), incoming)
        self.assertIn(("qualitative_memos", ("code_id", "analysis_id")), incoming)
        upgrade(directory=MIGRATIONS, revision=REVISION)
        self.assert_integrity()
        self.assertIn("color", {column["name"] for column in sa.inspect(db.engine).get_columns("qualitative_codes")})
        with db.engine.connect() as connection:
            self.assertIsNone(connection.exec_driver_sql("SELECT color FROM qualitative_codes").scalar())
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), REVISION)
        with db.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE qualitative_codes SET color = 'yellow' WHERE id = 'code'")
        with self.assertRaises(SystemExit):
            downgrade(directory=MIGRATIONS, revision=PREVIOUS)
        self.assert_integrity()
        with db.engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT color FROM qualitative_codes").scalar(), "yellow")
            connection.exec_driver_sql("UPDATE qualitative_codes SET color = NULL WHERE id = 'code'")
        downgrade(directory=MIGRATIONS, revision=PREVIOUS)
        self.assert_integrity()
        self.assertNotIn("color", {column["name"] for column in sa.inspect(db.engine).get_columns("qualitative_codes")})
        ddl = "\n".join(self.statements).upper()
        self.assertIn("ALTER TABLE QUALITATIVE_CODES ADD COLUMN", ddl)
        self.assertIn("ALTER TABLE QUALITATIVE_CODES DROP COLUMN", ddl)
        self.assertNotIn("DROP TABLE QUALITATIVE_CODES", ddl)
        self.assertNotIn("FOREIGN_KEYS=OFF", ddl.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
