"""Fundação qualitativa: integridade entre Bases, permissões e migrations."""

import tempfile
import unittest
from pathlib import Path

from flask_migrate import upgrade
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import NotFound

from platform_helpers import create_user, isolated_platform, login
from platform_core.analyses import analysis_dir, create_analysis, delete_analysis
from platform_core.extensions import db
from platform_core.models import (
    Analysis, AnalysisDocument, AuditLog, PlanTool, Project, QualitativeCode,
    QualitativeCoding, QualitativeExcerpt, QualitativeMemo, Tool, UserToolOverride, utcnow,
)
from platform_core.project_lifecycle import archive, delete_archived
from platform_core.qualitative import (
    get_qualitative_analysis, get_qualitative_code, get_qualitative_document,
    get_qualitative_excerpt, get_qualitative_memo,
)
from platform_core.scraping_types import (
    FREE, QUALITATIVE, QUALITATIVE_TOOL, SYSTEMATIC, TOOL_BY_TYPE, tool_for_project,
)
from platform_core.services import can_use_tool, seed_platform


class QualitativeFoundationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.user = create_user()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def project(self, owner=None, kind=QUALITATIVE):
        project = Project(owner_user_id=(owner or self.user).id, name="Pesquisa qualitativa",
                          scrape_type=kind)
        db.session.add(project)
        db.session.commit()
        return project

    def base(self, project=None, *, user=None, strategy="inductive"):
        project = project or self.project()
        return create_analysis(user_id=(user or self.user).id, project_id=project.id,
                               tool_id=QUALITATIVE_TOOL, tool_version="manual-v1",
                               parameters={"qualitative_strategy": strategy}, name="Base de leitura")

    def document(self, base, name="texto.pdf"):
        document = AnalysisDocument(analysis_id=base.id, original_name=name, stored_name=name)
        db.session.add(document)
        db.session.commit()
        return document

    def code(self, base, name="Identidade", *, author=None):
        code = QualitativeCode(analysis_id=base.id, name=name, description="",
                               created_by_user_id=(author or self.user).id)
        db.session.add(code)
        db.session.commit()
        return code

    def excerpt(self, base, document, *, start=0, end=5, page=1):
        excerpt = QualitativeExcerpt(
            analysis_id=base.id, document_id=document.id, page_number=page,
            start_offset=start, end_offset=end, quoted_text="trecho",
            page_text_hash="a" * 64, created_by_user_id=self.user.id,
        )
        db.session.add(excerpt)
        db.session.commit()
        return excerpt

    def rejects(self, item):
        db.session.add(item)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_type_tool_seed_and_existing_permissions_remain_unchanged(self):
        self.assertEqual(TOOL_BY_TYPE[QUALITATIVE], QUALITATIVE_TOOL)
        self.assertEqual(TOOL_BY_TYPE[FREE], "pdf_scraper")
        self.assertEqual(TOOL_BY_TYPE[SYSTEMATIC], "document_analysis")
        self.assertEqual(db.session.get(Tool, QUALITATIVE_TOOL).name, "Análise quali-dados")
        self.assertEqual(db.session.get(Tool, QUALITATIVE_TOOL).route, "/analise-qualitativa")
        self.assertIsNotNone(db.session.get(Tool, "pdf_scraper"))
        self.assertIsNotNone(db.session.get(Tool, "document_analysis"))
        self.assertEqual(db.session.scalar(select(func.count()).select_from(PlanTool).where(
            PlanTool.tool_id == QUALITATIVE_TOOL)), 0)
        self.assertFalse(can_use_tool(self.user, QUALITATIVE_TOOL))
        self.assertTrue(can_use_tool(self.user, "pdf_scraper"))
        self.assertTrue(can_use_tool(self.user, "document_analysis"))
        seed_platform()
        self.assertEqual(db.session.scalar(select(func.count()).select_from(PlanTool).where(
            PlanTool.tool_id == QUALITATIVE_TOOL)), 0)
        db.session.add(UserToolOverride(user_id=self.user.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        self.assertTrue(can_use_tool(self.user, QUALITATIVE_TOOL))

    def test_project_analysis_and_strategy_use_existing_models(self):
        project = self.project()
        base = self.base(project, strategy="deductive")
        self.assertEqual(tool_for_project(project), QUALITATIVE_TOOL)
        self.assertEqual(base.project_id, project.id)
        self.assertEqual(base.tool_id, QUALITATIVE_TOOL)
        self.assertEqual(base.parameters_json["qualitative_strategy"], "deductive")
        self.assertEqual(base.source_type, "project")
        self.assertEqual(base.status, "processando")
        self.base(project, strategy="hybrid")
        with self.assertRaises(ValueError):
            self.base(project, strategy="automatic")
        with self.assertRaises(ValueError):
            self.base(self.project(kind=FREE))

    def test_creation_accepts_own_qualitative_project_but_rejects_foreign_owner(self):
        own_project = self.project()
        self.assertEqual(self.base(own_project).project_id, own_project.id)
        other_user = create_user("Outra pessoa", "outra-base@example.org")
        foreign_project = self.project(owner=other_user)
        with self.assertRaises(ValueError):
            self.base(foreign_project)
        self.assertIsNone(db.session.scalar(select(Analysis.id).where(
            Analysis.project_id == foreign_project.id, Analysis.user_id == self.user.id)))

    def test_creation_still_rejects_wrong_type_archived_and_deleted_projects(self):
        with self.assertRaises(ValueError):
            self.base(self.project(kind=SYSTEMATIC))
        archived = self.project()
        archived.status = "archived"
        db.session.commit()
        with self.assertRaises(ValueError):
            self.base(archived)
        deleted = self.project()
        deleted.deleted_at = utcnow()
        db.session.commit()
        with self.assertRaises(ValueError):
            self.base(deleted)

    def test_existing_free_and_systematic_creation_rules_are_unchanged(self):
        other_user = create_user("Outro autor", "outro-autor@example.org")
        free_project = self.project(owner=other_user, kind=FREE)
        systematic_project = self.project(owner=other_user, kind=SYSTEMATIC)
        for project, tool_id in ((free_project, "pdf_scraper"),
                                 (systematic_project, "document_analysis")):
            base = create_analysis(user_id=self.user.id, project_id=project.id,
                                   tool_id=tool_id, tool_version="v1", parameters={},
                                   name="Base histórica")
            self.assertEqual(base.project_id, project.id)
            self.assertEqual(base.user_id, self.user.id)

    def test_code_name_is_normalized_unique_within_base_only(self):
        base = self.base()
        first = self.code(base, "  Identidade   DOCENTE  ")
        self.assertEqual(first.name, "Identidade DOCENTE")
        self.assertEqual(first.normalized_name, "identidade docente")
        self.rejects(QualitativeCode(analysis_id=base.id, name="identidade docente",
                                     created_by_user_id=self.user.id))
        second = self.code(self.base(), "identidade docente")
        self.assertNotEqual(first.analysis_id, second.analysis_id)

    def test_excerpt_requires_document_of_its_base_and_valid_offsets(self):
        base = self.base()
        document = self.document(base)
        excerpt = self.excerpt(base, document)
        self.assertEqual(excerpt.document_id, document.id)
        other = self.base()
        self.rejects(QualitativeExcerpt(analysis_id=other.id, document_id=document.id,
                                        page_number=1, start_offset=0, end_offset=3,
                                        quoted_text="abc", page_text_hash="a" * 64,
                                        created_by_user_id=self.user.id))
        for start, end, page in ((0, 0, 1), (-1, 2, 1), (4, 3, 1), (0, 2, 0)):
            self.rejects(QualitativeExcerpt(analysis_id=base.id, document_id=document.id,
                                            page_number=page, start_offset=start, end_offset=end,
                                            quoted_text="abc", page_text_hash="a" * 64,
                                            created_by_user_id=self.user.id))

    def test_coding_is_manual_unique_and_cannot_cross_bases(self):
        base = self.base()
        document = self.document(base)
        excerpt = self.excerpt(base, document)
        code = self.code(base)
        coding = QualitativeCoding(analysis_id=base.id, excerpt_id=excerpt.id, code_id=code.id,
                                   created_by_user_id=self.user.id)
        db.session.add(coding)
        db.session.commit()
        self.assertEqual(coding.origin, "manual")
        self.rejects(QualitativeCoding(analysis_id=base.id, excerpt_id=excerpt.id, code_id=code.id,
                                       created_by_user_id=self.user.id))
        other = self.base()
        other_code = self.code(other)
        other_excerpt = self.excerpt(other, self.document(other))
        self.rejects(QualitativeCoding(analysis_id=base.id, excerpt_id=excerpt.id, code_id=other_code.id,
                                       created_by_user_id=self.user.id))
        self.rejects(QualitativeCoding(analysis_id=base.id, excerpt_id=other_excerpt.id, code_id=code.id,
                                       created_by_user_id=self.user.id))

    def test_all_memo_scopes_and_single_target_constraint(self):
        base = self.base()
        document = self.document(base)
        code = self.code(base)
        excerpt = self.excerpt(base, document)
        for target in ({}, {"document_id": document.id}, {"code_id": code.id},
                       {"excerpt_id": excerpt.id}):
            memo = QualitativeMemo(analysis_id=base.id, text="Observação do pesquisador",
                                    created_by_user_id=self.user.id, **target)
            db.session.add(memo)
            db.session.commit()
            self.assertEqual(memo.analysis_id, base.id)
        self.rejects(QualitativeMemo(analysis_id=base.id, document_id=document.id, code_id=code.id,
                                     text="Dois alvos", created_by_user_id=self.user.id))

    def test_memo_targets_from_other_base_are_rejected(self):
        base = self.base()
        other = self.base()
        document = self.document(other)
        code = self.code(other)
        excerpt = self.excerpt(other, document)
        for target in ({"document_id": document.id}, {"code_id": code.id},
                       {"excerpt_id": excerpt.id}):
            self.rejects(QualitativeMemo(analysis_id=base.id, text="Alvo alheio",
                                         created_by_user_id=self.user.id, **target))

    def test_authorization_helpers_validate_entire_chain(self):
        base = self.base()
        document = self.document(base)
        code = self.code(base)
        excerpt = self.excerpt(base, document)
        memo = QualitativeMemo(analysis_id=base.id, text="Memo", created_by_user_id=self.user.id)
        db.session.add(memo)
        db.session.commit()
        with self.assertRaises(NotFound):
            get_qualitative_analysis(base.id, self.user)
        db.session.add(UserToolOverride(user_id=self.user.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        self.assertEqual(get_qualitative_analysis(base.id, self.user, manage=True).id, base.id)
        self.assertEqual(get_qualitative_document(base, document.id).id, document.id)
        self.assertEqual(get_qualitative_code(base, code.id).id, code.id)
        self.assertEqual(get_qualitative_excerpt(base, excerpt.id).id, excerpt.id)
        self.assertEqual(get_qualitative_memo(base, memo.id).id, memo.id)
        other = self.base()
        with self.assertRaises(NotFound):
            get_qualitative_document(other, document.id)
        with self.assertRaises(NotFound):
            get_qualitative_code(other, code.id)
        with self.assertRaises(NotFound):
            get_qualitative_excerpt(other, excerpt.id)
        with self.assertRaises(NotFound):
            get_qualitative_memo(other, memo.id)
        stranger = create_user("Outro", "outro-qualitativo@example.org")
        db.session.add(UserToolOverride(user_id=stranger.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        with self.assertRaises(NotFound):
            get_qualitative_analysis(base.id, stranger)

    def test_deleting_base_removes_only_its_qualitative_dependents(self):
        first = self.base()
        second = self.base()
        first_document = self.document(first)
        second_document = self.document(second)
        first_code = self.code(first)
        second_code = self.code(second)
        first_excerpt = self.excerpt(first, first_document)
        second_excerpt = self.excerpt(second, second_document)
        first_coding = QualitativeCoding(analysis_id=first.id, excerpt_id=first_excerpt.id,
                                         code_id=first_code.id, created_by_user_id=self.user.id)
        second_coding = QualitativeCoding(analysis_id=second.id, excerpt_id=second_excerpt.id,
                                          code_id=second_code.id, created_by_user_id=self.user.id)
        first_memo = QualitativeMemo(analysis_id=first.id, excerpt_id=first_excerpt.id,
                                     text="Primeira", created_by_user_id=self.user.id)
        second_memo = QualitativeMemo(analysis_id=second.id, excerpt_id=second_excerpt.id,
                                      text="Segunda", created_by_user_id=self.user.id)
        db.session.add_all([first_coding, second_coding, first_memo, second_memo])
        db.session.commit()
        first_ids = ((AnalysisDocument, first_document.id), (QualitativeCode, first_code.id),
                     (QualitativeExcerpt, first_excerpt.id), (QualitativeCoding, first_coding.id),
                     (QualitativeMemo, first_memo.id))
        second_ids = ((AnalysisDocument, second_document.id), (QualitativeCode, second_code.id),
                      (QualitativeExcerpt, second_excerpt.id), (QualitativeCoding, second_coding.id),
                      (QualitativeMemo, second_memo.id))
        first_path = analysis_dir(first.id)
        second_path = analysis_dir(second.id)
        delete_analysis(first)
        self.assertIsNone(db.session.get(Analysis, first.id))
        self.assertFalse(first_path.exists())
        for model, identifier in first_ids:
            self.assertIsNone(db.session.get(model, identifier))
        self.assertTrue(second_path.exists())
        for model, identifier in second_ids:
            self.assertIsNotNone(db.session.get(model, identifier))

    def test_deleting_archived_project_cleans_qualitative_base_and_preserves_audit(self):
        project = self.project()
        base = self.base(project)
        document = self.document(base)
        code = self.code(base)
        excerpt = self.excerpt(base, document)
        db.session.add_all([
            QualitativeCoding(analysis_id=base.id, excerpt_id=excerpt.id, code_id=code.id,
                              created_by_user_id=self.user.id),
            QualitativeMemo(analysis_id=base.id, code_id=code.id, text="Nota",
                             created_by_user_id=self.user.id),
        ])
        base.status = "concluida"  # Nesta etapa, somente preparação técnica.
        db.session.commit()
        project_id, base_id, code_id, excerpt_id = project.id, base.id, code.id, excerpt.id
        archive(project, self.user)
        self.assertEqual(delete_archived([project], self.user, "deletar"), 1)
        self.assertIsNone(db.session.get(Project, project_id))
        self.assertIsNone(db.session.get(Analysis, base_id))
        self.assertIsNone(db.session.get(QualitativeCode, code_id))
        self.assertIsNone(db.session.get(QualitativeExcerpt, excerpt_id))
        self.assertIsNotNone(db.session.scalar(select(AuditLog).where(
            AuditLog.target_type == "project", AuditLog.target_id == project_id)))

    def test_user_with_qualitative_authorship_in_foreign_base_cannot_be_deleted(self):
        admin = create_user("Admin", "admin-qualitativo@example.org", "admin", "institutional")
        target = create_user("Codificador", "codificador@example.org")
        base = self.base(self.project(owner=admin), user=admin)
        self.code(base, author=target)
        client = self.app.test_client()
        login(client, "admin-qualitativo@example.org")
        response = client.get(f"/admin/usuarios/{target.id}/excluir-permanentemente")
        self.assertEqual(response.status_code, 200)
        self.assertIn("registros de codificação qualitativa", response.get_data(as_text=True))


class QualitativeMigrationTests(unittest.TestCase):
    def test_upgrade_from_existing_schema_preserves_projects_bases_and_documents(self):
        from app import create_app
        from platform_core.services import seed_platform

        with tempfile.TemporaryDirectory(prefix="pesquisapdf-qualitative-migration-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root, "SECRET_KEY": "migration-test-only",
            })
            migrations = str(Path(__file__).resolve().parent.parent / "migrations")
            with app.app_context():
                upgrade(directory=migrations, revision="c94a67b2d501")
                seed_platform()
                user = create_user("Legado", "legado-qualitativo@example.org")
                project = Project(owner_user_id=user.id, name="Projeto anterior", scrape_type=SYSTEMATIC)
                db.session.add(project)
                db.session.commit()
                base = create_analysis(user_id=user.id, project_id=project.id,
                                       tool_id="document_analysis", tool_version="lexical",
                                       parameters={}, name="Base anterior")
                document = AnalysisDocument(analysis_id=base.id, original_name="anterior.pdf",
                                            stored_name="anterior.pdf")
                db.session.add(document)
                db.session.commit()
                project_id, base_id, document_id = project.id, base.id, document.id
                db.session.remove()
                upgrade(directory=migrations, revision="head")
                tables = set(inspect(db.engine).get_table_names())
                self.assertTrue({"qualitative_codes", "qualitative_excerpts",
                                 "qualitative_codings", "qualitative_memos"}.issubset(tables))
                self.assertEqual(db.session.get(Project, project_id).scrape_type, SYSTEMATIC)
                self.assertEqual(db.session.get(Analysis, base_id).name, "Base anterior")
                self.assertEqual(db.session.get(AnalysisDocument, document_id).analysis_id, base_id)
                db.session.remove()
                db.engine.dispose()
