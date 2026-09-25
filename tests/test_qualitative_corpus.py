"""Etapa 2: projetos, preparo persistente do corpus e leitura autorizada."""

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from sqlalchemy import select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.analyses import analysis_dir, create_analysis, delete_analysis, preserve_documents
from platform_core.extensions import db
from platform_core.models import (
    Analysis, AnalysisDocument, Project, QualitativeCode, QualitativeCoding,
    QualitativeExcerpt, QualitativeMemo, UserToolOverride,
)
from platform_core.project_lifecycle import archive, delete_archived
from platform_core.qualitative_corpus import (
    CorpusUnavailableError, QualitativePageNotFoundError, canonicalize_page,
    is_qualitative_corpus_ready, load_qualitative_manifest,
    qualitative_manifest_path, read_qualitative_page, validate_qualitative_corpus,
)
from platform_core.qualitative_routes import _run_added_documents_job, _run_corpus_job
from platform_core.qualitative_layout import read_qualitative_layout
from platform_core.qualitative_search import QualitativeSearchError, search_qualitative_document
from platform_core.scraping_types import QUALITATIVE, QUALITATIVE_TOOL


def pdf_bytes(pages=1):
    document = pymupdf.open()
    for number in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"Documento de teste, pagina {number + 1}. " * 10)
    data = document.tobytes()
    document.close()
    return data


class QualitativeCorpusTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.user = create_user()
        self.user_id = self.user.id
        self.client = self.app.test_client()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def allow(self, user=None):
        user = user or self.user
        db.session.add(UserToolOverride(user_id=user.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()

    def project(self, owner=None):
        project = Project(owner_user_id=(owner or self.user).id, name="Entrevistas",
                          description="Leitura manual", scrape_type=QUALITATIVE)
        db.session.add(project)
        db.session.commit()
        return project

    def upload(self, project, *, pages=2, filename="entrevista.pdf", strategy="inductive"):
        token = csrf_from(self.client.get(f"/analise-qualitativa/projetos/{project.id}/bases/nova"))
        return self.client.post(f"/analise-qualitativa/projetos/{project.id}/bases/nova", data={
            "csrf_token": token, "name": "Base de entrevistas", "qualitative_strategy": strategy,
            "documents": [(io.BytesIO(pdf_bytes(pages)), filename)],
        }, content_type="multipart/form-data")

    def historical_workspace(self, project):
        """Fixture de legado: dados antigos podem conter mais de um acervo."""
        analysis = create_analysis(user_id=self.user_id, project_id=project.id,
                                   tool_id=QUALITATIVE_TOOL, tool_version="manual-v1",
                                   name="Acervo anterior", parameters={})
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "historico.pdf"
            source.write_bytes(pdf_bytes())
            preserve_documents(analysis, [(source, "historico.pdf")])
        return analysis

    def run_job_without_ocr(self, analysis_id):
        def extract(path):
            with pymupdf.open(path) as document:
                for number, page in enumerate(document, start=1):
                    yield {"pagina_pdf": number, "texto": page.get_text("text"),
                           "ocr_utilizado": False}

        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas", side_effect=extract):
            _run_corpus_job(self.app, analysis_id)

    def prepared_base(self, *, pages=2):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.assertEqual(self.upload(project, pages=pages).status_code, 302)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        self.run_job_without_ocr(analysis.id)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        manifest = load_qualitative_manifest(analysis)
        return analysis, manifest

    def prepared_two_documents(self):
        self.allow()
        project = self.project()
        token = csrf_from(self.client.get(f"/analise-qualitativa/projetos/{project.id}/bases/nova"))
        with patch("app.EXECUTOR_ANALISES.submit"):
            response = self.client.post(f"/analise-qualitativa/projetos/{project.id}/bases/nova",
                data={"csrf_token": token, "name": "Acervo antigo", "qualitative_strategy": "inductive",
                      "documents": [(io.BytesIO(pdf_bytes()), "primeiro.pdf"),
                                    (io.BytesIO(pdf_bytes()), "segundo.pdf")]},
                content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        self.run_job_without_ocr(analysis.id)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        return analysis, load_qualitative_manifest(analysis)

    def delete_document_request(self, analysis, document_id):
        token = csrf_from(self.client.get("/"))
        return self.client.post(
            f"/analise-qualitativa/bases/{analysis.id}/documentos/{document_id}/excluir",
            data={"csrf_token": token})

    def test_document_delete_preserves_other_pdf_corpus_and_search(self):
        analysis, manifest = self.prepared_two_documents()
        first, second = manifest["documents"]
        first_text = read_qualitative_page(analysis, first["document_id"], 1)["text"]
        first_pdf = analysis_dir(analysis.id) / "documents" / first["stored_name"]
        first_pdf_bytes = first_pdf.read_bytes()
        page_url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
                    f"{first['document_id']}/paginas/1")
        html = self.client.get(page_url).get_data(as_text=True)
        self.assertIn("Excluir documento primeiro.pdf", html)
        self.assertIn("Excluir documento segundo.pdf", html)
        self.assertIn("data-document-delete-dialog", html)
        endpoint = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
                    f"{second['document_id']}/excluir")
        self.assertEqual(self.client.get(endpoint).status_code, 405)
        self.assertEqual(self.client.post(endpoint).status_code, 400)  # CSRF
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
                   side_effect=AssertionError("PDF antigo reprocessado")):
            response = self.delete_document_request(analysis, second["document_id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["document_count"], 1)
        self.assertIn(first["document_id"], response.json["next_url"])
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        self.assertEqual(analysis.document_count, 1)
        self.assertIsNone(db.session.get(AnalysisDocument, second["document_id"]))
        self.assertEqual(load_qualitative_manifest(analysis)["documents"], [first])
        self.assertEqual(read_qualitative_page(analysis, first["document_id"], 1)["text"], first_text)
        self.assertEqual(first_pdf.read_bytes(), first_pdf_bytes)
        self.assertFalse((analysis_dir(analysis.id) / "documents" / second["stored_name"]).exists())
        self.assertFalse((analysis_dir(analysis.id) / "qualitative_corpus" / second["document_id"]).exists())
        self.assertEqual(self.client.get(page_url).status_code, 200)
        self.assertEqual(self.client.get(page_url.replace(first["document_id"], second["document_id"])).status_code, 404)
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}/documentos/"
                                         f"{second['document_id']}/buscar?q=Documento").status_code, 404)
        new_html = self.client.get(page_url).get_data(as_text=True)
        self.assertIn("Documentos (1)", new_html)
        self.assertNotIn("segundo.pdf", new_html)

    def test_delete_last_document_returns_to_empty_uploadable_project(self):
        analysis, manifest = self.prepared_base()
        response = self.delete_document_request(analysis, manifest["documents"][0]["document_id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["document_count"], 0)
        empty = self.client.get(response.json["next_url"], follow_redirects=True)
        self.assertIn("Este projeto ainda não possui documentos.", empty.get_data(as_text=True))
        self.assertIn("Adicionar documentos", empty.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(db.session.get(Analysis, analysis.id).status, "processando")
        self.assertFalse((analysis_dir(analysis.id) / "qualitative_corpus").exists())
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}").status_code, 200)
        args = self.submit_new_documents(db.session.get(Analysis, analysis.id), "substituto.pdf")
        self.assertTrue(args[-1])
        self.run_added_job_without_ocr(args)
        db.session.expire_all()
        self.assertEqual(db.session.get(Analysis, analysis.id).status, "concluida")

    def test_foreign_document_delete_is_rejected(self):
        analysis, manifest = self.prepared_base()
        other = self.historical_workspace(db.session.get(Project, analysis.project_id))
        other_doc = db.session.scalar(select(AnalysisDocument).where(AnalysisDocument.analysis_id == other.id))
        self.assertEqual(self.delete_document_request(analysis, other_doc.id).status_code, 404)
        foreign = create_user("Outro", "outro-delete@example.org")
        self.allow(foreign)
        token = csrf_from(self.client.get("/"))
        self.client.post("/logout", data={"csrf_token": token})
        login(self.client, "outro-delete@example.org")
        self.assertEqual(self.delete_document_request(analysis, manifest["documents"][0]["document_id"])
                         .status_code, 404)

    def test_document_delete_waits_for_inflight_processing(self):
        analysis, manifest = self.prepared_base()
        from platform_core.qualitative_routes import _upload_lock
        lock = _upload_lock(analysis.id)
        lock.touch()
        try:
            response = self.delete_document_request(analysis, manifest["documents"][0]["document_id"])
            self.assertEqual(response.status_code, 409)
            self.assertIn("Aguarde a preparação", response.json["error"])
            self.assertEqual(load_qualitative_manifest(analysis), manifest)
        finally:
            lock.unlink()

    def test_document_delete_restores_artifacts_when_manifest_publish_fails(self):
        analysis, manifest = self.prepared_two_documents()
        second = manifest["documents"][1]
        original_replace = os.replace
        manifest_path = qualitative_manifest_path(analysis.id)

        def fail_publication(source, target):
            if Path(target) == manifest_path and Path(source).name.startswith(".manifest."):
                raise OSError("falha simulada ao publicar manifest")
            return original_replace(source, target)

        with patch("platform_core.qualitative_corpus.os.replace", side_effect=fail_publication):
            response = self.delete_document_request(analysis, second["document_id"])
        self.assertEqual(response.status_code, 500)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        self.assertIsNotNone(db.session.get(AnalysisDocument, second["document_id"]))
        self.assertEqual(analysis.document_count, 2)
        self.assertNotIn("_qualitative_delete_pending", analysis.parameters_json)
        self.assertEqual(load_qualitative_manifest(analysis), manifest)
        self.assertTrue((analysis_dir(analysis.id) / "documents" / second["stored_name"]).is_file())
        self.assertTrue((analysis_dir(analysis.id) / "qualitative_corpus" / second["document_id"]).is_dir())

    def test_delete_document_removes_only_its_analytic_dependents(self):
        analysis, manifest = self.prepared_two_documents()
        first, second = manifest["documents"]
        code = QualitativeCode(analysis_id=analysis.id, name="Tema", created_by_user_id=self.user_id)
        db.session.add(code)
        db.session.flush()
        excerpts = []
        for item in (first, second):
            excerpt = QualitativeExcerpt(analysis_id=analysis.id, document_id=item["document_id"],
                page_number=1, start_offset=0, end_offset=1, quoted_text="D",
                page_text_hash=item["pages"][0]["sha256"], created_by_user_id=self.user_id)
            db.session.add(excerpt)
            excerpts.append(excerpt)
        db.session.flush()
        for excerpt in excerpts:
            db.session.add(QualitativeCoding(analysis_id=analysis.id, excerpt_id=excerpt.id,
                code_id=code.id, created_by_user_id=self.user_id, origin="manual"))
            db.session.add(QualitativeMemo(analysis_id=analysis.id, excerpt_id=excerpt.id,
                text="Observação", created_by_user_id=self.user_id))
        db.session.add(QualitativeMemo(analysis_id=analysis.id, document_id=second["document_id"],
            text="Memo do PDF", created_by_user_id=self.user_id))
        db.session.commit()
        self.assertEqual(self.delete_document_request(analysis, second["document_id"]).status_code, 200)
        self.assertIsNotNone(db.session.get(QualitativeCode, code.id))
        self.assertEqual(len(db.session.scalars(select(QualitativeExcerpt).where(
            QualitativeExcerpt.analysis_id == analysis.id)).all()), 1)
        self.assertEqual(len(db.session.scalars(select(QualitativeCoding).where(
            QualitativeCoding.analysis_id == analysis.id)).all()), 1)
        self.assertEqual(len(db.session.scalars(select(QualitativeMemo).where(
            QualitativeMemo.analysis_id == analysis.id)).all()), 1)

    def submit_new_documents(self, analysis, *names):
        page = self.client.get(f"/analise-qualitativa/bases/{analysis.id}", follow_redirects=True)
        token = csrf_from(page)
        with patch("app.EXECUTOR_ANALISES.submit") as submit:
            response = self.client.post(
                f"/analise-qualitativa/bases/{analysis.id}/documentos",
                data={"csrf_token": token, "documents": [
                    (io.BytesIO(pdf_bytes()), name) for name in names
                ]}, content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(submit.call_count, 1)
        job, *args = submit.call_args.args
        self.assertIs(job, _run_added_documents_job)
        return args

    def test_project_opens_empty_environment_without_base_step(self):
        self.allow()
        project = self.project()
        response = self.client.get(f"/analise-qualitativa/projetos/{project.id}", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Este projeto ainda não possui documentos.", html)
        self.assertIn("Adicionar documentos", html)
        self.assertEqual(html.count('>Adicionar documentos</summary>'), 1)
        self.assertEqual(html.count("data-qualitative-info-open"), 1)
        self.assertIn('class="platform-qualitative-page-actions"', html)
        self.assertLess(html.index("Adicionar documentos"), html.index("data-qualitative-info-open"))
        self.assertNotIn("Bases do projeto", html)
        self.assertEqual(len(db.session.scalars(select(Analysis).where(
            Analysis.project_id == project.id)).all()), 1)
        self.assertEqual(self.client.get(f"/analise-qualitativa/projetos/{project.id}/bases/nova")
                         .status_code, 302)

    def test_existing_single_workspace_opens_reader_with_project_title(self):
        analysis, manifest = self.prepared_base()
        project = db.session.get(Project, analysis.project_id)
        response = self.client.get(f"/analise-qualitativa/projetos/{project.id}",
                                   follow_redirects=True)
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'<h1 class="h4">{project.name}</h1>', html)
        self.assertNotIn(f'<h1 class="h4">{analysis.name}</h1>', html)
        self.assertIn("Adicionar documentos", html)
        self.assertIn("Como funciona a Análise quali-dados", html)
        self.assertNotIn("Bases do projeto", html)
        self.assertEqual(manifest["documents"][0]["document_id"],
                         db.session.scalar(select(AnalysisDocument.id).where(
                             AnalysisDocument.analysis_id == analysis.id)))

    def test_qualitative_header_and_document_delete_are_visual_only(self):
        analysis, manifest = self.prepared_base()
        project = db.session.get(Project, analysis.project_id)
        document = manifest["documents"][0]
        html = self.client.get(
            f"/analise-qualitativa/bases/{analysis.id}/documentos/"
            f"{document['document_id']}/paginas/1").get_data(as_text=True)
        intro = html.split('<div class="platform-qualitative-page-intro">', 1)[1].split(
            '<div class="platform-qualitative-workspace"', 1)[0]
        self.assertIn(f'<h1 class="h4">{project.name}</h1>', intro)
        self.assertEqual(html.count('<h1 '), 1)
        self.assertIn('class="platform-qualitative-page-actions"', intro)
        self.assertLess(intro.index("Adicionar documentos"), intro.index("data-qualitative-info-open"))
        self.assertEqual(html.count('>Adicionar documentos</summary>'), 1)
        self.assertEqual(html.count("data-qualitative-info-open"), 1)
        self.assertNotIn("platform-qualitative-page-toolbar", html)
        self.assertIn('class="platform-qualitative-document-delete" type="button" data-delete-document', html)
        self.assertIn(f'data-document-id="{document["document_id"]}"', html)
        self.assertIn(f'data-document-name="{document["original_name"]}"', html)
        self.assertIn('data-delete-url="/analise-qualitativa/bases/', html)
        self.assertIn(f'title="Excluir {document["original_name"]}"', html)
        self.assertIn(f'aria-label="Excluir documento {document["original_name"]}"', html)
        self.assertIn('data-document-delete-dialog', html)
        self.assertIn('data-delete-cancel>Cancelar', html)
        self.assertIn('data-delete-confirm>Excluir documento', html)
        css = (Path(__file__).resolve().parents[1] / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn('.platform-qualitative-document-delete { display: inline-flex;', css)
        self.assertIn('border: 0; border-radius:', css)
        self.assertIn('background: transparent; box-shadow: none;', css)
        self.assertIn('.platform-qualitative-document-delete:hover', css)
        self.assertIn('.platform-qualitative-document-delete:focus-visible', css)
        self.assertIn('.platform-qualitative-focus .platform-qualitative-page-intro { display: none;', css)

    def test_foreign_project_cannot_open_or_upload_to_workspace(self):
        self.allow()
        other = create_user("Outro", "outro-qual-flow@example.org")
        project = self.project(owner=other)
        analysis = create_analysis(user_id=other.id, project_id=project.id,
                                   tool_id=QUALITATIVE_TOOL, tool_version="manual-v1",
                                   name="Ambiente alheio", parameters={})
        self.assertEqual(self.client.get(f"/analise-qualitativa/projetos/{project.id}").status_code, 404)
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}").status_code, 404)
        token = csrf_from(self.client.get("/"))
        response = self.client.post(f"/analise-qualitativa/bases/{analysis.id}/documentos",
                                    data={"csrf_token": token, "documents": [
                                        (io.BytesIO(pdf_bytes()), "outro.pdf")]},
                                    content_type="multipart/form-data")
        self.assertEqual(response.status_code, 404)

    def test_existing_project_with_multiple_workspaces_preserves_every_one(self):
        self.allow()
        project = self.project()
        first = self.historical_workspace(project)
        second = self.historical_workspace(project)
        response = self.client.get(f"/analise-qualitativa/projetos/{project.id}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"/analise-qualitativa/bases/{first.id}", html)
        self.assertIn(f"/analise-qualitativa/bases/{second.id}", html)
        self.assertEqual(len(db.session.scalars(select(Analysis).where(
            Analysis.project_id == project.id)).all()), 2)
        self.assertNotIn("Bases do projeto", html)

    def test_initial_project_upload_accepts_multiple_pdfs(self):
        self.allow()
        project = self.project()
        self.client.get(f"/analise-qualitativa/projetos/{project.id}")
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        args = self.submit_new_documents(analysis, "primeiro.pdf", "segundo.pdf")
        self.assertTrue(args[-1])  # primeiro preparo
        self.run_added_job_without_ocr(args)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        self.assertEqual(analysis.status, "concluida")
        manifest = load_qualitative_manifest(analysis)
        self.assertEqual(len(manifest["documents"]), 2)
        html = self.client.get(f"/analise-qualitativa/bases/{analysis.id}",
                               follow_redirects=True).get_data(as_text=True)
        self.assertIn("Documentos (2)", html)
        self.assertIn("primeiro.pdf", html)
        self.assertIn("segundo.pdf", html)

    def run_added_job_without_ocr(self, args, *, fail=False):
        seen = []
        def extract(path):
            seen.append(Path(path).name)
            if fail:
                raise RuntimeError("falha simulada no PDF novo")
            with pymupdf.open(path) as document:
                for number, page in enumerate(document, start=1):
                    yield {"pagina_pdf": number, "texto": page.get_text("text"),
                           "ocr_utilizado": False}
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas", side_effect=extract):
            _run_added_documents_job(*args)
        return seen

    def test_incremental_upload_does_not_reprocess_existing_pdf(self):
        analysis, old_manifest = self.prepared_base()
        old_document = old_manifest["documents"][0]
        old_page = read_qualitative_page(analysis, old_document["document_id"], 1)
        args = self.submit_new_documents(analysis, "novo-a.pdf", "novo-b.pdf")
        self.assertFalse(args[-1])
        from platform_core import qualitative_corpus
        with patch("platform_core.qualitative_corpus._read_verified_page",
                   wraps=qualitative_corpus._read_verified_page) as verify:
            processed = self.run_added_job_without_ocr(args)
        self.assertEqual(len(processed), 2)
        self.assertNotIn(old_document["stored_name"], processed)
        self.assertEqual(verify.call_count, 2)
        self.assertTrue(all(old_document["document_id"] not in call.args[1]["file"]
                            for call in verify.call_args_list))
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        self.assertEqual(analysis.status, "concluida")
        manifest = load_qualitative_manifest(analysis)
        self.assertEqual(len(manifest["documents"]), 3)
        self.assertEqual(manifest["documents"][0], old_document)
        self.assertEqual(read_qualitative_page(analysis, old_document["document_id"], 1), old_page)
        self.assertTrue(is_qualitative_corpus_ready(analysis))
        html = self.client.get(f"/analise-qualitativa/bases/{analysis.id}",
                               follow_redirects=True).get_data(as_text=True)
        self.assertIn("Documentos (3)", html)
        self.assertIn("novo-a.pdf", html)
        self.assertIn("novo-b.pdf", html)

    def test_failed_incremental_upload_keeps_old_corpus_usable(self):
        analysis, old_manifest = self.prepared_base()
        args = self.submit_new_documents(analysis, "falha.pdf")
        self.run_added_job_without_ocr(args, fail=True)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        self.assertEqual(analysis.status, "concluida")
        self.assertEqual(load_qualitative_manifest(analysis), old_manifest)
        self.assertEqual(analysis.document_count, 1)
        self.assertTrue(is_qualitative_corpus_ready(analysis))
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}")
                         .status_code, 302)

    def test_failed_final_verification_rolls_back_new_documents(self):
        analysis, old_manifest = self.prepared_base()
        args = self.submit_new_documents(analysis, "novo.pdf")
        def extract(path):
            with pymupdf.open(path) as document:
                for number, page in enumerate(document, start=1):
                    yield {"pagina_pdf": number, "texto": page.get_text("text"),
                           "ocr_utilizado": False}
        with patch("platform_core.qualitative_corpus._read_verified_page",
                   side_effect=CorpusUnavailableError("falha após publicação")) as verify, patch(
                   "platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
                   side_effect=extract):
            _run_added_documents_job(*args)
        self.assertEqual(verify.call_count, 1)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        self.assertEqual(analysis.status, "concluida")
        self.assertEqual(analysis.document_count, 1)
        self.assertEqual(load_qualitative_manifest(analysis), old_manifest)
        self.assertTrue(is_qualitative_corpus_ready(analysis))
        self.assertEqual(len(list((analysis_dir(analysis.id) / "documents").glob("*.pdf"))), 1)

    def test_project_creation_routes_permission_and_sidebar(self):
        self.assertEqual(self.client.get("/analise-qualitativa").status_code, 403)
        self.assertEqual(self.client.get("/projetos/qualitativos/novo").status_code, 403)
        self.assertIn('platform-nav-unavailable', self.client.get("/").get_data(as_text=True))
        self.allow()
        page = self.client.get("/projetos/qualitativos/novo")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/projetos/qualitativos"', page.get_data(as_text=True))
        token = csrf_from(page)
        result = self.client.post("/projetos/qualitativos/novo", data={
            "csrf_token": token, "name": "Nova pesquisa", "description": "Corpus",
        })
        self.assertEqual(result.status_code, 302)
        created = db.session.scalar(select(Project).where(Project.name == "Nova pesquisa"))
        self.assertEqual(created.scrape_type, QUALITATIVE)
        self.assertEqual(result.headers["Location"], f"/analise-qualitativa/projetos/{created.id}")
        self.assertEqual(db.session.scalar(select(Analysis).where(
            Analysis.project_id == created.id)).document_count, 0)
        self.assertIn("Nova pesquisa", self.client.get("/projetos/qualitativos").get_data(as_text=True))
        self.assertNotIn("Nova pesquisa", self.client.get("/projetos/livres").get_data(as_text=True))
        self.assertNotIn("Nova pesquisa", self.client.get("/projetos").get_data(as_text=True))

    def test_foreign_project_and_wrong_type_rejected_for_new_base(self):
        self.allow()
        foreign = create_user("Outra", "outra-qual@example.org")
        foreign_project = self.project(foreign)
        self.assertEqual(self.client.get(f"/analise-qualitativa/projetos/{foreign_project.id}/bases/nova").status_code, 404)
        regular = Project(owner_user_id=self.user_id, name="Livre", scrape_type="free")
        db.session.add(regular)
        db.session.commit()
        self.assertEqual(self.client.get(f"/analise-qualitativa/projetos/{regular.id}/bases/nova").status_code, 404)

    def test_multi_pdf_upload_and_persistent_manifest(self):
        self.allow()
        project = self.project()
        token = csrf_from(self.client.get(f"/analise-qualitativa/projetos/{project.id}/bases/nova"))
        with patch("app.EXECUTOR_ANALISES.submit") as submit:
            response = self.client.post(f"/analise-qualitativa/projetos/{project.id}/bases/nova", data={
                "csrf_token": token, "name": "Base de entrevistas", "qualitative_strategy": "hybrid",
                "documents": [(io.BytesIO(pdf_bytes(2)), "../../primeiro.pdf"),
                              (io.BytesIO(pdf_bytes(1)), "segundo.pdf")],
            }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        self.assertEqual(analysis.status, "processando")
        self.assertEqual(analysis.document_count, 2)
        self.assertEqual(analysis.parameters_json["qualitative_strategy"], "hybrid")
        self.assertEqual(len(db.session.scalars(select(AnalysisDocument).where(
            AnalysisDocument.analysis_id == analysis.id)).all()), 2)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}/progresso").json["status"],
                         "processando")
        self.run_job_without_ocr(analysis.id)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        self.assertEqual(analysis.status, "concluida")
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}/progresso").json["status"],
                         "concluida")
        manifest = load_qualitative_manifest(analysis)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["offset_unit"], "unicode_codepoint")
        self.assertEqual([item["page_count"] for item in manifest["documents"]], [2, 1])
        self.assertEqual(manifest["documents"][0]["original_name"], "primeiro.pdf")
        self.assertEqual(manifest["documents"][0]["stored_name"], "001-primeiro.pdf")
        for document in manifest["documents"]:
            for page in document["pages"]:
                content = (analysis_dir(analysis.id) / "qualitative_corpus" / page["file"]).read_bytes()
                self.assertEqual(page["sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(page["char_count"], len(content.decode("utf-8")))
                self.assertEqual(page["extraction_method"], "text")
        first = manifest["documents"][0]
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
                   side_effect=AssertionError("PDF reprocessado")):
            self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}").status_code, 302)
            page_url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
                        f"{first['document_id']}/paginas/2")
            self.assertEqual(self.client.get(page_url).status_code, 200)
            payload = self.client.get(page_url + "/dados").get_json()
            self.assertEqual(payload["text"], read_qualitative_page(analysis, first["document_id"], 2)["text"])
            self.assertEqual(payload["sha256"], first["pages"][1]["sha256"])
        self.assertIn("Acervo 1", self.client.get(
            f"/analise-qualitativa/projetos/{project.id}/bases").get_data(as_text=True))

    def test_background_executor_completes_without_request_context(self):
        from app import EXECUTOR_ANALISES

        self.allow()
        project = self.project()

        def extract(path):
            with pymupdf.open(path) as document:
                for number, page in enumerate(document, start=1):
                    yield {"pagina_pdf": number, "texto": page.get_text("text"),
                           "ocr_utilizado": False}

        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas", side_effect=extract):
            self.assertEqual(self.upload(project).status_code, 302)
            EXECUTOR_ANALISES.submit(lambda: None).result(timeout=30)
        db.session.expire_all()
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        self.assertEqual(analysis.status, "concluida")
        self.assertTrue(is_qualitative_corpus_ready(analysis))

    def test_bad_pdf_rejected_before_analysis_creation(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            response = self.client.post(f"/analise-qualitativa/projetos/{project.id}/bases/nova", data={
                "csrf_token": csrf_from(self.client.get(f"/analise-qualitativa/projetos/{project.id}/bases/nova")),
                "name": "Base", "qualitative_strategy": "inductive",
                "documents": [(io.BytesIO(b"not a PDF"), "enganoso.pdf")],
            }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(db.session.scalar(select(Analysis).where(Analysis.project_id == project.id)))

    def test_pdf_with_wrong_extension_and_missing_csrf_are_rejected(self):
        self.allow()
        project = self.project()
        url = f"/analise-qualitativa/projetos/{project.id}/bases/nova"
        response = self.client.post(url, data={"name": "Sem token"})
        self.assertEqual(response.status_code, 400)
        token = csrf_from(self.client.get(url))
        response = self.client.post(url, data={
            "csrf_token": token, "name": "Base", "qualitative_strategy": "deductive",
            "documents": [(io.BytesIO(pdf_bytes()), "conteudo.txt")],
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(db.session.scalar(select(Analysis).where(Analysis.project_id == project.id)))

    def test_canonical_unicode_and_integrity_checks(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.assertEqual(self.upload(project, pages=1).status_code, 302)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        expected = "Ação\r\nçaí\r😀 fim\n"
        canonical = "Ação\nçaí\n😀 fim\n"
        self.assertEqual(canonicalize_page(expected), canonical)
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas", return_value=iter([{
            "pagina_pdf": 1, "texto": expected, "ocr_utilizado": True,
        }])):
            _run_corpus_job(self.app, analysis.id)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        document = db.session.scalar(select(AnalysisDocument).where(AnalysisDocument.analysis_id == analysis.id))
        page = read_qualitative_page(analysis, document.id, 1)
        self.assertEqual(page["text"], canonical)
        self.assertEqual(page["char_count"], len(canonical))
        self.assertEqual(page["sha256"], hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(page["extraction_method"], "ocr")
        self.assertEqual((analysis_dir(analysis.id) / "qualitative_corpus" / document.id /
                          "pages" / "000001.txt").read_bytes(), canonical.encode("utf-8"))
        self.assertTrue(is_qualitative_corpus_ready(analysis))
        path = analysis_dir(analysis.id) / "qualitative_corpus" / document.id / "pages" / "000001.txt"
        path.write_bytes(b"alterado")
        self.assertFalse(is_qualitative_corpus_ready(analysis))
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}").status_code, 302)
        self.assertEqual(self.client.get(
            f"/analise-qualitativa/bases/{analysis.id}/documentos/{document.id}/paginas/1"
        ).status_code, 409)

    def test_missing_page_file_is_not_a_ready_corpus(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.upload(project)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        self.run_job_without_ocr(analysis.id)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        manifest = load_qualitative_manifest(analysis)
        page = analysis_dir(analysis.id) / "qualitative_corpus" / manifest["documents"][0]["pages"][0]["file"]
        page.unlink()
        self.assertFalse(is_qualitative_corpus_ready(analysis))
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}").status_code, 302)
        self.assertEqual(self.client.get(
            f"/analise-qualitativa/bases/{analysis.id}/documentos/"
            f"{manifest['documents'][0]['document_id']}/paginas/1"
        ).status_code, 409)

    def test_manifest_and_one_page_navigation_do_not_read_other_text_files(self):
        analysis, manifest = self.prepared_base(pages=3)
        document = manifest["documents"][0]
        requested = analysis_dir(analysis.id) / "qualitative_corpus" / document["pages"][1]["file"]
        reads = []
        original_read = Path.read_bytes

        def count_text_reads(path):
            if path.suffix == ".txt":
                reads.append(path)
            return original_read(path)

        with patch.object(Path, "read_bytes", count_text_reads):
            self.assertEqual(load_qualitative_manifest(analysis)["analysis_id"], analysis.id)
            self.assertEqual(reads, [])
            read_qualitative_page(analysis, document["document_id"], 2)
            self.assertEqual(reads, [requested])
            reads.clear()
            page_url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
                        f"{document['document_id']}/paginas/2")
            self.assertEqual(self.client.get(page_url).status_code, 200)
            self.assertEqual(reads, [requested])  # manifest passado à leitura; sem dupla validação
            reads.clear()
            self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}").status_code, 302)
            self.assertEqual(reads, [])
            validate_qualitative_corpus(analysis)
            self.assertEqual(len(reads), 3)  # diagnóstico profundo é explícito

    def test_large_simulated_corpus_opens_only_requested_page(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.assertEqual(self.upload(project, pages=1).status_code, 302)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))

        def many_pages(_source):
            for number in range(1, 251):
                yield {"pagina_pdf": number, "texto": f"Página {number} — ação 😀",
                       "ocr_utilizado": False}

        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas", side_effect=many_pages):
            _run_corpus_job(self.app, analysis.id)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        document = load_qualitative_manifest(analysis)["documents"][0]
        reads = []
        original_read = Path.read_bytes

        def count_text_reads(path):
            if path.suffix == ".txt":
                reads.append(path)
            return original_read(path)

        with patch.object(Path, "read_bytes", count_text_reads), patch(
            "platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
            side_effect=AssertionError("PDF reprocessado"),
        ):
            data = read_qualitative_page(analysis, document["document_id"], 125)
        self.assertEqual(data["text"], "Página 125 — ação 😀")
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].name, "000125.txt")

    def test_requested_page_hash_and_count_corruption_return_conflict(self):
        analysis, manifest = self.prepared_base()
        document = manifest["documents"][0]
        prefix = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
                  f"{document['document_id']}/paginas")
        second = analysis_dir(analysis.id) / "qualitative_corpus" / document["pages"][1]["file"]
        second.write_bytes(b"texto alterado")
        self.assertEqual(self.client.get(f"{prefix}/1").status_code, 200)
        self.assertEqual(self.client.get(f"{prefix}/2").status_code, 409)
        self.assertEqual(self.client.get(f"{prefix}/2/dados").status_code, 409)
        with self.assertRaises(CorpusUnavailableError):
            read_qualitative_page(analysis, document["document_id"], 2)

        manifest["documents"][0]["pages"][0]["char_count"] += 1
        qualitative_manifest_path(analysis.id).write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.client.get(f"{prefix}/1").status_code, 409)
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}").status_code, 302)

    def test_invalid_manifest_and_invalid_utf8_are_conflicts_not_not_found(self):
        analysis, manifest = self.prepared_base()
        document = manifest["documents"][0]
        url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
               f"{document['document_id']}/paginas/1")
        manifest_path = qualitative_manifest_path(analysis.id)
        manifest["schema_version"] = 999
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{analysis.id}").status_code, 409)
        self.assertEqual(self.client.get(url).status_code, 409)
        manifest["schema_version"] = 1
        manifest["documents"] = None
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.client.get(url).status_code, 409)
        manifest["documents"] = [document]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        page = analysis_dir(analysis.id) / "qualitative_corpus" / document["pages"][0]["file"]
        page.write_bytes(b"\xff")
        self.assertEqual(self.client.get(url).status_code, 409)
        self.assertEqual(self.client.get(url + "/dados").status_code, 409)

    def test_missing_resource_is_not_confused_with_inconsistent_corpus(self):
        analysis, manifest = self.prepared_base()
        document = manifest["documents"][0]
        with self.assertRaises(QualitativePageNotFoundError):
            read_qualitative_page(analysis, document["document_id"], 99)
        prefix = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
                  f"{document['document_id']}")
        self.assertEqual(self.client.get(f"{prefix}/paginas/99").status_code, 404)
        self.assertEqual(self.client.get(f"{prefix}/ir?page_number=99").status_code, 404)
        self.assertEqual(self.client.get(f"{prefix}/ir?page_number=1").status_code, 302)

    def test_post_publication_integrity_failure_does_not_complete_base(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.upload(project)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        with patch("platform_core.qualitative_routes.validate_qualitative_corpus",
                   side_effect=CorpusUnavailableError("corrupção simulada")), patch(
            "platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
            side_effect=lambda _path: iter([{"pagina_pdf": 1, "texto": "Texto",
                                            "ocr_utilizado": False}]),
        ):
            _run_corpus_job(self.app, analysis.id)
        db.session.expire_all()
        self.assertEqual(db.session.get(Analysis, analysis.id).status, "erro")
        self.assertFalse((analysis_dir(analysis.id) / "qualitative_corpus").exists())
        self.assertTrue((analysis_dir(analysis.id) / "documents").is_dir())

    def test_foreign_document_page_and_foreign_base_denied(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.upload(project)
        self.historical_workspace(project)
        analyses = db.session.scalars(select(Analysis).where(Analysis.project_id == project.id)).all()
        for analysis in analyses:
            self.run_job_without_ocr(analysis.id)
        db.session.expire_all()
        first, second = analyses
        own_doc = db.session.scalar(select(AnalysisDocument).where(AnalysisDocument.analysis_id == first.id))
        foreign_doc = db.session.scalar(select(AnalysisDocument).where(AnalysisDocument.analysis_id == second.id))
        prefix = f"/analise-qualitativa/bases/{first.id}/documentos"
        self.assertEqual(self.client.get(f"{prefix}/{foreign_doc.id}/paginas/1").status_code, 404)
        self.assertEqual(self.client.get(f"{prefix}/{own_doc.id}/paginas/99").status_code, 404)
        other_user = create_user("Terceiro", "terceiro-qual@example.org")
        self.allow(other_user)
        logout_token = csrf_from(self.client.get("/"))
        self.assertEqual(self.client.post("/logout", data={"csrf_token": logout_token}).status_code, 302)
        login(self.client, "terceiro-qual@example.org")
        self.assertEqual(self.client.get(f"/analise-qualitativa/bases/{first.id}").status_code, 404)
        self.assertEqual(self.client.get(f"{prefix}/{own_doc.id}/paginas/1/dados").status_code, 404)

    def test_extraction_failure_keeps_originals_and_error_status(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.upload(project)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        def partial(_path):
            yield {"pagina_pdf": 1, "texto": "Página inicial", "ocr_utilizado": False}
            raise RuntimeError("falha após a primeira página")

        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas", side_effect=partial):
            _run_corpus_job(self.app, analysis.id)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        self.assertEqual(analysis.status, "erro")
        self.assertFalse((analysis_dir(analysis.id) / "qualitative_corpus").exists())
        self.assertFalse(list(analysis_dir(analysis.id).glob(".qualitative_corpus.*.tmp")))
        self.assertEqual(len(list((analysis_dir(analysis.id) / "documents").glob("*.pdf"))), 1)

    def test_delete_base_removes_corpus_and_not_other_base(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.upload(project)
        self.historical_workspace(project)
        analyses = db.session.scalars(select(Analysis).where(Analysis.project_id == project.id)).all()
        for analysis in analyses:
            self.run_job_without_ocr(analysis.id)
        db.session.expire_all()
        first, second = analyses
        self.assertTrue(is_qualitative_corpus_ready(first))
        delete_analysis(first)
        self.assertFalse(analysis_dir(first.id).exists())
        self.assertTrue(is_qualitative_corpus_ready(second))

    def test_delete_archived_project_removes_its_prepared_corpus(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.upload(project)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        self.run_job_without_ocr(analysis.id)
        db.session.expire_all()
        self.assertTrue(is_qualitative_corpus_ready(db.session.get(Analysis, analysis.id)))
        archive(db.session.get(Project, project.id), self.user)
        self.assertEqual(delete_archived([db.session.get(Project, project.id)], self.user, "deletar"), 1)
        self.assertIsNone(db.session.get(Analysis, analysis.id))
        self.assertFalse(analysis_dir(analysis.id).exists())

    def test_original_pdf_is_authorized_and_supports_range(self):
        analysis, manifest = self.prepared_base()
        document = manifest["documents"][0]
        url = f"/analise-qualitativa/bases/{analysis.id}/documentos/{document['document_id']}/pdf"
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
                   side_effect=AssertionError("PDF reprocessado")):
            response = self.client.get(url, headers={"Range": "bytes=0-31"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertEqual(response.data[:4], b"%PDF")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        response.close()  # Windows mantém send_file aberto até fechar a resposta
        self.assertEqual(self.client.get(url + "/../outro.pdf").status_code, 404)

    def test_pdf_of_another_base_is_not_exposed(self):
        analysis, manifest = self.prepared_base()
        project = db.session.get(Project, analysis.project_id)
        self.historical_workspace(project)
        other = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id,
                                                    Analysis.id != analysis.id))
        foreign_document = db.session.scalar(select(AnalysisDocument).where(
            AnalysisDocument.analysis_id == other.id))
        url = f"/analise-qualitativa/bases/{analysis.id}/documentos/{foreign_document.id}/pdf"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_native_layout_is_exact_and_old_corpus_without_layout_opens(self):
        analysis, manifest = self.prepared_base()
        document = manifest["documents"][0]
        page = document["pages"][0]
        self.assertTrue(page["layout_available"])
        layout = read_qualitative_layout(analysis, document["document_id"], 1, manifest=manifest)
        self.assertTrue(layout["layout_available"])
        text = read_qualitative_page(analysis, document["document_id"], 1)["text"]
        for item in layout["items"]:
            self.assertEqual(item["text"], text[item["start"]:item["end"]])
            self.assertTrue(0 <= item["start"] < item["end"] <= len(text))
            self.assertTrue(0 <= item["bbox"][0] < item["bbox"][2] <= layout["width"] + 1)
        url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
               f"{document['document_id']}/paginas/1/layout")
        self.assertTrue(self.client.get(url).json["layout_available"])
        page.pop("layout_available")  # formato da Etapa 2 permanece válido
        qualitative_manifest_path(analysis.id).write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.client.get(url).json, {"layout_available": False, "items": []})
        self.assertEqual(self.client.get(url.removesuffix("/layout")).status_code, 200)

    def test_real_native_extraction_produces_verified_search_geometry(self):
        self.allow()
        project = self.project()
        pdf = pymupdf.open()
        page = pdf.new_page()
        for number in range(8):
            page.insert_text((72, 72 + number * 20),
                             f"Documento de pesquisa social, linha {number + 1}: análise documental.")
        payload = pdf.tobytes()
        pdf.close()
        url = f"/analise-qualitativa/projetos/{project.id}/bases/nova"
        with patch("app.EXECUTOR_ANALISES.submit"):
            response = self.client.post(url, data={
                "csrf_token": csrf_from(self.client.get(url)), "name": "Texto nativo",
                "qualitative_strategy": "inductive", "documents": [(io.BytesIO(payload), "nativo.pdf")],
            }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        _run_corpus_job(self.app, analysis.id)  # extrator real, sem OCR nesta página
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        manifest = load_qualitative_manifest(analysis)
        document = manifest["documents"][0]
        self.assertEqual(document["pages"][0]["extraction_method"], "text")
        self.assertTrue(document["pages"][0]["layout_available"])
        layout = read_qualitative_layout(analysis, document["document_id"], 1)
        self.assertTrue(layout["layout_available"])
        search = search_qualitative_document(analysis, document["document_id"], "Documento")
        first = search["results"][0]
        self.assertEqual(first["page_text_hash"], layout["page_text_hash"])
        self.assertTrue(any(item["start"] <= first["start_offset"] < item["end"]
                            for item in layout["items"]))

    def test_ocr_page_has_search_but_no_invented_geometry(self):
        self.allow()
        project = self.project()
        with patch("app.EXECUTOR_ANALISES.submit"):
            self.upload(project, pages=1)
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        canonical = "Ação\nçaí\n😀 fim\n"
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas", return_value=iter([{
            "pagina_pdf": 1, "texto": "Ação\r\nçaí\r😀 fim\n", "ocr_utilizado": True,
        }])):
            _run_corpus_job(self.app, analysis.id)
        db.session.expire_all()
        analysis = db.session.get(Analysis, analysis.id)
        document = load_qualitative_manifest(analysis)["documents"][0]
        self.assertFalse(document["pages"][0]["layout_available"])
        self.assertFalse(read_qualitative_layout(analysis, document["document_id"], 1)["layout_available"])
        url = f"/analise-qualitativa/bases/{analysis.id}/documentos/{document['document_id']}/buscar"
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
                   side_effect=AssertionError("OCR reexecutado")):
            result = self.client.get(url, query_string={"q": "AÇÃO"}).json
            emoji = self.client.get(url, query_string={"q": "😀"}).json
            strict = self.client.get(url, query_string={"q": "AÇÃO", "case": "1"}).json
        self.assertEqual(read_qualitative_page(analysis, document["document_id"], 1)["text"], canonical)
        self.assertEqual(result["results"][0]["start_offset"], 0)
        self.assertEqual(result["results"][0]["end_offset"], 4)
        self.assertEqual(emoji["results"][0]["start_offset"], canonical.index("😀"))
        self.assertEqual(emoji["results"][0]["end_offset"], canonical.index("😀") + 1)
        self.assertEqual(strict["total"], 0)
        self.assertEqual(self.client.get(url, query_string={"q": "A..o", "grep": "1"}).json["total"], 1)
        invalid = self.client.get(url, query_string={"q": "(", "grep": "1"})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("inválida", invalid.json["error"])

    def test_search_is_scoped_to_current_document_and_limits_pathological_regex(self):
        analysis, manifest = self.prepared_base(pages=3)
        document = manifest["documents"][0]
        url = f"/analise-qualitativa/bases/{analysis.id}/documentos/{document['document_id']}/buscar"
        result = self.client.get(url, query_string={"q": "pagina", "case": "1"}).json
        self.assertGreater(result["total"], 0)
        self.assertEqual({item["page_number"] for item in result["results"]}, {1, 2, 3})
        self.assertEqual(result["offset_unit"], "unicode_codepoint")
        self.assertEqual(self.client.get(url, query_string={"q": "x" * 201}).status_code, 400)
        with patch("platform_core.qualitative_search.SEARCH_TIMEOUT_SECONDS", 0.001), patch(
            "platform_core.qualitative_search.read_qualitative_page",
            return_value={"text": "a" * 20000 + "!", "sha256": "0" * 64},
        ):
            with self.assertRaises(QualitativeSearchError):
                search_qualitative_document(analysis, document["document_id"], "(a+)+$", grep=True)

    def test_viewer_uses_local_pdfjs_and_progressive_rendering(self):
        analysis, manifest = self.prepared_base(pages=2)
        document = manifest["documents"][0]
        url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
               f"{document['document_id']}/paginas/1")
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
                   side_effect=AssertionError("PDF reprocessado")):
            html = self.client.get(url).get_data(as_text=True)
        self.assertIn('data-qualitative-viewer', html)
        self.assertIn('data-pdf-url=', html)
        self.assertIn('data-zoom', html)
        self.assertIn('Usar ReGex', html)
        self.assertNotIn('Usar GREP', html)
        self.assertIn('Códigos (0)', html)
        self.assertIn('Margem analítica', html)
        top = html.split('class="platform-panel platform-qualitative-top"', 1)[1].split(
            'class="platform-qualitative-bottom"', 1)[0]
        bottom = html.split('class="platform-qualitative-bottom"', 1)[1]
        self.assertIn('id="qualitative-explorer"', top)
        self.assertIn('data-search-form', top)
        self.assertNotIn('data-pdf-scroll', top)
        self.assertIn('data-pdf-scroll', bottom)
        self.assertIn('data-zoom', bottom)
        self.assertIn('platform-qualitative-margin', bottom)
        self.assertLess(bottom.index('platform-qualitative-document'), bottom.index('platform-qualitative-margin'))
        self.assertIn('static/js/qualitative_viewer.js', html)
        viewer_js = (Path(__file__).resolve().parents[1] / "static/js/qualitative_viewer.js").read_text(encoding="utf-8")
        self.assertIn('IntersectionObserver', viewer_js)
        self.assertIn('rendering < 2', viewer_js)
        self.assertIn('releaseDistant', viewer_js)

    def test_focus_mode_reuses_reader_controls_without_internal_explorer_scroll(self):
        analysis, manifest = self.prepared_base(pages=2)
        document = manifest["documents"][0]
        url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
               f"{document['document_id']}/paginas/1")
        with patch("platform_core.qualitative_corpus.pdf_extractor.extrair_paginas",
                   side_effect=AssertionError("PDF reprocessado")):
            html = self.client.get(url).get_data(as_text=True)
        self.assertEqual(html.count('data-focus-panel>'), 1)
        self.assertEqual(html.count('data-focus-toggle'), 1)
        self.assertEqual(html.count('data-search-form'), 1)
        self.assertEqual(html.count('id="qualitative-explorer"'), 1)
        self.assertIn('data-focus-panel-toggle aria-controls="qualitative-controls"', html)
        self.assertIn('data-focus-handle', html)
        self.assertIn(f'title="{document["original_name"]}"', html)
        controls = html.split('id="qualitative-controls"', 1)[1].split('class="platform-qualitative-bottom"', 1)[0]
        self.assertLess(controls.index('data-search-form'), controls.index('id="qualitative-explorer"'))
        css = (Path(__file__).resolve().parents[1] / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn('align-items: stretch', css)
        self.assertIn('text-overflow: ellipsis', css)
        self.assertIn('.platform-qualitative-focus .platform-qualitative-search { order: 0', css)
        self.assertIn('.platform-qualitative-focus .platform-qualitative-explorer { order: 1', css)
        self.assertIn('.platform-qualitative-top.is-minimized .platform-qualitative-controls { display: none', css)
        self.assertNotIn('.platform-qualitative-explorer { overflow-y:', css)
        viewer_js = (Path(__file__).resolve().parents[1] / "static/js/qualitative_viewer.js").read_text(encoding="utf-8")
        self.assertEqual(viewer_js.count("focusToggle.addEventListener('click'"), 1)
        self.assertIn('setPointerCapture', viewer_js)
        self.assertIn('movePanelTo', viewer_js)
        self.assertIn('rerenderVisiblePages', viewer_js)

    def test_explorer_menus_are_popovers_shared_by_standard_and_focus_modes(self):
        analysis, manifest = self.prepared_base(pages=2)
        document = manifest["documents"][0]
        url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
               f"{document['document_id']}/paginas/1")
        html = self.client.get(url).get_data(as_text=True)
        explorer = html.split('id="qualitative-explorer"', 1)[1].split('class="platform-qualitative-bottom"', 1)[0]
        for kind in ("documents", "codes", "memos"):
            menu_id = f"qualitative-{kind}-menu"
            self.assertIn(f'popovertarget="{menu_id}"', explorer)
            self.assertIn(f'id="{menu_id}" popover="auto"', explorer)
            self.assertIn(f'aria-controls="{menu_id}" aria-expanded="false"', explorer)
        self.assertEqual(explorer.count('data-explorer-trigger'), 3)
        self.assertEqual(explorer.count('data-explorer-popover'), 3)
        self.assertIn(f'aria-current="page"', explorer)
        self.assertIn(f'title="{document["original_name"]}"', explorer)
        self.assertIn('Nenhum código criado.', explorer)
        self.assertIn('Nenhum memo criado.', explorer)
        css = (Path(__file__).resolve().parents[1] / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn('.platform-qualitative-explorer-popover { position: fixed;', css)
        self.assertIn('max-height: min(22rem, calc(100dvh - 1rem)); overflow-y: auto', css)
        self.assertNotIn('.platform-qualitative-explorer { overflow-y:', css)
        js = (Path(__file__).resolve().parents[1] / "static/js/qualitative_viewer.js").read_text(encoding="utf-8")
        self.assertIn("popover.addEventListener('toggle'", js)
        self.assertIn('closeExplorerPopovers();', js)
        self.assertIn('repositionExplorerPopovers();', js)

    def test_pdf_wheel_zoom_uses_existing_control_only_with_ctrl(self):
        js = (Path(__file__).resolve().parents[1] / "static/js/qualitative_viewer.js").read_text(encoding="utf-8")
        wheel = js.split("scroll.addEventListener('wheel'", 1)[1].split('}, { passive: false });', 1)[0]
        self.assertIn('if (!event.ctrlKey || !documentPdf || !event.deltaY) return;', wheel)
        self.assertIn('event.preventDefault();', wheel)
        self.assertIn('event.deltaY < 0 ? 1 : -1', wheel)
        self.assertIn('zoom.options.length - 1', wheel)
        self.assertIn('zoom.selectedIndex = index;', wheel)
        self.assertIn('setTimeout(rerenderVisiblePages, 90)', wheel)
        self.assertEqual(js.count("scroll.addEventListener('wheel'"), 1)

    def test_search_controls_are_compact_without_changing_regex_protocol(self):
        analysis, manifest = self.prepared_base(pages=2)
        document = manifest["documents"][0]
        url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
               f"{document['document_id']}/paginas/1")
        html = self.client.get(url).get_data(as_text=True)
        self.assertIn('class="platform-qualitative-search-options"', html)
        self.assertIn('name="grep"> Usar ReGex', html)
        self.assertIn('name="case"> Distinguir maiúsculas e minúsculas', html)
        self.assertNotIn('Usar GREP', html)
        css = (Path(__file__).resolve().parents[1] / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn('.platform-qualitative-search-options { display: flex; flex-wrap: wrap', css)
        self.assertIn('.platform-qualitative-search-options label { display: inline-flex', css)
        self.assertIn('white-space: nowrap; cursor: pointer', css)
        self.assertIn('.platform-qualitative-search-results [data-result-prev]', css)
        self.assertIn('.platform-qualitative-search-results [data-result-count]', css)
        viewer_js = (Path(__file__).resolve().parents[1] / "static/js/qualitative_viewer.js").read_text(encoding="utf-8")
        self.assertIn("grep: form.has('grep') ? '1' : '0'", viewer_js)
        search_url = (f"/analise-qualitativa/bases/{analysis.id}/documentos/"
                      f"{document['document_id']}/buscar")
        self.assertGreater(self.client.get(search_url, query_string={"q": "Documento de teste",
                                                                  "grep": "1"}).json["total"], 0)


if __name__ == "__main__":
    unittest.main()
