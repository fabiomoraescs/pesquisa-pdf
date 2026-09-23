"""Apresentação compacta das bibliotecas sem alterar seus dados."""

import unittest
from pathlib import Path

from platform_helpers import create_user, isolated_platform, login
from platform_core.extensions import db
from platform_core.models import VocabularyLibrary
from platform_core.official_libraries import create_draft


class LibraryListViewTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        admin = create_user("Admin", "admin-libraries@example.org", role="admin")
        login(self.client, admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_list_is_initial_and_preserves_official_library_information_and_actions(self):
        library = db.session.get(VocabularyLibrary, "relacoes_raciais")
        self.assertEqual(library.counts_json, {"grupos": 8, "entidades": 75, "variantes": 117})
        self.assertEqual(library.version, "v1")
        response = self.client.get("/admin/bibliotecas")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(
            'class="platform-view-list" id="admin-libraries-list" data-view-container '
            'data-view-storage-key="pesquisapdf-libraries-view-mode"', html,
        )
        self.assertIn('data-view-mode="list" aria-label="Visualização em lista" aria-pressed="true"', html)
        self.assertIn('data-view-mode="cardbox" aria-label="Visualização em cartões" aria-pressed="false"', html)
        self.assertIn("Relações raciais", html)
        self.assertIn("8 grupos · 75 entidades · 117 variantes · v1", html)
        self.assertIn("Publicada", html)
        self.assertIn('title="Abrir ficha de Relações raciais" aria-label="Abrir ficha de Relações raciais"', html)
        self.assertIn('title="Desativar biblioteca" aria-label="Desativar biblioteca"', html)
        self.assertEqual(html.count('class="platform-list-item"'), 1)
        self.assertNotIn(library.description, html)
        self.assertIn(library.description, self.client.get("/admin/bibliotecas/relacoes_raciais").get_data(as_text=True))

    def test_multiple_libraries_keep_same_list_items_and_existing_actions(self):
        db.session.add(create_draft("Biblioteca de teste", "Descrição longa para a ficha."))
        db.session.commit()
        html = self.client.get("/admin/bibliotecas").get_data(as_text=True)
        self.assertEqual(html.count('class="platform-list-item"'), 2)
        self.assertIn("Biblioteca de teste", html)
        self.assertIn("Rascunho", html)
        self.assertIn('title="Abrir ficha de Biblioteca de teste" aria-label="Abrir ficha de Biblioteca de teste"', html)
        self.assertIn('title="Excluir biblioteca" aria-label="Excluir biblioteca"', html)

    def test_cardbox_uses_existing_shared_toggle_without_new_script(self):
        script = (Path(__file__).resolve().parent.parent / "static" / "js" / "platform.js").read_text(encoding="utf-8")
        self.assertIn("container.dataset.viewStorageKey || 'pesquisapdf-view-mode'", script)
        self.assertIn("container.classList.toggle('platform-view-cardbox'", script)


if __name__ == "__main__":
    unittest.main()
