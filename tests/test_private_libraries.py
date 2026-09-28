"""Bibliotecas privadas reutilizam o fluxo oficial, sem vazamento entre usuários."""
import io
import re
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select, text

from platform_helpers import create_project, create_user, csrf_from, isolated_platform, login, seed_legacy_platform
from platform_core.extensions import db
from platform_core.models import Project, ProjectLibrary, VocabularyLibrary
from test_library_import import workbook_bytes

PREFIX = "/busca-estruturada/bibliotecas"


class PrivateLibraryTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform(); self.app = self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        # O fixture mantém app_context; cada cliente deve carregar sua própria sessão.
        from flask import g
        def clear_request_cache(_):
            for key in ("_login_user", "csrf_token", "csrf_valid"):
                g.pop(key, None)
        self.app.teardown_request(clear_request_cache)
        self.a = create_user(email="a@example.org"); self.b = create_user(email="b@example.org")
        self.admin = create_user(email="admin@example.org", role="admin")
        self.client = self.app.test_client(); login(self.client, "a@example.org")
        self.other = self.app.test_client(); login(self.other, "b@example.org")

    def draft(self, name="Minha biblioteca"):
        response = self.client.post(PREFIX + "/nova", data={
            "csrf_token": csrf_from(self.client.get(PREFIX + "/nova")), "name": name,
            "owner_user_id": self.b.id, "scope": "global"})
        self.assertEqual(response.status_code, 302)
        return db.session.get(VocabularyLibrary, response.location.rsplit("/", 1)[-1])

    def mutate(self, library, action, **values):
        url = PREFIX + "/" + library.id
        return self.client.post(url + action, data={
            "csrf_token": csrf_from(self.client.get(url)), "base_hash": library.content_hash, **values})

    def test_manual_editor_owner_publish_selection_and_direct_access(self):
        self.assertIn("Inserir biblioteca", self.client.get("/projetos/novo").text)
        library = self.draft()
        self.assertEqual(library.owner_user_id, self.a.id)
        self.assertNotIn(library.id, self.client.get("/projetos/novo").text)  # rascunho
        self.assertEqual(self.mutate(library, "/grupos", name="Pesquisa", active="on").status_code, 302)
        self.assertEqual(self.mutate(library, "/entidades", canonical="Escola", groups="pesquisa",
                                     entity_type="conceito", active="on").status_code, 302)
        published = self.mutate(library, "/publicar", confirm="yes")
        self.assertEqual(published.location, f"/projetos/novo?library_id={library.id}")
        own = self.client.get(published.location).text
        self.assertIn(f'value="{library.id}" checked', own)
        self.assertNotIn(library.id, self.other.get("/projetos/novo").text)
        self.assertIn('value="relacoes_raciais"', self.other.get("/projetos/novo").text)
        url = PREFIX + "/" + library.id
        self.assertEqual(self.other.get(url).status_code, 404)
        token = csrf_from(self.other.get("/projetos/novo"))
        for action in ("/grupos", "/entidades", "/variantes", "/itens/estado", "/publicar"):
            with self.subTest(action=action):
                self.assertEqual(self.other.post(url + action, data={"csrf_token": token}).status_code, 404)
        denied = self.other.post("/projetos/novo", data={"csrf_token": token, "name": "Tentativa", "libraries": library.id})
        self.assertIn("Selecione ao menos uma biblioteca disponível", denied.text)
        self.assertIsNone(db.session.scalar(select(Project).where(Project.name == "Tentativa")))
        project_id = create_project(self.client, libraries=(library.id,))
        self.assertIsNotNone(db.session.get(ProjectLibrary, (project_id, library.id)))
        self.assertIn(library.name, self.client.get(f"/analise-documental/projetos/{project_id}").text)

    def test_import_private_template_and_admin_supervision(self):
        response = self.client.post(PREFIX + "/importar", data={
            "csrf_token": csrf_from(self.client.get(PREFIX + "/importar")),
            "spreadsheet": (io.BytesIO(workbook_bytes(name="Importada privada")), "minha.xlsx")})
        self.assertEqual(response.status_code, 200)
        preview = re.search('name="preview_token" value="([^"]+)"', response.text).group(1)
        saved = self.client.post(PREFIX + "/importar/confirmar", data={"csrf_token": csrf_from(response), "preview_token": preview})
        library = db.session.get(VocabularyLibrary, saved.location.rsplit("/", 1)[-1])
        self.assertEqual(library.owner_user_id, self.a.id)
        self.mutate(library, "/publicar", confirm="yes")
        self.assertIn(library.id, self.client.get("/projetos/novo").text)
        self.assertNotIn(library.id, self.other.get("/projetos/novo").text)
        self.assertEqual(self.other.get(PREFIX + "/" + library.id).status_code, 404)
        download = self.client.get(PREFIX + "/modelo")
        self.assertEqual(download.status_code, 200); download.close()
        admin_client = self.app.test_client(); login(admin_client, "admin@example.org")
        self.assertEqual(admin_client.get("/admin/bibliotecas/" + library.id).status_code, 200)
        self.assertNotIn(library.id, admin_client.get("/projetos/novo").text)

    def test_official_creation_remains_global_and_preview_cannot_change_context(self):
        client = self.app.test_client(); login(client, "admin@example.org")
        response = client.post("/admin/bibliotecas/importar", data={
            "csrf_token": csrf_from(client.get("/admin/bibliotecas/importar")),
            "spreadsheet": (io.BytesIO(workbook_bytes(name="Biblioteca oficial nova")), "oficial.xlsx")})
        preview = re.search('name="preview_token" value="([^"]+)"', response.text).group(1)
        wrong = client.post(PREFIX + "/importar/confirmar", data={"csrf_token": csrf_from(response), "preview_token": preview})
        self.assertEqual(wrong.location, PREFIX + "/importar")
        client.post("/admin/bibliotecas/importar/confirmar", data={"csrf_token": csrf_from(response), "preview_token": preview})
        library = db.session.get(VocabularyLibrary, "biblioteca_oficial_nova")
        self.assertIsNone(library.owner_user_id)
        client.post("/admin/bibliotecas/" + library.id + "/publicar", data={
            "csrf_token": csrf_from(response), "confirm": "yes", "base_hash": library.content_hash})
        for viewer in (self.client, self.other):
            self.assertIn(library.id, viewer.get("/projetos/novo").text)
        # Mesmo nome em contextos privados não revela nem bloqueia nomes de outros donos.
        self.draft("Biblioteca oficial nova")

    def test_security_and_shared_compact_buttons(self):
        self.assertEqual(self.app.test_client().get(PREFIX + "/nova").status_code, 302)
        self.assertEqual(self.client.post(PREFIX + "/nova", data={"name": "Sem token"}).status_code, 400)
        self.assertEqual(self.client.get("/admin/bibliotecas/nova").status_code, 403)
        self.assertEqual(self.client.get(PREFIX + "/relacoes_raciais").status_code, 404)
        for path in ("/nova", "/importar"):
            html = self.client.get(PREFIX + path).text
            for label in ("Criar manualmente", "Importar biblioteca", "Baixar modelo"):
                self.assertRegex(html, r'class="btn [^"]*btn-sm"[^>]*>' + label + "</a>")
        from platform_core.models import UserToolOverride
        db.session.add(UserToolOverride(user_id=self.a.id, tool_id="document_analysis", decision="deny")); db.session.commit()
        self.assertEqual(self.client.get(PREFIX + "/nova").status_code, 403)


