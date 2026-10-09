"""Contratos finais da Fase 6: tutoriais e exclusão permanente Instagram."""

from __future__ import annotations

import unittest
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import func, select

from platform_helpers import create_user, csrf_from, isolated_platform, login
from platform_core.extensions import db
from platform_core.instagram_lifecycle import delete_instagram_project_permanently
from platform_core.instagram_taxonomy import classify_media, create_initial_taxonomy, taxonomy_dimensions
from platform_core.models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramAccountSnapshot,
    InstagramMedia,
    InstagramMediaClassification,
    InstagramMediaObservation,
    InstagramTaxonomyCategory,
    InstagramTaxonomyDimension,
    PlanTool,
)
from platform_core.platform_help import tutorial_for
from platform_core.services import ANALYTICS_INSTAGRAM_TOOL


UTC = timezone.utc
OBSERVED_AT = datetime(2026, 10, 6, 14, 30, tzinfo=UTC)


class InstagramFinalAdjustmentsTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()
        self.owner = create_user("Pesquisadora", "pesquisadora-instagram@example.org")
        self.other = create_user("Outra", "outra-instagram-final@example.org")
        db.session.add(PlanTool(plan_id="student", tool_id=ANALYTICS_INSTAGRAM_TOOL))
        db.session.commit()
        self.project, self.account, self.media = self._project_graph(self.owner, "Projeto A")
        login(self.client, self.owner.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    @staticmethod
    def _archive(project):
        project.status = "archived"
        project.archived_at = OBSERVED_AT
        db.session.commit()

    def _project_graph(self, owner, name):
        project = AnalyticsProject(owner_user_id=owner.id, name=name)
        db.session.add(project)
        db.session.flush()
        account = InstagramAccount(
            analytics_project_id=project.id,
            username=f"@{name.lower().replace(' ', '_')}",
            username_normalized=name.lower().replace(" ", "_"),
            external_id=f"account-{project.id}",
        )
        run = AnalyticsRun(
            analytics_project_id=project.id,
            source_kind="csv",
            status="completed",
            started_at=OBSERVED_AT,
            completed_at=OBSERVED_AT,
            record_count=1,
        )
        db.session.add_all((account, run))
        db.session.flush()
        media = InstagramMedia(
            instagram_account_id=account.id,
            external_id=f"media-{project.id}",
            caption_original="Conteúdo de teste para exclusão.",
            published_at=OBSERVED_AT,
            media_type_raw="REEL",
        )
        db.session.add(media)
        db.session.flush()
        db.session.add_all((
            InstagramMediaObservation(
                instagram_media_id=media.id, analytics_run_id=run.id,
                observed_at=OBSERVED_AT, reach=1000, interactions=80,
            ),
            InstagramAccountSnapshot(
                instagram_account_id=account.id, analytics_run_id=run.id,
                observed_at=OBSERVED_AT, followers_count=8000,
            ),
        ))
        create_initial_taxonomy(project)
        dimensions = taxonomy_dimensions(project.id)
        selections = {}
        for dimension in dimensions:
            if not dimension.categories:
                continue
            selections[dimension.id] = [
                category.id for category in dimension.categories[:2 if dimension.selection_mode == "multi" else 1]
            ]
        classify_media(project, media.id, selections, owner)
        # Uma troca em dimensão única cria também uma classificação histórica.
        first_single = next(item for item in dimensions if item.selection_mode == "single" and len(item.categories) > 1)
        selections[first_single.id] = [first_single.categories[1].id]
        classify_media(project, media.id, selections, owner)
        db.session.commit()
        return project, account, media

    def _delete_url(self, project=None):
        project = project or self.project
        return f"/analytics/instagram/projects/{project.id}/delete"

    def _post_delete(self, project=None, *, confirmation="deletar", csrf=True):
        project = project or self.project
        data = {"confirmation": confirmation}
        if csrf:
            data["csrf_token"] = csrf_from(self.client.get("/analytics/instagram"))
        return self.client.post(self._delete_url(project), data=data)

    def test_tutorials_reuse_dialog_and_document_their_own_real_interfaces(self):
        dashboard_url = f"/analytics/instagram/projects/{self.project.id}/dashboard"
        taxonomy_url = f"/analytics/instagram/projects/{self.project.id}/taxonomies"
        classifications_url = f"/analytics/instagram/projects/{self.project.id}/classifications"
        expected = {
            dashboard_url: (
                "Como usar e interpretar o Dashboard", "Interações ÷ alcance × 100",
                "8/10", "Taxonomias", "Não classificado", "recomendações editoriais",
            ),
            taxonomy_url: (
                "Como organizar as Taxonomias", "Tipo técnico e classificação editorial",
                "Escolha única", "Múltiplas escolhas", "Criar estrutura inicial",
                "Classificação de conteúdos", "Arquivar uma dimensão ou categoria",
            ),
            classifications_url: (
                "Como classificar os conteúdos", "classificação é manual e humana",
                "Parcialmente classificado", "Uma nova importação de métricas",
                "Use o campo Exibir para visualizar Todas, Não classificadas, Parcialmente classificadas ou Classificadas.",
                "A classificação organiza a análise",
            ),
        }
        for url, texts in expected.items():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                self.assertIn('class="btn btn-outline-primary info-icon-button', body)
                self.assertIn('data-platform-tutorial-open', body)
                self.assertIn('id="platform-tutorial-dialog"', body)
                self.assertIn('platform_tutorial.js', body)
                for text in texts:
                    self.assertIn(text, body)

        for key, required_sections in {
            "instagram_dashboard": {"finalidade-e-periodo", "indicadores-e-cobertura", "indicadores-de-desempenho", "comparacoes", "graficos-ranking-e-tabela"},
            "instagram_taxonomies": {"finalidade", "dimensoes-e-categorias", "estrutura-inicial", "personalizar", "arquivar-e-integrar"},
            "instagram_classifications": {"finalidade-e-fluxo", "lista-e-estados", "preencher-dimensoes", "historico-e-reimportacao", "dashboard-e-consistencia"},
        }.items():
            with self.subTest(tutorial=key):
                self.assertTrue(required_sections.issubset({section["id"] for section in tutorial_for(key)["sections"]}))

    def test_local_actions_keep_context_and_reuse_icon_only_action_buttons(self):
        dimensions = taxonomy_dimensions(self.project.id)
        first_dimension = dimensions[0]
        category_dimension = next(
            item for item in dimensions if item.id != first_dimension.id and item.categories
        )
        category = category_dimension.categories[0]
        self.media.permalink = "https://instagram.example.test/p/conteudo"
        db.session.commit()

        dashboard_url = f"/analytics/instagram/projects/{self.project.id}/dashboard"
        dashboard = self.client.get(
            dashboard_url + f"?period=all&sort=reach&taxonomy_dimension={first_dimension.id}"
        ).get_data(as_text=True)
        self.assertLess(dashboard.index("Voltar ao projeto"), dashboard.index("data-platform-tutorial-open"))
        self.assertIn('class="platform-project-tabs"', dashboard)
        self.assertIn('data-project-tabs', dashboard)
        self.assertIn('class="platform-project-tabs-menu-toggle"', dashboard)
        self.assertIn('aria-label="Menu de navegação do projeto"', dashboard)
        self.assertIn('aria-expanded="false"', dashboard)
        self.assertIn('aria-controls="instagram-project-tabs-menu-', dashboard)
        self.assertIn('class="platform-project-tabs-mobile-menu"', dashboard)
        self.assertEqual(len(re.findall(r'class="platform-project-tab(?: is-current)?"', dashboard)), 14)
        mobile_tabs = dashboard[dashboard.index('class="platform-project-tabs-mobile-menu"'):]
        self.assertLess(mobile_tabs.index('>Dashboard</a>'), mobile_tabs.index('>Taxonomias</a>'))
        self.assertLess(mobile_tabs.index('>Taxonomias</a>'), mobile_tabs.index('>Classificação de conteúdos</a>'))
        self.assertLess(mobile_tabs.index('>Classificação de conteúdos</a>'), mobile_tabs.index('>Fonte de dados</a>'))
        self.assertLess(mobile_tabs.index('>Fonte de dados</a>'), mobile_tabs.index('>Comentários</a>'))
        self.assertLess(mobile_tabs.index('>Comentários</a>'), mobile_tabs.index('>Insights</a>'))
        self.assertLess(mobile_tabs.index('>Insights</a>'), mobile_tabs.index('>Relatórios</a>'))
        self.assertIn(f'href="{dashboard_url}" aria-current="page">Dashboard</a>', mobile_tabs)
        self.assertIn(f'href="{dashboard_url}" aria-current="page">Dashboard</a>', dashboard)
        self.assertIn(
            f'href="/analytics/instagram/projects/{self.project.id}/taxonomies">Taxonomias</a>', dashboard,
        )
        self.assertIn(
            f'href="/analytics/instagram/projects/{self.project.id}/classifications">Classificação de conteúdos</a>',
            dashboard,
        )
        self.assertIn(
            f'href="/analytics/instagram/projects/{self.project.id}/comments">Comentários</a>', dashboard,
        )
        self.assertIn(
            f'href="/analytics/instagram/projects/{self.project.id}/imports/new">Fonte de dados</a>', dashboard,
        )
        self.assertIn(
            f'href="/analytics/instagram/projects/{self.project.id}/insights">Insights</a>', dashboard,
        )
        self.assertIn(
            f'href="/analytics/instagram/projects/{self.project.id}/reports">Relatórios</a>', dashboard,
        )
        self.assertLess(
            dashboard.index(f'href="/analytics/instagram/projects/{self.project.id}/imports/new">Fonte de dados</a>'),
            dashboard.index(f'href="/analytics/instagram/projects/{self.project.id}/comments">Comentários</a>'),
        )
        self.assertLess(
            dashboard.index(f'href="/analytics/instagram/projects/{self.project.id}/comments">Comentários</a>'),
            dashboard.index(f'href="/analytics/instagram/projects/{self.project.id}/insights">Insights</a>'),
        )
        self.assertLess(
            dashboard.index(f'href="/analytics/instagram/projects/{self.project.id}/insights">Insights</a>'),
            dashboard.index(f'href="/analytics/instagram/projects/{self.project.id}/reports">Relatórios</a>'),
        )
        self.assertIn('id="instagram-taxonomy-performance"', dashboard)
        self.assertIn('action="' + dashboard_url + '#instagram-taxonomy-performance"', dashboard)
        self.assertIn('data-partial-target="#instagram-taxonomy-performance"', dashboard)
        self.assertIn('id="instagram-media-table"', dashboard)
        self.assertIn('id="instagram-dashboard-content"', dashboard)
        self.assertIn('action="' + dashboard_url + '#instagram-dashboard-period"', dashboard)
        self.assertIn('data-partial-target="#instagram-dashboard-content"', dashboard)
        self.assertIn('#instagram-media-table', dashboard)
        self.assertIn('data-partial-target="#instagram-media-table"', dashboard)
        self.assertIn('class="form-select platform-filter-select" id="instagram-dashboard-period-select"', dashboard)
        self.assertIn('name="period" data-filter-width="short" data-auto-submit', dashboard)
        self.assertIn('<label class="visually-hidden" for="instagram-dashboard-period-select">Período</label>', dashboard)
        self.assertIn('<option value="all" selected>Período: Todo o histórico</option>', dashboard)
        self.assertIn('name="sort" value="reach"', dashboard)
        self.assertIn(f'name="taxonomy_dimension" value="{first_dimension.id}"', dashboard)
        self.assertIn('class="form-select platform-filter-select" id="instagram-dashboard-sort-select"', dashboard)
        self.assertIn('name="sort" data-filter-width="long" data-auto-submit', dashboard)
        self.assertIn('<label class="visually-hidden" for="instagram-dashboard-sort-select">Ordenar por</label>', dashboard)
        self.assertIn('<option value="reach" selected>Ordenar por: Maior alcance</option>', dashboard)
        self.assertEqual(dashboard.count('platform-filter-control-row'), 2)
        self.assertEqual(dashboard.count('platform-filter-band'), 1)
        self.assertNotIn('platform-panel mb-3" id="instagram-dashboard-period"', dashboard)
        self.assertNotIn(f'href="{dashboard_url}?period=7d', dashboard)
        self.assertNotIn(f'href="{dashboard_url}?period=all&amp;sort=recent', dashboard)
        self.assertIn('js/platform_auto_submit.js', dashboard)
        self.assertEqual(dashboard.count('platform-themed-table'), 4)
        media_table = dashboard[dashboard.index('id="instagram-media-table"'):]
        self.assertIn('class="btn btn-outline-primary btn-sm platform-action-button"', media_table)
        self.assertIn('title="Abrir publicação media-', media_table)
        self.assertIn('aria-label="Abrir publicação media-', media_table)
        self.assertIn('target="_blank" rel="noopener noreferrer"', media_table)
        self.assertNotIn('>Abrir</a>', media_table)

        refreshed_media_table = self.client.get(
            dashboard_url + f"?period=all&sort=interactions&taxonomy_dimension={first_dimension.id}"
        ).get_data(as_text=True)
        refreshed_media_table = refreshed_media_table[
            refreshed_media_table.index('id="instagram-media-table"'):
        ]
        self.assertIn('platform-themed-table', refreshed_media_table)
        self.assertIn('title="Abrir publicação media-', refreshed_media_table)

        taxonomy_url = f"/analytics/instagram/projects/{self.project.id}/taxonomies"
        taxonomy_page = self.client.get(taxonomy_url)
        taxonomy = taxonomy_page.get_data(as_text=True)
        self.assertLess(taxonomy.index("Voltar ao projeto"), taxonomy.index("data-platform-tutorial-open"))
        self.assertIn(f'href="{taxonomy_url}" aria-current="page">Taxonomias</a>', taxonomy)
        taxonomy_mobile_tabs = taxonomy[taxonomy.index('class="platform-project-tabs-mobile-menu"'):]
        self.assertIn(f'href="{taxonomy_url}" aria-current="page">Taxonomias</a>', taxonomy_mobile_tabs)
        self.assertGreater(taxonomy.index('class="platform-project-tabs"'), taxonomy.index("data-platform-tutorial-open"))
        self.assertIn(f'id="instagram-taxonomy-dimension-{first_dimension.id}"', taxonomy)
        self.assertIn(f'title="Arquivar dimensão {first_dimension.name}"', taxonomy)
        self.assertIn(f'aria-label="Arquivar categoria {category.name}"', taxonomy)
        self.assertIn(f'data-partial-target="#instagram-taxonomy-dimension-{first_dimension.id}"', taxonomy)
        self.assertIn(f'data-partial-target="#instagram-taxonomy-dimension-{category_dimension.id}"', taxonomy)
        self.assertIn('data-partial-target="#instagram-taxonomy-workspace"', taxonomy)
        self.assertIn("platform-action-button", taxonomy)
        self.assertNotIn(">Arquivar</button>", taxonomy)

        dimension_response = self.client.post(
            f"{taxonomy_url}/dimensions/{first_dimension.id}/state",
            data={"active": "false", "csrf_token": csrf_from(taxonomy_page)},
        )
        self.assertEqual(dimension_response.status_code, 302)
        self.assertTrue(dimension_response.headers["Location"].endswith(
            f"#instagram-taxonomy-dimension-{first_dimension.id}"
        ))
        restored_taxonomy = self.client.get(taxonomy_url).get_data(as_text=True)
        self.assertIn(f'title="Restaurar dimensão {first_dimension.name}"', restored_taxonomy)

        category_page = self.client.get(taxonomy_url)
        category_response = self.client.post(
            f"{taxonomy_url}/categories/{category.id}/state",
            data={"active": "false", "csrf_token": csrf_from(category_page)},
        )
        self.assertEqual(category_response.status_code, 302)
        self.assertTrue(category_response.headers["Location"].endswith(
            f"#instagram-taxonomy-dimension-{category_dimension.id}"
        ))

        classifications_url = f"/analytics/instagram/projects/{self.project.id}/classifications"
        filtered_classifications = self.client.get(classifications_url + "?status=partial").get_data(as_text=True)
        self.assertLess(filtered_classifications.index("Voltar ao projeto"), filtered_classifications.index("data-platform-tutorial-open"))
        self.assertIn(
            f'href="{classifications_url}" aria-current="page">Classificação de conteúdos</a>', filtered_classifications,
        )
        self.assertIn('action="' + classifications_url + '#instagram-classification-filters"', filtered_classifications)
        self.assertIn('id="instagram-classifications-list"', filtered_classifications)
        self.assertIn('data-partial-target="#instagram-classifications-list"', filtered_classifications)
        self.assertIn('class="form-select platform-filter-select" id="instagram-classification-status-select"', filtered_classifications)
        self.assertIn('name="status" data-filter-width="standard" data-auto-submit', filtered_classifications)
        self.assertIn('<label class="visually-hidden" for="instagram-classification-status-select">Exibir</label>', filtered_classifications)
        self.assertIn('<option value="partial" selected>Exibir: Parcialmente classificadas</option>', filtered_classifications)
        self.assertEqual(filtered_classifications.count('platform-filter-control-row'), 1)
        self.assertEqual(filtered_classifications.count('platform-filter-band'), 1)
        self.assertNotIn('platform-panel mb-3" id="instagram-classification-filters"', filtered_classifications)
        self.assertNotIn(f'href="{classifications_url}?status=all', filtered_classifications)
        self.assertIn('js/platform_auto_submit.js', filtered_classifications)
        classifications = self.client.get(classifications_url).get_data(as_text=True)
        self.assertIn('title="Editar classificação"', classifications)
        self.assertIn('aria-label="Abrir publicação media-', classifications)
        self.assertIn('target="_blank" rel="noopener noreferrer"', classifications)
        self.assertNotIn(">Classificar</a>", classifications)
        self.assertNotIn(">Abrir</a>", classifications)

        edit_page = self.client.get(
            f"{classifications_url}/{self.media.id}"
        )
        selections = {"csrf_token": csrf_from(edit_page)}
        for dimension in taxonomy_dimensions(self.project.id, active_only=True):
            category = next((item for item in dimension.categories if item.active), None)
            if category is not None:
                selections[f"dimension_{dimension.id}"] = str(category.id)
        updated = self.client.post(f"{classifications_url}/{self.media.id}", data=selections)
        self.assertEqual(updated.status_code, 302)
        self.assertTrue(updated.headers["Location"].endswith(
            f"#instagram-classification-{self.media.id}"
        ))

        import_page = self.client.get(
            f"/analytics/instagram/projects/{self.project.id}/imports/new"
        ).get_data(as_text=True)
        self.assertIn(
            f'href="/analytics/instagram/projects/{self.project.id}/imports/new" aria-current="page">Fonte de dados</a>',
            import_page,
        )

        edit_page_html = edit_page.get_data(as_text=True)
        self.assertIn(
            f'href="{classifications_url}" aria-current="page">Classificação de conteúdos</a>', edit_page_html,
        )

    def test_archived_project_tabs_keep_importation_unavailable_but_allow_connection_consultation(self):
        self._archive(self.project)
        dashboard = self.client.get(
            f"/analytics/instagram/projects/{self.project.id}/dashboard"
        ).get_data(as_text=True)
        self.assertIn('class="platform-project-tab is-disabled" aria-disabled="true"', dashboard)
        self.assertEqual(dashboard.count('Fonte de dados</span>'), 2)
        self.assertNotIn(f'href="/analytics/instagram/projects/{self.project.id}/imports/new"', dashboard)
        import_page = self.client.get(
            f"/analytics/instagram/projects/{self.project.id}/imports/new"
        )
        self.assertEqual(import_page.status_code, 200)
        self.assertIn("Projeto arquivado", import_page.get_data(as_text=True))

    def test_auto_submit_listener_is_generic(self):
        script = (Path(__file__).resolve().parents[1] / "static" / "js" / "platform_auto_submit.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("select[data-auto-submit]", script)
        self.assertIn("requestSubmit", script)
        self.assertIn("AbortController", script)
        self.assertIn("DOMParser", script)
        self.assertIn("history.replaceState", script)
        self.assertIn("platform:partial-updated", script)
        self.assertIn("HTMLFormElement.prototype.submit", script)
        self.assertIn("const currentUrl = new URL(window.location.href)", script)
        self.assertIn("Hidden inputs retain no-JavaScript behavior", script)
        self.assertNotIn("instagram-dashboard", script)
        self.assertNotIn("instagram-classification", script)

    def test_project_tabs_use_an_accessible_mobile_menu_without_changing_desktop_tabs(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "templates" / "platform" / "instagram" / "_project_tabs.html").read_text(
            encoding="utf-8"
        )
        css = (root / "static" / "css" / "platform.css").read_text(encoding="utf-8")
        script = (root / "static" / "js" / "platform.js").read_text(encoding="utf-8")

        self.assertIn('data-project-tabs-toggle', template)
        self.assertIn('aria-label="Menu de navegação do projeto"', template)
        self.assertIn('aria-expanded="false"', template)
        self.assertIn('aria-controls="{{ menu_id }}"', template)
        self.assertIn('data-project-tabs-menu', template)
        self.assertIn("@media (max-width: 800px)", css)
        self.assertIn('.platform-project-tabs { display: none; }', css)
        self.assertIn('.platform-project-tabs-menu-toggle { display: none;', css)
        self.assertIn('.platform-project-tabs-mobile-menu { display: grid;', css)
        self.assertIn('.platform-project-tabs-shell[data-project-tabs-ready] .platform-project-tabs-menu-toggle', css)
        self.assertIn("document.querySelectorAll('[data-project-tabs]')", script)
        self.assertIn("event.key === 'Escape'", script)
        self.assertIn("menu.querySelectorAll('a').forEach", script)

    def test_instagram_tables_use_the_shared_theme_surface_tokens(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "static" / "css" / "platform.css").read_text(encoding="utf-8")
        self.assertIn(".platform-themed-table", css)
        self.assertIn("--bs-table-bg: var(--surface)", css)
        self.assertIn("--bs-table-striped-bg: var(--surface-muted)", css)
        self.assertIn("--bs-table-hover-bg: var(--surface-muted)", css)
        self.assertIn(".platform-themed-table thead th", css)
        self.assertIn("background-color: var(--surface-muted)", css)

        templates = (
            "dashboard.html",
            "taxonomies.html",
            "project_detail.html",
            "import_new.html",
            "import_preview.html",
        )
        for template_name in templates:
            with self.subTest(template=template_name):
                template = (root / "templates" / "platform" / "instagram" / template_name).read_text(
                    encoding="utf-8"
                )
                self.assertIn("platform-themed-table", template)

    def test_project_list_keeps_active_actions_icon_only_and_partial(self):
        other_project, _, _ = self._project_graph(self.owner, "Projeto B")
        home = self.client.get("/analytics/instagram")
        self.assertEqual(home.status_code, 200)
        body = home.get_data(as_text=True)
        self.assertIn('id="instagram-project-list"', body)
        first_item = body[body.index(f'id="instagram-project-{self.project.id}"'):]
        first_item = first_item[:first_item.index("</article>")]
        self.assertLess(first_item.index("Abrir projeto Projeto A"), first_item.index("Arquivar projeto"))
        self.assertIn('data-partial-update data-partial-target="#instagram-project-list"', first_item)
        self.assertIn('title="Arquivar projeto"', first_item)
        self.assertIn('aria-label="Arquivar projeto Projeto A"', first_item)
        self.assertIn('name="csrf_token"', first_item)
        self.assertIn('name="confirm" value="yes"', first_item)
        self.assertIn('js/platform_auto_submit.js', body)

        response = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/archive",
            data={"confirm": "yes", "csrf_token": csrf_from(home)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("#instagram-project-list"))
        db.session.expire_all()
        self.assertEqual(db.session.get(AnalyticsProject, self.project.id).status, "archived")
        active_list = self.client.get("/analytics/instagram").get_data(as_text=True)
        self.assertNotIn("Projeto A", active_list)
        self.assertIn(other_project.name, active_list)

        archived_list = self.client.get("/analytics/instagram/projects/archived")
        archived_body = archived_list.get_data(as_text=True)
        self.assertIn('id="instagram-archived-project-list"', archived_body)
        self.assertNotIn('title="Arquivar projeto"', archived_body)
        self.assertIn('title="Restaurar projeto"', archived_body)
        self.assertIn('title="Excluir projeto permanentemente"', archived_body)
        restore = self.client.post(
            f"/analytics/instagram/projects/{self.project.id}/restore",
            data={"confirm": "yes", "csrf_token": csrf_from(archived_list)},
        )
        self.assertEqual(restore.status_code, 302)
        self.assertTrue(restore.headers["Location"].endswith("#instagram-archived-project-list"))
        db.session.expire_all()
        self.assertEqual(db.session.get(AnalyticsProject, self.project.id).status, "active")

    def test_archived_project_delete_removes_only_its_data_and_project_directory(self):
        other_project, other_account, other_media = self._project_graph(self.owner, "Projeto B")
        project_id = self.project.id
        account_id = self.account.id
        media_id = self.media.id
        self._archive(self.project)
        project_directory = Path(self.app.config["PLATFORM_DATA_DIR"]) / "analytics" / project_id
        other_directory = Path(self.app.config["PLATFORM_DATA_DIR"]) / "analytics" / other_project.id
        (project_directory / "import_previews" / self.owner.id).mkdir(parents=True)
        (project_directory / "import_previews" / self.owner.id / "preview.json").write_text("{}", encoding="utf-8")
        other_directory.mkdir(parents=True)
        (other_directory / "kept.json").write_text("{}", encoding="utf-8")

        confirmation = self.client.get(self._delete_url())
        self.assertEqual(confirmation.status_code, 200)
        confirmation_html = confirmation.get_data(as_text=True)
        self.assertIn("Esta ação não pode ser desfeita", confirmation_html)
        self.assertIn("taxonomias, categorias, classificações atuais e históricas", confirmation_html)
        self.assertIn('data-delete-confirm="final-delete-button"', confirmation_html)
        archived_html = self.client.get("/analytics/instagram/projects/archived").get_data(as_text=True)
        self.assertIn('title="Excluir projeto permanentemente"', archived_html)

        response = self.client.post(self._delete_url(), data={"confirmation": "deletar"})
        self.assertEqual(response.status_code, 400)  # CSRF obrigatório.
        response = self.client.post(self._delete_url(), data={
            "confirmation": "deletar", "csrf_token": csrf_from(confirmation),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/analytics/instagram/projects/archived"))
        db.session.expire_all()

        self.assertIsNone(db.session.get(AnalyticsProject, project_id))
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun).where(
            AnalyticsRun.analytics_project_id == project_id,
        )), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccount).where(
            InstagramAccount.analytics_project_id == project_id,
        )), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation).where(
            InstagramMediaObservation.instagram_media_id == media_id,
        )), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot).where(
            InstagramAccountSnapshot.instagram_account_id == account_id,
        )), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaClassification).where(
            InstagramMediaClassification.instagram_media_id == media_id,
        )), 0)
        self.assertFalse(project_directory.exists())
        self.assertTrue(other_directory.exists())
        self.assertIsNotNone(db.session.get(AnalyticsProject, other_project.id))
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccount).where(
            InstagramAccount.analytics_project_id == other_project.id,
        )), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia).where(
            InstagramMedia.instagram_account_id == other_account.id,
        )), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaObservation).where(
            InstagramMediaObservation.instagram_media_id == other_media.id,
        )), 1)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccountSnapshot).where(
            InstagramAccountSnapshot.instagram_account_id == other_account.id,
        )), 1)
        other_dimensions = select(InstagramTaxonomyDimension.id).where(
            InstagramTaxonomyDimension.analytics_project_id == other_project.id,
        )
        self.assertGreater(db.session.scalar(select(func.count()).select_from(InstagramTaxonomyCategory).where(
            InstagramTaxonomyCategory.instagram_taxonomy_dimension_id.in_(other_dimensions),
        )), 0)
        self.assertGreater(db.session.scalar(select(func.count()).select_from(InstagramMediaClassification).where(
            InstagramMediaClassification.instagram_media_id == other_media.id,
        )), 0)
        self.assertEqual(db.session.scalar(select(func.count()).select_from(AnalyticsRun).where(
            AnalyticsRun.analytics_project_id == other_project.id,
        )), 1)

    def test_active_other_user_and_missing_projects_cannot_be_deleted(self):
        self.assertEqual(self.client.get(self._delete_url()).status_code, 400)
        active = self._post_delete()
        self.assertEqual(active.status_code, 400)
        self.assertIsNotNone(db.session.get(AnalyticsProject, self.project.id))
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramAccount).where(
            InstagramAccount.analytics_project_id == self.project.id,
        )), 1)

        self._archive(self.project)
        self.client.post("/logout", data={"csrf_token": csrf_from(self.client.get("/"))})
        login(self.client, self.other.email)
        self.assertEqual(self.client.get(self._delete_url()).status_code, 404)
        self.assertEqual(self.client.get(f"/analytics/instagram/projects/{uuid4()}/delete").status_code, 404)
        self.assertIsNotNone(db.session.get(AnalyticsProject, self.project.id))

    def test_database_failure_rolls_back_and_restores_staged_preview_directory(self):
        self._archive(self.project)
        project_directory = Path(self.app.config["PLATFORM_DATA_DIR"]) / "analytics" / self.project.id
        project_directory.mkdir(parents=True)
        (project_directory / "preview.json").write_text("{}", encoding="utf-8")
        with patch("platform_core.instagram_lifecycle._delete_project_records", side_effect=RuntimeError("falha controlada")):
            with self.assertRaisesRegex(RuntimeError, "falha controlada"):
                delete_instagram_project_permanently(self.project, self.owner, "deletar")
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(AnalyticsProject, self.project.id))
        self.assertTrue((project_directory / "preview.json").exists())
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMediaClassification)), 5)

    def test_archive_and_restore_still_preserve_data(self):
        archive_url = f"/analytics/instagram/projects/{self.project.id}/archive"
        restore_url = f"/analytics/instagram/projects/{self.project.id}/restore"
        page = self.client.get(f"/analytics/instagram/projects/{self.project.id}")
        self.assertEqual(self.client.post(archive_url, data={
            "confirm": "yes", "csrf_token": csrf_from(page),
        }).status_code, 302)
        db.session.expire_all()
        archived = db.session.get(AnalyticsProject, self.project.id)
        self.assertEqual(archived.status, "archived")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 1)
        archived_page = self.client.get("/analytics/instagram/projects/archived")
        self.assertEqual(self.client.post(restore_url, data={
            "confirm": "yes", "csrf_token": csrf_from(archived_page),
        }).status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(AnalyticsProject, self.project.id).status, "active")
        self.assertEqual(db.session.scalar(select(func.count()).select_from(InstagramMedia)), 1)


if __name__ == "__main__":
    unittest.main()
