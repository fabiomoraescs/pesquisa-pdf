"""Navegação, ciclo de vida e bibliotecas oficiais sem tocar no banco real."""

import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select

from platform_helpers import create_project, create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AuditLog, Project, ProjectLibrary, ProjectVocabularyVersion, User, VocabularyLibrary
from platform_core.project_lifecycle import delete_archived
from platform_core.vocabularies import project_store
from historico_racial.routes import JOBS_LOCK, PROGRESSOS_HR


class RefinementTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def token(self, path="/projetos"):
        return csrf_from(self.client.get(path))

    def archive(self, project_id):
        return self.client.post(f"/projetos/{project_id}/arquivar", data={
            "csrf_token": self.token(), "confirm": "yes",
        })

    def test_sidebar_mobile_controls_shared_container_and_permissions(self):
        create_user()
        login(self.client)
        for path in ("/projetos", "/projetos/novo", "/projetos/arquivados", "/perfil"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn('class="app-content-container"', html)
            self.assertIn('id="platform-sidebar"', html)
            self.assertIn('id="platform-menu-toggle"', html)
            self.assertIn('aria-label="Abrir menu"', html)
            self.assertIn('Raspagem de dados', html)
            self.assertIn('Busca por termos', html)
            self.assertIn('Busca estruturada', html)
            self.assertNotIn('>Administração</a>', html)
        self.assertEqual(self.client.post("/projetos/novo", data={"name": "Sem CSRF"}).status_code, 400)
        self.assertEqual(self.client.get("/admin/bibliotecas/nova").status_code, 403)

    def test_archive_restore_preserves_vocabulary_and_isolated_list(self):
        create_user()
        login(self.client)
        project_id = create_project(self.client, "Projeto arquivável")
        other_id = create_project(self.client, "Projeto ativo")
        original_hash = project_store(project_id).capturar_ativa()["hash"]
        self.assertEqual(self.archive(project_id).status_code, 302)
        project = db.session.get(Project, project_id)
        self.assertEqual(project.status, "archived")
        self.assertIsNotNone(project.archived_at)
        self.assertNotIn("Projeto arquivável", self.client.get("/projetos").get_data(as_text=True))
        self.assertIn("Projeto arquivável", self.client.get("/projetos/arquivados").get_data(as_text=True))
        self.assertNotIn("Projeto ativo", self.client.get("/projetos/arquivados").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{project_id}").status_code, 404)
        response = self.client.post(f"/projetos/{project_id}/desarquivar", data={
            "csrf_token": self.token("/projetos/arquivados"), "confirm": "yes",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(project.status, "active")
        self.assertIsNone(project.archived_at)
        self.assertEqual(project_store(project_id).capturar_ativa()["hash"], original_hash)
        self.assertIsNotNone(db.session.get(Project, other_id))
        actions = {item.action for item in db.session.scalars(select(AuditLog).where(AuditLog.target_id == project_id))}
        self.assertTrue({"project_archived", "project_restored"}.issubset(actions))

    def test_blocked_project_remains_visible_outside_archive(self):
        create_user()
        login(self.client)
        project_id = create_project(self.client, "Projeto bloqueado")
        project = db.session.get(Project, project_id)
        project.status = "blocked"
        db.session.commit()
        self.assertIn("Projeto bloqueado", self.client.get("/projetos").get_data(as_text=True))
        self.assertNotIn("Projeto bloqueado", self.client.get("/projetos/arquivados").get_data(as_text=True))

    def test_individual_delete_requires_archive_and_word_removes_exclusive_data(self):
        owner = create_user()
        login(self.client)
        project_id = create_project(self.client)
        root = Path(self.app.config["PLATFORM_DATA_DIR"]) / "projects" / project_id
        self.assertTrue((root / "vocabulario" / "versions" / "v1.0.json").is_file())
        self.assertEqual(self.client.get(f"/projetos/{project_id}/excluir").status_code, 400)
        self.archive(project_id)
        page = self.client.get(f"/projetos/{project_id}/excluir")
        self.assertIn("Esta ação não pode ser desfeita.", page.get_data(as_text=True))
        token = csrf_from(page)
        for word in ("", "DELETAR", "deletar agora"):
            response = self.client.post(f"/projetos/{project_id}/excluir", data={
                "csrf_token": token, "confirmation": word,
            })
            self.assertEqual(response.status_code, 400)
            self.assertIsNotNone(db.session.get(Project, project_id))
        response = self.client.post(f"/projetos/{project_id}/excluir", data={
            "csrf_token": token, "confirmation": "  deletar  ",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(Project, project_id))
        self.assertFalse(root.exists())
        self.assertFalse(db.session.scalars(select(ProjectLibrary).where(ProjectLibrary.project_id == project_id)).all())
        self.assertFalse(db.session.scalars(select(ProjectVocabularyVersion).where(ProjectVocabularyVersion.project_id == project_id)).all())
        self.assertIsNotNone(db.session.get(User, owner.id))
        self.assertIsNotNone(db.session.get(VocabularyLibrary, "relacoes_raciais"))
        audit = db.session.scalar(select(AuditLog).where(AuditLog.action == "project_permanently_deleted", AuditLog.target_id == project_id))
        self.assertEqual(audit.admin_user_id, owner.id)
        self.assertEqual(audit.before_json["owner_user_id"], owner.id)

    def test_batch_is_atomic_for_active_foreign_empty_and_invalid_ids(self):
        create_user()
        login(self.client)
        first = create_project(self.client, "Primeiro")
        second = create_project(self.client, "Segundo")
        active = create_project(self.client, "Ativo")
        self.archive(first)
        self.archive(second)
        token = self.token("/projetos/arquivados")
        endpoint = "/projetos/arquivados/excluir"
        self.assertEqual(self.client.post(endpoint, data={"csrf_token": token, "confirmation": "deletar"}).status_code, 400)
        self.assertEqual(self.client.post(endpoint, data={"csrf_token": token, "confirmation": "deletar", "project_ids": [first, active]}).status_code, 400)
        self.assertEqual(self.client.post(endpoint, data={"csrf_token": token, "confirmation": "deletar", "project_ids": [first, str(uuid4())]}).status_code, 404)
        self.assertIsNotNone(db.session.get(Project, first))
        self.assertIsNotNone(db.session.get(Project, second))
        self.client.post("/logout", data={"csrf_token": token})
        create_user("Outro", "outro@example.org")
        login(self.client, "outro@example.org")
        foreign = create_project(self.client)
        self.client.post("/logout", data={"csrf_token": self.token()})
        login(self.client)
        token = self.token("/projetos/arquivados")
        self.assertEqual(self.client.post(endpoint, data={"csrf_token": token, "confirmation": "deletar", "project_ids": [first, foreign]}).status_code, 404)
        self.assertIsNotNone(db.session.get(Project, first))
        confirm = self.client.post("/projetos/arquivados/excluir/confirmar", data={
            "csrf_token": token, "project_ids": [first, second],
        })
        self.assertEqual(confirm.status_code, 200)
        self.assertIn("2 projeto(s)", confirm.get_data(as_text=True))
        self.assertEqual(self.client.post(endpoint, data={"csrf_token": csrf_from(confirm), "confirmation": "deletar", "project_ids": [first, second]}).status_code, 302)
        self.assertIsNone(db.session.get(Project, first))
        self.assertIsNone(db.session.get(Project, second))
        self.assertIsNotNone(db.session.get(Project, active))
        self.assertIsNotNone(db.session.get(Project, foreign))
        audits = db.session.scalars(select(AuditLog).where(AuditLog.action == "project_permanently_deleted")).all()
        self.assertEqual({item.target_id for item in audits}, {first, second})

    def test_active_job_blocks_deletion(self):
        create_user()
        login(self.client)
        project_id = create_project(self.client)
        self.archive(project_id)
        job_id = str(uuid4())
        with JOBS_LOCK:
            PROGRESSOS_HR[job_id] = {"project_id": project_id, "status": "processando"}
        try:
            response = self.client.post(f"/projetos/{project_id}/excluir", data={
                "csrf_token": self.token("/projetos/arquivados"), "confirmation": "deletar",
            })
            self.assertEqual(response.status_code, 400)
            self.assertIsNotNone(db.session.get(Project, project_id))
        finally:
            with JOBS_LOCK:
                PROGRESSOS_HR.pop(job_id, None)

    def test_other_user_cannot_restore_or_delete_archived_project(self):
        create_user()
        login(self.client)
        project_id = create_project(self.client, "Privado")
        self.archive(project_id)
        self.client.post("/logout", data={"csrf_token": self.token("/projetos/arquivados")})
        create_user("Outro", "outro@example.org")
        login(self.client, "outro@example.org")
        token = self.token("/projetos/arquivados")
        self.assertNotIn("Privado", self.client.get("/projetos/arquivados").get_data(as_text=True))
        self.assertEqual(self.client.post(f"/projetos/{project_id}/desarquivar", data={"csrf_token": token, "confirm": "yes"}).status_code, 404)
        self.assertEqual(self.client.get(f"/projetos/{project_id}/excluir").status_code, 404)
        self.assertEqual(self.client.post(f"/projetos/{project_id}/excluir", data={"csrf_token": token, "confirmation": "deletar"}).status_code, 404)
        self.assertIsNotNone(db.session.get(Project, project_id))

    def test_sql_failure_restores_staged_vocabulary_directory(self):
        owner = create_user()
        login(self.client)
        project_id = create_project(self.client)
        self.archive(project_id)
        directory = Path(self.app.config["PLATFORM_DATA_DIR"]) / "projects" / project_id
        project = db.session.get(Project, project_id)
        with patch.object(db.session, "commit", side_effect=RuntimeError("Falha de teste")):
            with self.assertRaisesRegex(RuntimeError, "Falha de teste"):
                delete_archived([project], owner, "deletar")
        self.assertTrue((directory / "vocabulario" / "versions" / "v1.0.json").is_file())
        self.assertIsNotNone(db.session.get(Project, project_id))
        self.assertIsNotNone(db.session.scalar(select(ProjectVocabularyVersion).where(ProjectVocabularyVersion.project_id == project_id)))

    def test_admin_can_manage_archived_project_but_no_delete_shortcut(self):
        create_user()
        login(self.client)
        project_id = create_project(self.client)
        self.client.post("/logout", data={"csrf_token": self.token()})
        admin = create_user("Admin", "admin@example.org", "admin", "institutional")
        login(self.client, "admin@example.org")
        token = self.token("/admin/projetos")
        self.assertEqual(self.client.post(f"/admin/projetos/{project_id}/estado", data={"csrf_token": token, "confirm": "yes", "status": "deleted"}).status_code, 400)
        self.assertEqual(self.client.post(f"/admin/projetos/{project_id}/estado", data={"csrf_token": token, "confirm": "yes", "status": "archived"}).status_code, 302)
        self.assertEqual(self.client.get(f"/admin/projetos/{project_id}/excluir").status_code, 200)
        self.assertEqual(self.client.post(f"/admin/projetos/{project_id}/excluir", data={"csrf_token": token, "confirmation": "deletar"}).status_code, 302)
        self.assertIsNone(db.session.get(Project, project_id))
        audit = db.session.scalar(select(AuditLog).where(AuditLog.action == "project_permanently_deleted", AuditLog.target_id == project_id))
        self.assertEqual(audit.admin_user_id, admin.id)

    def test_admin_library_draft_edit_publish_inactivate_and_project_provenance(self):
        base = db.session.get(VocabularyLibrary, "relacoes_raciais")
        baseline = (base.content_hash, base.snapshot_json, base.counts_json)
        create_user("Admin", "admin@example.org", "admin", "institutional")
        login(self.client, "admin@example.org")
        token = self.token("/admin/bibliotecas")
        create = self.client.post("/admin/bibliotecas/nova", data={
            "csrf_token": token, "name": "Biblioteca de teste", "description": "Conteúdo de teste manual",
        })
        self.assertEqual(create.status_code, 302)
        library = db.session.get(VocabularyLibrary, "biblioteca_de_teste")
        self.assertEqual((library.status, library.active, library.counts_json),
                         ("draft", False, {"grupos": 0, "entidades": 0, "variantes": 0}))
        self.assertNotIn("Biblioteca de teste", self.client.get("/projetos/novo").get_data(as_text=True))
        group = self.client.post(f"/admin/bibliotecas/{library.id}/grupos", data={
            "csrf_token": token, "base_hash": library.content_hash, "name": "Grupo de teste", "active": "on",
        })
        self.assertEqual(group.status_code, 302)
        self.assertIn("grupo_de_teste", library.snapshot_json["grupos"])
        entity = self.client.post(f"/admin/bibliotecas/{library.id}/entidades", data={
            "csrf_token": token, "base_hash": library.content_hash, "canonical": "Conceito de teste",
            "groups": ["grupo_de_teste"], "entity_type": "conceito", "active": "on",
        })
        self.assertEqual(entity.status_code, 302)
        self.assertEqual(library.counts_json["entidades"], 1)
        variant = self.client.post(f"/admin/bibliotecas/{library.id}/variantes", data={
            "csrf_token": token, "base_hash": library.content_hash, "entity_key": "conceito_de_teste",
            "text": "Outra grafia de teste", "active": "on",
        })
        self.assertEqual(variant.status_code, 302)
        self.assertEqual(library.counts_json, {"grupos": 1, "entidades": 1, "variantes": 2})
        duplicate = self.client.post(f"/admin/bibliotecas/{library.id}/variantes", data={
            "csrf_token": token, "base_hash": library.content_hash, "entity_key": "conceito_de_teste",
            "text": "Outra grafia de teste", "active": "on",
        })
        self.assertEqual(duplicate.status_code, 302)
        self.assertEqual(library.counts_json["variantes"], 2)
        toggle = self.client.post(f"/admin/bibliotecas/{library.id}/itens/estado", data={
            "csrf_token": token, "base_hash": library.content_hash, "kind": "variant",
            "key": "conceito_de_teste", "variant_index": "1", "active": "false",
        })
        self.assertEqual(toggle.status_code, 302)
        self.assertFalse(library.snapshot_json["entidades"][0]["variantes"][1]["ativo"])
        self.client.post(f"/admin/bibliotecas/{library.id}/itens/estado", data={
            "csrf_token": token, "base_hash": library.content_hash, "kind": "variant",
            "key": "conceito_de_teste", "variant_index": "1", "active": "true",
        })
        self.assertTrue(library.snapshot_json["entidades"][0]["variantes"][1]["ativo"])
        self.assertEqual(self.client.post(f"/admin/bibliotecas/{library.id}/publicar", data={"csrf_token": token, "confirm": "yes", "base_hash": library.content_hash}).status_code, 302)
        self.assertEqual((library.status, library.active), ("published", True))
        frozen_hash = library.content_hash
        self.assertEqual(self.client.post(f"/admin/bibliotecas/{library.id}/grupos", data={"csrf_token": token, "base_hash": frozen_hash, "name": "Inválido", "active": "on"}).status_code, 302)
        self.assertEqual(library.content_hash, frozen_hash)
        self.assertIn("Biblioteca de teste", self.client.get("/projetos/novo").get_data(as_text=True))
        project_id = create_project(self.client, libraries=("relacoes_raciais", library.id))
        self.assertEqual(len(db.session.scalars(select(ProjectLibrary).where(ProjectLibrary.project_id == project_id)).all()), 2)
        link = db.session.get(ProjectLibrary, (project_id, library.id))
        self.assertEqual((link.source_hash, link.source_version), (frozen_hash, "v1"))
        self.assertEqual(self.client.post(f"/admin/bibliotecas/{library.id}/estado", data={"csrf_token": token, "confirm": "yes"}).status_code, 302)
        self.assertEqual((library.status, library.active), ("inactive", False))
        self.assertNotIn("Biblioteca de teste", self.client.get("/projetos/novo").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{project_id}").status_code, 200)
        self.assertEqual((base.content_hash, base.snapshot_json, base.counts_json), baseline)
        self.assertEqual(base.counts_json, {"grupos": 8, "entidades": 75, "variantes": 117})


if __name__ == "__main__":
    unittest.main()
