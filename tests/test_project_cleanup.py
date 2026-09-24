"""Limpeza dos projetos legados excluídos e proteção do fluxo permanente atual."""

import unittest
from uuid import uuid4

from historico_racial.routes import JOBS_LOCK, RESULTADOS_HR
from platform_helpers import create_project, create_user, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AuditLog, Project, ProjectLibrary, ProjectVocabularyVersion, utcnow
from platform_core.project_lifecycle import ProjectActionError, purge_legacy_deleted
from platform_core.services import record_audit


class ProjectCleanupTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.admin = create_user("Admin", "admin@example.org", role="admin", plan="researcher")
        login(self.client, self.admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_legacy_deleted_projects_are_hidden_and_physically_purged_with_audit(self):
        ids = [create_project(self.client, name=name) for name in ("Legado um", "Legado dois", "Legado três")]
        projects = [db.session.get(Project, project_id) for project_id in ids]
        for project in projects:
            project.status = "deleted"
            project.deleted_at = utcnow()
            record_audit(self.admin, "project_status_changed", "project", project.id,
                         {"status": "active"}, {"status": "deleted"})
        db.session.commit()
        html = self.client.get("/admin/projetos").get_data(as_text=True)
        for project in projects:
            self.assertNotIn(project.name, html)
            self.assertEqual(self.client.get(f"/admin/projetos/{project.id}").status_code, 404)
        self.assertEqual(purge_legacy_deleted(projects, None, "deletar"), 3)
        for project_id, name in zip(ids, ("Legado um", "Legado dois", "Legado três")):
            self.assertIsNone(db.session.get(Project, project_id))
            self.assertIsNone(db.session.get(ProjectLibrary, (project_id, "relacoes_raciais")))
            self.assertEqual(db.session.query(ProjectVocabularyVersion).filter_by(project_id=project_id).count(), 0)
            actions = db.session.query(AuditLog).filter_by(target_type="project", target_id=project_id).all()
            self.assertEqual({row.action for row in actions},
                             {"project_status_changed", "project_permanently_deleted"})
            deletion = next(row for row in actions if row.action == "project_permanently_deleted")
            self.assertIsNone(deletion.admin_user_id)
            self.assertEqual(deletion.before_json["name"], name)
            self.assertEqual(deletion.before_json["owner_user_id"], self.admin.id)

    def test_legacy_cleanup_rejects_operational_projects(self):
        project_id = create_project(self.client, name="Ativo preservado")
        project = db.session.get(Project, project_id)
        with self.assertRaises(ProjectActionError):
            purge_legacy_deleted([project], self.admin, "deletar")
        with self.assertRaises(ProjectActionError):
            purge_legacy_deleted([project], self.admin, "incorreto")
        self.assertIsNotNone(db.session.get(Project, project_id))

    def test_project_info_reuses_existing_modal_pattern(self):
        project_id = create_project(self.client, name="Pesquisa informativa")
        html = self.client.get(f"/analise-documental/projetos/{project_id}").get_data(as_text=True)
        legacy = self.client.get("/raspagem-livre").get_data(as_text=True)
        self.assertIn('class="btn btn-outline-primary info-icon-button"', html)
        self.assertIn('aria-label="Como funciona" title="Como funciona"', html)
        self.assertIn('data-bs-target="#modal-sobre-historico-racial"', html)
        self.assertIn('class="modal-dialog modal-dialog-scrollable modal-lg"', html)
        for term in ("Preparação do projeto", "Documentos e leitura dos PDFs",
                     "Bibliotecas, grupos, entidades e variantes", "análise lexical",
                     "busca semântica", "Híbrido", "limiar", "Resultados e contexto",
                     "CODIFICACAO", "COOCORRENCIAS", "Rastreabilidade metodológica",
                     "Limites da interpretação automática"):
            self.assertIn(term, html)
        self.assertIn('class="modal-body modal-help" id="descricao-modal-sobre-historico-racial"', html)
        self.assertIn('class="modal-help__methodological-note"', html)
        self.assertIn('o processamento atual não calcula nem preenche pares automaticamente', html)
        self.assertIn('data-bs-target="#modal-como-funciona"', legacy)
        self.assertIn('class="btn btn-outline-primary info-icon-button"', legacy)

    def test_results_info_explains_the_same_project_flow(self):
        project_id = create_project(self.client, name="Pesquisa informativa")
        job_id = str(uuid4())
        with JOBS_LOCK:
            RESULTADOS_HR[job_id] = {
                "project_id": project_id, "owner_user_id": self.admin.id,
                "library_names": ["Relações raciais"], "metodo_analise": "lexical",
                "limiar_semantico": None, "vocabulario_version": "v1.0",
                "vocabulario_hash": "a" * 64, "total_pdfs": 0,
                "total_ocorrencias": 0, "entidades_distintas": 0,
                "erros": [], "ocorrencias": [],
            }
        try:
            response = self.client.get(f"/analise-documental/projetos/{project_id}/resultado/{job_id}")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('aria-label="Como funciona" title="Como funciona"', html)
            self.assertIn('class="modal-body modal-help" id="descricao-modal-sobre-historico-racial"', html)
            for term in ("Preparação do projeto", "busca semântica", "Método de análise e limiar",
                         "COOCORRENCIAS", "Limites da interpretação automática"):
                self.assertIn(term, html)
        finally:
            with JOBS_LOCK:
                RESULTADOS_HR.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
