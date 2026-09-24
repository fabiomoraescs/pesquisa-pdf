"""Fluxos administrativos e apresentação adicionados após a exclusão de usuários."""

import re
import unittest
from uuid import uuid4

import app as legacy
from historico_racial import routes as documentary
from platform_helpers import create_project, create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AccessGrant, AuditLog, Plan, Project, User, utcnow
from platform_core.semantic_threshold import DEFAULT, MAXIMUM, MINIMUM, STEP, normalize
from platform_core.services import current_grant, replace_grant


class RemainingRefinementsTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.admin = create_user("Administrador", "admin@example.org", role="admin")
        login(self.client, self.admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_admin_reset_requires_confirmation_csrf_and_stores_only_hash(self):
        user = create_user("Alvo", "alvo@example.org")
        url = f"/admin/usuarios/{user.id}/redefinir-senha"
        reset_page = self.client.get(url)
        self.assertEqual(reset_page.status_code, 200)
        self.assertIn("raspagemdedados", reset_page.get_data(as_text=True))
        self.assertNotIn("1a12", reset_page.get_data(as_text=True))
        self.assertFalse(user.must_change_password)
        self.assertEqual(self.client.post(url, data={"confirmation": "redefinir"}).status_code, 400)
        token = csrf_from(self.client.get(url))
        self.assertEqual(self.client.post(url, data={"csrf_token": token, "confirmation": "sim"}).status_code, 400)
        self.assertFalse(user.must_change_password)
        self.assertEqual(self.client.post(url, data={"csrf_token": token, "confirmation": "redefinir"}).status_code, 302)
        db.session.refresh(user)
        self.assertTrue(user.must_change_password)
        self.assertTrue(user.check_password("raspagemdedados"))
        self.assertFalse(user.check_password("1a12"))
        self.assertNotEqual(user.password_hash, "raspagemdedados")
        self.assertNotIn("raspagemdedados", user.password_hash)
        self.assertFalse(user.check_password("senha-de-teste-segura-123"))
        self.assertEqual(user.status, "active")
        self.assertIsNone(user.deleted_at)
        log = db.session.query(AuditLog).filter_by(action="user_password_reset", target_id=user.id).one()
        serialized = f"{log.before_json} {log.after_json}"
        self.assertNotIn("raspagemdedados", serialized)
        self.assertNotIn(user.password_hash, serialized)

    def test_temporary_password_cannot_be_registered_as_final_password(self):
        visitor = self.app.test_client()
        with self.app.app_context():
            response = visitor.post("/cadastro", data={
                "csrf_token": csrf_from(visitor.get("/cadastro")),
                "name": "Nova pessoa", "email": "nova@example.org",
                "password": "raspagemdedados", "confirm": "raspagemdedados",
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn("diferente da senha temporária", response.get_data(as_text=True))
        self.assertIsNone(db.session.query(User).filter_by(email="nova@example.org").first())

    def test_temporary_password_cannot_be_used_for_new_admin(self):
        result = self.app.test_cli_runner().invoke(
            args=["create-admin", "--name", "Outro Admin", "--email", "outro-admin@example.org"],
            input="raspagemdedados\nraspagemdedados\n",
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("diferente da senha temporária", result.output)
        self.assertIsNone(db.session.query(User).filter_by(email="outro-admin@example.org").first())

    def test_controlled_admin_reset_login_and_mandatory_change_cycle(self):
        user = create_user("Pessoa de teste", "fluxo-reset@example.org")
        previous_hash = user.password_hash
        previous_status = user.status
        previous_deleted_at = user.deleted_at
        visitor = self.app.test_client()
        with self.app.app_context():
            initial = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email,
                "password": "senha-de-teste-segura-123",
            })
            self.assertEqual(initial.status_code, 302)
            self.assertEqual(visitor.get("/projetos").status_code, 200)

        url = f"/admin/usuarios/{user.id}/redefinir-senha"
        reset = self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get(url)), "confirmation": "redefinir",
        })
        self.assertEqual(reset.status_code, 302)
        db.session.refresh(user)
        self.assertNotEqual(user.password_hash, previous_hash)
        self.assertTrue(user.must_change_password)
        self.assertEqual((user.status, user.deleted_at), (previous_status, previous_deleted_at))
        with self.app.app_context():
            self.assertIn("/alterar-senha", visitor.get("/projetos").headers["Location"])

        with self.app.app_context():
            visitor.post("/logout", data={
                "csrf_token": csrf_from(visitor.get("/alterar-senha")),
            })
            old_login = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email,
                "password": "senha-de-teste-segura-123",
            })
            self.assertEqual(old_login.status_code, 200)
            temporary_login = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email,
                "password": "raspagemdedados",
            })
            self.assertEqual(temporary_login.status_code, 302)
            self.assertIn("/alterar-senha", temporary_login.headers["Location"])
            self.assertEqual(visitor.get("/alterar-senha").status_code, 200)
            self.assertIn("/alterar-senha", visitor.get("/login").headers["Location"])
            self.assertIn("/alterar-senha", visitor.get("/projetos").headers["Location"])
            change = visitor.post("/alterar-senha", data={
                "csrf_token": csrf_from(visitor.get("/alterar-senha")),
                "current_password": "raspagemdedados",
                "password": "nova-senha-definitiva-123",
                "confirm": "nova-senha-definitiva-123",
            })
            self.assertEqual(change.status_code, 302)
            self.assertEqual(change.headers["Location"], "/perfil")
            self.assertEqual(visitor.get("/projetos").status_code, 200)

        db.session.refresh(user)
        self.assertFalse(user.must_change_password)
        self.assertTrue(user.check_password("nova-senha-definitiva-123"))
        self.assertFalse(user.check_password("raspagemdedados"))
        self.assertEqual((user.status, user.deleted_at), (previous_status, previous_deleted_at))
        with self.app.app_context():
            visitor.post("/logout", data={"csrf_token": csrf_from(visitor.get("/projetos"))})
            obsolete = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email,
                "password": "raspagemdedados",
            })
            self.assertEqual(obsolete.status_code, 200)
            final_login = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email,
                "password": "nova-senha-definitiva-123",
            })
            self.assertEqual(final_login.status_code, 302)
            self.assertEqual(final_login.headers["Location"], "/perfil")

    def test_common_user_cannot_reset_another_password(self):
        target = create_user("Alvo", "alvo@example.org")
        ordinary = create_user("Comum", "comum@example.org")
        client = self.app.test_client()
        with self.app.app_context():
            login(client, ordinary.email)
            url = f"/admin/usuarios/{target.id}/redefinir-senha"
            self.assertEqual(client.get(url).status_code, 403)
            self.assertEqual(client.post(url, data={
                "csrf_token": csrf_from(client.get("/perfil")), "confirmation": "redefinir",
            }).status_code, 403)
        self.assertFalse(target.must_change_password)

    def test_reset_forces_old_and_new_session_to_change_password_before_any_tool(self):
        user = create_user("Alvo", "alvo@example.org")
        old_session = self.app.test_client()
        with self.app.app_context():
            login(old_session, user.email)
        url = f"/admin/usuarios/{user.id}/redefinir-senha"
        self.client.post(url, data={"csrf_token": csrf_from(self.client.get(url)), "confirmation": "redefinir"})
        with self.app.app_context():
            self.assertIn("/alterar-senha", old_session.get("/projetos").headers["Location"])
            self.assertIn("/alterar-senha", old_session.get("/").headers["Location"])
            self.assertEqual(old_session.post("/projetos/novo", data={"csrf_token": csrf_from(old_session.get("/alterar-senha"))}).status_code, 302)
            self.assertIn("/alterar-senha", old_session.get("/admin").headers["Location"])
            token = csrf_from(old_session.get("/alterar-senha"))
            self.assertEqual(old_session.post("/alterar-senha", data={
                "csrf_token": token, "current_password": "1a12", "password": "nova-senha-segura-123",
                "confirm": "nova-senha-segura-123",
            }).status_code, 200)
            self.assertEqual(old_session.post("/alterar-senha", data={
                "csrf_token": token, "current_password": "errada", "password": "nova-senha-segura-123",
                "confirm": "nova-senha-segura-123",
            }).status_code, 200)
            self.assertEqual(old_session.post("/alterar-senha", data={
                "csrf_token": token, "current_password": "raspagemdedados", "password": "raspagemdedados", "confirm": "raspagemdedados",
            }).status_code, 200)
            self.assertTrue(user.must_change_password)
            self.assertEqual(old_session.post("/alterar-senha", data={
                "csrf_token": token, "current_password": "raspagemdedados", "password": "nova-senha-segura-123",
                "confirm": "nova-senha-segura-123",
            }).status_code, 302)
            self.assertEqual(old_session.get("/projetos").status_code, 200)
        db.session.refresh(user)
        self.assertFalse(user.must_change_password)
        self.assertTrue(user.check_password("nova-senha-segura-123"))
        self.assertFalse(user.check_password("raspagemdedados"))
        self.assertIsNotNone(db.session.query(AuditLog).filter_by(action="password_changed_after_reset").first())

        fresh = self.app.test_client()
        with self.app.app_context():
            rejected = fresh.post("/login", data={
                "csrf_token": csrf_from(fresh.get("/login")), "email": user.email,
                "password": "raspagemdedados",
            })
            self.assertEqual(rejected.status_code, 200)
            response = fresh.post("/login", data={
                "csrf_token": csrf_from(fresh.get("/login")), "email": user.email,
                "password": "nova-senha-segura-123",
            })
        self.assertEqual(response.headers["Location"], "/perfil")

    def test_new_login_with_temporary_password_reaches_only_change_page(self):
        user = create_user("Alvo", "alvo@example.org")
        url = f"/admin/usuarios/{user.id}/redefinir-senha"
        self.client.post(url, data={"csrf_token": csrf_from(self.client.get(url)), "confirmation": "redefinir"})
        visitor = self.app.test_client()
        with self.app.app_context():
            old_password = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email, "password": "1a12",
            })
            self.assertEqual(old_password.status_code, 200)
            response = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email, "password": "raspagemdedados",
            })
            self.assertIn("/alterar-senha", response.headers["Location"])
            self.assertIn("/alterar-senha", visitor.get("/perfil").headers["Location"])

    def test_pending_legacy_temporary_password_is_retired_until_admin_resets_again(self):
        user = create_user("Alvo", "alvo@example.org")
        user.set_password("1a12")
        user.must_change_password = True
        db.session.commit()

        visitor = self.app.test_client()
        with self.app.app_context():
            old_login = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email, "password": "1a12",
            })
            self.assertEqual(old_login.status_code, 200)
            self.assertTrue(user.must_change_password)

        url = f"/admin/usuarios/{user.id}/redefinir-senha"
        self.assertEqual(self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get(url)), "confirmation": "redefinir",
        }).status_code, 302)
        self.assertFalse(user.check_password("1a12"))
        self.assertTrue(user.check_password("raspagemdedados"))
        with self.app.app_context():
            new_login = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email,
                "password": "raspagemdedados",
            })
            self.assertIn("/alterar-senha", new_login.headers["Location"])

    def test_soft_deleted_or_blocked_account_cannot_be_reset_as_if_login_would_work(self):
        user = create_user("Alvo", "alvo@example.org")
        url = f"/admin/usuarios/{user.id}/redefinir-senha"
        original_hash = user.password_hash
        user.deleted_at = utcnow()
        db.session.commit()
        self.assertEqual(user.status, "active")
        self.assertFalse(user.is_active)
        self.assertIn("excluída logicamente", self.client.get(url).get_data(as_text=True))
        self.assertEqual(self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get(url)), "confirmation": "redefinir",
        }).status_code, 400)
        self.assertEqual(user.password_hash, original_hash)
        self.assertFalse(user.must_change_password)
        self.assertNotIn(user.email, self.client.get("/admin/usuarios").get_data(as_text=True))
        self.assertIn(user.email, self.client.get("/admin/usuarios/excluidos").get_data(as_text=True))
        restore_url = f"/admin/usuarios/{user.id}/restaurar"
        self.assertEqual(self.client.post(restore_url, data={
            "csrf_token": csrf_from(self.client.get(restore_url)), "confirm": "yes", "restore_status": "active",
        }).status_code, 302)
        db.session.refresh(user)
        self.assertIsNone(user.deleted_at)
        self.assertTrue(user.is_active)
        self.assertEqual(self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get(url)), "confirmation": "redefinir",
        }).status_code, 302)
        self.assertTrue(user.check_password("raspagemdedados"))
        visitor = self.app.test_client()
        with self.app.app_context():
            login_result = visitor.post("/login", data={
                "csrf_token": csrf_from(visitor.get("/login")), "email": user.email,
                "password": "raspagemdedados",
            })
            self.assertIn("/alterar-senha", login_result.headers["Location"])

        user.status = "blocked"
        db.session.commit()
        self.assertIn("bloqueada", self.client.get(url).get_data(as_text=True))
        self.assertEqual(self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get(url)), "confirmation": "redefinir",
        }).status_code, 400)

    def test_inactive_plan_blocks_reset_with_clear_reason(self):
        user = create_user("Alvo", "alvo@example.org")
        original_hash = user.password_hash
        db.session.get(Plan, "student").active = False
        db.session.commit()
        url = f"/admin/usuarios/{user.id}/redefinir-senha"
        self.assertIn("não possui acesso ativo", self.client.get(url).get_data(as_text=True))
        self.assertEqual(self.client.post(url, data={
            "csrf_token": csrf_from(self.client.get(url)), "confirmation": "redefinir",
        }).status_code, 400)
        self.assertEqual(user.password_hash, original_hash)
        self.assertFalse(user.must_change_password)

    def test_plan_state_admin_csrf_audit_and_existing_grant(self):
        user = create_user("Beneficiário", "beneficiario@example.org", plan="researcher")
        grant_id = current_grant(user).id
        url = "/admin/planos/researcher/estado"
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, data={"confirm": "yes", "active": "false"}).status_code, 400)
        token = csrf_from(self.client.get("/admin/planos"))
        self.assertEqual(self.client.post(url, data={"csrf_token": token, "confirm": "yes", "active": "false"}).status_code, 302)
        self.assertFalse(db.session.get(Plan, "researcher").active)
        self.assertEqual(current_grant(user).id, grant_id)
        self.assertIsNotNone(db.session.get(AccessGrant, grant_id))
        self.assertEqual(self.client.post(f"/admin/usuarios/{user.id}/acesso", data={
            "csrf_token": token, "plan_id": "researcher", "access_mode": "paid", "access_status": "active",
        }).status_code, 400)
        html = self.client.get("/admin/planos").get_data(as_text=True)
        self.assertIn('title="Ativar plano" aria-label="Ativar plano"', html)
        self.assertIn('id="admin-plans-list" data-view-container', html)
        self.assertEqual(self.client.post(url, data={"csrf_token": token, "confirm": "yes", "active": "true"}).status_code, 302)
        self.assertTrue(db.session.get(Plan, "researcher").active)
        logs = db.session.query(AuditLog).filter_by(action="plan_state_changed", target_id="researcher").all()
        self.assertEqual(len(logs), 2)

    def test_common_user_cannot_change_plan_state(self):
        ordinary = create_user("Comum", "comum@example.org")
        client = self.app.test_client()
        with self.app.app_context():
            login(client, ordinary.email)
            self.assertEqual(client.post("/admin/planos/researcher/estado", data={
                "csrf_token": csrf_from(client.get("/perfil")), "confirm": "yes", "active": "false",
            }).status_code, 403)
        self.assertTrue(db.session.get(Plan, "researcher").active)

    def test_inactive_student_plan_rejects_new_registration_without_removing_old_grants(self):
        existing = create_user("Estudante antigo", "antigo@example.org")
        grant_id = current_grant(existing).id
        db.session.get(Plan, "student").active = False
        db.session.commit()
        visitor = self.app.test_client()
        with self.app.app_context():
            response = visitor.post("/cadastro", data={
                "csrf_token": csrf_from(visitor.get("/cadastro")),
                "name": "Estudante novo", "email": "novo@example.org",
                "password": "senha-muito-segura-123", "confirm": "senha-muito-segura-123",
            })
            self.assertIn("Plano Estudante está indisponível", response.get_data(as_text=True))
        self.assertIsNone(db.session.query(User).filter_by(email="novo@example.org").first())
        self.assertEqual(current_grant(existing).id, grant_id)
        with self.assertRaises(ValueError):
            replace_grant(existing, "student", "student_free", "active")

    def test_archived_project_delete_is_physical_audited_and_blocks_active_job(self):
        project_id = create_project(self.client)
        project = db.session.get(Project, project_id)
        delete_url = f"/projetos/{project_id}/excluir"
        self.assertEqual(self.client.get(delete_url).status_code, 400)
        self.client.post(f"/projetos/{project_id}/arquivar", data={
            "csrf_token": csrf_from(self.client.get("/projetos")), "confirm": "yes",
        })
        self.assertEqual(project.status, "archived")
        page = self.client.get(delete_url)
        self.assertEqual(page.status_code, 200)
        job_id = str(uuid4())
        try:
            with documentary.JOBS_LOCK:
                documentary.PROGRESSOS_HR[job_id] = {"project_id": project_id, "status": "processando"}
            blocked = self.client.post(delete_url, data={
                "csrf_token": csrf_from(page), "confirmation": "deletar",
            })
            self.assertEqual(blocked.status_code, 400)
            self.assertIsNotNone(db.session.get(Project, project_id))
        finally:
            with documentary.JOBS_LOCK:
                documentary.PROGRESSOS_HR.pop(job_id, None)
        self.assertEqual(self.client.post(delete_url, data={
            "csrf_token": csrf_from(self.client.get(delete_url)), "confirmation": "deletar",
        }).status_code, 302)
        self.assertIsNone(db.session.get(Project, project_id))
        self.assertNotIn(project_id, self.client.get("/projetos/arquivados").get_data(as_text=True))
        self.assertIsNotNone(db.session.query(AuditLog).filter_by(
            action="project_permanently_deleted", target_id=project_id).first())

    def test_shared_threshold_markup_and_normalization(self):
        self.assertEqual((MINIMUM, MAXIMUM, DEFAULT), (0.50, 0.90, 0.50))
        project_id = create_project(self.client)
        legacy_html = self.client.get("/raspagem-livre").get_data(as_text=True)
        hybrid_html = self.client.get(f"/analise-documental/projetos/{project_id}").get_data(as_text=True)
        for attribute, expected in (("min", MINIMUM), ("max", MAXIMUM), ("step", STEP), ("value", DEFAULT)):
            pattern = rf'id="(?:limiar-semantico|hr-limiar)"[^>]*{attribute}="([^"]+)"'
            self.assertEqual(float(re.search(pattern, legacy_html).group(1)), expected)
            self.assertEqual(float(re.search(pattern, hybrid_html).group(1)), expected)
        self.assertIn('id="hr-valor-limiar"', hybrid_html)
        self.assertIn('id="hr-limiar" name="limiar_semantico" type="range"', hybrid_html)
        self.assertIn("valor inicial <strong>0,50</strong>", hybrid_html)
        self.assertNotIn("valor inicial <strong>0,70</strong>", hybrid_html)
        self.assertIn("O valor inicial é 0,50", legacy_html)
        self.assertEqual(normalize(None), DEFAULT)
        self.assertEqual(normalize("invalido"), DEFAULT)
        for selected in (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
            with self.subTest(selected=selected):
                self.assertEqual(normalize(str(selected)), selected)
        with self.app.test_request_context("/", method="POST", data={"incluir_lexical": "on", "incluir_semantica": "on"}):
            self.assertEqual(legacy._configuracoes_v3()["limiar_semantico"], DEFAULT)
        for raw in ("0.49", "0.70", "0.91", "invalido"):
            with self.subTest(raw=raw):
                with self.app.test_request_context("/", method="POST", data={
                    "incluir_lexical": "on", "incluir_semantica": "on", "limiar_semantico": raw,
                }):
                    self.assertEqual(legacy._configuracoes_v3()["limiar_semantico"], normalize(raw))

    def test_action_buttons_present_in_list_and_cardbox_markup(self):
        target = create_user("Alvo", "alvo@example.org")
        users_html = self.client.get("/admin/usuarios").get_data(as_text=True)
        self.assertIn('title="Redefinir senha" aria-label="Redefinir senha"', users_html)
        self.assertIn('title="Excluir usuário" aria-label="Excluir usuário"', users_html)
        self.assertNotIn('title="Excluir usuário permanentemente"', users_html)
        self.assertIn('id="admin-users-list" data-view-container', users_html)
        self.assertIn('class="btn btn-outline-primary btn-sm platform-action-button"', users_html)
        self.assertIn(f"/admin/usuarios/{target.id}/redefinir-senha", users_html)
        self.assertIn("Raspagem livre", self.client.get("/raspagem-livre").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
