"""Dashboard leve e foto do próprio usuário em armazenamento isolado."""

import io
import re
import unittest

from PIL import Image

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import Analysis, PlanTool, Project
from platform_core.profile import profile_photo_path


def image_file(width=800, height=400, color="red", format="PNG"):
    payload = io.BytesIO()
    Image.new("RGB", (width, height), color).save(payload, format=format)
    payload.seek(0)
    return payload


class DashboardAvatarTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def _send_photo(self, payload, filename="foto.png", **crop):
        return self.client.post("/perfil/foto", data={
            "csrf_token": csrf_from(self.client.get("/perfil")),
            "photo": (payload, filename), **crop,
        }, content_type="multipart/form-data")

    def test_dashboard_is_root_and_uses_only_accessible_owned_records(self):
        owner = create_user()
        stranger = create_user("Outro", "outro@example.org")
        own_free = Project(owner_user_id=owner.id, name="Livre própria", scrape_type="free")
        own_systematic = Project(owner_user_id=owner.id, name="Sistemática própria", scrape_type="systematic")
        foreign = Project(owner_user_id=stranger.id, name="Projeto alheio", scrape_type="free")
        db.session.add_all((own_free, own_systematic, foreign))
        db.session.flush()
        db.session.add_all((
            Analysis(user_id=owner.id, project_id=own_free.id, name="Base própria", name_confirmed=True,
                     source_type="project", tool_id="pdf_scraper", tool_version="v1"),
            Analysis(user_id=owner.id, project_id=own_systematic.id, name="Base sistemática", name_confirmed=True,
                     source_type="project", tool_id="document_analysis", tool_version="lexical"),
            Analysis(user_id=stranger.id, project_id=foreign.id, name="Base alheia", name_confirmed=True,
                     source_type="project", tool_id="pdf_scraper", tool_version="v1"),
        ))
        db.session.commit()
        login(self.client)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for label in ("Dashboard", "Bases de análise recentes", "Projeto: Livre própria",
                      "Projeto: Sistemática própria", "Base própria", "Base sistemática"):
            self.assertIn(label, html)
        self.assertNotIn("Projetos recentes", html)
        self.assertNotIn("Projeto alheio", html)
        self.assertNotIn("Base alheia", html)
        self.assertIn("Projetos ativos</span><strong>2</strong>", html)
        self.assertIn("Bases de análise</span><strong>2</strong>", html)
        db.session.delete(db.session.get(PlanTool, ("student", "pdf_scraper")))
        db.session.commit()
        restricted = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Livre própria", restricted)
        self.assertNotIn("Base própria", restricted)
        self.assertIn("Sistemática própria", restricted)
        self.assertIn("Projetos ativos</span><strong>1</strong>", restricted)

    def test_header_uses_profile_link_and_fallback_without_sidebar_profile(self):
        create_user()
        login(self.client)
        html = self.client.get("/").get_data(as_text=True)
        sidebar = html.split('<aside class="platform-sidebar"', 1)[1].split("</aside>", 1)[0]
        self.assertIn('href="/"', sidebar)
        self.assertIn('aria-current="page"', sidebar)
        self.assertNotIn('href="/perfil"', sidebar)
        self.assertIn('class="platform-header-user">Pesquisador', html)
        self.assertIn('class="platform-header-profile-link" href="/perfil">Perfil</a>', html)
        self.assertIn('class="platform-header-avatar" href="/perfil"', html)
        self.assertIn('<svg class="platform-icon"', html)
        self.assertEqual(self.client.get("/perfil/foto").status_code, 404)
        profile = self.client.get("/perfil").get_data(as_text=True)
        self.assertIn('class="platform-header-context">Perfil', profile)
        self.assertNotIn('aria-current="page" href="/"', profile)

    def test_upload_crops_normalizes_replaces_persists_and_removes_photo(self):
        user = create_user()
        login(self.client)
        first = self._send_photo(image_file(), "../../../../nome-original.png",
                                 crop_x="0", crop_y="0", crop_size="400")
        self.assertEqual(first.status_code, 302)
        path = profile_photo_path(user.id)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "avatar.webp")
        self.assertEqual([item.name for item in path.parent.iterdir()], ["avatar.webp"])
        with Image.open(path) as photo:
            self.assertEqual((photo.format, photo.size), ("WEBP", (512, 512)))
        response = self.client.get("/perfil/foto")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/webp")
        self.assertTrue(response.cache_control.private)
        first_url = re.search(r'/perfil/foto\?v=\d+', self.client.get("/").get_data(as_text=True)).group()
        self.assertEqual(self._send_photo(image_file(400, 800, "green", "JPEG"), "vertical.jpg").status_code, 302)
        with Image.open(path) as photo:
            self.assertEqual(photo.size, (512, 512))
            self.assertEqual(photo.getpixel((256, 256))[1] > 90, True)
        second_url = re.search(r'/perfil/foto\?v=\d+', self.client.get("/").get_data(as_text=True)).group()
        self.assertNotEqual(first_url, second_url)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/perfil"))})
        login(self.client)
        self.assertIn('/perfil/foto?v=', self.client.get("/").get_data(as_text=True))
        removed = self.client.post("/perfil/foto/remover", data={
            "csrf_token": csrf_from(self.client.get("/perfil")),
        })
        self.assertEqual(removed.status_code, 302)
        self.assertFalse(path.exists())
        self.assertNotIn('/perfil/foto?v=', self.client.get("/").get_data(as_text=True))

    def test_photo_rejects_invalid_large_and_out_of_bounds_input(self):
        user = create_user()
        login(self.client)
        self.assertEqual(self._send_photo(io.BytesIO(b"not an image")).status_code, 400)
        self.assertEqual(self._send_photo(io.BytesIO(b"x" * (5 * 1024 * 1024 + 1))).status_code, 413)
        self.assertEqual(self._send_photo(image_file(), crop_x="700", crop_y="0", crop_size="400").status_code, 400)
        malformed = self._send_photo(image_file(), crop_x="invalido", crop_y="0", crop_size="400")
        self.assertEqual(malformed.status_code, 400)
        self.assertIn("A área de recorte é inválida", malformed.get_data(as_text=True))
        self.assertFalse(profile_photo_path(user.id).exists())
        self.assertEqual(self.client.post("/perfil/foto", data={
            "photo": (image_file(), "foto.png"),
        }, content_type="multipart/form-data").status_code, 400)

    def test_square_webp_upload_keeps_square_output(self):
        user = create_user()
        login(self.client)
        self.assertEqual(self._send_photo(image_file(300, 300, "blue", "WEBP"), "quadrada.webp").status_code, 302)
        with Image.open(profile_photo_path(user.id)) as photo:
            self.assertEqual((photo.format, photo.size), ("WEBP", (512, 512)))

    def test_photo_is_private_to_session_and_removed_with_permanent_account(self):
        target = create_user()
        create_user("Outro", "outro@example.org")
        create_user("Admin", "admin@example.org", "admin", "institutional")
        login(self.client)
        self.assertEqual(self._send_photo(image_file()).status_code, 302)
        path = profile_photo_path(target.id)
        outsider = self.app.test_client()
        with self.app.app_context():
            login(outsider, "outro@example.org")
            self.assertEqual(outsider.get("/perfil/foto").status_code, 404)
            self.assertEqual(outsider.post(f"/perfil/{target.id}/foto", data={
                "csrf_token": csrf_from(outsider.get("/perfil")),
            }).status_code, 404)
        self.assertTrue(path.exists())
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/perfil"))})
        login(self.client, "admin@example.org")
        endpoint = f"/admin/usuarios/{target.id}/excluir-permanentemente"
        deleted = self.client.post(endpoint, data={
            "csrf_token": csrf_from(self.client.get(endpoint)), "confirmation": "excluir",
        })
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
