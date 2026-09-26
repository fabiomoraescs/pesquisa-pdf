"""Gestão manual de códigos e memos no ambiente qualitativo existente."""

import unittest
from pathlib import Path

from sqlalchemy import select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.analyses import create_analysis
from platform_core.extensions import db
from platform_core.models import (
    AnalysisDocument, Project, QualitativeCode, QualitativeCoding,
    QualitativeExcerpt, QualitativeMemo, UserToolOverride,
)
from platform_core.scraping_types import QUALITATIVE, QUALITATIVE_TOOL


class QualitativeRecordTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.user = create_user()
        self.client = self.app.test_client()
        db.session.add(UserToolOverride(user_id=self.user.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        login(self.client)
        self.csrf = csrf_from(self.client.get("/"))

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def workspace(self, owner=None):
        user = owner or self.user
        project = Project(owner_user_id=user.id, name="Projeto qualitativo",
                          scrape_type=QUALITATIVE, description="")
        db.session.add(project)
        db.session.commit()
        analysis = create_analysis(user_id=user.id, project_id=project.id,
                                   tool_id=QUALITATIVE_TOOL, tool_version="manual-v1",
                                   name=project.name, parameters={})
        return project, analysis

    def api(self, method, url, payload=None, *, token=True):
        headers = {"X-CSRFToken": self.csrf} if token else {}
        return self.client.open(url, method=method, json=payload, headers=headers)

    def test_code_crud_uniqueness_and_empty_project_interface(self):
        project, analysis = self.workspace()
        page = self.client.get(f"/analise-qualitativa/projetos/{project.id}", follow_redirects=True)
        html = page.get_data(as_text=True)
        self.assertIn("Você ainda não tem arquivos adicionados", html)
        self.assertIn("Adicione para começar a análise quali-dados.", html)
        self.assertNotIn("Códigos (0)", html)
        self.assertNotIn("Memos (0)", html)
        self.assertNotIn("Novo código", html)
        self.assertNotIn("data-qualitative-records", html)
        url = f"/analise-qualitativa/bases/{analysis.id}/codigos"
        self.assertEqual(self.api("POST", url, {"name": " ", "description": ""}).status_code, 400)
        self.assertEqual(self.api("POST", url, {"name": "x" * 161}).status_code, 400)
        self.assertEqual(self.api("POST", url, {"name": "Nome", "description": "x" * 4001}).status_code, 400)
        created = self.api("POST", url, {"name": "  Raça  ", "description": "Conceito inicial"})
        self.assertEqual(created.status_code, 201)
        code = db.session.scalar(select(QualitativeCode).where(QualitativeCode.analysis_id == analysis.id))
        self.assertEqual(code.name, "Raça")
        self.assertEqual(created.json["codes"][0]["id"], code.id)
        self.assertEqual(self.api("POST", url, {"name": "raça"}).status_code, 409)
        second = self.api("POST", url, {"name": "Outro tema"})
        self.assertEqual(second.status_code, 201)
        self.assertEqual(len(second.json["codes"]), 2)
        # Ocultar os controles sem documentos não apaga registros já persistidos.
        self.assertNotIn("data-qualitative-records", self.client.get(f"/analise-qualitativa/bases/{analysis.id}")
                         .get_data(as_text=True))
        item_url = f"{url}/{code.id}"
        self.assertEqual(self.api("PATCH", item_url, {"name": "outro TEMA"}).status_code, 409)
        edited = self.api("PATCH", item_url, {"name": "Classificação racial", "description": "Revisado"})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(next(item for item in edited.json["codes"] if item["name"] == "Classificação racial")["id"], code.id)
        self.assertEqual(db.session.get(QualitativeCode, code.id).description, "Revisado")
        self.assertEqual(self.api("DELETE", item_url).status_code, 200)
        self.assertIsNone(db.session.get(QualitativeCode, code.id))
        self.assertEqual(len(db.session.scalars(select(QualitativeCode).where(
            QualitativeCode.analysis_id == analysis.id)).all()), 1)

    def test_same_code_name_allowed_in_different_projects_and_foreign_ids_rejected(self):
        _, own = self.workspace()
        foreign = create_user("Outra", "outra-record@example.org")
        db.session.add(UserToolOverride(user_id=foreign.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        _, other = self.workspace(foreign)
        own_url = f"/analise-qualitativa/bases/{own.id}/codigos"
        other_url = f"/analise-qualitativa/bases/{other.id}/codigos"
        self.assertEqual(self.api("POST", own_url, {"name": "Identidade"}).status_code, 201)
        self.assertEqual(self.api("POST", other_url, {"name": "Identidade"}).status_code, 404)
        other_code = QualitativeCode(analysis_id=other.id, name="Identidade", created_by_user_id=foreign.id)
        db.session.add(other_code)
        db.session.commit()
        self.assertEqual(self.api("PATCH", f"{own_url}/{other_code.id}", {"name": "Outro"}).status_code, 404)
        self.assertEqual(self.api("DELETE", f"{own_url}/{other_code.id}").status_code, 404)
        self.assertEqual(self.api("DELETE", f"{other_url}/{other_code.id}").status_code, 404)
        self.assertIsNotNone(db.session.get(QualitativeCode, other_code.id))

    def test_deleting_code_keeps_excerpt_and_detaches_contextual_memo(self):
        _, analysis = self.workspace()
        document = AnalysisDocument(analysis_id=analysis.id, original_name="arquivo.pdf", stored_name="arquivo.pdf")
        db.session.add(document)
        db.session.flush()
        first = QualitativeCode(analysis_id=analysis.id, name="Tema", created_by_user_id=self.user.id)
        second = QualitativeCode(analysis_id=analysis.id, name="Outro", created_by_user_id=self.user.id)
        excerpt = QualitativeExcerpt(analysis_id=analysis.id, document_id=document.id, page_number=1,
                                     start_offset=0, end_offset=4, quoted_text="Tema", page_text_hash="a" * 64,
                                     created_by_user_id=self.user.id)
        db.session.add_all([first, second, excerpt])
        db.session.flush()
        db.session.add_all([
            QualitativeCoding(analysis_id=analysis.id, excerpt_id=excerpt.id, code_id=first.id,
                              created_by_user_id=self.user.id),
            QualitativeCoding(analysis_id=analysis.id, excerpt_id=excerpt.id, code_id=second.id,
                              created_by_user_id=self.user.id),
            QualitativeMemo(analysis_id=analysis.id, code_id=first.id, text="Memo do código",
                            created_by_user_id=self.user.id),
        ])
        db.session.commit()
        response = self.api("DELETE", f"/analise-qualitativa/bases/{analysis.id}/codigos/{first.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(db.session.get(QualitativeExcerpt, excerpt.id))
        self.assertIsNotNone(db.session.get(QualitativeCode, second.id))
        self.assertEqual(db.session.scalar(select(QualitativeCoding.code_id)), second.id)
        memo = db.session.scalar(select(QualitativeMemo))
        self.assertIsNotNone(memo)
        self.assertIsNone(memo.code_id)
        self.assertIsNotNone(db.session.get(AnalysisDocument, document.id))

    def test_memo_crud_validation_and_cross_project_rejection(self):
        _, analysis = self.workspace()
        url = f"/analise-qualitativa/bases/{analysis.id}/memos"
        self.assertEqual(self.api("POST", url, {"text": " "}).status_code, 400)
        self.assertEqual(self.api("POST", url, {"text": "x" * 20001}).status_code, 400)
        self.assertEqual(self.api("POST", url, {"text": "x" * 200000}).status_code, 400)
        created = self.api("POST", url, {"text": "Hipótese inicial\nSobre os documentos"})
        self.assertEqual(created.status_code, 201)
        memo_id = created.json["memos"][0]["id"]
        self.assertEqual(created.json["memos"][0]["text"], "Hipótese inicial\nSobre os documentos")
        self.assertNotIn("Hipótese inicial", self.client.get(f"/analise-qualitativa/bases/{analysis.id}")
                         .get_data(as_text=True))
        edited = self.api("PATCH", f"{url}/{memo_id}", {"text": "Revisão da hipótese"})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json["memos"][0]["id"], memo_id)
        self.assertEqual(db.session.get(QualitativeMemo, memo_id).text, "Revisão da hipótese")
        _, other = self.workspace()
        self.assertEqual(self.api("DELETE", f"/analise-qualitativa/bases/{other.id}/memos/{memo_id}")
                         .status_code, 404)
        self.assertEqual(self.api("DELETE", f"{url}/{memo_id}").status_code, 200)
        self.assertIsNone(db.session.get(QualitativeMemo, memo_id))

    def test_csrf_methods_permissions_and_info(self):
        project, analysis = self.workspace()
        codes_url = f"/analise-qualitativa/bases/{analysis.id}/codigos"
        memos_url = f"/analise-qualitativa/bases/{analysis.id}/memos"
        self.assertEqual(self.api("POST", codes_url, {"name": "Teste"}, token=False).status_code, 400)
        self.assertEqual(self.api("POST", memos_url, {"text": "Teste"}, token=False).status_code, 400)
        created = self.api("POST", codes_url, {"name": "Teste"})
        code_id = created.json["codes"][0]["id"]
        self.assertEqual(self.api("PATCH", f"{codes_url}/{code_id}", {"name": "Novo"}, token=False)
                         .status_code, 400)
        self.assertEqual(self.api("DELETE", f"{codes_url}/{code_id}", token=False).status_code, 400)
        self.assertEqual(self.client.get(codes_url).status_code, 405)
        self.assertEqual(self.client.get(memos_url).status_code, 405)
        db.session.get(Project, project.id).status = "archived"
        db.session.commit()
        self.assertEqual(self.api("POST", codes_url, {"name": "Teste"}).status_code, 404)
        page = self.client.get(f"/analise-qualitativa/bases/{analysis.id}")
        html = page.get_data(as_text=True)
        self.assertIn("criar códigos", html)
        self.assertIn("registrar memos", html)
        self.assertNotIn("Códigos e memos ainda não podem ser criados", html)
        self.assertNotIn("Bases", html)
        self.assertIn("interpretação permanece responsabilidade", html)

    def test_revoked_tool_cannot_mutate_records(self):
        _, analysis = self.workspace()
        db.session.get(UserToolOverride, (self.user.id, QUALITATIVE_TOOL)).decision = "deny"
        db.session.commit()
        url = f"/analise-qualitativa/bases/{analysis.id}/codigos"
        self.assertEqual(self.api("POST", url, {"name": "Bloqueado"}).status_code, 403)
        self.assertEqual(db.session.scalar(select(QualitativeCode.id)), None)

    def test_ui_uses_async_safe_rendering_and_keeps_reader_state(self):
        script = (Path(__file__).resolve().parents[1] / "static/js/qualitative_records.js").read_text(encoding="utf-8")
        self.assertIn("fetch(url", script)
        self.assertIn("textContent = kind === 'code'", script)
        self.assertIn("list.replaceChildren(fragment)", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("window.location", script)
        self.assertNotIn("document.location", script)
        self.assertIn("'X-CSRFToken': csrf", script)


if __name__ == "__main__":
    unittest.main()