class PrivateLibraryMigrationTests(unittest.TestCase):
    def test_upgrade_from_current_head_preserves_global_data_and_constraints(self):
        from app import create_app
        from flask_migrate import upgrade, downgrade
        from sqlalchemy import inspect
        from platform_core.official_libraries import create_draft
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({"TESTING": True, "SECRET_KEY": "migration-test",
                              "SQLALCHEMY_DATABASE_URI": f"sqlite:///{Path(directory).as_posix()}/db.sqlite",
                              "PLATFORM_DATA_DIR": directory})
            with app.app_context():
                migrations = str(Path(__file__).resolve().parents[1] / "migrations")
                try:
                    seed_legacy_platform(migrations, "c73a91e24f08")
                    before = db.session.execute(text("SELECT id, snapshot_json, content_hash FROM vocabulary_libraries")).all()
                    self.assertNotIn("owner_user_id", {c["name"] for c in inspect(db.engine).get_columns("vocabulary_libraries")})
                    db.session.remove(); upgrade(directory=migrations, revision="head")
                    after = db.session.execute(text("SELECT id, snapshot_json, content_hash FROM vocabulary_libraries")).all()
                    self.assertEqual(before, after)
                    self.assertTrue(all(lib.owner_user_id is None for lib in db.session.scalars(select(VocabularyLibrary))))
                    user = create_user(); private = create_draft("Privada migrada", "", owner_user_id=user.id)
                    db.session.commit(); self.assertEqual(private.owner_user_id, user.id)
                    self.assertEqual(db.session.execute(text("PRAGMA foreign_key_check")).all(), [])
                    db.session.remove()
                    with self.assertRaises(SystemExit):  # Flask-Migrate converte recusa em saída não-zero.
                        downgrade(directory=migrations, revision="c73a91e24f08")
                    self.assertIsNotNone(db.session.get(VocabularyLibrary, private.id).owner_user_id)
                finally:
                    db.session.remove(); db.engine.dispose()
