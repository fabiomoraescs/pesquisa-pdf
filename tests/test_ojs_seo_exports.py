"""Exportações OJS SEO em SQLite descartável, sem auditorias nem rede."""

from __future__ import annotations

import csv
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook
from sqlalchemy import func, select

from app import create_app
from platform_core.extensions import db
from platform_core.models import (
    AnalyticsProject,
    OjsArticle,
    OjsArticleHtmlAnalysis,
    OjsArticleObservation,
    OjsHtmlAuditCampaign,
    OjsHtmlAuditRun,
    OjsHtmlEvidenceReuse,
    OjsJournal,
    OjsSeoAuditRun,
    PlanTool,
    Tool,
    User,
    utcnow,
)
from platform_core.ojs_seo_exports import build_ojs_seo_export, render_ojs_seo_export
from platform_core.services import replace_grant, seed_platform


TEST_TEMP_ROOT = str(Path(__file__).resolve().parents[1] / ".test-tmp")


class OjsSeoExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-exports-", dir=TEST_TEMP_ROOT)
        database = Path(self.directory.name) / "exports.sqlite3"
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "ojs-export-test-secret",
            "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-export-test-secret-with-adequate-length",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "PLATFORM_DATA_DIR": self.directory.name,
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        seed_platform()
        db.session.add(Tool(id="ojs_seo", name="OJS SEO de teste", route="/analytics/ojs-seo", active=True))
        db.session.commit()
        db.session.add(PlanTool(plan_id="marketing_sites", tool_id="ojs_seo"))
        db.session.commit()
        self.owner = self._user("Pessoa titular", "owner-exports@example.test")
        self.other = self._user("Outra pessoa", "other-exports@example.test")
        self.journal, self.run, self.articles = self._journal_with_evidence(self.owner)
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.context.pop()
        self.directory.cleanup()

    @staticmethod
    def _user(name: str, email: str) -> User:
        user = User(name=name, email=email, role="user", status="active")
        user.set_password("senha-de-teste-segura-123")
        db.session.add(user)
        db.session.flush()
        replace_grant(user, "marketing_sites", "ojs-export-test", "active")
        db.session.commit()
        return user

    def _journal_with_evidence(self, owner: User):
        project = AnalyticsProject(owner_user_id=owner.id, module_key="ojs_seo", name="Revista Acentuada", description="")
        journal = OjsJournal(
            analytics_project=project,
            public_url="https://journal.example.test",
            oai_endpoint_url="https://journal.example.test/oai",
            repository_name="Revista Acentuada",
            discovery_status="identified",
        )
        db.session.add(journal)
        db.session.flush()
        run = OjsSeoAuditRun(
            journal_id=journal.id, status="completed", created_at=utcnow() - timedelta(minutes=3),
            started_at=utcnow() - timedelta(minutes=3), completed_at=utcnow() - timedelta(minutes=2),
            record_count=2, doi_count=1,
            metrics_json={
                "collection": {"active_records": 2},
                "coverage": {key: 2 for key in ("title", "creator", "description", "subject", "language", "date", "type", "identifier")},
            },
        )
        db.session.add(run)
        db.session.flush()
        first = self._article(journal, run, "oai:export:one", "=Título seguro", "10.1234/export-one")
        second = self._article(journal, run, "oai:export:two", "Título sem DOI", "")
        html_run = OjsHtmlAuditRun(
            journal_id=journal.id, source_oai_audit_run_id=run.id, status="completed",
            created_at=utcnow() - timedelta(minutes=1), started_at=utcnow() - timedelta(minutes=1),
            completed_at=utcnow(), eligible_article_count=2, analyzed_page_count=1,
            confirmed_problem_count=1, potential_problem_count=1,
        )
        db.session.add(html_run)
        db.session.flush()
        db.session.add(OjsArticleHtmlAnalysis(
            journal_id=journal.id, article_id=first[0].id, source_observation_id=first[1].id,
            html_audit_run_id=html_run.id, status="analyzed", source_url=first[1].dublin_core_json["identifier"][0],
            final_url=first[1].dublin_core_json["identifier"][0], http_status=200, content_type="text/html",
            findings_json=[{
                "code": "canonical_missing", "classification": "confirmed", "severity": "high",
                "applicability": "applicable", "evidence": "Canonical ausente.",
                "recommendation": "Defina canonical.", "impact": "Pode afetar a URL canônica.", "audience": "technical",
            }, {
                "code": "title_oai_html_divergence", "classification": "potential", "severity": "medium",
                "applicability": "context_dependent", "evidence": "Os títulos divergem.",
                "recommendation": "Revise os títulos.", "impact": "Pode dificultar identificação.", "audience": "editorial",
            }],
            facts_json={"oai_html_consistency": {
                "criteria_version": "ojs-html-comparison-v3",
                "comparisons": [{
                    "field": "title", "oai_values": ["=Título seguro"],
                    "html_values": ["Título HTML divergente"], "result": "potential",
                }],
            }},
            confirmed_problem_count=1, potential_problem_count=1,
        ))
        db.session.commit()
        return journal, run, (first, second)

    @staticmethod
    def _article(journal: OjsJournal, run: OjsSeoAuditRun, identifier: str, title: str, doi: str):
        fields = {
            "title": [title], "creator": ["Autora com acentuação"], "description": ["Resumo longo\ncom quebra de linha"],
            "subject": ["Assunto"], "language": ["pt-BR"], "date": ["2026-10-30"], "type": ["article"],
            "identifier": [f"https://journal.example.test/article/{identifier.rsplit(':', 1)[-1]}"],
        }
        article = OjsArticle(
            journal_id=journal.id, oai_identifier=identifier, record_status="active", title=title,
            titles_json=fields["title"], creators_json=fields["creator"], descriptions_json=fields["description"],
            subjects_json=fields["subject"], identifiers_json=fields["identifier"], dates_json=fields["date"],
            languages_json=fields["language"], types_json=fields["type"], doi=doi or None,
            dois_json=[doi] if doi else [], metadata_hash=(identifier[-1] * 64), last_observed_at=utcnow(),
        )
        db.session.add(article)
        db.session.flush()
        observation = OjsArticleObservation(
            journal_id=journal.id, article_id=article.id, audit_run_id=run.id, record_status="active",
            dublin_core_json=fields, doi_observed=doi or None, dois_observed_json=[doi] if doi else [],
            metadata_hash=article.metadata_hash, observed_at=utcnow(),
        )
        db.session.add(observation)
        db.session.flush()
        return article, observation

    @staticmethod
    def _login(client, user: User):
        # Cada caso mantém o mesmo application context. Remover o usuário
        # memorizado pelo request anterior evita que uma consulta anônima de
        # segurança contamine a sessão autenticada do cliente de teste.
        from flask import g

        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session["_user_id"] = user.id
            session["_fresh"] = True

    def test_consolidated_xlsx_is_read_only_and_has_traceable_sheets(self):
        before = {
            "audits": db.session.scalar(select(func.count()).select_from(OjsSeoAuditRun)),
            "campaigns": db.session.scalar(select(func.count()).select_from(OjsHtmlAuditCampaign)),
            "analyses": db.session.scalar(select(func.count()).select_from(OjsArticleHtmlAnalysis)),
        }
        bundle = build_ojs_seo_export(self.owner, self.journal.id, dataset="consolidated")
        exported = render_ojs_seo_export(bundle, export_format="xlsx")
        workbook = load_workbook(BytesIO(exported.content), data_only=False)
        self.assertIn("Informações gerais", workbook.sheetnames)
        self.assertIn("Visão geral", workbook.sheetnames)
        self.assertIn("Diagnóstico bibliográfico", workbook.sheetnames)
        self.assertIn("Diagnóstico HTML", workbook.sheetnames)
        self.assertIn("Comparação OAI × HTML", workbook.sheetnames)
        self.assertIn("Problemas e recomendações", workbook.sheetnames)
        self.assertIn("Publicações afetadas", workbook.sheetnames)
        self.assertIn("Histórico de análises", workbook.sheetnames)
        bibliographic = workbook["Diagnóstico bibliográfico"]
        self.assertEqual("Identificador OAI-PMH", bibliographic["A1"].value)
        self.assertTrue(str(bibliographic["B2"].value).startswith("'="))
        self.assertEqual("A2", bibliographic.freeze_panes)
        self.assertIsNotNone(bibliographic.auto_filter.ref)
        description_column = [cell.value for cell in bibliographic[1]].index("Descrição/resumo") + 1
        self.assertEqual("Resumo longo com quebra de linha", bibliographic.cell(2, description_column).value)
        for worksheet in workbook.worksheets:
            self.assertEqual(15, worksheet.row_dimensions[1].height)
            for row_index, row in enumerate(worksheet.iter_rows(), start=1):
                if row_index > 1:
                    self.assertEqual(15, worksheet.row_dimensions[row_index].height)
                for cell in row:
                    self.assertIsNot(cell.alignment.wrap_text, True)
                    if isinstance(cell.value, str):
                        self.assertNotRegex(cell.value, r"[\r\n]")
        history = workbook["Histórico de análises"]
        self.assertIn("Evidências reaproveitadas", [cell.value for cell in history[1]])
        self.assertTrue(all(len(row) == history.max_column for row in history.iter_rows(values_only=True)))
        comparison = workbook["Comparação OAI × HTML"]
        self.assertEqual("'=Título seguro", comparison["D2"].value)
        self.assertEqual("Título HTML divergente", comparison["E2"].value)
        after = {
            "audits": db.session.scalar(select(func.count()).select_from(OjsSeoAuditRun)),
            "campaigns": db.session.scalar(select(func.count()).select_from(OjsHtmlAuditCampaign)),
            "analyses": db.session.scalar(select(func.count()).select_from(OjsArticleHtmlAnalysis)),
        }
        self.assertEqual(before, after)

    def test_csv_uses_bom_semicolon_and_same_backend_filter(self):
        bundle = build_ojs_seo_export(
            self.owner, self.journal.id, dataset="bibliographic",
            filters={"audit": self.run.id, "filter": "missing_doi", "state": "active"},
        )
        exported = render_ojs_seo_export(bundle, export_format="csv")
        self.assertTrue(exported.content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(StringIO(exported.content.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual("Identificador OAI-PMH", rows[0][0])
        self.assertEqual(2, len(rows))
        self.assertEqual("oai:export:two", rows[1][0])
        self.assertEqual("Título sem DOI", rows[1][1])

        all_rows = list(csv.reader(StringIO(render_ojs_seo_export(
            build_ojs_seo_export(self.owner, self.journal.id, dataset="bibliographic"),
            export_format="csv",
        ).content.decode("utf-8-sig")), delimiter=";"))
        self.assertTrue(all_rows[1][1].startswith("'="))
        self.assertIn("Resumo longo\ncom quebra de linha", all_rows[1])

    def test_html_export_preserves_reused_evidence_origin_without_calling_html(self):
        article, target_observation = self.articles[1]
        source = OjsSeoAuditRun(
            journal_id=self.journal.id, status="completed", created_at=utcnow() - timedelta(days=2),
            started_at=utcnow() - timedelta(days=2), completed_at=utcnow() - timedelta(days=2), record_count=1,
        )
        db.session.add(source)
        db.session.flush()
        source_observation = OjsArticleObservation(
            journal_id=self.journal.id, article_id=article.id, audit_run_id=source.id, record_status="active",
            dublin_core_json=target_observation.dublin_core_json, doi_observed=None,
            metadata_hash=article.metadata_hash, observed_at=utcnow() - timedelta(days=2),
        )
        db.session.add(source_observation)
        db.session.flush()
        source_html_run = OjsHtmlAuditRun(
            journal_id=self.journal.id, source_oai_audit_run_id=source.id, status="completed",
            started_at=utcnow() - timedelta(days=2), completed_at=utcnow() - timedelta(days=2),
            eligible_article_count=1, analyzed_page_count=1,
        )
        db.session.add(source_html_run)
        db.session.flush()
        source_analysis = OjsArticleHtmlAnalysis(
            journal_id=self.journal.id, article_id=article.id, source_observation_id=source_observation.id,
            html_audit_run_id=source_html_run.id, status="analyzed", source_url=source_observation.dublin_core_json["identifier"][0],
            final_url=source_observation.dublin_core_json["identifier"][0], http_status=200, findings_json=[],
            checked_at=utcnow() - timedelta(days=2),
        )
        db.session.add(source_analysis)
        db.session.flush()
        db.session.add(OjsHtmlEvidenceReuse(
            journal_id=self.journal.id, target_oai_audit_run_id=self.run.id, source_oai_audit_run_id=source.id,
            article_id=article.id, target_observation_id=target_observation.id, source_observation_id=source_observation.id,
            source_html_audit_run_id=source_html_run.id, source_html_analysis_id=source_analysis.id,
            criteria_version="ojs-html-comparison-v3", reuse_reason="stable_identifier_metadata_url",
        ))
        db.session.commit()

        bundle = build_ojs_seo_export(self.owner, self.journal.id, dataset="html", filters={"audit": self.run.id})
        table = bundle.tables[-1]
        reused_row = next(row for row in table.rows if row[0] == article.oai_identifier)
        self.assertEqual("Evidência reaproveitada", reused_row[3])
        self.assertEqual("Evidência histórica reaproveitada", reused_row[4])
        self.assertEqual(source.id, reused_row[14])

    def test_authenticated_route_downloads_and_blocks_cross_journal_access(self):
        unauthenticated = self.app.test_client()
        self.assertEqual(
            302,
            unauthenticated.get(f"/analytics/ojs-seo/journals/{self.journal.id}/exports/history?format=csv").status_code,
        )
        self._login(self.client, self.owner)
        base = f"/analytics/ojs-seo/journals/{self.journal.id}/exports/recommendations"
        response = self.client.get(f"{base}?format=csv&audit={self.run.id}&certainty=confirmed")
        self.assertEqual(200, response.status_code)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("Canonical", response.data.decode("utf-8-sig"))
        self.assertEqual("no-store", response.headers["Cache-Control"])

        other_client = self.app.test_client()
        self._login(other_client, self.other)
        self.assertEqual(404, other_client.get(f"{base}?format=csv").status_code)

        # O mesmo app context é compartilhado pela fixture: após o cliente de
        # outro usuário, restaura-se o titular antes de verificar entradas
        # inválidas da rota autorizada.
        self._login(self.client, self.owner)
        self.assertEqual(400, self.client.get(f"{base}?format=bad").status_code)
        self.assertEqual(400, self.client.get(
            f"/analytics/ojs-seo/journals/{self.journal.id}/exports/consolidated?format=csv"
        ).status_code)
        self.assertEqual(400, self.client.get(f"{base}?format=csv&certainty=invalid").status_code)

    def test_journal_without_audit_still_exports_context_without_creating_records(self):
        project = AnalyticsProject(owner_user_id=self.owner.id, module_key="ojs_seo", name="Sem auditoria", description="")
        journal = OjsJournal(
            analytics_project=project, public_url="https://empty.example.test",
            oai_endpoint_url="https://empty.example.test/oai", repository_name="Sem auditoria",
            discovery_status="identified",
        )
        db.session.add(journal)
        db.session.commit()
        bundle = build_ojs_seo_export(self.owner, journal.id, dataset="consolidated")
        self.assertEqual(("information", "overview"), tuple(table.key for table in bundle.tables))
        self.assertIn("Não há auditoria bibliográfica persistida", bundle.tables[0].rows[-1][1])
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsHtmlAuditCampaign)))
        empty_history = build_ojs_seo_export(self.owner, journal.id, dataset="history")
        self.assertEqual("Sem dados persistidos para o escopo selecionado.", empty_history.tables[-1].rows[0][0])
        exported = render_ojs_seo_export(bundle, export_format="xlsx")
        self.assertGreater(len(exported.content), 100)

    def test_templates_offer_only_supported_export_actions(self):
        self._login(self.client, self.owner)
        overview = self.client.get(f"/analytics/ojs-seo/journals/{self.journal.id}")
        self.assertEqual(200, overview.status_code)
        self.assertIn("Exportar XLSX", overview.get_data(as_text=True))
        articles = self.client.get(f"/analytics/ojs-seo/journals/{self.journal.id}/articles?audit={self.run.id}")
        self.assertEqual(200, articles.status_code)
        self.assertIn(f"/exports/bibliographic?format=xlsx", articles.get_data(as_text=True))
        recommendations = self.client.get(
            f"/analytics/ojs-seo/journals/{self.journal.id}/recommendations?audit={self.run.id}"
        )
        self.assertEqual(200, recommendations.status_code)
        self.assertIn(f"/exports/recommendations?format=xlsx", recommendations.get_data(as_text=True))
        history = self.client.get(f"/analytics/ojs-seo/journals/{self.journal.id}/history")
        self.assertEqual(200, history.status_code)
        self.assertIn(f"/exports/history?format=xlsx", history.get_data(as_text=True))
        for page in (articles, recommendations, history):
            markup = page.get_data(as_text=True)
            self.assertNotIn("Na referência bibliográfica selecionada.", markup)
            self.assertNotIn("Na coleta bibliográfica de referência.", markup)
            self.assertNotIn("Na referência bibliográfica atual.", markup)

        root = Path(__file__).resolve().parents[1]
        css = (root / "static/css/platform.css").read_text(encoding="utf-8")
        component = (root / "templates/platform/_metric_card.html").read_text(encoding="utf-8")
        self.assertIn(".platform-metric-card:has(.platform-metric-card__description)", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(3.25rem, 4.75rem);", css)
        self.assertIn("minmax(2.75rem, 3.25rem)", css)
        self.assertIn('stroke-width: 1.6;', css)
        self.assertEqual(5, component.count('class="platform-metric-card__sparkline-line"'))
