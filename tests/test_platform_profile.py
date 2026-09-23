"""Perfil, tradução e interface da Etapa 3.1.1."""
import re
import unittest
from html import unescape

from sqlalchemy import select

from platform_helpers import create_project, create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import AuditLog, User, UserProfile, VocabularyLibrary
from platform_core.profile import validated_profile
from platform_core.presentation import EDUCATION, GENDER, RACE_COLOR
from platform_core.services import current_grant


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def register(self, **extra):
        payload = {
            "csrf_token": csrf_from(self.client.get("/cadastro")),
            "name": "Ana Silva", "email": "ana@example.org",
            "password": "senha-muito-segura-123", "confirm": "senha-muito-segura-123",
        }
        payload.update(extra)
        return self.client.post("/cadastro", data=payload)

    def test_registration_profile_one_to_one_and_original_access(self):
        response = self.register(education_level="masters", formation_area="Sociologia",
                                 occupation="Professora", institutional_affiliation="Universidade",
                                 gender="female", race_color="brown")
        self.assertEqual(response.status_code, 302)
        user = db.session.scalar(select(User).where(User.email == "ana@example.org"))
        self.assertIsNotNone(user.profile)
        self.assertEqual(db.session.get(UserProfile, user.id), user.profile)
        self.assertEqual(user.profile.education_level, "masters")
        self.assertEqual(user.profile.race_color, "brown")
        self.assertEqual((user.role, current_grant(user).plan_id, current_grant(user).access_mode),
                         ("user", "student", "student_free"))
        self.assertTrue(user.check_password("senha-muito-segura-123"))
        self.assertNotIn("senha-muito-segura-123", user.password_hash)
        self.assertEqual(len(UserProfile.__table__.primary_key.columns), 1)
        self.assertEqual(next(iter(UserProfile.__table__.primary_key.columns)).name, "user_id")

    def test_invalid_education_and_confirmation_leave_no_account(self):
        for extra in ({"education_level": "invalid"}, {"confirm": "outra-senha"}):
            with self.subTest(extra=extra):
                response = self.register(**extra)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(db.session.scalar(select(User).where(User.email == "ana@example.org")))
                self.assertEqual(db.session.query(UserProfile).count(), 0)

    def test_valid_select_values_and_invalid_values(self):
        for field, choices in (("education_level", EDUCATION), ("gender", GENDER), ("race_color", RACE_COLOR)):
            for value in choices:
                with self.subTest(field=field, value=value):
                    self.assertIsNone(validated_profile({field: value})[1])
            self.assertIsNotNone(validated_profile({field: "invalid"})[1])

    def test_existing_user_without_profile_is_safe_and_edit_persists(self):
        user = create_user()
        login(self.client)
        self.assertIsNone(user.profile)
        profile_page = self.client.get("/perfil")
        self.assertEqual(profile_page.status_code, 200)
        self.assertIn('value="pesquisador@example.org"', profile_page.get_data(as_text=True))
        self.assertIsNone(user.profile)
        token = csrf_from(self.client.get("/perfil"))
        response = self.client.post("/perfil", data={
            "csrf_token": token, "name": "Novo nome", "education_level": "doctorate",
            "formation_area": "História", "occupation": "Pesquisador",
            "institutional_affiliation": "Instituto", "gender": "male", "race_color": "black",
            "email": "outro@example.org",
        })
        self.assertEqual(response.status_code, 302)
        db.session.refresh(user)
        self.assertEqual((user.name, user.email), ("Novo nome", "pesquisador@example.org"))
        self.assertEqual((user.profile.education_level, user.profile.gender, user.profile.race_color),
                         ("doctorate", "male", "black"))

    def test_profile_requires_login_and_cannot_edit_another_user(self):
        self.assertIn("/login", self.client.get("/perfil").headers["Location"])
        first = create_user()
        other = create_user("Outra", "outra@example.org")
        login(self.client)
        self.assertEqual(self.client.get("/perfil").status_code, 200)
        token = csrf_from(self.client.get("/perfil"))
        self.client.post("/perfil", data={"csrf_token": token, "name": "Pesquisador", "race_color": "white"})
        self.assertIsNone(other.profile)
        self.assertEqual(other.name, "Outra")
        self.assertEqual(first.profile.race_color, "white")

    def test_admin_individual_profile_but_no_demographics_in_listing(self):
        create_user("Admin", "admin@example.org", "admin", "institutional")
        target = create_user("Ana", "ana@example.org")
        db.session.add(UserProfile(user_id=target.id, gender="female", race_color="brown"))
        db.session.commit()
        login(self.client, "admin@example.org")
        detail = self.client.get(f"/admin/usuarios/{target.id}").get_data(as_text=True)
        self.assertIn("Raça/cor", detail)
        self.assertIn("Parda", detail)
        listing = self.client.get("/admin/usuarios").get_data(as_text=True)
        self.assertNotIn("Raça/cor", listing)
        self.assertNotIn("Gênero", listing)
        self.assertNotIn("Parda", listing)

    def test_password_toggle_markup_and_portuguese_options(self):
        login_html = self.client.get("/login").get_data(as_text=True)
        register_html = self.client.get("/cadastro").get_data(as_text=True)
        self.assertEqual(login_html.count('data-password-toggle='), 1)
        self.assertEqual(register_html.count('data-password-toggle='), 2)
        self.assertEqual(register_html.count('aria-label="Mostrar senha"'), 2)
        self.assertIn('type="button" data-password-toggle="password"', login_html)
        self.assertIn('type="button" data-password-toggle="confirm"', register_html)
        self.assertIn('name="csrf_token"', login_html)
        self.assertIn('name="csrf_token"', register_html)
        for field in ("email", "password", "confirm", "education_level", "gender", "race_color"):
            self.assertIn(f'for="{field}"', register_html)
            self.assertIn(f'id="{field}"', register_html)

    def test_admin_select_labels_are_portuguese(self):
        create_user("Admin", "admin@example.org", "admin", "institutional")
        target = create_user("Ana", "ana@example.org")
        login(self.client, "admin@example.org")
        html = self.client.get(f"/admin/usuarios/{target.id}").get_data(as_text=True)
        for value in ("active", "suspended", "blocked", "trial", "payment_pending",
                      "expired", "canceled", "paid", "student_free", "admin_courtesy",
                      "inherit", "allow", "deny"):
            for visible in re.findall(rf'<option value="{value}"[^>]*>([^<]+)</option>', html):
                self.assertNotEqual(unescape(visible), value)
        self.assertIn('>Bloqueado</option>', html)
        self.assertIn('>Permitir</option>', html)
        self.assertIn('>Gratuito — Estudante</option>', html)
        user_list = self.client.get("/admin/usuarios").get_data(as_text=True)
        self.assertIn("Administrador", user_list)
        self.assertNotIn(">admin<", user_list)
        self.assertNotIn(">active<", user_list)

    def test_audit_and_other_admin_sections_translate_codes(self):
        admin = create_user("Admin", "admin@example.org", "admin", "institutional")
        db.session.add(AuditLog(admin_user_id=admin.id, action="tool_override_changed", target_type="user",
                                target_id=admin.id, before_json={"decision": "inherit"},
                                after_json={"decision": "deny"}))
        db.session.commit()
        login(self.client, "admin@example.org")
        audit = self.client.get("/admin/auditoria").get_data(as_text=True)
        self.assertIn("Permissão de ferramenta alterada", audit)
        self.assertIn("Herdar do plano", audit)
        self.assertIn("Bloquear", audit)
        self.assertNotIn(">tool_override_changed<", audit)
        self.assertNotIn(">inherit<", audit)
        for route in ("/admin/ferramentas", "/admin/planos", "/admin/projetos", "/admin/bibliotecas"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)

    def test_csrf_permission_project_and_library_regression(self):
        self.assertEqual(self.client.post("/perfil", data={"name": "Teste"}).status_code, 400)
        user = create_user()
        login(self.client)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        project_id = create_project(self.client)
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{project_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{project_id}/vocabulario").status_code, 200)
        self.assertEqual(db.session.get(VocabularyLibrary, "relacoes_raciais").counts_json,
                         {"grupos": 8, "entidades": 75, "variantes": 117})
