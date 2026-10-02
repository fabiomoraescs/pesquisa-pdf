"""Regressões do Dashboard, controles de projeto, trechos e rodapé."""

import json
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import app as legacy
from platform_helpers import create_user, isolated_platform, login
from platform_core.analyses import create_analysis, save_success
from platform_core.extensions import db
from platform_core.models import (Analysis, AnalysisDocument, PlanTool, Project, QualitativeCode,
                                  QualitativeCoding, QualitativeExcerpt, UserToolOverride)
from platform_core.scraping_types import QUALITATIVE_TOOL


def chart_data(html, element_id):
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>',
                      html, re.DOTALL)
    assert match, f"Dados do gráfico {element_id} ausentes"
    return json.loads(match.group(1))


class DashboardRefinementsTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.client = self.app.test_client()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def _project(self, user, name, kind):
        project = Project(owner_user_id=user.id, name=name, scrape_type=kind)
        db.session.add(project)
        db.session.commit()
        return project

    def _base(self, user, name, tool, result=None, project=None, status="concluida"):
        analysis = create_analysis(user_id=user.id, project_id=project.id if project else None,
                                   tool_id=tool, tool_version="v1", parameters={}, name=name)
        if status == "concluida":
            save_success(analysis, result or {}, [], 1)
        else:
            analysis.status = status
            db.session.commit()
        return analysis

    def test_project_actions_and_view_controls_share_right_hand_group_for_both_types(self):
        create_user()
        login(self.client)
        for path in ("/projetos/livres", "/projetos"):
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                toolbar = html.split('<div class="platform-toolbar platform-toolbar-actions">', 1)[1]
                toolbar = toolbar.split('<div class="platform-view-list"', 1)[0]
                self.assertIn('class="d-flex flex-wrap gap-2 align-items-center ms-auto"', toolbar)
                positions = [toolbar.index(label) for label in (
                    'Novo projeto</a>', 'Projetos arquivados</a>', 'data-view-mode="list"',
                    'data-view-mode="cardbox"')]
                self.assertEqual(positions, sorted(positions))
                self.assertEqual(toolbar.count('btn btn-outline-primary btn-sm'), 2)

    def test_dashboard_keeps_the_platform_authentication_policy(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertIn(response.status_code, (301, 302, 303, 307, 308))
        self.assertIn("/login", response.headers["Location"])

    def test_recent_analysis_labels_distinguish_all_three_tools_including_qualitative(self):
        owner = create_user()
        db.session.add_all((
            UserToolOverride(user_id=owner.id, tool_id="document_analysis", decision="allow"),
            UserToolOverride(user_id=owner.id, tool_id=QUALITATIVE_TOOL, decision="allow"),
        ))
        db.session.commit()
        project = self._project(owner, "Projeto qualitativo", "qualitative")
        qualitative = create_analysis(user_id=owner.id, project_id=project.id,
            tool_id=QUALITATIVE_TOOL, tool_version="manual-v1", parameters={}, name="Leitura qualitativa")
        systematic_project = self._project(owner, "Projeto estruturado", "systematic")
        expected = (
            (self._base(owner, "Busca de termos", "pdf_scraper", status="processando"), "Busca por termos"),
            (self._base(owner, "Busca de entidades", "document_analysis", project=systematic_project,
                        status="processando"), "Busca estruturada"),
            (qualitative, "Análise quali-dados"),
        )
        login(self.client)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        recent = re.search(r'<section[^>]+aria-labelledby="continue-working-heading">(.*?)</section>', html, re.DOTALL)[1]
        items = re.findall(r'<li[^>]*>(.*?)</li>', recent, re.DOTALL)
        self.assertEqual(len(items), 3)
        for base, label in expected:
            item = next(item for item in items if base.display_name in item)
            self.assertIn(base.display_name, item)
            self.assertIn(label, item)
            self.assertIn(
                f'/analise-qualitativa/bases/{base.id}' if base.tool_id == QUALITATIVE_TOOL
                else f'/analises/{base.id}',
                item,
            )
        self.assertIn(f'/analise-qualitativa/bases/{qualitative.id}', recent)

    def test_recent_work_is_limited_to_five_newest_accessible_bases(self):
        owner = create_user()
        analyses = []
        for index in range(6):
            analysis = self._base(owner, f"Base {index}", "pdf_scraper", status="processando")
            analysis.created_at = datetime.now(timezone.utc) - timedelta(days=6 - index)
            analyses.append(analysis)
        db.session.commit()
        login(self.client)

        html = self.client.get("/").get_data(as_text=True)
        continuation = re.search(
            r'<section[^>]+aria-labelledby="continue-working-heading">(.*?)</section>', html, re.DOTALL
        )[1]
        self.assertEqual(continuation.count('platform-workspace-work-item'), 5)
        self.assertNotIn(analyses[0].display_name, continuation)
        self.assertLess(continuation.index(analyses[-1].display_name), continuation.index(analyses[1].display_name))

    def test_continuation_uses_an_accessible_open_icon_without_status_or_date(self):
        owner = create_user()
        analysis = self._base(owner, "Base concluída", "pdf_scraper", status="concluida")
        analysis.document_count = 8
        db.session.commit()
        login(self.client)

        html = self.client.get("/").get_data(as_text=True)
        continuation = re.search(
            r'<section[^>]+aria-labelledby="continue-working-heading">(.*?)</section>', html, re.DOTALL
        )[1]
        self.assertIn("Base concluída", continuation)
        self.assertIn("Busca por termos", continuation)
        self.assertIn("8 documento(s)", continuation)
        self.assertNotIn(">Continuar<", continuation)
        self.assertNotIn("Concluída · Concluída em", continuation)
        self.assertNotIn("Concluída em", continuation)
        self.assertIn('platform-action-button platform-workspace-open-action', continuation)
        self.assertIn(f'href="/analises/{analysis.id}"', continuation)
        self.assertIn('title="Abrir Base concluída" aria-label="Abrir Base concluída"', continuation)
        self.assertIn('<svg class="platform-icon"', continuation)

    def test_dashboard_has_only_summary_and_continuation_for_regular_users(self):
        create_user()
        login(self.client)

        html = self.client.get("/").get_data(as_text=True)
        body = html.split("<main ", 1)[1].split("</main>", 1)[0]
        self.assertIn("Resumo da área de trabalho", body)
        self.assertIn("Continuar trabalhando", body)
        self.assertLess(body.index("Resumo da área de trabalho"), body.index("Continuar trabalhando"))
        self.assertNotIn("workspace-heading", body)
        self.assertNotIn("quick-actions-heading", body)
        self.assertNotIn("Ações rápidas", body)
        self.assertNotIn("Atividade recente", body)
        self.assertNotIn("dashboard-workspace-chart-data", body)
        self.assertNotIn("Bases concluídas por ferramenta", body)
        self.assertNotIn("dashboard-admin-chart-data", body)

    def test_workspace_metrics_use_only_accessible_analysis_metadata(self):
        owner = create_user()
        stranger = create_user("Outro", "metricas-alheias@example.org")
        own_project = self._project(owner, "Projeto próprio", "free")
        foreign_project = self._project(stranger, "Projeto alheio", "free")
        own = self._base(owner, "Base própria", "pdf_scraper", project=own_project, status="processando")
        own.document_count = 7
        foreign = self._base(stranger, "Base alheia", "pdf_scraper", project=foreign_project, status="processando")
        foreign.document_count = 91
        db.session.commit()
        login(self.client)

        html = self.client.get("/").get_data(as_text=True)
        self.assertRegex(html, r'<strong>1</strong>\s*<span>Projetos</span>')
        self.assertRegex(html, r'<strong>1</strong>\s*<span>Bases</span>')
        self.assertRegex(html, r'<strong>7</strong>\s*<span>Documentos nas Bases</span>')
        self.assertNotIn("Projeto alheio", html)
        self.assertNotIn("Base alheia", html)

    def test_latest_qualitative_analysis_empty_one_and_most_recent_workspace(self):
        owner = create_user()
        db.session.add(UserToolOverride(user_id=owner.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        login(self.client)

        def continuation():
            html = self.client.get("/").get_data(as_text=True)
            return re.search(r'<section[^>]+aria-labelledby="continue-working-heading">(.*?)</section>', html, re.DOTALL)[1]

        self.assertIn("Você ainda não possui análises para continuar.", continuation())
        first_project = self._project(owner, "Primeira leitura", "qualitative")
        first = self._base(owner, "Nome técnico", QUALITATIVE_TOOL, project=first_project, status="processando")
        first.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        db.session.commit()
        self.assertIn("Nome técnico", continuation())
        self.assertIn(f'/analise-qualitativa/bases/{first.id}', continuation())
        second_project = self._project(owner, "Leitura mais recente", "qualitative")
        second = self._base(owner, "Outro nome técnico", QUALITATIVE_TOOL, project=second_project, status="processando")
        current = continuation()
        self.assertIn("Outro nome técnico", current)
        self.assertIn(f'/analise-qualitativa/bases/{second.id}', current)
        self.assertLess(current.index("Outro nome técnico"), current.index("Nome técnico"))
        # O ambiente vazio continua sendo destino funcional; não exige preparo.
        self.assertEqual(self.client.get(f'/analise-qualitativa/bases/{second.id}', follow_redirects=True).status_code, 200)

    def test_workspace_layout_is_compact_responsive_and_uses_metric_icons(self):
        create_user(role="admin")
        login(self.client)

        html = self.client.get("/").get_data(as_text=True)
        for heading in ("workspace-summary-heading", "continue-working-heading"):
            self.assertIn(f'aria-labelledby="{heading}"', html)
        self.assertLess(html.index("Resumo da área de trabalho"), html.index("Continuar trabalhando"))
        summary = re.search(
            r'<section[^>]+aria-labelledby="workspace-summary-heading">(.*?)</section>', html, re.DOTALL
        )[1]
        self.assertEqual(summary.count('platform-workspace-metric-card'), 4)
        self.assertEqual(summary.count('<svg class="platform-icon"'), 4)
        self.assertRegex(summary, r'(?s)<svg class="platform-icon".*?</svg>\s*<strong>0</strong>\s*<span>Projetos</span>')
        css = (Path(__file__).resolve().parents[1] / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertIn('.platform-workspace-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));', css)
        self.assertIn('.platform-workspace-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }', css)
        self.assertIn('flex-direction: row;', css)
        self.assertIn('min-height: 0; padding: .7rem .8rem;', css)
        self.assertIn('width: 1.65rem; height: 1.65rem;', css)
        self.assertIn('font-size: clamp(1.55rem, 2.8vw, 1.9rem);', css)
        self.assertIn('text-overflow: ellipsis; white-space: nowrap;', css)
        self.assertIn('grid-template-columns: minmax(12rem, 1fr) minmax(0, auto) auto;', css)

    def test_qualitative_work_is_textual_and_does_not_load_coding_chart_data(self):
        owner = create_user()
        grant = UserToolOverride(user_id=owner.id, tool_id=QUALITATIVE_TOOL, decision="allow")
        db.session.add(grant)
        db.session.commit()
        current = self._base(owner, "Atual", QUALITATIVE_TOOL,
                             project=self._project(owner, "Atual", "qualitative"), status="processando")
        db.session.commit()
        login(self.client)
        html = self.client.get("/").text
        self.assertIn("Continuar trabalhando", html)
        self.assertIn("Atual", html)
        self.assertIn(f'/analise-qualitativa/bases/{current.id}', html)
        self.assertNotIn('dashboard-chart-qualitative', html)
        self.assertNotIn('Trechos por código', html)
        grant.decision = "deny"
        db.session.commit()
        html = self.client.get("/").text
        self.assertIn("Você ainda não possui análises para continuar.", html)

    def test_latest_qualitative_analysis_respects_project_ownership_state_and_tool(self):
        owner = create_user()
        other = create_user("Outro", "private-qualitative@example.org")
        permission = UserToolOverride(user_id=owner.id, tool_id=QUALITATIVE_TOOL, decision="allow")
        db.session.add(permission)
        db.session.commit()
        own_project = self._project(owner, "Leitura acessível", "qualitative")
        own = self._base(owner, "Técnico próprio", QUALITATIVE_TOOL, project=own_project, status="processando")
        own.created_at = datetime.now(timezone.utc) - timedelta(days=10)
        # Regra histórica: a propriedade do projeto prevalece sobre a autoria.
        own.user_id = other.id
        for name, state, deleted in (("Arquivado sigiloso", "archived", False),
                                     ("Bloqueado sigiloso", "blocked", False),
                                     ("Excluído sigiloso", "active", True)):
            project = self._project(owner, name, "qualitative")
            self._base(owner, name, QUALITATIVE_TOOL, project=project, status="processando")
            project.status = state
            if deleted:
                project.deleted_at = datetime.now(timezone.utc)
        foreign_project = self._project(other, "Projeto de outro usuário", "qualitative")
        foreign = self._base(other, "Análise alheia", QUALITATIVE_TOOL, project=foreign_project, status="processando")
        foreign.user_id = owner.id  # vínculo inconsistente não concede acesso ao projeto
        db.session.commit()
        login(self.client)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Técnico próprio", html)
        self.assertIn(f'/analise-qualitativa/bases/{own.id}', html)
        for forbidden in ("Arquivado sigiloso", "Bloqueado sigiloso", "Excluído sigiloso", "Projeto de outro usuário", foreign.id):
            self.assertNotIn(forbidden, html)
        permission.decision = "deny"
        db.session.commit()
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Técnico próprio", html)
        self.assertIn("Você ainda não possui análises para continuar.", html)

    def test_free_empty_state_has_no_legacy_shortcut_and_library_form_has_no_body_title(self):
        create_user(role="admin")
        login(self.client)
        html = self.client.get("/projetos/livres").get_data(as_text=True)
        self.assertNotIn("Bases anteriores sem projeto", html)
        self.assertIn("Novo projeto", html)
        self.assertEqual(self.client.get("/analises").status_code, 200)
        html = self.client.get("/admin/bibliotecas/nova").get_data(as_text=True)
        self.assertIn('<span class="platform-header-context">Bibliotecas</span>', html)
        body = html.split("<main ", 1)[1].split("</main>", 1)[0]
        self.assertNotIn("Nova biblioteca oficial", body)
        self.assertNotIn("<h1", body)
        self.assertIn('name="name"', body)
        self.assertIn("Criar rascunho", body)

    def test_latest_completed_bases_restore_persisted_charts_without_processing_or_leaking_projects(self):
        owner = create_user()
        stranger = create_user("Outro", "outro@example.org")
        free_project = self._project(owner, "Projeto livre próprio", "free")
        systematic_project = self._project(owner, "Projeto sistemático próprio", "systematic")
        foreign_project = self._project(stranger, "Projeto sigiloso alheio", "free")
        old = self._base(owner, "Base livre antiga", "pdf_scraper", {
            "versao": "v1", "dashboard": {"por_livro": {"rotulos": ["Livro antigo"], "valores": [1]}},
        }, free_project)
        new = self._base(owner, "Base livre recente", "pdf_scraper", {
            "versao": "v1", "dashboard": {"por_livro": {"rotulos": ["Livro atual"], "valores": [4]}},
        }, free_project)
        systematic = self._base(owner, "Base sistemática recente", "document_analysis", {
            "ocorrencias": [
                {"entidade_canonica": "Movimento negro", "tipo_correspondencia": "Lexical"},
                {"entidade_canonica": "Movimento negro", "tipo_correspondencia": "Semântica"},
            ],
        }, systematic_project)
        old.completed_at = datetime.now(timezone.utc) - timedelta(days=2)
        new.completed_at = datetime.now(timezone.utc) - timedelta(days=1)
        systematic.completed_at = datetime.now(timezone.utc)
        self._base(owner, "Base ainda processando", "pdf_scraper", status="processando")
        self._base(owner, "Base com erro", "document_analysis", status="erro")
        self._base(stranger, "Base secreta", "pdf_scraper", {
            "versao": "v1", "dashboard": {"por_livro": {"rotulos": ["Segredo"], "valores": [9]}},
        }, foreign_project)
        # Mesmo um relacionamento inconsistente não deve expor o nome de um projeto alheio.
        self._base(owner, "Base em projeto alheio", "pdf_scraper", {
            "versao": "v1", "dashboard": {"por_livro": {"rotulos": ["Inseguro"], "valores": [2]}},
        }, foreign_project)
        db.session.commit()
        login(self.client)
        with patch.object(legacy, "executar_analises", side_effect=AssertionError("reprocessou PDF")):
            html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Continuar trabalhando", html)
        self.assertIn("Resumo da área de trabalho", html)
        self.assertNotIn("Ações rápidas", html)
        self.assertNotIn("Atividade recente", html)
        self.assertNotIn("Bases concluídas por ferramenta", html)
        self.assertIn("Busca por termos", html)
        self.assertIn("Busca estruturada", html)
        self.assertNotIn("Projeto sigiloso alheio", html)
        self.assertNotIn("Base em projeto alheio", html)
        self.assertNotIn("Base secreta", html)
        self.assertIn('>Base livre recente</a>', html)
        self.assertIn('>Base sistemática recente</a>', html)
        self.assert_recent_records_have_no_chart(html)
        self.assertRegex(html, r'<strong>2</strong>\s*<span>Projetos</span>')
        self.assertRegex(html, r'<strong>4</strong>\s*<span>Bases</span>')
        self.assertRegex(html, r'<strong>3</strong>\s*<span>Bases concluídas</span>')
        self.assertNotIn("dashboard-workspace-chart-data", html)
        self.assertNotIn('dashboard-chart-workspace', html)
        self.assertNotIn("dashboard-admin-chart-data", html)

    def test_project_lists_keep_global_header_without_duplicate_body_heading(self):
        owner = create_user()
        db.session.add(UserToolOverride(user_id=owner.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        login(self.client)
        for kind, path, label in (
            ("free", "/projetos/livres", "Busca por termos"),
            ("systematic", "/projetos", "Busca estruturada"),
            ("qualitative", "/projetos/qualitativos", "Análise quali-dados"),
        ):
            with self.subTest(kind=kind):
                project = self._project(owner, f"Projeto {label}", kind)
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn(f'<span class="platform-header-context">Projetos de {label}</span>', html)
                body = html.split('<main ', 1)[1].split('</main>', 1)[0]
                self.assertNotIn(f"Projetos de {label}", body)
                self.assertNotRegex(body, r"<h[12]\b")
                self.assertIn(project.name, body)
                self.assertIn('id="my-projects-list"', body)
                self.assertIn('Novo projeto</a>', body)
                self.assertIn('Projetos arquivados</a>', body)

    def test_qualitative_dashboard_counts_only_accessible_active_own_projects(self):
        owner = create_user()
        stranger = create_user("Outro", "outro-qual@example.org")
        grant = UserToolOverride(user_id=owner.id, tool_id=QUALITATIVE_TOOL, decision="allow")
        db.session.add(grant)
        db.session.commit()
        self._project(owner, "Livre", "free")
        self._project(owner, "Estruturada", "systematic")
        self._project(stranger, "Qualitativo alheio", "qualitative")
        archived = self._project(owner, "Arquivado", "qualitative")
        archived.status = "archived"
        blocked = self._project(owner, "Bloqueado", "qualitative")
        blocked.status = "blocked"
        deleted = self._project(owner, "Excluído", "qualitative")
        deleted.deleted_at = datetime.now(timezone.utc)
        db.session.commit()
        login(self.client)

        def check(qualitative, total):
            html = self.client.get("/").get_data(as_text=True)
            self.assertRegex(html, rf'<strong>{total}</strong>\s*<span>Projetos</span>')
            self.assertRegex(html, r'<strong>0</strong>\s*<span>Bases</span>')
            self.assertNotIn("dashboard-admin-chart-data", html)

        check(0, 2)
        self._project(owner, "Qualitativo próprio", "qualitative")
        check(1, 3)
        self._project(owner, "Outro qualitativo próprio", "qualitative")
        check(2, 4)
        grant.decision = "deny"
        db.session.commit()
        check(0, 2)

    def test_admin_qualitative_project_metric_is_personal_not_platform_total(self):
        admin = create_user("Admin", "admin-qual@example.org", role="admin")
        other = create_user()
        self._project(other, "Projeto de outro usuário", "qualitative")
        self._project(admin, "Projeto do administrador", "qualitative")
        login(self.client, admin.email)
        html = self.client.get("/").get_data(as_text=True)
        self.assertRegex(html, r'<strong>1</strong>\s*<span>Projetos</span>')
        self.assertNotIn("Projeto de outro usuário", html)
        self.assertIn("dashboard-admin-chart-data", html)
        self.assertEqual(html.count('id="dashboard-chart-users"'), 1)
        self.assertEqual(html.count('id="dashboard-chart-usage"'), 1)
        self.assertNotIn("dashboard-workspace-chart-data", html)

    def test_project_owner_sees_bases_created_by_another_user_but_not_foreign_projects(self):
        owner = create_user()
        creator = create_user("Outro", "outro@example.org")
        free_project = self._project(owner, "Projeto livre próprio", "free")
        systematic_project = self._project(owner, "Projeto sistemático próprio", "systematic")
        foreign_project = self._project(creator, "Projeto alheio sigiloso", "free")
        older = self._base(owner, "Base própria antiga", "pdf_scraper", {
            "versao": "v1", "dashboard": {"por_livro": {"rotulos": ["Livro antigo"], "valores": [1]}},
        }, free_project)
        shared_free = self._base(creator, "Base livre do projeto", "pdf_scraper", {
            "versao": "v1", "dashboard": {"por_livro": {"rotulos": ["Livro compartilhado"], "valores": [5]}},
        }, free_project)
        shared_systematic = self._base(creator, "Base sistemática do projeto", "document_analysis", {
            "ocorrencias": [{"entidade_canonica": "Entidade compartilhada"}],
        }, systematic_project)
        self._base(creator, "Base livre alheia", "pdf_scraper", project=None)
        self._base(owner, "Base em projeto alheio", "pdf_scraper", project=foreign_project)
        older.completed_at = datetime.now(timezone.utc) - timedelta(days=2)
        shared_free.completed_at = datetime.now(timezone.utc) - timedelta(days=1)
        shared_systematic.completed_at = datetime.now(timezone.utc)
        db.session.commit()

        login(self.client)
        self.assertIn("Base livre do projeto", self.client.get("/analises/livres").get_data(as_text=True))
        self.assertIn("Base sistemática do projeto", self.client.get("/analises/sistematicas").get_data(as_text=True))
        with patch.object(legacy, "executar_analises", side_effect=AssertionError("reprocessou PDF")):
            html = self.client.get("/").get_data(as_text=True)
        self.assertRegex(html, r'<strong>2</strong>\s*<span>Projetos</span>')
        self.assertRegex(html, r'<strong>3</strong>\s*<span>Bases</span>')
        self.assertNotIn("dashboard-workspace-chart-data", html)
        self.assertIn("Base livre do projeto", html)
        self.assertIn("Base sistemática do projeto", html)
        self.assertNotIn("Base livre alheia", html)
        self.assertNotIn("Base em projeto alheio", html)
        self.assertNotIn("Projeto alheio sigiloso", html)
        self.assert_recent_records_have_no_chart(html)
        self.assertNotIn("dashboard-admin-chart-data", html)

    def test_empty_chart_states_and_unlinked_base_fallback(self):
        owner = create_user()
        self._base(owner, "Base sem projeto", "pdf_scraper", status="processando")
        login(self.client)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Base sem projeto", html)
        self.assertNotIn("Projeto: Sem projeto", html)
        self.assertNotIn("Atividade analítica", html)
        self.assertNotIn("dashboard-chart-workspace", html)

    def test_v3_uses_its_persisted_book_series_and_revoked_tool_is_not_exposed(self):
        owner = create_user()
        free_project = self._project(owner, "Projeto V3", "free")
        self._base(owner, "Base V3", "pdf_scraper", {
            "versao": "v3", "dashboard": {"resultados_por_livro": {
                "rotulos": ["Livro híbrido"], "valores": [7],
            }},
        }, free_project)
        login(self.client)
        first = self.client.get("/").get_data(as_text=True)
        self.assertIn("Base V3", first)
        self.assertRegex(first, r'<strong>1</strong>\s*<span>Projetos</span>')
        self.assertRegex(first, r'<strong>1</strong>\s*<span>Bases</span>')
        self.assertNotIn("dashboard-workspace-chart-data", first)
        db.session.delete(db.session.get(PlanTool, ("student", "pdf_scraper")))
        db.session.commit()
        restricted = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Base V3", restricted)
        self.assertNotIn("Projeto V3", restricted)
        self.assertNotIn("Livro híbrido", restricted)
        self.assertIn("Você ainda não possui análises para continuar.", restricted)
        self.assertNotIn("dashboard-workspace-chart-data", restricted)

    def test_admin_receives_historical_global_charts_while_continuation_stays_personal(self):
        admin = create_user("Admin", "admin@example.org", "admin", "institutional")
        regular = create_user()
        self._base(regular, "Livre pronta", "pdf_scraper", {
            "versao": "v1", "dashboard": {"por_livro": {"rotulos": [], "valores": []}},
        })
        self._base(regular, "Sistemática pronta", "document_analysis", {"ocorrencias": []})
        self._base(regular, "Pendente", "pdf_scraper", status="processando")
        self._base(regular, "Falhou", "document_analysis", status="erro")
        self._project(regular, "Projeto sem execução", "free")
        db.session.commit()
        login(self.client)
        self.assertEqual(self.client.get("/raspagem-livre").status_code, 200)
        regular_html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Livre pronta", regular_html)
        self.assertNotIn("dashboard-admin-chart-data", regular_html)
        self.client.post("/logout", data={"csrf_token": self._csrf(regular_html)})
        login(self.client, admin.email)
        admin_html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Livre pronta", admin_html)
        self.assertNotIn("Sistemática pronta", admin_html)
        self.assertIn("Você ainda não possui análises para continuar.", admin_html)
        self.assertIn("Visão geral da plataforma", admin_html)
        self.assertEqual(admin_html.count('id="dashboard-chart-users"'), 1)
        self.assertEqual(admin_html.count('id="dashboard-chart-usage"'), 1)
        self.assertNotIn("dashboard-chart-workspace", admin_html)
        self.assertEqual(chart_data(admin_html, "dashboard-admin-chart-data")["usage"], {
            "labels": ["Busca por termos", "Busca estruturada", "Análise quali-dados",
                       "Análise quantitativa", "ChatDoc"],
            "values": [1, 1, 0, 0, 0],
        })

    def assert_recent_records_have_no_chart(self, html):
        section = re.search(r'<section[^>]+aria-labelledby="continue-working-heading">(.*?)</section>', html, re.DOTALL)[1]
        self.assertNotRegex(section, r'<canvas|class="(?:chart|empty-chart)|role="img"|skeleton|Plotly')

    def test_empty_summaries_have_no_chart_placeholders_and_chatdoc_is_visual_only(self):
        create_user()
        login(self.client)
        html = self.client.get("/").text
        self.assert_recent_records_have_no_chart(html)
        self.assertIn("Você ainda não possui análises para continuar.", html)
        self.assertNotIn('dashboard-chart-workspace', html)
        self.assertNotIn('cdn.plot.ly', html)
        for label in ("Projetos", "Bases", "Documentos nas Bases", "Bases concluídas"):
            self.assertRegex(html, rf'<strong>0</strong>\s*<span>{re.escape(label)}</span>')
        for name in ("Análise quantitativa", "ChatDoc"):
            self.assertRegex(html, r'(?s)<span class="platform-nav-unavailable" aria-disabled="true">\s*<svg[^>]*>.*?</svg>\s*<span>' + name + r'</span></span>')
        self.assertFalse(any("chatdoc" in rule.rule.lower() for rule in self.app.url_map.iter_rules()))
        self.assertFalse(any("chatdoc" in name.lower() for name in db.metadata.tables))

    @staticmethod
    def _csrf(html):
        return re.search(r'<meta name="csrf-token" content="([^"]+)"', html).group(1)

    def test_systematic_dashboard_replaces_first_occurrences_with_analytic_charts(self):
        owner = create_user()
        project = self._project(owner, "Projeto sistemático", "systematic")
        analysis = self._base(owner, "Base contextual", "document_analysis", {
            "project_name": project.name, "metodo_analise": "lexical", "limiar_semantico": None,
            "entidades_distintas": 1, "vocabulario_version": "v1", "vocabulario_hash": "abc",
            "library_names": [], "grupos": {"grupo": "Grupo"},
            "ocorrencias": [{"arquivo_pdf": "teste.pdf", "pagina_pdf": 1,
                "entidade_canonica": "Entidade", "forma_original_no_texto": "termo",
                "tipo_correspondencia": "Lexical", "similaridade_semantica": None,
                "grupo": ["grupo"], "trecho_anterior": "antes",
                "trecho_ocorrencia": "termo", "trecho_posterior": "depois"}],
        }, project)
        login(self.client)
        html = self.client.get(f"/analises/{analysis.id}").get_data(as_text=True)
        self.assertIn('id="grafico-grupos-documentos"', html)
        self.assertIn('id="grafico-composicao-grupos"', html)
        self.assertIn('id="structured-analysis-chart-data"', html)
        self.assertNotIn("Primeiras ocorrências", html)
        self.assertNotIn('title="Ver trechos" aria-label="Ver trechos"', html)
        self.assertIn("© 2026 Análysis. Todos os direitos reservados.", html)

    def test_shared_footer_has_exact_wording_and_external_link_safety(self):
        create_user()
        login_html = self.client.get("/login").get_data(as_text=True)
        login(self.client)
        for path in ("/", "/raspagem-livre", "/login"):
            with self.subTest(path=path):
                html = login_html if path == "/login" else self.client.get(path).get_data(as_text=True)
                footer = html.split('<footer class="app-footer">', 1)[1].split('</footer>', 1)[0]
                plain = " ".join(unescape(re.sub(r"<[^>]+>", "", footer)).split())
                self.assertEqual(plain, "© 2026 Análysis. Todos os direitos reservados. "
                                 "Criado e desenvolvido por Fabio Monteiro de Moraes. "
                                 "Parceria: Grupo de pesquisa: ConsCiências-Sociais")
                self.assertIn('href="http://lattes.cnpq.br/0075160400322127" '
                              'target="_blank" rel="noopener noreferrer"', footer)
                self.assertIn('href="http://dgp.cnpq.br/dgp/espelhogrupo/538438" '
                              'target="_blank" rel="noopener noreferrer"', footer)
                self.assertNotIn("Versão beta", html)


class DashboardChartFrontendTests(unittest.TestCase):
    def test_historical_admin_charts_render_their_two_aggregate_series(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        source = (Path(__file__).resolve().parents[1] / "static/js/platform_dashboard.js").read_text(encoding="utf-8")
        script = r"""
const assert=require('node:assert/strict');
const calls=[], handlers={};
const chart=()=>({classList:{remove(){}},closest:()=>null});
const charts={
  'dashboard-chart-users':chart(),
  'dashboard-chart-usage':chart(),
};
const admin={registrations:{labels:['2026-09','2026-10'],values:[1,2]},usage:{labels:['Busca por termos','Busca estruturada'],values:[2,1]}};
const document={getElementById:id=>id==='dashboard-admin-chart-data'?{textContent:JSON.stringify(admin)}:charts[id]||null,
  addEventListener:(name,fn)=>handlers[name]=fn,querySelectorAll:()=>[]};
const Plotly={react:(...args)=>calls.push(args),Plots:{resize(){}}};
const window={Plotly,PesquisaPdfPlotTheme:{palette:()=>['a','b','c','d','e'],layout:margin=>({margin}),axis:x=>x}};
""" + source + r"""
assert.equal(calls.length,2);
assert.deepEqual(calls[0][1][0].x,['09/2026','10/2026']);
assert.deepEqual(calls[0][1][0].y,[1,2]);
assert.equal(calls[0][1][0].orientation,'v');
assert.deepEqual(calls[1][1][0].x,[2,1]);
assert.deepEqual(calls[1][1][0].y,['Busca por termos','Busca estruturada']);
assert.equal(calls[1][1][0].orientation,'h');
assert.match(calls[1][1][0].hovertemplate,/Base\(s\) concluída\(s\)/);
assert.equal(calls[1][3].responsive,true);
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_admin_charts_react_to_theme_and_resize(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        source = (Path(__file__).resolve().parents[1] / "static/js/platform_dashboard.js").read_text(encoding="utf-8")
        script = r"""
const assert = require('node:assert/strict');
const handlers = {}, calls = [], resized = [], frames = new Map();
const classes = new Set(['js-plotly-plot']);
const cards=[{}, {}]; let observed=[], serial=0;
const chart=()=>({classList:{contains:x=>classes.has(x),add:x=>classes.add(x),remove:x=>classes.delete(x)}});
const charts=[chart(), chart()];
const document = {
  getElementById:id=>id==='dashboard-admin-chart-data'?{textContent:JSON.stringify({registrations:{labels:['2026-10'],values:[1]},usage:{labels:['Busca por termos'],values:[1]}})}:
    id==='dashboard-chart-users'?charts[0]:id==='dashboard-chart-usage'?charts[1]:null,
  addEventListener:(name,fn)=>handlers[name]=fn,
  querySelectorAll:selector=>selector.endsWith('.js-plotly-plot')?charts:selector.endsWith('.platform-panel')?cards:[],
};
const ResizeObserver = class {constructor(fn){this.fn=fn;} observe(target){observed.push(target);}};
const requestAnimationFrame=fn=>{frames.set(++serial,fn);return serial;}, cancelAnimationFrame=id=>frames.delete(id);
const Plotly={react:(...args)=>calls.push(args),Plots:{resize:target=>resized.push(target)}};
const window={Plotly,ResizeObserver,PesquisaPdfPlotTheme:{palette:()=>['red','blue','green'],layout:margin=>({margin}),axis:x=>x}};
""" + source + r"""
assert.equal(calls.length,2);
assert.deepEqual(observed,cards);
handlers['tema-alterado']();assert.equal(calls.length,4);
handlers['dashboard-redimensionar']();
assert.equal(frames.size,1);for(const fn of frames.values())fn();frames.clear();
assert.deepEqual(resized,charts);
handlers['dashboard-redimensionar']();assert.equal(frames.size,1);
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
