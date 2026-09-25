"""Regressão da navegação por tipo de raspagem e bases persistentes."""

import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from platform_helpers import create_project, create_user, csrf_from, isolated_platform, login
from platform_core.analyses import create_analysis
from platform_core.extensions import db
from platform_core.models import Analysis, PlanTool, Project
from platform_core.project_lifecycle import archive, restore
from platform_core.services import can_use_tool, seed_platform


class AnalysisBaseTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.user = create_user()
        login(self.client)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def free_project(self, name="Projeto livre"):
        token = csrf_from(self.client.get("/projetos/livres/novo"))
        response = self.client.post("/projetos/livres/novo", data={
            "csrf_token": token, "name": name, "description": "",
        })
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlparse(response.headers["Location"]).query)["project_id"][0]

    def test_menu_and_projects_are_separated(self):
        free_id = self.free_project()
        systematic_id = create_project(self.client, "Projeto sistemático")
        free = self.client.get("/projetos/livres").get_data(as_text=True)
        systematic = self.client.get("/projetos").get_data(as_text=True)
        self.assertIn("Raspagem de dados", free)
        self.assertIn("Busca por termos", free)
        self.assertIn("Busca estruturada", free)
        self.assertNotIn("Raspagem livre", free)
        self.assertNotIn("Raspagem sistemática", free)
        self.assertIn("Análise quali-dados", free)
        self.assertIn("Análise quantitativa", free)
        self.assertNotIn("Raspagem padrão", free)
        self.assertIn("Projeto livre", free)
        self.assertNotIn("Projeto sistemático", free)
        self.assertIn("Projeto sistemático", systematic)
        self.assertNotIn("Projeto livre", systematic)
        self.assertEqual(db.session.get(Project, free_id).scrape_type, "free")
        self.assertEqual(db.session.get(Project, systematic_id).scrape_type, "systematic")
        self.assertEqual(self.client.get(f"/projetos/livres/{systematic_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{free_id}").status_code, 404)

    def test_version_radios_and_project_navigation_keep_existing_values(self):
        page = self.client.get("/raspagem-livre").get_data(as_text=True)
        self.assertNotIn('<select class="form-select" id="versao"', page)
        for version in ("v1", "v3"):
            self.assertIn(f'type="radio" name="versao" value="{version}"', page)
        self.assertNotIn('type="radio" name="versao" value="v2"', page)
        self.assertIn('> Lexical</label>', page)
        self.assertIn('> Híbrido</label>', page)
        self.assertIn('Método de raspagem', page)
        self.assertIn('Escolha o método de raspagem conforme o objetivo', page)
        for path, new_path, archived_path in (
            ("/projetos/livres", "/projetos/livres/novo", "/projetos/livres/arquivados"),
            ("/projetos", "/projetos/novo", "/projetos/arquivados"),
        ):
            html = self.client.get(path).get_data(as_text=True)
            nav = html.split('<nav class="platform-nav"', 1)[1].split('</nav>', 1)[0]
            self.assertNotIn('Projetos arquivados', nav)
            self.assertNotIn(f'href="{new_path}">Novo projeto</a>', nav)
            self.assertIn(f'href="{archived_path}">Projetos arquivados</a>', html)
            self.assertIn('btn btn-outline-primary btn-sm', html)
            self.assertNotIn('Criar projeto', html)

    def test_new_projects_open_scraper_directly_and_forms_have_no_large_heading(self):
        for path in ("/projetos/livres/novo", "/projetos/novo"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn('<h1 class="visually-hidden">Novo projeto</h1>', html)
            self.assertNotIn('<div class="platform-page-heading"><h1>Novo projeto</h1>', html)
            self.assertIn('name="name"', html)
            self.assertIn('name="description"', html)

        free_token = csrf_from(self.client.get("/projetos/livres/novo"))
        free = self.client.post("/projetos/livres/novo", data={
            "csrf_token": free_token, "name": "Livre direto", "description": "Teste",
        })
        self.assertEqual(free.status_code, 302)
        free_id = parse_qs(urlparse(free.headers["Location"]).query)["project_id"][0]
        self.assertEqual(urlparse(free.headers["Location"]).path, "/raspagem-livre")
        free_tool = self.client.get(free.headers["Location"])
        self.assertEqual(free_tool.status_code, 200)
        self.assertIn(f'<option value="{free_id}" selected>', free_tool.get_data(as_text=True))

        systematic_token = csrf_from(self.client.get("/projetos/novo"))
        systematic = self.client.post("/projetos/novo", data={
            "csrf_token": systematic_token, "name": "Sistemático direto",
            "description": "Teste", "libraries": ["relacoes_raciais"],
        })
        self.assertEqual(systematic.status_code, 302)
        systematic_id = systematic.headers["Location"].rsplit("/", 1)[1]
        self.assertEqual(systematic.headers["Location"],
                         f"/analise-documental/projetos/{systematic_id}")
        systematic_tool = self.client.get(systematic.headers["Location"])
        self.assertEqual(systematic_tool.status_code, 200)
        self.assertIn("Método de raspagem", systematic_tool.get_data(as_text=True))

    def test_project_actions_replace_ficha_and_old_url_redirects(self):
        free_id = self.free_project()
        systematic_id = create_project(self.client, "Projeto sistemático")
        free_html = self.client.get("/projetos/livres").get_data(as_text=True)
        systematic_html = self.client.get("/projetos").get_data(as_text=True)
        for html, new_url, archive_url, scraper_url, project_id, project_name in (
            (free_html, "/projetos/livres/novo", "/projetos/livres/arquivados",
             f"/raspagem-livre?project_id={free_id}", free_id, "Projeto livre"),
            (systematic_html, "/projetos/novo", "/projetos/arquivados",
             f"/analise-documental/projetos/{systematic_id}", systematic_id,
             "Projeto sistemático"),
        ):
            toolbar = html.split('<div class="platform-toolbar platform-toolbar-actions">', 1)[1].split('</div>', 1)[0]
            self.assertIn(f'class="btn btn-outline-primary btn-sm" href="{new_url}">Novo projeto</a>', toolbar)
            self.assertIn(f'class="btn btn-outline-primary btn-sm" href="{archive_url}">Projetos arquivados</a>', toolbar)
            self.assertNotIn("+ Novo projeto", toolbar)
            self.assertNotIn("Abrir ficha", html)
            self.assertIn(f'href="{scraper_url}" title="Nova raspagem em {project_name}"', html)
            self.assertIn(f'href="/analises/projeto/{project_id}" title="Bases de análise de {project_name}"', html)
            self.assertEqual(self.client.get(scraper_url).status_code, 200)
            self.assertEqual(self.client.get(f"/analises/projeto/{project_id}").status_code, 200)

        old_free = self.client.get(f"/projetos/livres/{free_id}")
        self.assertEqual(old_free.status_code, 302)
        self.assertEqual(old_free.headers["Location"], f"/raspagem-livre?project_id={free_id}")
        other_user = create_user("Outro", "outro-projeto@example.org")
        other = Project(owner_user_id=other_user.id, name="Alheio", scrape_type="free")
        db.session.add(other)
        db.session.commit()
        self.assertEqual(self.client.get(f"/projetos/livres/{other.id}").status_code, 404)
        self.assertEqual(self.client.get(f"/raspagem-livre?project_id={other.id}").status_code, 404)
        self.assertEqual(self.client.get(f"/analises/projeto/{other.id}").status_code, 404)

    def test_general_base_histories_filter_type_and_owner(self):
        free_id = self.free_project()
        systematic_id = create_project(self.client, "Projeto sistemático")
        free_base = create_analysis(user_id=self.user.id, project_id=free_id,
                                    tool_id="pdf_scraper", tool_version="v1", parameters={}, name="Base livre")
        systematic_base = create_analysis(user_id=self.user.id, project_id=systematic_id,
                                          tool_id="document_analysis", tool_version="lexical", parameters={},
                                          name="Base sistemática")
        old_base = create_analysis(user_id=self.user.id, project_id=None,
                                   tool_id="pdf_scraper", tool_version="v2", parameters={}, name="Base antiga")
        other_user = create_user("Outro", "outro-historico@example.org")
        other_base = create_analysis(user_id=other_user.id, project_id=None,
                                     tool_id="pdf_scraper", tool_version="v1", parameters={}, name="Base alheia")
        free = self.client.get("/analises/livres")
        systematic = self.client.get("/analises/sistematicas")
        self.assertEqual((free.status_code, systematic.status_code), (200, 200))
        free_html = free.get_data(as_text=True)
        systematic_html = systematic.get_data(as_text=True)
        self.assertIn(free_base.id, free_html)
        self.assertIn(old_base.id, free_html)
        self.assertIn("Método legado", free_html)
        self.assertNotIn(systematic_base.id, free_html)
        self.assertNotIn(other_base.id, free_html)
        self.assertIn(systematic_base.id, systematic_html)
        self.assertNotIn(free_base.id, systematic_html)
        self.assertNotIn(old_base.id, systematic_html)
        self.assertIn('Projeto: Projeto livre', free_html)
        self.assertIn('Projeto: Projeto sistemático', systematic_html)

    def test_archiving_preserves_type_and_filters_lists(self):
        free_id = self.free_project()
        systematic_id = create_project(self.client)
        archive(db.session.get(Project, free_id), self.user)
        self.assertIn("Projeto livre", self.client.get("/projetos/livres/arquivados").get_data(as_text=True))
        self.assertNotIn("Projeto livre", self.client.get("/projetos/arquivados").get_data(as_text=True))
        self.assertNotIn("Pesquisa de teste", self.client.get("/projetos/livres/arquivados").get_data(as_text=True))
        restore(db.session.get(Project, free_id), self.user)
        self.assertEqual(db.session.get(Project, free_id).scrape_type, "free")
        self.assertEqual(db.session.get(Project, systematic_id).scrape_type, "systematic")

    def test_completed_base_requires_name_and_survives_relogin(self):
        project_id = self.free_project()
        base = create_analysis(user_id=self.user.id, project_id=project_id,
                               tool_id="pdf_scraper", tool_version="v1", parameters={})
        base.status = "concluida"
        db.session.commit()
        first = self.client.get(f"/analises/{base.id}")
        self.assertEqual(first.status_code, 302)
        self.assertIn("/nomear", first.headers["Location"])
        history = self.client.get(f"/analises/projeto/{project_id}").get_data(as_text=True)
        self.assertIn("Base de análise sem nome", history)
        self.assertNotIn("Análise 24/", history)
        naming = self.client.get(first.headers["Location"])
        self.assertIn("Salvar base", naming.get_data(as_text=True))
        token = csrf_from(naming)
        blank = self.client.post(first.headers["Location"], data={"csrf_token": token, "name": "  "})
        self.assertFalse(db.session.get(Analysis, base.id).name_confirmed)
        self.assertEqual(blank.status_code, 200)
        saved = self.client.post(first.headers["Location"], data={"csrf_token": token,
                                                                "name": "Representações do Nordeste"})
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(db.session.get(Analysis, base.id).name, "Representações do Nordeste")
        self.client.post("/logout", data={"csrf_token": token})
        login(self.client)
        self.assertIn("Representações do Nordeste", self.client.get(
            f"/analises/projeto/{project_id}").get_data(as_text=True))

    def test_free_project_has_original_versions_and_systematic_has_libraries(self):
        free_id = self.free_project()
        systematic_id = create_project(self.client)
        free_page = self.client.get(f"/raspagem-livre?project_id={free_id}").get_data(as_text=True)
        system_page = self.client.get(f"/analise-documental/projetos/{systematic_id}").get_data(as_text=True)
        for version in ("v1", "v3"):
            self.assertIn(f'value="{version}"', free_page)
        self.assertNotIn('name="versao" value="v2"', free_page)
        self.assertIn("Relações raciais", system_page)
        self.assertIn("Bibliotecas", system_page)

    def test_plan_permissions_do_not_cross_types_or_return_after_seed(self):
        free_id = self.free_project()
        systematic_id = create_project(self.client)
        db.session.delete(db.session.get(PlanTool, ("student", "pdf_scraper")))
        db.session.commit()
        seed_platform()
        self.assertFalse(can_use_tool(self.user, "pdf_scraper"))
        self.assertEqual(self.client.get(f"/projetos/livres/{free_id}").status_code, 403)
        self.assertEqual(self.client.get(f"/projetos/livres/novo").status_code, 403)
        self.assertEqual(self.client.get("/analises/livres").status_code, 403)
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{systematic_id}").status_code, 200)
        db.session.delete(db.session.get(PlanTool, ("student", "document_analysis")))
        db.session.commit()
        seed_platform()
        self.assertEqual(self.client.get(f"/analise-documental/projetos/{systematic_id}").status_code, 403)
        self.assertEqual(self.client.get("/analises/sistematicas").status_code, 403)

    def test_matrices_and_graphs_use_theme_tokens(self):
        matrix = Path("templates/platform/access_matrix.html").read_text(encoding="utf-8")
        charts = Path("static/js/plot_theme.js").read_text(encoding="utf-8")
        project_chart = Path("static/js/analysis_dashboard.js").read_text(encoding="utf-8")
        self.assertIn("platform-access-matrix", matrix)
        for token in ("--plot-paper", "--plot-bg", "--plot-text", "--plot-grid"):
            self.assertIn(token, charts)
        self.assertIn("tema-alterado", project_chart)


if __name__ == "__main__":
    unittest.main()
