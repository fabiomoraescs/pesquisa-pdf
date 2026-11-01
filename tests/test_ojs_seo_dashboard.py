"""Agregações do Dashboard OJS SEO em SQLite descartável."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from app import create_app
from platform_core.extensions import db
from platform_core.models import (
    AnalyticsProject,
    OjsArticle,
    OjsArticleHtmlAnalysis,
    OjsArticleObservation,
    OjsHtmlAuditRun,
    OjsHtmlEvidenceReuse,
    OjsJournal,
    OjsSeoAuditRun,
    PlanTool,
    Tool,
    User,
    utcnow,
)
from platform_core.ojs_seo_dashboard import (
    build_ojs_seo_consolidated_dashboard,
    build_ojs_seo_dashboard,
)
from platform_core.platform_help import tutorial_for
from platform_core.services import replace_grant, seed_platform


TEST_TEMP_ROOT = str(Path(__file__).resolve().parents[1] / ".test-tmp")


class OjsSeoDashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="pesquisapdf-ojs-dashboard-", dir=TEST_TEMP_ROOT,
        )
        database = Path(self.directory.name) / "dashboard.sqlite3"
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "ojs-dashboard-test-only-secret",
            "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-dashboard-test-secret-with-adequate-length",
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
        self.owner = self._user("Dashboard owner", "dashboard-owner@example.test")
        self.other = self._user("Dashboard other", "dashboard-other@example.test")

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
        replace_grant(user, "marketing_sites", "dashboard_test", "active")
        db.session.commit()
        return user

    @staticmethod
    def _journal(owner: User, name: str) -> OjsJournal:
        project = AnalyticsProject(owner_user_id=owner.id, module_key="ojs_seo", name=name, description="")
        journal = OjsJournal(
            analytics_project=project,
            public_url="https://journal.example.test",
            oai_endpoint_url="https://journal.example.test/oai",
            repository_name=name,
            discovery_status="identified",
        )
        db.session.add(journal)
        db.session.flush()
        return journal

    @staticmethod
    def _run(journal: OjsJournal, *, active: int, records: int, dois: int, offset: int) -> OjsSeoAuditRun:
        finished = utcnow() + timedelta(minutes=offset)
        run = OjsSeoAuditRun(
            journal_id=journal.id,
            status="completed",
            created_at=finished - timedelta(minutes=1),
            started_at=finished - timedelta(minutes=1),
            completed_at=finished,
            record_count=records,
            deleted_record_count=records - active,
            doi_count=dois,
            metrics_json={
                "collection": {"active_records": active, "unique_identifiers": records},
                "coverage": {
                    "title": active, "creator": active, "description": active - 1,
                    "subject": active - 1, "language": active, "date": active,
                    "type": active, "identifier": active,
                },
            },
        )
        db.session.add(run)
        db.session.flush()
        return run

    @staticmethod
    def _article(journal: OjsJournal, run: OjsSeoAuditRun, identifier: str, *, hash_value: str) -> tuple[OjsArticle, OjsArticleObservation]:
        fields = {
            "title": [f"Título {identifier}"], "creator": ["Autora Exemplo"],
            "description": ["Resumo"], "subject": ["Assunto"], "language": ["pt-BR"],
            "date": ["2026"], "type": ["article"],
            "identifier": [f"https://journal.example.test/article/{identifier}"],
        }
        article = OjsArticle(
            journal_id=journal.id, oai_identifier=identifier, record_status="active",
            title=fields["title"][0], titles_json=fields["title"], creators_json=fields["creator"],
            descriptions_json=fields["description"], subjects_json=fields["subject"],
            identifiers_json=fields["identifier"], dates_json=fields["date"],
            languages_json=fields["language"], types_json=fields["type"], doi="10.1234/example",
            dois_json=["10.1234/example"], metadata_hash=hash_value, last_observed_at=utcnow(),
        )
        db.session.add(article)
        db.session.flush()
        observation = OjsArticleObservation(
            journal_id=journal.id, article_id=article.id, audit_run_id=run.id,
            record_status="active", dublin_core_json=fields, doi_observed="10.1234/example",
            dois_observed_json=["10.1234/example"], metadata_hash=hash_value,
        )
        db.session.add(observation)
        db.session.flush()
        return article, observation

    @staticmethod
    def _analysis(
        journal: OjsJournal, article: OjsArticle, observation: OjsArticleObservation, source_run: OjsSeoAuditRun,
    ) -> OjsArticleHtmlAnalysis:
        html_run = OjsHtmlAuditRun(
            journal_id=journal.id, source_oai_audit_run_id=source_run.id, status="completed",
            started_at=utcnow() - timedelta(seconds=4), completed_at=utcnow(),
            eligible_article_count=1, analyzed_page_count=1,
        )
        db.session.add(html_run)
        db.session.flush()
        analysis = OjsArticleHtmlAnalysis(
            journal_id=journal.id, article_id=article.id, source_observation_id=observation.id,
            html_audit_run_id=html_run.id, status="analyzed",
            source_url=observation.dublin_core_json["identifier"][0],
            final_url=observation.dublin_core_json["identifier"][0], http_status=200,
            content_type="text/html", response_time_ms=42, facts_json={}, findings_json=[],
        )
        db.session.add(analysis)
        db.session.flush()
        return analysis

    def test_dashboard_distinguishes_direct_and_reused_evidence(self):
        journal = self._journal(self.owner, "Revista de dashboard")
        source = self._run(journal, active=1, records=1, dois=1, offset=-4)
        article_one, source_observation = self._article(journal, source, "oai:one", hash_value="a" * 64)
        source_analysis = self._analysis(journal, article_one, source_observation, source)

        target = self._run(journal, active=2, records=2, dois=2, offset=-1)
        target_observation_one = OjsArticleObservation(
            journal_id=journal.id, article_id=article_one.id, audit_run_id=target.id,
            record_status="active", dublin_core_json=source_observation.dublin_core_json,
            doi_observed="10.1234/example", dois_observed_json=["10.1234/example"], metadata_hash="a" * 64,
        )
        db.session.add(target_observation_one)
        article_two, target_observation_two = self._article(journal, target, "oai:two", hash_value="b" * 64)
        direct_analysis = self._analysis(journal, article_two, target_observation_two, target)
        direct_analysis.confirmed_problem_count = 1
        direct_analysis.findings_json = [{
            "code": "canonical_missing", "classification": "confirmed", "severity": "high",
            "applicability": "applicable", "evidence": "A resposta HTML não declarou canonical.",
            "impact": "A consequência depende da configuração técnica.",
            "recommendation": "Verifique a configuração do tema.", "audience": ["technical"],
        }]
        db.session.add(OjsHtmlEvidenceReuse(
            journal_id=journal.id, target_oai_audit_run_id=target.id, source_oai_audit_run_id=source.id,
            article_id=article_one.id, target_observation_id=target_observation_one.id,
            source_observation_id=source_observation.id,
            source_html_audit_run_id=source_analysis.html_audit_run_id,
            source_html_analysis_id=source_analysis.id, criteria_version="ojs-html-comparison-v3",
            reuse_reason="stable_identifier_metadata_url",
        ))
        db.session.commit()

        dashboard = build_ojs_seo_dashboard(self.owner, journal.id)
        self.assertEqual(target.id, dashboard.reference.audit_run.id)
        self.assertIsNotNone(dashboard.coverage)
        self.assertEqual(2, dashboard.coverage.eligible_article_count)
        self.assertEqual(1, dashboard.coverage.verified_article_count)
        self.assertEqual(1, dashboard.coverage.reused_evidence_article_count)
        self.assertEqual(100, dashboard.coverage.available_coverage_percentage)
        self.assertEqual(["verified", "reused", "unverified", "unprocessed"], [row.key for row in dashboard.coverage_segments])
        self.assertEqual(2, next(item.value for item in dashboard.bibliography if item.key == "active"))
        active_metric = next(item for item in dashboard.bibliography if item.key == "active")
        self.assertEqual((1, 2), active_metric.series)
        self.assertIsNone(active_metric.sparkline_path)
        self.assertIsNotNone(active_metric.comparison)
        self.assertEqual("up", active_metric.comparison.direction)
        self.assertEqual(100, active_metric.comparison.percentage)
        eligible_metric = next(item for item in dashboard.bibliography if item.key == "eligible")
        self.assertEqual((), eligible_metric.series)
        self.assertIsNone(eligible_metric.comparison)
        self.assertEqual(1, dashboard.confirmed_articles)
        self.assertEqual("canonical_missing", dashboard.problem_types[0].key)
        self.assertEqual(1, dashboard.high_priority_recommendation_count)
        self.assertEqual(
            {"critical": 0, "high": 1, "medium": 0, "low": 0, "informative": 0},
            {item.key: item.value for item in dashboard.problems_by_severity},
        )
        self.assertEqual(
            {"bibliographic": 0, "technical": 1, "comparison": 0},
            {item.key: item.value for item in dashboard.problems_by_nature},
        )
        self.assertEqual(
            {"confirmed": 1, "potential": 0, "unverified": 0},
            {item.key: item.value for item in dashboard.problems_by_certainty},
        )
        self.assertEqual(target.id, direct_analysis.html_audit_run.source_oai_audit_run_id)

    def test_metric_trends_use_only_registered_comparable_audits(self):
        journal = self._journal(self.owner, "Série real de métricas")
        self._run(journal, active=10, records=10, dois=4, offset=-9)
        self._run(journal, active=12, records=12, dois=6, offset=-6)
        latest = self._run(journal, active=15, records=16, dois=9, offset=-3)
        db.session.commit()

        dashboard = build_ojs_seo_dashboard(self.owner, journal.id)
        self.assertEqual(latest.id, dashboard.reference.audit_run.id)
        records = next(item for item in dashboard.bibliography if item.key == "records")
        self.assertEqual((10, 12, 16), records.series)
        self.assertIsNotNone(records.sparkline_path)
        self.assertIn("3 auditorias", records.sparkline_label)
        self.assertIsNotNone(records.comparison)
        self.assertEqual(33.3, records.comparison.percentage)
        self.assertEqual("up", records.comparison.direction)
        self.assertIn("não comprova melhoria editorial", records.comparison.label)

        deleted = next(item for item in dashboard.bibliography if item.key == "deleted")
        self.assertEqual((0, 0, 1), deleted.series)
        self.assertIsNone(deleted.comparison)
        self.assertIsNotNone(deleted.sparkline_path)

    def test_consolidated_dashboard_is_owner_scoped_and_archives_are_historical(self):
        active = self._journal(self.owner, "Periódico ativo")
        archived = self._journal(self.owner, "Periódico arquivado")
        archived.analytics_project.status = "archived"
        archived.analytics_project.archived_at = utcnow()
        foreign = self._journal(self.other, "Periódico de terceiro")
        self._run(active, active=1, records=1, dois=1, offset=-2)
        self._run(archived, active=1, records=1, dois=1, offset=-1)
        self._run(foreign, active=9, records=9, dois=9, offset=-1)
        db.session.commit()

        dashboard = build_ojs_seo_consolidated_dashboard(self.owner)
        self.assertEqual([active.id], [item.journal.id for item in dashboard.active_dashboards])
        self.assertEqual([archived.id], [item.journal.id for item in dashboard.archived_dashboards])
        self.assertEqual(1, dashboard.active_journals)
        self.assertEqual(0, dashboard.eligible_articles)

    def test_dashboard_without_audit_uses_empty_reference_without_inventing_metrics(self):
        journal = self._journal(self.owner, "Periódico sem coleta")
        db.session.commit()

        dashboard = build_ojs_seo_dashboard(self.owner, journal.id)
        self.assertIsNone(dashboard.reference)
        self.assertEqual((), dashboard.bibliography)
        self.assertEqual((), dashboard.coverage_segments)
        self.assertEqual((), dashboard.history)

    def test_ojs_tutorial_explains_dashboard_evidence_and_limits(self):
        tutorial = tutorial_for("ojs_seo")
        section = next(item for item in tutorial["sections"] if item["id"] == "dashboard-geral")
        text = " ".join(section["body"])
        self.assertIn("evidências históricas compatíveis", text)
        self.assertIn("não ausência de problemas", text)
        self.assertIn("Panorama dos periódicos", " ".join(item["name"] for item in section["items"]))
