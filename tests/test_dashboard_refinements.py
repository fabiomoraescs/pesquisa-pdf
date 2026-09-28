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

    def test_recent_analysis_labels_distinguish_all_three_tools_including_qualitative(self):
        owner = create_user()
        project = self._project(owner, "Projeto qualitativo", "qualitative")
        qualitative = create_analysis(user_id=owner.id, project_id=project.id,
            tool_id=QUALITATIVE_TOOL, tool_version="manual-v1", parameters={}, name="Leitura qualitativa")
        expected = [
            (self._base(owner, "Busca de termos", "pdf_scraper", status="processando"), "Busca por termos"),
            (self._base(owner, "Busca de entidades", "document_analysis", status="processando"), "Busca estruturada"),
            (qualitative, "Análise quali-dados"),
        ]
        login(self.client)
        render = legacy.render_template

        def render_with_all_modalities(template, **context):
            # Exercita a apresentação caso a lista receba uma análise qualitativa,
            # sem ampliar o escopo/autorização da consulta real do Dashboard.
            context["recent_bases"] = [(base, None) for base, _ in expected]
            return render(template, **context)

        with patch.object(legacy, "render_template", side_effect=render_with_all_modalities):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        recent = re.search(r'<ul class="platform-dashboard-records">(.*?)</ul>', html, re.DOTALL)[1]
        items = re.findall(r'<li>(.*?)</li>', recent, re.DOTALL)
        self.assertEqual(len(items), 3)
        for item, (base, label) in zip(items, expected):
            self.assertIn(base.display_name, item)
            self.assertIn(f'<small>{label} · ', item)
        self.assertNotIn("Busca estruturada", items[2])
        self.assertNotIn("Busca por termos", items[2])

    def test_latest_qualitative_analysis_empty_one_and_most_recent_workspace(self):
        owner = create_user()
        db.session.add(UserToolOverride(user_id=owner.id, tool_id=QUALITATIVE_TOOL, decision="allow"))
        db.session.commit()
        login(self.client)

        def card():
            html = self.client.get("/").get_data(as_text=True)
            return re.search(r'<section class="platform-panel" aria-labelledby="dashboard-latest-qualitative">(.*?)</section>', html, re.DOTALL)[1]

        self.assertIn("Nenhuma análise quali-dados ainda.", card())
        first_project = self._project(owner, "Primeira leitura", "qualitative")
        first = self._base(owner, "Nome técnico", QUALITATIVE_TOOL, project=first_project, status="processando")
        first.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        db.session.commit()
        self.assertIn("Primeira leitura", card())
        self.assertIn(f'/analise-qualitativa/bases/{first.id}', card())
        second_project = self._project(owner, "Leitura mais recente", "qualitative")
        second = self._base(owner, "Outro nome técnico", QUALITATIVE_TOOL, project=second_project, status="processando")
        with patch.object(legacy, "load_result", side_effect=AssertionError("Não carregar corpus/resultados")):
            current = card()
        self.assertIn("Última análise quali-dados", current)
        self.assertIn("Leitura mais recente", current)
        self.assertIn(f'/analise-qualitativa/bases/{second.id}', current)
        self.assertNotIn("Primeira leitura", current)
        self.assertNotIn("nome técnico", current)
        # O ambiente vazio continua sendo destino funcional; não exige preparo.
        self.assertEqual(self.client.get(f'/analise-qualitativa/bases/{second.id}', follow_redirects=True).status_code, 200)

    def test_three_latest_summaries_share_responsive_grid_without_changing_admin_grid(self):
        create_user(role="admin")
        login(self.client)

        class GridParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.divs = []
                self.cards = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "div":
                    self.divs.append(attrs.get("class", "").split())
                if tag == "section" and attrs.get("aria-labelledby", "").startswith("dashboard-latest-"):
                    self.cards[attrs["aria-labelledby"]] = self.divs[-1]

            def handle_endtag(self, tag):
                if tag == "div":
                    self.divs.pop()

        html = self.client.get("/").get_data(as_text=True)
        parser = GridParser()
        parser.feed(html)
        self.assertEqual(set(parser.cards), {
            "dashboard-latest-free", "dashboard-latest-systematic", "dashboard-latest-qualitative"})
        for classes in parser.cards.values():
            self.assertEqual(classes, ["platform-dashboard-charts", "platform-dashboard-latest"])
        self.assertEqual(html.count('class="platform-dashboard-charts platform-dashboard-latest"'), 1)
        self.assertIn('class="platform-dashboard-charts"', html)  # grade administrativa preservada
        css = (Path(__file__).resolve().parents[1] / "static/css/platform.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.platform-dashboard-charts\.platform-dashboard-latest\s*\{\s*grid-template-columns: minmax\(0, 1fr\)")
        self.assertNotRegex(css, r"\.platform-dashboard-latest[^}]*repeat\([23],")
        self.assertRegex(css, r"\.platform-dashboard-charts\s*\{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)[^}]*gap: 1rem")
        self.assertRegex(css, r"(?s)@media \(max-width: 640px\).*?\.platform-dashboard-charts \{ grid-template-columns: minmax\(0, 1fr\);")

    def test_qualitative_chart_counts_distinct_excerpts_per_code_from_latest_accessible_analysis(self):
        from platform_core.qualitative_routes import _records_payload

        owner = create_user()
        other = create_user("Outro", "other-chart@example.org")
        grant = UserToolOverride(user_id=owner.id, tool_id=QUALITATIVE_TOOL, decision="allow")
        db.session.add(grant)
        db.session.commit()
        old = self._base(owner, "Anterior", QUALITATIVE_TOOL,
                         project=self._project(owner, "Anterior", "qualitative"), status="processando")
        old.created_at = datetime.now(timezone.utc) - timedelta(days=1)
        current = self._base(owner, "Atual", QUALITATIVE_TOOL,
                             project=self._project(owner, "Atual", "qualitative"), status="processando")
        # Projetos inelegíveis não podem contribuir com códigos ou contagens.
        for author, name, state, deleted in (
            (other, "Sigiloso", "active", False), (owner, "Arquivado", "archived", False),
            (owner, "Excluído", "active", True),
        ):
            project = self._project(author, name, "qualitative")
            analysis = self._base(author, name, QUALITATIVE_TOOL, project=project, status="processando")
            project.status = state
            if deleted:
                project.deleted_at = datetime.now(timezone.utc)
            db.session.add(QualitativeCode(analysis_id=analysis.id, name=name, created_by_user_id=author.id))
        db.session.add(QualitativeCode(analysis_id=old.id, name="Código antigo", created_by_user_id=owner.id))
        codes = [QualitativeCode(analysis_id=current.id, name=name, created_by_user_id=owner.id)
                 for name in ("Racismo", "Educação", "Sem trechos")]
        document = AnalysisDocument(analysis_id=current.id, original_name="teste.pdf", stored_name="teste.pdf")
        db.session.add_all([*codes, document])
        db.session.flush()
        excerpts = [QualitativeExcerpt(analysis_id=current.id, document_id=document.id,
            page_number=page, start_offset=0, end_offset=6, quoted_text="trecho", page_text_hash="a" * 64,
            created_by_user_id=owner.id) for page in (1, 2)]
        db.session.add_all(excerpts)
        db.session.flush()
        codings = [QualitativeCoding(analysis_id=current.id, excerpt_id=excerpt.id,
            code_id=code.id, created_by_user_id=owner.id)
            for code, excerpt in ((codes[0], excerpts[0]), (codes[0], excerpts[1]), (codes[1], excerpts[0]))]
        db.session.add_all(codings)
        db.session.commit()
        login(self.client)

        def check(expected):
            with patch.object(legacy, "load_result", side_effect=AssertionError("Não ler/reprocessar corpus")):
                html = self.client.get("/").text
            series = chart_data(html, "dashboard-chart-data")["qualitative"]
            self.assertEqual(series["title"], "Trechos por código")
            self.assertEqual(series["unit"], "trecho(s)")
            self.assertEqual(dict(zip(series["labels"], series["values"])), expected)
            self.assertEqual(expected, {code["name"]: code["excerpt_count"]
                                       for code in _records_payload(current)["codes"]})
            self.assertIn('id="dashboard-chart-qualitative" class="chart" role="img"', html)
            self.assert_recent_records_have_no_chart(html)

        # O primeiro trecho conta nos dois códigos, sem contar duas vezes no mesmo.
        check({"Racismo": 2, "Educação": 1, "Sem trechos": 0})
        db.session.delete(codings[0])
        db.session.commit()
        check({"Racismo": 1, "Educação": 1, "Sem trechos": 0})
        grant.decision = "deny"
        db.session.commit()
        html = self.client.get("/").text
        self.assertNotIn("qualitative", chart_data(html, "dashboard-chart-data"))
        self.assertNotIn('id="dashboard-chart-qualitative"', html)

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
        self.assertIn("Leitura acessível", html)
        self.assertIn(f'/analise-qualitativa/bases/{own.id}', html)
        for forbidden in ("Arquivado sigiloso", "Bloqueado sigiloso", "Excluído sigiloso", "Projeto de outro usuário", foreign.id):
            self.assertNotIn(forbidden, html)
        permission.decision = "deny"
        db.session.commit()
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Leitura acessível", html)
        self.assertIn("Nenhuma análise quali-dados ainda.", html)

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
        with patch.object(legacy, "executar_analises", side_effect=AssertionError("reprocessou PDF")), \
             patch.object(legacy, "criar_dashboard", side_effect=AssertionError("reprocessou séries")):
            html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Visão geral dos seus projetos e das suas Bases de análise</p>", html)
        self.assertNotIn("Olá,", html)
        self.assertNotIn("Projetos recentes", html)
        self.assertIn("Projeto: Projeto livre próprio", html)
        self.assertIn("Projeto: Projeto sistemático próprio", html)
        self.assertNotIn("Projeto sigiloso alheio", html)
        self.assertNotIn("Base em projeto alheio", html)
        self.assertNotIn("Base secreta", html)
        self.assertIn('>Base livre recente</a>', html)
        self.assertIn('>Base sistemática recente</a>', html)
        self.assert_recent_records_have_no_chart(html)
        charts = chart_data(html, "dashboard-chart-data")
        self.assertEqual(charts["free"], {"title": "Ocorrências por livro", "unit": "ocorrência(s)",
                                        "labels": ["Livro atual"], "values": [4]})
        self.assertEqual(charts["systematic"]["labels"], ["Movimento negro"])
        self.assertEqual(charts["systematic"]["values"], [2])
        for kind in ("free", "systematic"):
            self.assertIn(f'id="dashboard-chart-{kind}" class="chart" role="img"', html)
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
            self.assertIn(f"<span>Análise quali-dados</span><strong>{qualitative}</strong>", html)
            self.assertIn(f"<span>Projetos ativos</span><strong>{total}</strong>", html)
            for label in ("Busca por termos", "Busca estruturada"):
                self.assertIn(f"<span>{label}</span><strong>1</strong>", html)
            self.assertIn("<span>Bases de análise</span><strong>0</strong>", html)
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
        self.assertIn("<span>Análise quali-dados</span><strong>1</strong>", html)
        self.assertIn("<span>Projetos ativos</span><strong>1</strong>", html)
        self.assertIn("dashboard-admin-chart-data", html)

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
        self.assertIn("<span>Bases de análise</span><strong>3</strong>", html)
        self.assertIn("Base livre do projeto", html)
        self.assertIn("Base sistemática do projeto", html)
        self.assertIn("Projeto: Projeto livre próprio", html)
        self.assertIn("Projeto: Projeto sistemático próprio", html)
        self.assertNotIn("Base livre alheia", html)
        self.assertNotIn("Base em projeto alheio", html)
        self.assertNotIn("Projeto alheio sigiloso", html)
        charts = chart_data(html, "dashboard-chart-data")
        self.assertEqual(charts["free"]["labels"], ["Livro compartilhado"])
        self.assertEqual(charts["free"]["values"], [5])
        self.assertEqual(charts["systematic"]["labels"], ["Entidade compartilhada"])
        self.assertEqual(charts["systematic"]["values"], [1])
        self.assert_recent_records_have_no_chart(html)
        self.assertNotIn("dashboard-admin-chart-data", html)

    def test_empty_chart_states_and_unlinked_base_fallback(self):
        owner = create_user()
        self._base(owner, "Base sem projeto", "pdf_scraper", status="processando")
        login(self.client)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Projeto: Sem projeto", html)
        self.assertEqual(html.count("Nenhuma Base concluída ainda."), 2)

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
        self.assertEqual(chart_data(first, "dashboard-chart-data")["free"], {
            "title": "Resultados recuperados por livro", "unit": "resultado(s)",
            "labels": ["Livro híbrido"], "values": [7]})
        db.session.delete(db.session.get(PlanTool, ("student", "pdf_scraper")))
        db.session.commit()
        restricted = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Base V3", restricted)
        self.assertNotIn("Projeto V3", restricted)
        self.assertNotIn("Livro híbrido", restricted)
        self.assertNotIn("free", chart_data(restricted, "dashboard-chart-data"))

    def test_admin_monthly_registrations_and_completed_usage_are_aggregated_only_for_admin(self):
        admin = create_user("Admin", "admin@example.org", "admin", "institutional")
        regular = create_user()
        previous_month = (datetime.now(timezone.utc).replace(day=1) - timedelta(days=1)).replace(day=1)
        regular.created_at = previous_month
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
        self.assertNotIn("Visão geral da plataforma", regular_html)
        self.assertNotIn("dashboard-admin-chart-data", regular_html)
        self.client.post("/logout", data={"csrf_token": self._csrf(regular_html)})
        login(self.client, admin.email)
        admin_html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Visão geral da plataforma", admin_html)
        self.assertEqual(chart_data(admin_html, "dashboard-chart-data"), {})
        self.assert_recent_records_have_no_chart(admin_html)
        aggregates = chart_data(admin_html, "dashboard-admin-chart-data")
        self.assertEqual(aggregates["usage"], {
            "labels": ["Busca por termos", "Busca estruturada", "Análise quali-dados", "Análise quantitativa", "ChatDoc"],
            "values": [1, 1, 0, 0, 0]})
        self.assertEqual(aggregates["registrations"]["values"][-2:], [1, 1])

    def assert_recent_records_have_no_chart(self, html):
        section = re.search(r'<section[^>]+aria-labelledby="dashboard-bases-title">(.*?)</section>', html, re.DOTALL)[1]
        self.assertNotRegex(section, r'<(?:canvas|svg)|class="(?:chart|empty-chart)|role="img"|skeleton|Plotly')

    def test_empty_summaries_have_no_chart_placeholders_and_chatdoc_is_visual_only(self):
        create_user()
        login(self.client)
        html = self.client.get("/").text
        self.assert_recent_records_have_no_chart(html)
        self.assertEqual(chart_data(html, "dashboard-chart-data"), {})
        for kind in ("free", "systematic", "qualitative"):
            self.assertNotIn(f'id="dashboard-chart-{kind}"', html)
        self.assertIn("Nenhuma Base de análise disponível.", html)
        self.assertEqual(html.count("Nenhuma Base concluída ainda."), 2)
        self.assertIn("Nenhuma análise quali-dados ainda.", html)
        self.assertIn('src="https://cdn.plot.ly/plotly-2.35.2.min.js"', html)
        for name in ("Análise quantitativa", "ChatDoc"):
            self.assertRegex(html, r'(?s)<span class="platform-nav-unavailable" aria-disabled="true">\s*<svg[^>]*>.*?</svg>\s*<span>' + name + r'</span></span>')
        self.assertFalse(any("chatdoc" in rule.rule.lower() for rule in self.app.url_map.iter_rules()))
        self.assertFalse(any("chatdoc" in name.lower() for name in db.metadata.tables))

    @staticmethod
    def _csrf(html):
        return re.search(r'<meta name="csrf-token" content="([^"]+)"', html).group(1)

    def test_systematic_context_uses_accessible_icon_without_changing_details(self):
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
        self.assertIn('title="Ver trechos" aria-label="Ver trechos"', html)
        self.assertIn('<summary class="btn btn-outline-primary btn-sm platform-action-button"', html)
        self.assertNotIn('<summary>Ver trechos</summary>', html)
        self.assertIn('Ocorrência:</strong> termo', html)
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
    def test_usage_keeps_zero_categories_even_when_all_counts_are_zero(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        source = (Path(__file__).resolve().parents[1] / "static/js/platform_dashboard.js").read_text(encoding="utf-8")
        script = r"""
const assert=require('node:assert/strict');
const labels=['Busca por termos','Busca estruturada','Análise quali-dados','Análise quantitativa','ChatDoc'];
for(const values of [[2,1,3,0,0],[0,0,0,0,0]]) {
  const calls=[],target={classList:{contains:()=>false,add(){},remove(){}}};
  const document={getElementById:id=>id==='dashboard-chart-data'?{textContent:'{}'}:
    id==='dashboard-admin-chart-data'?{textContent:JSON.stringify({registrations:{labels:[],values:[]},usage:{labels,values}})}:
    id==='dashboard-chart-usage'?target:null,addEventListener(){},querySelectorAll:()=>[]};
  const Plotly={react:(...args)=>calls.push(args),purge:()=>{throw Error('Não omitir ferramentas zeradas');}};
  const window={Plotly,PesquisaPdfPlotTheme:{palette:()=>['a','b','c','d'],layout:margin=>({margin}),axis:x=>x}};
""" + source + r"""
  assert.equal(calls.length,1);assert.deepEqual(calls[0][1][0].x,values);assert.deepEqual(calls[0][1][0].y,labels);
  assert.equal(calls[0][1][0].marker.color,'d');assert.equal(calls[0][1][0].orientation,'h');
  assert.match(calls[0][1][0].hovertemplate,/Base\(s\) concluída\(s\)/);assert.equal(calls[0][3].responsive,true);
}
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preserved_series_theme_resize_and_empty_chart(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        source = (Path(__file__).resolve().parents[1] / "static/js/platform_dashboard.js").read_text(encoding="utf-8")
        script = r"""
const assert = require('node:assert/strict');
const charts = new Map(), cards = [{}, {}, {}], handlers = {}, calls = [], resized = [], frames = new Map();
const personal = {
  free:{labels:['Livro A','Livro B'],values:[4,2],unit:'resultado(s)'},
  systematic:{labels:['Entidade'],values:[3]},
  qualitative:{labels:['Racismo','Educação'],values:[2,1]},
};
for(const kind of ['free','systematic','qualitative']) {
  const classes = new Set(['js-plotly-plot']);
  charts.set('dashboard-chart-'+kind,{id:kind,classList:{contains:x=>classes.has(x),add:x=>classes.add(x),remove:x=>classes.delete(x)}});
}
let observe, serial=0;
const document = {
  getElementById:id=>id==='dashboard-chart-data'?{textContent:JSON.stringify(personal)}:charts.get(id),
  querySelectorAll:selector=>selector.endsWith('.chart-card')?cards:[...charts.values()],
  addEventListener:(name,fn)=>handlers[name]=fn,
};
const ResizeObserver = class {constructor(fn){observe=fn;} observe(card){assert.ok(cards.includes(card));}};
const requestAnimationFrame=fn=>{frames.set(++serial,fn);return serial;}, cancelAnimationFrame=id=>frames.delete(id);
const Plotly={react:(...args)=>calls.push(args),purge:target=>target.purged=true,Plots:{resize:target=>resized.push(target)}};
const window={Plotly,ResizeObserver,PesquisaPdfPlotTheme:{palette:()=>['red','blue','green'],layout:margin=>({margin}),axis:x=>x}};
""" + source + r"""
assert.equal(calls.length,3);
for(const [target, data, layout, config] of calls) {
  assert.deepEqual(data[0].x,personal[target.id].values);
  assert.deepEqual(data[0].y,personal[target.id].labels);
  assert.equal(data[0].type,'bar');assert.equal(data[0].orientation,'h');
  assert.equal(layout.autosize,true);assert.equal(config.responsive,true);
  assert.match(data[0].hovertemplate,/%\{y\}/);
}
assert.match(calls[2][1][0].hovertemplate,/trecho\(s\)/);
handlers['tema-alterado']();assert.equal(calls.length,6);
observe(cards.map(target=>({target,contentRect:{width:900}})));
observe(cards.map(target=>({target,contentRect:{width:600}})));
assert.equal(frames.size,1);for(const fn of frames.values())fn();frames.clear();
assert.equal(resized.length,3);
observe(cards.map(target=>({target,contentRect:{width:600}})));assert.equal(frames.size,0);
handlers['dashboard-redimensionar']();assert.equal(frames.size,1);
personal.qualitative.values=[0,0];
// O JSON é uma cópia própria do componente; simula novo carregamento sem códigos aplicados.
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Novo carregamento com todas as contagens zeradas usa o estado vazio.
        empty = script.split(source, 1)[0].replace("values:[2,1]", "values:[0,0]") + source + r"""
assert.equal(calls.length,2);
const target=charts.get('dashboard-chart-qualitative');
assert.equal(target.purged,true);assert.equal(target.classList.contains('empty-chart'),true);
assert.equal(target.textContent,'Ainda não há dados para exibir neste gráfico.');
"""
        result = subprocess.run([node, "-e", empty], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
