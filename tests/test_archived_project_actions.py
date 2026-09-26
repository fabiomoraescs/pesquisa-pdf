"""Regressões das ações de arquivados, inclusive ambientes qualitativos vazios."""

import unittest
from pathlib import Path
import shutil
import subprocess
from sqlalchemy import select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.analyses import analysis_dir
from platform_core.extensions import db
from platform_core.models import Analysis, AnalysisDocument, Project, QualitativeCode, QualitativeMemo, UserToolOverride
from platform_core.qualitative_routes import ensure_project_workspace
from platform_core.scraping_types import QUALITATIVE_TOOL


class ArchivedProjectActionsTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.owner = create_user()
        db.session.add(UserToolOverride(user_id=self.owner.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def project(self, name, kind="qualitative"):
        project = Project(name=name, owner_user_id=self.owner.id, scrape_type=kind)
        db.session.add(project)
        db.session.commit()
        if kind == "qualitative":
            ensure_project_workspace(project)
        project.status = "archived"
        db.session.commit()
        return project

    def token(self):
        return csrf_from(self.client.get("/projetos/qualitativos/arquivados"))

    def test_empty_qualitative_workspace_is_not_a_running_job_for_individual_deletion(self):
        project = self.project("Vazio arquivado")
        identifier = project.id
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == identifier))
        # O fluxo real cria o workspace com este estado antes de qualquer upload/job.
        self.assertEqual((analysis.status, analysis.document_count), ("processando", 0))
        directory = analysis_dir(analysis.id)
        db.session.add_all([
            QualitativeCode(analysis_id=analysis.id, name="Código inicial", created_by_user_id=self.owner.id),
            QualitativeMemo(analysis_id=analysis.id, text="Memo geral", created_by_user_id=self.owner.id),
        ])
        db.session.commit()
        response = self.client.post(f"/projetos/{identifier}/excluir", data={
            "csrf_token": self.token(), "confirmation": "deletar"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(db.session.get(Project, identifier))
        self.assertFalse(directory.exists())
        self.assertEqual(db.session.query(QualitativeCode).count(), 0)
        self.assertEqual(db.session.query(QualitativeMemo).count(), 0)
        self.assertNotIn(project.name, response.get_data(as_text=True))

    def test_batch_removes_only_selected_empty_workspaces(self):
        projects = [self.project(name) for name in ("Selecionado A", "Selecionado B", "Preservado")]
        selected = [p.id for p in projects[:2]]
        token = self.token()
        confirm = self.client.post("/projetos/arquivados/excluir/confirmar", data={
            "csrf_token": token, "project_ids": selected})
        self.assertEqual(confirm.status_code, 200)
        self.assertIn('href="/projetos/qualitativos/arquivados"', confirm.get_data(as_text=True))
        response = self.client.post("/projetos/arquivados/excluir", data={
            "csrf_token": token, "project_ids": selected, "confirmation": "deletar"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        for identifier in selected:
            self.assertIsNone(db.session.get(Project, identifier))
        self.assertIsNotNone(db.session.get(Project, projects[2].id))
        self.assertIn("Preservado", response.get_data(as_text=True))

    def test_initial_and_incremental_upload_locks_still_block_deletion(self):
        project = self.project("Upload protegido")
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        lock = analysis_dir(analysis.id) / ".qualitative_upload.lock"
        lock.touch()
        for status in ("processando", "concluida"):
            with self.subTest(status=status):
                analysis.status = status
                db.session.commit()
                response = self.client.post(f"/projetos/{project.id}/excluir", data={
                    "csrf_token": self.token(), "confirmation": "deletar"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("Aguarde", response.get_data(as_text=True))
                self.assertTrue(lock.is_file())
                self.assertIsNotNone(db.session.get(Project, project.id))

    def test_batch_rejects_fewer_than_two_distinct_projects_at_both_endpoints(self):
        project = self.project("Não excluir sem seleção em lote")
        token = self.token()
        for endpoint in ("/projetos/arquivados/excluir/confirmar", "/projetos/arquivados/excluir"):
            for selection in ([], [project.id], [project.id, project.id]):
                with self.subTest(endpoint=endpoint, selection=selection):
                    response = self.client.post(endpoint, data={
                        "csrf_token": token, "project_ids": selection, "confirmation": "deletar"})
                    self.assertEqual(response.status_code, 400)
                    self.assertIsNotNone(db.session.get(Project, project.id))

    def test_all_types_keep_header_controls_csrf_and_owner_checks(self):
        for kind, path, label in (
            ("free", "/projetos/livres/arquivados", "Busca por termos"),
            ("systematic", "/projetos/arquivados", "Busca estruturada"),
            ("qualitative", "/projetos/qualitativos/arquivados", "Análise quali-dados"),
        ):
            with self.subTest(kind=kind):
                project = self.project(f"Arquivado {kind}", kind)
                html = self.client.get(path).get_data(as_text=True)
                self.assertIn(f'<span class="platform-header-context">Projetos arquivados de {label}</span>', html)
                body = html.split("<main ", 1)[1].split("</main>", 1)[0]
                self.assertNotIn("<h1", body)
                self.assertIn("Voltar aos projetos", body)
                self.assertIn('form="bulk-delete-form"', body)
                self.assertIn("Excluir permanentemente selecionados", body)
                self.assertIn('data-bulk-delete hidden disabled', body)
                endpoint = f"/projetos/{project.id}/excluir"
                self.assertEqual(self.client.post(endpoint, data={"confirmation": "deletar"}).status_code, 400)
                self.assertEqual(self.client.post("/projetos/arquivados/excluir", data={
                    "project_ids": [project.id], "confirmation": "deletar"}).status_code, 400)
                other = create_user(f"Outro {kind}", f"other-{kind}@example.org")
                project.owner_user_id = other.id
                db.session.commit()
                self.assertEqual(self.client.post(endpoint, data={"csrf_token": self.token(), "confirmation": "deletar"}).status_code, 404)
                project.owner_user_id = self.owner.id
                db.session.commit()
                response = self.client.post(f"/projetos/{project.id}/desarquivar", data={
                    "csrf_token": self.token(), "confirm": "yes"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(project.status, "active")

    def test_processing_with_document_rows_is_not_mistaken_for_empty_workspace(self):
        project = self.project("Preparo real")
        analysis = db.session.scalar(select(Analysis).where(Analysis.project_id == project.id))
        # Mesmo que o contador esteja desatualizado, os documentos reais prevalecem.
        db.session.add(AnalysisDocument(analysis_id=analysis.id, stored_name="001.pdf", original_name="Teste.pdf"))
        db.session.commit()
        response = self.client.post(f"/projetos/{project.id}/excluir", data={
            "csrf_token": self.token(), "confirmation": "deletar"})
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(db.session.get(Project, project.id))


class ArchivedSelectionFrontendTests(unittest.TestCase):
    def test_bulk_button_updates_immediately_for_zero_one_two_and_more_selections(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        root = Path(__file__).resolve().parents[1]
        script = (root / "static/js/platform.js").read_text(encoding="utf-8")
        dom = r"""
const assert = require('node:assert/strict');
const element = (attributes={}) => ({...attributes, listeners:{},
  addEventListener(name, fn) { this.listeners[name] = fn; },
  emit(name, event={}) { this.listeners[name]?.(event); }});
const inputs = Array.from({length:3}, () => element({type:'checkbox', name:'project_ids', checked:false}));
const button = element({hidden:true, disabled:true});
const form = element({elements:[{type:'hidden', name:'csrf_token'}, ...inputs, button]});
const window = element();
const document = {
  getElementById: id => id === 'bulk-delete-form' ? form : null,
  querySelector: selector => selector === '[data-bulk-delete]' ? button : null,
  querySelectorAll: () => []
};
"""
        checks = r"""
const check = visible => {
  assert.equal(button.hidden, !visible); assert.equal(button.disabled, !visible);
  let prevented = false;
  form.emit('submit', {preventDefault() { prevented = true; }});
  assert.equal(prevented, !visible);
};
check(false);
inputs[0].checked=true; inputs[0].emit('change'); check(false);
inputs[1].checked=true; inputs[1].emit('change'); check(true);
inputs[2].checked=true; inputs[2].emit('change'); check(true);
inputs[0].checked=false; inputs[0].emit('change'); check(true);
inputs[1].checked=false; inputs[1].emit('change'); check(false);
inputs[2].checked=false; inputs[2].emit('change'); check(false);
// Restauração de checkboxes pelo navegador também sincroniza o botão.
inputs[0].checked=inputs[1].checked=true; window.emit('pageshow'); check(true);
inputs[1].disabled=true; window.emit('pageshow'); check(false);
"""
        result = subprocess.run([node, "-e", dom + script + checks], cwd=root,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        css = (root / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn('[data-bulk-delete][hidden] { display: none; }', css)
