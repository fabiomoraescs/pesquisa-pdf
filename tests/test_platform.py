"""Autenticação, isolamento e bibliotecas da Etapa 3.1."""

import copy
import io
import re
import unittest
from datetime import timedelta
from uuid import UUID

import pymupdf
from sqlalchemy import select

from platform_helpers import create_project, create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import (
    AccessGrant, AuditLog, Plan, Project, ProjectVocabularyVersion, Tool,
    User, UserToolOverride, VocabularyLibrary, utcnow,
)
from platform_core.services import can_use_tool, current_grant, replace_grant
from platform_core.vocabularies import merge_libraries, project_store
from historico_racial.vocabulary import VocabularioError
from historico_racial.vocabulary import entidades_pesquisaveis
from historico_racial.occurrences import BuscadorLexical


def sample_pdf(text="Du Bois discutiu a população negra."):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 80), text)
    data = doc.tobytes()
    doc.close()
    return data


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_public_routes_and_legacy_redirect(self):
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/cadastro").status_code, 200)
        static_response = self.client.get("/static/css/style.css")
        self.assertEqual(static_response.status_code, 200)
        static_response.close()
        self.assertIn("/login", self.client.get("/").headers["Location"])
        self.assertIn("/login", self.client.get("/analise-documental").headers["Location"])

    def test_registration_hash_student_access_and_logout(self):
        token = csrf_from(self.client.get("/cadastro"))
        response = self.client.post("/cadastro", data={
            "csrf_token": token, "name": "Ana", "email": "ANA@example.org",
            "password": "senha-muito-segura-123", "confirm": "senha-muito-segura-123",
        })
        self.assertEqual(response.status_code, 302)
        user = db.session.scalar(select(User).where(User.email == "ana@example.org"))
        self.assertEqual((user.role, user.status), ("user", "active"))
        self.assertNotIn("senha-muito-segura-123", user.password_hash)
        self.assertTrue(user.check_password("senha-muito-segura-123"))
        self.assertEqual((current_grant(user).plan_id, current_grant(user).access_mode), ("student", "student_free"))
        self.assertTrue(can_use_tool(user, "pdf_scraper"))
        self.assertTrue(can_use_tool(user, "document_analysis"))
        self.assertIsNotNone(db.session.scalar(select(AuditLog).where(AuditLog.action == "user_registered", AuditLog.target_id == user.id)))
        self.assertEqual(self.client.get("/").headers["Location"], "/perfil")
        self.assertEqual(self.client.get("/raspagem-livre").status_code, 200)
        self.assertEqual(self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/projetos"))}).status_code, 302)
        self.assertIn("/login", self.client.get("/").headers["Location"])

    def test_duplicate_email_and_invalid_login(self):
        create_user(email="ana@example.org")
        token = csrf_from(self.client.get("/cadastro"))
        response = self.client.post("/cadastro", data={
            "csrf_token": token, "name": "Outra", "email": "ANA@example.org",
            "password": "senha-muito-segura-123", "confirm": "senha-muito-segura-123",
        })
        self.assertIn("já está cadastrado", response.get_data(as_text=True))
        token = csrf_from(self.client.get("/login"))
        response = self.client.post("/login", data={"csrf_token": token, "email": "ana@example.org", "password": "incorreta"})
        self.assertIn("E-mail ou senha inválidos", response.get_data(as_text=True))

    def test_blocked_suspended_and_nonadmin_admin_denial(self):
        user = create_user()
        login(self.client)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        for status in ("suspended", "blocked"):
            user.status = status
            db.session.commit()
            response = self.client.get("/")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response.headers["Location"])
            user.status = "active"
            db.session.commit()
            self.client = self.app.test_client()
            with self.client.session_transaction() as session:
                session["_user_id"] = user.id
                session["_fresh"] = True

    def test_plans_grants_courtesy_expiry_and_overrides(self):
        user = create_user()
        self.assertEqual({item.id for item in db.session.scalars(select(Plan))}, {"student", "researcher", "pro", "institutional"})
        admin = create_user("Admin", "admin@example.org", "admin", "institutional")
        replace_grant(user, "pro", "admin_courtesy", "active", admin, utcnow() + timedelta(days=10))
        db.session.commit()
        self.assertEqual(len(user.grants), 2)
        self.assertEqual(current_grant(user).granted_by_id, admin.id)
        self.assertTrue(can_use_tool(user, "document_analysis"))
        db.session.add(UserToolOverride(user_id=user.id, tool_id="document_analysis", decision="deny"))
        db.session.commit()
        self.assertFalse(can_use_tool(user, "document_analysis"))
        override = db.session.get(UserToolOverride, (user.id, "document_analysis"))
        override.decision = "allow"
        db.session.commit()
        self.assertTrue(can_use_tool(user, "document_analysis"))
        user.status = "blocked"
        db.session.commit()
        self.assertFalse(can_use_tool(user, "document_analysis"))
        user.status = "active"
        current_grant(user).expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertFalse(can_use_tool(user, "document_analysis"))

    def test_seed_exact_and_immutable_library(self):
        library = db.session.get(VocabularyLibrary, "relacoes_raciais")
        self.assertEqual(library.counts_json, {"grupos": 8, "entidades": 75, "variantes": 117})
        self.assertEqual(len(library.snapshot_json["entidades"]), 75)
        user = create_user()
        login(self.client)
        self.assertEqual(self.client.get("/admin/bibliotecas").status_code, 403)
        token = csrf_from(self.client.get("/projetos"))
        self.assertEqual(self.client.post("/admin/bibliotecas/relacoes_raciais/estado", data={"csrf_token": token, "confirm": "yes"}).status_code, 403)
        project_id = create_project(self.client)
        self.assertEqual(db.session.get(VocabularyLibrary, "relacoes_raciais").content_hash, library.content_hash)
        self.assertEqual(project_store(project_id).capturar_ativa()["version"], "v1.0")

    def test_project_isolation_versions_and_noop(self):
        user_a = create_user()
        login(self.client)
        project_a = create_project(self.client, "Projeto A")
        project_b = create_project(self.client, "Projeto B")
        self.assertNotEqual(project_a, project_b)
        self.assertEqual(project_store(project_a).capturar_ativa()["version"], "v1.0")
        self.assertEqual(project_store(project_b).capturar_ativa()["version"], "v1.0")
        token = csrf_from(self.client.get(f"/analise-documental/projetos/{project_a}/vocabulario"))
        original = project_store(project_a).capturar_ativa()
        response = self.client.post(f"/analise-documental/projetos/{project_a}/vocabulario/versoes", json={
            "base_version": "v1.0", "vocabulario": original["vocabulario"], "nota": "Só nota",
        }, headers={"X-CSRFToken": token})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nenhuma alteração", response.json["erro"])
        changed = copy.deepcopy(original["vocabulario"])
        changed["grupos"][next(iter(changed["grupos"]))]["ativo"] = False
        response = self.client.post(f"/analise-documental/projetos/{project_a}/vocabulario/versoes", json={
            "base_version": "v1.0", "vocabulario": changed, "nota": "Teste",
        }, headers={"X-CSRFToken": token})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(project_store(project_a).capturar_ativa()["version"], "v1.1")
        self.assertEqual(project_store(project_b).capturar_ativa()["version"], "v1.0")
        self.assertEqual(project_store(project_a).carregar("v1.0")["hash"], original["hash"])
        self.assertEqual(db.session.scalar(select(ProjectVocabularyVersion).where(ProjectVocabularyVersion.project_id == project_a, ProjectVocabularyVersion.active.is_(True))).version, "v1.1")

    def test_cross_user_project_result_and_processing_denied(self):
        create_user()
        login(self.client)
        project_id = create_project(self.client)
        token = csrf_from(self.client.get(f"/analise-documental/projetos/{project_id}"))
        self.client.post("/logout", data={"csrf_token": token})
        create_user("Outro", "outro@example.org")
        login(self.client, "outro@example.org")
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{project_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{project_id}/vocabulario").status_code, 404)
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{project_id}/resultado/{'0'*8}-{'0'*4}-{'0'*4}-{'0'*4}-{'0'*12}").status_code, 404)
        token = csrf_from(self.client.get("/projetos"))
        self.assertEqual(self.client.post(f"/analise-documental/projetos/{project_id}/vocabulario/versoes", json={"base_version": "v1.0", "vocabulario": {}}, headers={"X-CSRFToken": token}).status_code, 404)
        self.assertEqual(self.client.post(f"/projetos/{project_id}/arquivar", data={"csrf_token": token}).status_code, 404)
        response = self.client.post(f"/analise-documental/projetos/{project_id}/analisar", data={"pdfs": (io.BytesIO(sample_pdf()), "a.pdf"), "csrf_token": token}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 404)

    def test_csrf_rejects_unprotected_post(self):
        create_user()
        login(self.client)
        self.assertEqual(self.client.post("/projetos/novo", data={"name": "Falha"}).status_code, 400)
        self.assertEqual(db.session.scalar(select(Project)), None)

    def test_multiple_library_merge_and_conflict(self):
        base = db.session.get(VocabularyLibrary, "relacoes_raciais")
        second = copy.deepcopy(base.snapshot_json)
        second["grupos"] = {"extra": {"id_grupo": "extra", "nome": "Grupo de teste", "descricao": "", "ativo": True}}
        same = copy.deepcopy(next(entity for entity in second["entidades"] if entity["id_entidade"] == "du_bois"))
        same["grupo"] = ["extra"]
        second["entidades"] = [same]
        extra = VocabularyLibrary(id="teste", name="Teste", snapshot_json=second, content_hash="x" * 64, counts_json={}, active=True, status="published")
        merged = merge_libraries([base, extra])
        self.assertEqual(len(merged["entidades"]), 75)
        du_bois = next(entity for entity in merged["entidades"] if entity["id_entidade"] == "du_bois")
        self.assertEqual(du_bois["source_libraries"], ["relacoes_raciais", "teste"])
        conflict = copy.deepcopy(second)
        conflict["entidades"][0]["id_entidade"] = "outra_entidade"
        extra.snapshot_json = conflict
        with self.assertRaises(VocabularioError):
            merge_libraries([base, extra])

    def test_admin_panel_actions_and_audit(self):
        admin = create_user("Admin", "admin@example.org", "admin", "institutional")
        user = create_user()
        login(self.client, "admin@example.org")
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/admin/bibliotecas").status_code, 200)
        token = csrf_from(self.client.get(f"/admin/usuarios/{user.id}"))
        response = self.client.post(f"/admin/usuarios/{user.id}/status", data={"csrf_token": token, "status": "blocked"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(user.status, "blocked")
        response = self.client.post(f"/admin/usuarios/{user.id}/acesso", data={
            "csrf_token": token, "plan_id": "pro", "access_mode": "admin_courtesy", "access_status": "active",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(current_grant(user).plan_id, "pro")
        response = self.client.post(f"/admin/usuarios/{user.id}/ferramenta/document_analysis", data={"csrf_token": token, "decision": "deny"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(UserToolOverride, (user.id, "document_analysis")).decision, "deny")
        self.assertGreaterEqual(db.session.scalar(select(db.func.count()).select_from(AuditLog)), 2)

    def test_admin_soft_delete_preserves_user_and_audit(self):
        create_user("Admin", "admin@example.org", "admin", "institutional")
        target = create_user()
        login(self.client, "admin@example.org")
        token = csrf_from(self.client.get(f"/admin/usuarios/{target.id}"))
        response = self.client.post(f"/admin/usuarios/{target.id}/excluir", data={"csrf_token": token, "confirm": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(db.session.get(User, target.id).deleted_at)
        self.assertFalse(db.session.get(User, target.id).is_active)
        self.assertIsNotNone(db.session.scalar(select(AuditLog).where(AuditLog.action == "user_soft_deleted")))

    def test_cli_first_admin_without_default_password(self):
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=["create-admin", "--name", "Primeiro Admin", "--email", "primeiro@example.org"],
                               input="senha-administrativa-123\nsenha-administrativa-123\n")
        self.assertEqual(result.exit_code, 0, result.output)
        admin = db.session.scalar(select(User).where(User.email == "primeiro@example.org"))
        self.assertEqual(admin.role, "admin")
        self.assertTrue(admin.check_password("senha-administrativa-123"))
        self.assertNotIn("senha-administrativa-123", result.output)
        self.assertEqual(current_grant(admin).access_mode, "admin_courtesy")
        self.assertIsNotNone(db.session.scalar(select(AuditLog).where(AuditLog.action == "admin_created", AuditLog.target_id == admin.id)))

    def test_global_tool_block_and_admin_ability(self):
        user = create_user()
        admin = create_user("Admin", "admin@example.org", "admin", "institutional")
        tool = db.session.get(Tool, "document_analysis")
        tool.active = False
        db.session.commit()
        self.assertFalse(can_use_tool(user, "document_analysis"))
        self.assertFalse(can_use_tool(admin, "document_analysis"))
        login(self.client)
        self.assertEqual(self.client.get("/projetos").status_code, 403)
        self.assertEqual(self.client.get("/").headers["Location"], "/perfil")
        self.assertEqual(self.client.get("/raspagem-livre").status_code, 200)

    def test_admin_project_archive_restore_and_no_deleted_shortcut(self):
        create_user()
        login(self.client)
        project_id = create_project(self.client)
        token = csrf_from(self.client.get("/projetos"))
        self.client.post("/logout", data={"csrf_token": token})
        create_user("Admin", "admin@example.org", "admin", "institutional")
        login(self.client, "admin@example.org")
        self.assertEqual(self.client.get(f"/admin/projetos/{project_id}").status_code, 200)
        token = csrf_from(self.client.get("/admin/projetos"))
        shortcut = self.client.post(f"/admin/projetos/{project_id}/estado", data={
            "csrf_token": token, "confirm": "yes", "status": "deleted",
        })
        self.assertEqual(shortcut.status_code, 400)
        response = self.client.post(f"/admin/projetos/{project_id}/estado", data={
            "csrf_token": token, "confirm": "yes", "status": "archived",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(Project, project_id).status, "archived")
        self.assertIsNotNone(db.session.scalar(select(AuditLog).where(AuditLog.action == "project_archived")))
        self.assertEqual(self.client.get(f"/admin/projetos/{project_id}").status_code, 200)
        response = self.client.post(f"/admin/projetos/{project_id}/estado", data={
            "csrf_token": token, "confirm": "yes", "status": "active",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(Project, project_id).status, "active")

    def test_actual_other_user_result_is_private(self):
        owner = create_user()
        login(self.client)
        project_id = create_project(self.client)
        token = csrf_from(self.client.get(f"/analise-documental/projetos/{project_id}"))
        response = self.client.post(f"/analise-documental/projetos/{project_id}/analisar", data={
            "csrf_token": token, "pdfs": (io.BytesIO(sample_pdf()), "privado.pdf"),
        }, content_type="multipart/form-data", headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 202)
        from historico_racial.routes import EXECUTOR_HR
        EXECUTOR_HR.submit(lambda: None).result(timeout=30)
        result_url = self.client.get(response.json["progresso_url"]).json["resultado_url"]
        self.assertEqual(self.client.get(result_url).status_code, 200)
        self.client.post("/logout", data={"csrf_token": token})
        create_user("Intruso", "intruso@example.org")
        login(self.client, "intruso@example.org")
        self.assertEqual(self.client.get(result_url).status_code, 404)
        self.assertEqual(self.client.get(response.json["progresso_url"]).status_code, 404)

    def test_two_selected_libraries_create_one_merged_entity(self):
        base = db.session.get(VocabularyLibrary, "relacoes_raciais")
        source = copy.deepcopy(base.snapshot_json)
        du_bois = copy.deepcopy(next(item for item in source["entidades"] if item["id_entidade"] == "du_bois"))
        du_bois["grupo"] = ["novo"]
        source = {"grupos": {"novo": {"id_grupo": "novo", "nome": "Novo grupo", "descricao": "", "ativo": True}}, "entidades": [du_bois]}
        db.session.add(VocabularyLibrary(id="segunda", name="Segunda", active=True, snapshot_json=source, content_hash="z" * 64, counts_json={"grupos": 1, "entidades": 1, "variantes": len(du_bois["variantes"])}))
        db.session.commit()
        create_user()
        login(self.client)
        project_id = create_project(self.client, libraries=("relacoes_raciais", "segunda"))
        snapshot = project_store(project_id).capturar_ativa()
        self.assertEqual(snapshot["counts"]["entidades"], 75)
        self.assertEqual(len(next(item for item in snapshot["vocabulario"]["entidades"] if item["id_entidade"] == "du_bois")["source_libraries"]), 2)
        matches = BuscadorLexical(entidades_pesquisaveis(snapshot["vocabulario"])).localizar("Du Bois foi citado.")
        self.assertEqual(len([item for item in matches if item.id_entidade == "du_bois"]), 1)

    def test_vocabulary_history_is_paginated(self):
        create_user()
        login(self.client)
        project_id = create_project(self.client)
        store = project_store(project_id)
        for index in range(1, 11):
            current = store.capturar_ativa()
            content = copy.deepcopy(current["vocabulario"])
            content["grupos"]["A_classificacao_racial_brasileira"]["descricao"] = f"Revisão {index}"
            store.salvar(current["version"], content)
        page = self.client.get(f"/analise-documental/projetos/{project_id}/vocabulario")
        self.assertIn("Ver versões anteriores", page.get_data(as_text=True))
        older = self.client.get(f"/analise-documental/projetos/{project_id}/vocabulario?page=2")
        self.assertEqual(older.status_code, 200)
        self.assertIn("v1.0", older.get_data(as_text=True))

    def test_real_document_job_and_snapshot_metadata(self):
        create_user()
        login(self.client)
        project_id = create_project(self.client)
        token = csrf_from(self.client.get(f"/analise-documental/projetos/{project_id}"))
        response = self.client.post(f"/analise-documental/projetos/{project_id}/analisar", data={
            "csrf_token": token, "pdfs": (io.BytesIO(sample_pdf()), "teste.pdf"),
        }, content_type="multipart/form-data", headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job_id = response.json["job_id"]
        UUID(job_id)
        from historico_racial.routes import EXECUTOR_HR, PROGRESSOS_HR, RESULTADOS_HR
        EXECUTOR_HR.submit(lambda: None).result(timeout=30)
        progress = self.client.get(response.json["progresso_url"])
        self.assertEqual(progress.status_code, 200)
        self.assertEqual(progress.json["status"], "concluido")
        result = RESULTADOS_HR[job_id]
        self.assertEqual(result["project_id"], project_id)
        self.assertEqual(result["vocabulario_version"], "v1.0")
        self.assertEqual(result["vocabulario_hash"], project_store(project_id).capturar_ativa()["hash"])
        self.assertGreater(result["total_ocorrencias"], 0)
        self.assertEqual(self.client.get(progress.json["resultado_url"]).status_code, 200)


if __name__ == "__main__":
    unittest.main()
