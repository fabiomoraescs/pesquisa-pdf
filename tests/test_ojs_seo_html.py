from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from multiprocessing import get_context
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from flask_migrate import downgrade, upgrade
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

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
    Workspace,
    WorkspaceMembership,
    WorkspaceSubscription,
    WorkspaceSubscriptionOjsJournal,
    utcnow,
)
from platform_core.ojs_html_client import (
    HtmlClientConfig,
    HtmlFetchResult,
    HtmlHttpResponse,
    HtmlPageClient,
    analyze_html_page,
    html_finding_label,
)
from platform_core.ojs_oai_client import OaiTransportError
from platform_core.ojs_seo_executor import claim_ojs_seo_audit
from platform_core.ojs_seo_coordinator import (
    advance_integrated_after_html_campaign,
    advance_integrated_after_oai,
    request_ojs_integrated_analysis,
)
from platform_core.ojs_seo_html_executor import (
    HTML_MINIMUM_PAGE_BUDGET_SECONDS,
    _has_page_budget,
    claim_ojs_html_audit,
    process_ojs_html_audit,
    recover_expired_ojs_html_audits,
    run_ojs_html_worker,
)
from platform_core.ojs_seo_services import (
    OjsSeoProjectLifecycleError,
    OjsSeoServiceError,
    archive_ojs_seo_project,
)
from platform_core.ojs_seo_recommendations import (
    get_ojs_recommendation_detail,
    list_ojs_recommendations,
)
from platform_core.ojs_seo_technical import (
    HTML_SELECTION_REVALIDATE,
    HTML_SELECTION_RETRY_UNVERIFIED,
    HtmlAuditCandidate,
    get_ojs_html_coverage,
    list_html_audit_candidates,
    list_ojs_html_analysis_pages,
    persist_html_audit_results,
    request_ojs_html_audit_campaign,
    request_ojs_html_audit,
    reuse_compatible_html_evidence,
)
from platform_core.services import replace_grant, seed_platform


UTC = timezone.utc
MIGRATIONS = str(Path(__file__).parents[1] / "migrations")
TEST_TEMP_ROOT = str(Path(__file__).parents[1] / ".test-tmp")


def _claim_ojs_run_in_process(database_uri: str, data_dir: str, kind: str, audit_run_id: str, start, output) -> None:
    """Comprova que os workers OAI e HTML compartilham o mesmo lock do periódico."""
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "ojs-html-concurrency-test-only",
        "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-html-test-secret-with-adequate-length-123456",
        "SQLALCHEMY_DATABASE_URI": database_uri,
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 30}},
        "PLATFORM_DATA_DIR": data_dir,
    })
    try:
        with app.app_context():
            start.wait(15)
            claim = (
                claim_ojs_seo_audit(audit_run_id, lease_seconds=120)
                if kind == "oai" else claim_ojs_html_audit(audit_run_id, lease_seconds=120)
            )
            output.put((kind, bool(claim)))
            db.session.remove()
            db.engine.dispose()
    except BaseException as exc:  # Evidência do processo auxiliar, sem HTTP.
        output.put((kind, "error", type(exc).__name__))


def _resolver(host, _port, **_kwargs):
    if host == "private.example.test":
        return [(None, None, None, None, ("127.0.0.1", 443))]
    return [(None, None, None, None, ("93.184.216.34", 443))]


class _RedirectTransport:
    def __init__(self):
        self.targets: list[str] = []

    def get(self, url, _config):
        self.targets.append(url.canonical_url)
        if len(self.targets) == 1:
            return HtmlHttpResponse(302, {"location": "https://public.example.test/article"}, b"")
        return HtmlHttpResponse(200, {"content-type": "text/html"}, b"<html><title>OK</title></html>")


class OjsHtmlClientTests(unittest.TestCase):
    def test_parser_records_evidence_without_html_and_distinguishes_findings(self):
        html = b'''<html><head><title>Artigo OJS</title><meta name="description" content="Resumo"><meta name="robots" content="noindex"><meta name="citation_title" content="Titulo OAI"><meta name="citation_author" content="Ana"><meta name="citation_journal_title" content="Revista"><meta name="citation_publication_date" content="2026-10-01"><meta name="citation_doi" content="10.1234/teste"><link rel="canonical" href="https://public.example.test/article"><script type="application/ld+json">{"@type":"ScholarlyArticle"}</script></head><body><h1>Artigo OJS</h1></body></html>'''
        result = analyze_html_page(
            HtmlFetchResult("https://public.example.test/article", "https://public.example.test/article", 200,
                            {"content-type": "text/html", "x-robots-tag": "noindex"}, html, 0, 12),
            oai_fields={"title": ["Titulo OAI"], "creator": ["Ana"]},
            oai_doi="10.1234/teste", repository_name="Revista",
        )
        self.assertEqual("analyzed", result.status)
        self.assertEqual(("ScholarlyArticle",), result.facts["structured_data"]["schema_types"])
        self.assertIn("noindex_observed", {item.code for item in result.findings})
        self.assertNotIn("canonical_missing", {item.code for item in result.findings})
        self.assertNotIn(b"<html", str(result.facts).encode())

    def test_comparisons_keep_original_evidence_and_ignore_formal_title_variations(self):
        html = b'''<html><head><title>Analise OJS</title><meta name="citation_title" content="Analise OJS &amp; Metadados"><meta name="citation_author" content="Joao da Silva"><meta name="citation_author" content="Maria de Souza"><meta name="citation_journal_title" content="Revista de Testes"><meta name="citation_publication_date" content="2025-04-20"><meta name="citation_doi" content="https://doi.org/10.1234/ABC.1"><link rel="canonical" href="https://public.example.test/article"></head><body><h1>Analise OJS</h1></body></html>'''
        result = analyze_html_page(
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"}, html, 0, 12,
            ),
            oai_fields={
                "title": ["Análise: OJS & Metadados"],
                "creator": ["João da Silva", "Maria Souza"],
                "date": ["2025"],
            },
            oai_doi="doi:10.1234/abc.1",
            repository_name="Revista de Testes",
            oai_dois=("https://doi.org/10.1234/ABC.1",),
        )

        evidence = result.facts["oai_html_consistency"]
        self.assertEqual("ojs-html-comparison-v3", evidence["criteria_version"])
        rows = {row["field"]: row for row in evidence["comparisons"]}
        self.assertEqual("compatible", rows["title"]["result"])
        self.assertEqual(["Análise: OJS & Metadados"], list(rows["title"]["oai_values"]))
        self.assertEqual(["analise ojs metadados"], list(rows["title"]["oai_normalized"]))
        self.assertEqual("potential", rows["authors"]["result"])
        self.assertIn("não são tratados como prova de identidade", rows["authors"]["reason"])
        self.assertEqual("compatible", rows["publication_date"]["result"])
        self.assertIn("não comprova igualdade da data completa", rows["publication_date"]["reason"])
        self.assertEqual("compatible", rows["doi"]["result"])
        self.assertEqual(2, len(rows["doi"]["oai_values"]))
        self.assertEqual("not_applicable", rows["issn"]["result"])
        self.assertIn("authors_oai_html_divergence", {item.code for item in result.findings})
        self.assertNotIn("title_oai_html_divergence", {item.code for item in result.findings})

    def test_author_comparison_recognizes_explicit_bibliographic_order_only(self):
        def authors_row(oai_authors, html_authors):
            tags = "".join(
                f'<meta name="citation_author" content="{author}">' for author in html_authors
            )
            result = analyze_html_page(
                HtmlFetchResult(
                    "https://public.example.test/article", "https://public.example.test/article", 200,
                    {"content-type": "text/html"},
                    f"<html><head><title>Registro</title>{tags}<link rel='canonical' href='https://public.example.test/article'></head><body><h1>Registro</h1></body></html>".encode(),
                    0, 8,
                ),
                oai_fields={"creator": oai_authors}, oai_doi=None, repository_name=None,
            )
            row = next(item for item in result.facts["oai_html_consistency"]["comparisons"] if item["field"] == "authors")
            return row, {item.code for item in result.findings}

        compatible_cases = (
            (
                ["Cucinotta, Caterina", "Patrício, Catarina"],
                ["Catarina Patrício", "Caterina Cucinotta"],
            ),
            (["de la Cruz, María José"], ["María José de la Cruz"]),
            (["Van der Waals, Johannes Diderik"], ["Johannes Diderik van der Waals"]),
            (["D'Ávila, Ana-Maria"], ["Ana Maria DAvila"]),
        )
        for oai_authors, html_authors in compatible_cases:
            with self.subTest(oai_authors=oai_authors, html_authors=html_authors):
                row, findings = authors_row(oai_authors, html_authors)
                self.assertEqual("compatible", row["result"])
                self.assertIn("formato bibliográfico explícito", row["reason"])
                self.assertNotIn("authors_oai_html_divergence", findings)

        for oai_authors, html_authors in (
            (["Cucinotta, C."], ["Caterina Cucinotta"]),
            (["Cucinotta, Caterina"], ["Catarina Patrício"]),
            (["Cucinotta Caterina"], ["Caterina Cucinotta"]),
            (["Universidade, Federal do Ceará"], ["Federal do Ceará Universidade"]),
        ):
            with self.subTest(oai_authors=oai_authors, html_authors=html_authors):
                row, findings = authors_row(oai_authors, html_authors)
                self.assertEqual("potential", row["result"])
                self.assertIn("authors_oai_html_divergence", findings)

    def test_finding_labels_are_readable_without_exposing_internal_codes(self):
        expected = {
            "canonical_missing": "Canonical ausente",
            "citation_publication_date_missing": "Data de publicação não identificada",
            "authors_oai_html_divergence": "Divergência de autoria",
            "http_403": "Resposta HTTP 403",
            "connect_timeout": "Tempo de conexão excedido",
            "redirect_limit_reached": "Limite de redirecionamentos atingido",
            "non_public_dns_address": "Destino não público recusado",
        }
        for code, label in expected.items():
            with self.subTest(code=code):
                self.assertEqual(label, html_finding_label(code))
        self.assertEqual("Achado técnico não classificado", html_finding_label("future_internal_code"))

    def test_full_dates_that_differ_are_potential_not_compatible(self):
        result = analyze_html_page(
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"},
                b"<html><head><title>Registro</title><meta name='citation_publication_date' content='2025-12-20'><link rel='canonical' href='https://public.example.test/article'></head><body><h1>Registro</h1></body></html>",
                0, 8,
            ),
            oai_fields={"date": ["2025-04-20"]}, oai_doi=None, repository_name=None,
        )

        rows = {row["field"]: row for row in result.facts["oai_html_consistency"]["comparisons"]}
        self.assertEqual("potential", rows["publication_date"]["result"])
        self.assertEqual("low", rows["publication_date"]["severity"])
        self.assertIn("publication_date_oai_html_divergence", {item.code for item in result.findings})

    def test_redirect_is_revalidated_and_private_destination_is_rejected(self):
        transport = _RedirectTransport()
        fetched = HtmlPageClient(resolver=_resolver, transport=transport, sleeper=lambda _delay: None).fetch(
            "https://source.example.test/article"
        )
        self.assertEqual(200, fetched.http_status)
        self.assertEqual("https://public.example.test/article", fetched.final_url)
        self.assertEqual(1, fetched.redirect_count)
        self.assertEqual(2, len(transport.targets))

        class _UnsafeRedirect:
            def get(self, _url, _config):
                return HtmlHttpResponse(302, {"location": "https://private.example.test/article"}, b"")

        blocked = HtmlPageClient(resolver=_resolver, transport=_UnsafeRedirect(), sleeper=lambda _delay: None).fetch(
            "https://source.example.test/article"
        )
        self.assertEqual("non_public_dns_address", blocked.issue_code)

    def test_timeout_is_not_classified_as_missing_metadata(self):
        result = analyze_html_page(
            HtmlFetchResult("https://public.example.test/a", None, None, {}, None, 0, 50, "read_timeout", "Tempo excedido"),
            oai_fields={}, oai_doi=None, repository_name=None,
        )
        self.assertEqual("not_verified", result.status)
        self.assertEqual("unverified", result.findings[0].classification)

    def test_transport_timeout_uses_an_html_specific_public_message(self):
        class _TimeoutTransport:
            def get(self, _url, _config):
                raise OaiTransportError(
                    "connect_timeout",
                    "A conexão com o endpoint OAI-PMH excedeu o prazo permitido.",
                )

        fetched = HtmlPageClient(
            config=HtmlClientConfig(max_request_attempts=1),
            resolver=_resolver,
            transport=_TimeoutTransport(),
            sleeper=lambda _delay: None,
        ).fetch("https://public.example.test/article")

        self.assertEqual("connect_timeout", fetched.issue_code)
        self.assertEqual("A conexão com a página HTML excedeu o prazo permitido.", fetched.issue_message)
        self.assertNotIn("endpoint OAI-PMH", fetched.issue_message)

    def test_http_failure_is_a_confirmed_accessibility_finding(self):
        for status in (403, 404, 500):
            with self.subTest(status=status):
                result = analyze_html_page(
                    HtmlFetchResult("https://public.example.test/a", "https://public.example.test/a", status,
                                    {"content-type": "text/html"}, b"not found", 0, 4),
                    oai_fields={}, oai_doi=None, repository_name=None,
                )
                self.assertEqual("analyzed", result.status)
                self.assertIn(f"http_{status}", {item.code for item in result.findings})
                self.assertEqual("confirmed", result.findings[0].classification)


class OjsHtmlPersistenceAndExecutorTests(unittest.TestCase):
    def setUp(self):
        self.scope = tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-html-", dir=TEST_TEMP_ROOT)
        database = Path(self.scope.name) / "ojs-html.sqlite3"
        self.database_uri = f"sqlite:///{database.as_posix()}"
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "ojs-html-test-only",
            "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-html-test-secret-with-adequate-length-123456",
            "SQLALCHEMY_DATABASE_URI": self.database_uri,
            "PLATFORM_DATA_DIR": self.scope.name,
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        seed_platform()
        self.user = User(name="HTML OJS", email="html-ojs@example.test", role="user", status="active")
        self.user.set_password("senha-de-teste-segura-123")
        db.session.add(self.user)
        db.session.flush()
        replace_grant(self.user, "marketing_sites", "html_test", "active")
        workspace = Workspace(name="Área HTML OJS", workspace_type="individual", status="active", owner_user_id=self.user.id)
        membership = WorkspaceMembership(workspace=workspace, user=self.user, role="owner", status="active")
        self.subscription = WorkspaceSubscription(
            workspace=workspace, plan_id="marketing_sites", status="active",
            terms_json={"plan_limits": {"instagram_accounts": 0, "ojs_journals": 2}},
        )
        db.session.add_all((Tool(id="ojs_seo", name="OJS SEO teste", route="/test-ojs", active=True), workspace, membership, self.subscription))
        db.session.flush()
        db.session.add(PlanTool(plan_id="marketing_sites", tool_id="ojs_seo"))
        project = AnalyticsProject(owner_user_id=self.user.id, module_key="ojs_seo", name="Periódico HTML", description="")
        self.journal = OjsJournal(analytics_project=project, public_url="https://public.example.test/journal", oai_endpoint_url="https://public.example.test/oai", repository_name="Revista", discovery_status="identified")
        db.session.add(self.journal)
        db.session.flush()
        db.session.add(WorkspaceSubscriptionOjsJournal(
            workspace_subscription_id=self.subscription.id, ojs_journal_id=self.journal.id,
            canonical_identity_hash=hashlib.sha256(b"html-journal").hexdigest(),
            canonical_endpoint_url=self.journal.oai_endpoint_url, status="active",
        ))
        self.source = OjsSeoAuditRun(journal_id=self.journal.id, status="completed", completed_at=utcnow())
        db.session.add(self.source)
        db.session.flush()
        self.article = OjsArticle(
            journal_id=self.journal.id, oai_identifier="oai:html:1", title="Título OAI",
            record_status="active", metadata_hash="a" * 64,
        )
        db.session.add(self.article)
        db.session.flush()
        self.observation = OjsArticleObservation(
            journal_id=self.journal.id, article_id=self.article.id, audit_run_id=self.source.id,
            record_status="active", dublin_core_json={"title": ["Título OAI"], "creator": ["Ana"], "identifier": ["https://public.example.test/article", "10.1000/example"]}, doi_observed="10.1000/example", metadata_hash="a" * 64,
        )
        db.session.add(self.observation)
        db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); db.engine.dispose(); self.context.pop(); self.scope.cleanup()

    def _running(self):
        now = utcnow()
        run = OjsHtmlAuditRun(
            journal_id=self.journal.id, source_oai_audit_run_id=self.source.id, status="running",
            started_at=now, claim_token="a" * 36, claimed_at=now, lease_expires_at=now + timedelta(minutes=5),
        )
        db.session.add(run); db.session.commit()
        return run

    def _add_article(self, suffix: str) -> tuple[OjsArticle, OjsArticleObservation]:
        article = OjsArticle(
            journal_id=self.journal.id,
            oai_identifier=f"oai:html:{suffix}",
            title=f"Título {suffix}",
            record_status="active",
            metadata_hash=hashlib.sha256(f"metadata:{suffix}".encode()).hexdigest(),
        )
        db.session.add(article)
        db.session.flush()
        observation = OjsArticleObservation(
            journal_id=self.journal.id,
            article_id=article.id,
            audit_run_id=self.source.id,
            record_status="active",
            dublin_core_json={"title": [article.title], "identifier": [f"https://public.example.test/article/{suffix}"]},
            metadata_hash=article.metadata_hash,
        )
        db.session.add(observation)
        db.session.commit()
        return article, observation

    def _persist_result(self, article, observation, fetched: HtmlFetchResult, *, final_status: str = "completed"):
        run = self._running()
        result = analyze_html_page(
            fetched,
            oai_fields=observation.dublin_core_json,
            oai_doi=observation.doi_observed,
            repository_name="Revista",
        )
        persist_html_audit_results(
            self.journal,
            run,
            ((HtmlAuditCandidate(article, observation, fetched.source_url), result),),
            eligible_article_count=2,
            final_status=final_status,
        )
        db.session.commit()
        return run

    def _completed_updated_reference(self, observations, *, additions=()):
        """Registra uma nova referência OAI para testar somente a coordenação.

        ``observations`` contém ``(article, old_observation, overrides)``. O
        helper não executa HTTP nem worker; ele simula somente a persistência
        que já teria sido concluída pelo executor OAI-PMH.
        """
        operation = request_ojs_integrated_analysis(self.user, self.journal.id)
        current = db.session.get(OjsSeoAuditRun, operation.oai_audit_run_id)
        current.status, current.completed_at = "completed", utcnow()
        for article, original, overrides in observations:
            values = {
                "record_status": original.record_status,
                "dublin_core_json": dict(original.dublin_core_json),
                "doi_observed": original.doi_observed,
                "dois_observed_json": list(original.dois_observed_json),
                "oai_datestamp": original.oai_datestamp,
                "metadata_hash": original.metadata_hash,
            }
            values.update(overrides)
            db.session.add(OjsArticleObservation(
                journal_id=self.journal.id,
                article_id=article.id,
                audit_run_id=current.id,
                **values,
            ))
        added: list[tuple[OjsArticle, OjsArticleObservation]] = []
        for suffix, metadata_hash, public_url in additions:
            article = OjsArticle(
                journal_id=self.journal.id,
                oai_identifier=f"oai:html:new:{suffix}",
                title=f"Novo artigo {suffix}",
                record_status="active",
                metadata_hash=metadata_hash,
            )
            db.session.add(article)
            db.session.flush()
            observation = OjsArticleObservation(
                journal_id=self.journal.id,
                article_id=article.id,
                audit_run_id=current.id,
                record_status="active",
                dublin_core_json={"title": [article.title], "identifier": [public_url]},
                metadata_hash=metadata_hash,
            )
            db.session.add(observation)
            added.append((article, observation))
        db.session.flush()
        return operation, current, advance_integrated_after_oai(self.journal, current), tuple(added)

    def test_persistence_keeps_oai_snapshot_and_stores_only_html_evidence(self):
        run = self._running()
        fetched = HtmlFetchResult("https://public.example.test/article", "https://public.example.test/article", 200, {"content-type": "text/html"}, b"<title>HTML</title><h1>HTML</h1><link rel='canonical' href='https://public.example.test/article'>", 0, 4)
        result = analyze_html_page(fetched, oai_fields=self.observation.dublin_core_json, oai_doi=self.observation.doi_observed, repository_name="Revista")
        candidate = HtmlAuditCandidate(self.article, self.observation, fetched.source_url)
        stored = persist_html_audit_results(self.journal, run, ((candidate, result),), eligible_article_count=1, final_status="completed")
        db.session.commit()
        self.assertEqual("completed", stored.status)
        self.assertEqual(0, stored.confirmed_problem_count)
        self.assertEqual(1, stored.potential_problem_count)
        analysis = db.session.scalar(select(OjsArticleHtmlAnalysis).where(OjsArticleHtmlAnalysis.html_audit_run_id == run.id))
        self.assertEqual(self.observation.id, analysis.source_observation_id)
        self.assertEqual("Título OAI", db.session.get(OjsArticleObservation, self.observation.id).dublin_core_json["title"][0])
        self.assertNotIn("<title>", str(analysis.facts_json))
        self.assertEqual("ojs-html-comparison-v3", analysis.facts_json["oai_html_consistency"]["criteria_version"])
        self.assertTrue(all(
            {"severity", "applicability", "impact", "audience"}.issubset(finding)
            for finding in analysis.findings_json
        ))

    def test_executor_processes_pending_run_with_local_client_only(self):
        run = OjsHtmlAuditRun(journal_id=self.journal.id, source_oai_audit_run_id=self.source.id, status="pending")
        db.session.add(run); db.session.commit()

        class _LocalClient:
            def fetch(self, url, *, deadline):
                return HtmlFetchResult(url, url, 200, {"content-type": "text/html"}, b"<html><head><title>Titulo OAI</title><link rel='canonical' href='https://public.example.test/article'><meta name='citation_title' content='T\xc3\xadtulo OAI'><meta name='citation_author' content='Ana'><meta name='citation_journal_title' content='Revista'><meta name='citation_publication_date' content='2026-10-01'><meta name='citation_doi' content='10.1000/example'></head><body><h1>Titulo OAI</h1></body></html>", 0, 2)

        processed = process_ojs_html_audit(run.id, client_factory=lambda: _LocalClient(), lease_seconds=120)
        self.assertEqual("completed", processed.status)
        self.assertEqual(1, processed.analyzed_page_count)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(OjsArticleHtmlAnalysis).where(
            OjsArticleHtmlAnalysis.html_audit_run_id == run.id
        )))

    def test_candidate_selection_skips_doi_resolver_and_is_deterministically_limited(self):
        self.observation.dublin_core_json = {
            "identifier": ["https://doi.org/10.1000/example", "https://public.example.test/article"],
        }
        for number in range(100):
            article = OjsArticle(
                journal_id=self.journal.id, oai_identifier=f"oai:html:bulk:{number:03d}",
                record_status="active",
            )
            db.session.add(article)
            db.session.flush()
            db.session.add(OjsArticleObservation(
                journal_id=self.journal.id, article_id=article.id, audit_run_id=self.source.id,
                record_status="active", dublin_core_json={
                    "identifier": [f"https://public.example.test/article/{number:03d}"],
                },
            ))
        db.session.commit()
        pending = OjsHtmlAuditRun(
            journal_id=self.journal.id, source_oai_audit_run_id=self.source.id, status="pending",
        )
        db.session.add(pending)
        db.session.commit()

        eligible, candidates = list_html_audit_candidates(self.journal, pending)

        self.assertEqual(101, eligible)
        self.assertEqual(100, len(candidates))
        selected = {candidate.source_url for candidate in candidates}
        self.assertNotIn("https://doi.org/10.1000/example", selected)
        self.assertIn("https://public.example.test/article", selected)

    def test_progressive_coverage_advances_to_articles_not_processed_before(self):
        second_article, second_observation = self._add_article("2")
        self._persist_result(
            self.article,
            self.observation,
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"}, b"<html><title>OK</title></html>", 0, 2,
            ),
            final_status="partial",
        )
        pending = OjsHtmlAuditRun(
            journal_id=self.journal.id,
            source_oai_audit_run_id=self.source.id,
            status="pending",
            parameters_json={"selection_mode": "continue"},
        )
        db.session.add(pending)
        db.session.commit()

        eligible, candidates = list_html_audit_candidates(self.journal, pending)
        coverage = get_ojs_html_coverage(self.user, self.journal.id)

        self.assertEqual(2, eligible)
        self.assertEqual([second_article.id], [candidate.article.id for candidate in candidates])
        self.assertEqual(1, coverage.processed_article_count)
        self.assertEqual(1, coverage.verified_article_count)
        self.assertEqual(1, coverage.unprocessed_article_count)
        self.assertEqual(second_observation.id, candidates[0].observation.id)

    def test_retry_selection_is_limited_and_terminal_failures_are_not_repeated(self):
        timeout = HtmlFetchResult(
            "https://public.example.test/article", None, None, {}, None, 0, 1,
            "connect_timeout", "Tempo excedido",
        )
        self._persist_result(self.article, self.observation, timeout)
        retry = OjsHtmlAuditRun(
            journal_id=self.journal.id,
            source_oai_audit_run_id=self.source.id,
            status="pending",
            parameters_json={"selection_mode": HTML_SELECTION_RETRY_UNVERIFIED},
        )
        db.session.add(retry)
        db.session.commit()
        _eligible, candidates = list_html_audit_candidates(self.journal, retry)
        self.assertEqual([self.article.id], [candidate.article.id for candidate in candidates])

        db.session.delete(retry)
        db.session.commit()
        self._persist_result(self.article, self.observation, timeout)
        exhausted_retry = OjsHtmlAuditRun(
            journal_id=self.journal.id,
            source_oai_audit_run_id=self.source.id,
            status="pending",
            parameters_json={"selection_mode": HTML_SELECTION_RETRY_UNVERIFIED},
        )
        db.session.add(exhausted_retry)
        db.session.commit()
        _eligible, candidates = list_html_audit_candidates(self.journal, exhausted_retry)
        coverage = get_ojs_html_coverage(self.user, self.journal.id)
        self.assertEqual((), candidates)
        self.assertEqual(1, coverage.exhausted_unverified_count)

        db.session.delete(exhausted_retry)
        db.session.commit()
        terminal_article, terminal_observation = self._add_article("terminal")
        self._persist_result(
            terminal_article,
            terminal_observation,
            HtmlFetchResult(
                "https://public.example.test/article/terminal", None, None, {}, None, 0, 1,
                "tls_failed", "Falha TLS",
            ),
        )
        terminal_retry = OjsHtmlAuditRun(
            journal_id=self.journal.id,
            source_oai_audit_run_id=self.source.id,
            status="pending",
            parameters_json={"selection_mode": HTML_SELECTION_RETRY_UNVERIFIED},
        )
        db.session.add(terminal_retry)
        db.session.commit()
        _eligible, candidates = list_html_audit_candidates(self.journal, terminal_retry)
        self.assertNotIn(terminal_article.id, {candidate.article.id for candidate in candidates})

    def test_progressive_coverage_does_not_mix_a_different_oai_reference(self):
        self._persist_result(
            self.article,
            self.observation,
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"}, b"<html><title>OK</title></html>", 0, 2,
            ),
        )
        newer_source = OjsSeoAuditRun(journal_id=self.journal.id, status="completed", completed_at=utcnow())
        db.session.add(newer_source)
        db.session.flush()
        db.session.add(OjsArticleObservation(
            journal_id=self.journal.id,
            article_id=self.article.id,
            audit_run_id=newer_source.id,
            record_status="active",
            dublin_core_json={"title": ["Título OAI"], "identifier": ["https://public.example.test/article"]},
        ))
        db.session.commit()
        pending = OjsHtmlAuditRun(
            journal_id=self.journal.id,
            source_oai_audit_run_id=newer_source.id,
            status="pending",
            parameters_json={"selection_mode": "continue"},
        )
        db.session.add(pending)
        db.session.commit()

        _eligible, candidates = list_html_audit_candidates(self.journal, pending)
        old_coverage = get_ojs_html_coverage(
            self.user, self.journal.id, source_oai_audit_run_id=self.source.id,
        )
        new_coverage = get_ojs_html_coverage(
            self.user, self.journal.id, source_oai_audit_run_id=newer_source.id,
        )

        self.assertEqual([self.article.id], [candidate.article.id for candidate in candidates])
        self.assertEqual(1, old_coverage.verified_article_count)
        self.assertEqual(0, new_coverage.processed_article_count)

    def test_request_records_selection_mode_and_prevents_two_open_html_runs(self):
        requested = request_ojs_html_audit(self.user, self.journal.id)
        db.session.commit()
        self.assertEqual("continue", requested.parameters_json["selection_mode"])
        with self.assertRaises(IntegrityError):
            db.session.add(OjsHtmlAuditRun(
                journal_id=self.journal.id,
                source_oai_audit_run_id=self.source.id,
                status="pending",
            ))
            db.session.commit()
        db.session.rollback()

    def test_insufficient_global_budget_does_not_start_or_record_the_next_page(self):
        run = OjsHtmlAuditRun(journal_id=self.journal.id, source_oai_audit_run_id=self.source.id, status="pending")
        db.session.add(run)
        db.session.commit()

        class _NeverCalledClient:
            calls = 0

            def fetch(self, _url, *, deadline):
                self.calls += 1
                raise AssertionError(f"não deveria buscar uma página sem orçamento: {deadline}")

        client = _NeverCalledClient()
        with patch("platform_core.ojs_seo_html_executor.time.monotonic", side_effect=(0.0, 211.0)):
            processed = process_ojs_html_audit(run.id, client_factory=lambda: client, lease_seconds=300)

        self.assertEqual("partial", processed.status)
        self.assertEqual("execution_time_limit", processed.completion_reason)
        self.assertEqual(0, processed.analyzed_page_count)
        self.assertEqual(0, client.calls)
        self.assertFalse(_has_page_budget(240.0, now=240.0 - HTML_MINIMUM_PAGE_BUDGET_SECONDS + 0.1))

    def test_cross_worker_claims_are_serialized_by_journal(self):
        oai_run = OjsSeoAuditRun(journal_id=self.journal.id, status="pending")
        html_run = OjsHtmlAuditRun(
            journal_id=self.journal.id, source_oai_audit_run_id=self.source.id, status="pending",
        )
        db.session.add_all((oai_run, html_run))
        db.session.commit()

        oai_claim = claim_ojs_seo_audit(oai_run.id, lease_seconds=120)

        self.assertIsNotNone(oai_claim)
        self.assertIsNone(claim_ojs_html_audit(html_run.id, lease_seconds=120))
        self.assertEqual("pending", db.session.get(OjsHtmlAuditRun, html_run.id).status)

    def test_oai_and_html_workers_cannot_claim_the_same_journal_in_two_processes(self):
        oai_run = OjsSeoAuditRun(journal_id=self.journal.id, status="pending")
        html_run = OjsHtmlAuditRun(
            journal_id=self.journal.id, source_oai_audit_run_id=self.source.id, status="pending",
        )
        db.session.add_all((oai_run, html_run))
        db.session.commit()
        oai_run_id, html_run_id = oai_run.id, html_run.id
        db.session.remove()

        context = get_context("spawn")
        start, output = context.Event(), context.Queue()
        workers = (
            context.Process(target=_claim_ojs_run_in_process, args=(self.database_uri, self.scope.name, "oai", oai_run_id, start, output)),
            context.Process(target=_claim_ojs_run_in_process, args=(self.database_uri, self.scope.name, "html", html_run_id, start, output)),
        )
        for worker in workers:
            worker.start()
        start.set()
        responses = [output.get(timeout=25), output.get(timeout=25)]
        for worker in workers:
            worker.join(25)
            self.assertEqual(0, worker.exitcode)

        self.assertEqual(1, sum(result[1] is True for result in responses))
        self.assertNotIn("error", {result[1] for result in responses if len(result) > 1})

    def test_open_html_audit_blocks_archiving_without_changing_bibliographic_data(self):
        run = OjsHtmlAuditRun(
            journal_id=self.journal.id, source_oai_audit_run_id=self.source.id, status="pending",
        )
        db.session.add(run)
        db.session.commit()

        with self.assertRaises(OjsSeoProjectLifecycleError) as error:
            archive_ojs_seo_project(self.user, self.journal.id)

        self.assertEqual("ojs_project_audit_open", error.exception.code)
        self.assertEqual("active", db.session.get(AnalyticsProject, self.journal.analytics_project_id).status)
        self.assertIsNotNone(db.session.get(OjsArticle, self.article.id))

    def test_technical_listing_uses_scoped_database_filters(self):
        run = self._running()
        fetched = HtmlFetchResult(
            "https://public.example.test/article", "https://public.example.test/article", 200,
            {"content-type": "text/html"}, b"<html><title>HTML</title></html>", 0, 3,
        )
        result = analyze_html_page(
            fetched, oai_fields=self.observation.dublin_core_json,
            oai_doi=self.observation.doi_observed, repository_name="Revista",
        )
        persist_html_audit_results(
            self.journal, run,
            ((HtmlAuditCandidate(self.article, self.observation, fetched.source_url), result),),
            eligible_article_count=1, final_status="completed",
        )
        db.session.commit()

        matching = list_ojs_html_analysis_pages(
            self.user, self.journal.id, run.id, selected_filter="confirmed", search="oai:html:1",
        )
        empty = list_ojs_html_analysis_pages(
            self.user, self.journal.id, run.id, selected_filter="unverified", search="sem-correspondencia",
        )

        self.assertEqual(1, matching.pages.total)
        self.assertEqual(0, empty.pages.total)

    def test_recommendations_deduplicate_articles_and_contextualize_historical_comparisons(self):
        """A consolidação é somente leitura e não deixa um retry falho apagar HTML útil."""
        second_article, second_observation = self._add_article("recommendation-2")
        now = utcnow()
        successful = OjsHtmlAuditRun(
            journal_id=self.journal.id, source_oai_audit_run_id=self.source.id,
            status="completed", completed_at=now - timedelta(minutes=3),
        )
        historical = OjsHtmlAuditRun(
            journal_id=self.journal.id, source_oai_audit_run_id=self.source.id,
            status="completed", completed_at=now - timedelta(minutes=2),
        )
        failed_retry = OjsHtmlAuditRun(
            journal_id=self.journal.id, source_oai_audit_run_id=self.source.id,
            status="partial", completed_at=now - timedelta(minutes=1),
        )
        db.session.add_all((successful, historical, failed_retry))
        db.session.flush()
        db.session.add_all((
            OjsArticleHtmlAnalysis(
                journal_id=self.journal.id, article_id=self.article.id,
                source_observation_id=self.observation.id, html_audit_run_id=successful.id,
                status="analyzed", source_url="https://public.example.test/article",
                final_url="https://public.example.test/article", http_status=200,
                checked_at=now - timedelta(minutes=3),
                facts_json={"oai_html_consistency": {"criteria_version": "ojs-html-comparison-v3"}},
                findings_json=[
                    {"code": "canonical_missing", "classification": "confirmed", "severity": "medium", "applicability": "applicable", "evidence": "Canonical ausente.", "recommendation": "Configure canonical.", "impact": "A ausência foi confirmada.", "audience": "Administração técnica do OJS."},
                    {"code": "canonical_missing", "classification": "confirmed", "severity": "medium", "applicability": "applicable", "evidence": "Canonical ausente repetido.", "recommendation": "Configure canonical.", "impact": "A ausência foi confirmada.", "audience": "Administração técnica do OJS."},
                ],
            ),
            OjsArticleHtmlAnalysis(
                journal_id=self.journal.id, article_id=second_article.id,
                source_observation_id=second_observation.id, html_audit_run_id=historical.id,
                status="analyzed", source_url="https://public.example.test/article/recommendation-2",
                final_url="https://public.example.test/article/recommendation-2", http_status=200,
                checked_at=now - timedelta(minutes=2),
                facts_json={"oai_html_consistency": {"criteria_version": "ojs-html-comparison-v2"}},
                findings_json=[
                    {"code": "authors_oai_html_divergence", "classification": "potential", "severity": "medium", "applicability": "context_dependent", "evidence": "Autoria em formato histórico.", "recommendation": "Confira as autorias.", "impact": "A representação pode variar.", "audience": "Equipe editorial e administração técnica do OJS."},
                ],
            ),
            # A tentativa posterior não verificada deve continuar registrada,
            # mas não pode substituir a análise HTML concluída do primeiro artigo.
            OjsArticleHtmlAnalysis(
                journal_id=self.journal.id, article_id=self.article.id,
                source_observation_id=self.observation.id, html_audit_run_id=failed_retry.id,
                status="not_verified", source_url="https://public.example.test/article",
                checked_at=now - timedelta(minutes=1), facts_json={},
                findings_json=[{"code": "connect_timeout", "classification": "unverified"}],
                unverified_count=1,
            ),
        ))
        db.session.commit()

        listing = list_ojs_recommendations(self.user, self.journal.id, audit_run_id=self.source.id)
        canonical = next(item for item in listing.recommendations.items if item.code == "canonical_missing")
        historical_authors = next(
            item for item in listing.recommendations.items if item.code == "authors_oai_html_divergence"
        )
        self.assertEqual((1, 2), (canonical.article_count, canonical.occurrence_count))
        self.assertEqual("ojs-html-comparison-v3", canonical.articles[0].criteria_version)
        self.assertTrue(historical_authors.historical_criteria)
        self.assertEqual(("potential", "review"), (historical_authors.certainty, historical_authors.priority))
        self.assertGreaterEqual(listing.indicators.confirmed_articles, 1)
        self.assertGreaterEqual(listing.indicators.potential_articles, 1)

        detail = get_ojs_recommendation_detail(
            self.user, self.journal.id, self.source.id, "canonical_missing",
        )
        self.assertEqual(1, detail.articles.total)
        self.assertEqual(self.article.id, detail.articles.items[0].article_id)
        review_listing = list_ojs_recommendations(
            self.user, self.journal.id, audit_run_id=self.source.id, priority="review",
        )
        self.assertIn(
            "authors_oai_html_divergence",
            {item.code for item in review_listing.recommendations.items},
        )

    def test_subject_missing_uses_explicit_document_type_for_editorial_applicability(self):
        """dc:type, e não o título, controla a prioridade editorial de assuntos."""
        self.article.title = "Apresentação de dossiê"
        self.observation.dublin_core_json = {
            "title": [self.article.title],
            "identifier": ["https://public.example.test/article"],
            "subject": [],
            "type": ["Article"],
        }
        editorial, editorial_observation = self._add_article("subject-editorial")
        editorial.title = "Artigo científico anunciado"
        editorial_observation.dublin_core_json = {
            "title": [editorial.title],
            "identifier": ["https://public.example.test/article/subject-editorial"],
            "subject": [],
            "type": ["Editorial"],
        }
        generic, generic_observation = self._add_article("subject-generic")
        generic_observation.dublin_core_json = {
            "title": [generic.title],
            "identifier": ["https://public.example.test/article/subject-generic"],
            "subject": [],
            "type": ["Text"],
        }
        missing_type, missing_type_observation = self._add_article("subject-no-type")
        missing_type_observation.dublin_core_json = {
            "title": [missing_type.title],
            "identifier": ["https://public.example.test/article/subject-no-type"],
            "subject": [],
            "type": [],
        }
        db.session.commit()

        listing = list_ojs_recommendations(self.user, self.journal.id, audit_run_id=self.source.id)
        article_subject = next(item for item in listing.recommendations.items if item.code == "oai_subject_missing")
        contextual_subject = next(
            item for item in listing.recommendations.items if item.code == "oai_subject_missing_needs_review"
        )
        missing_type_recommendation = next(item for item in listing.recommendations.items if item.code == "oai_type_missing")

        self.assertEqual((1, "applicable", "medium", "medium"), (
            article_subject.article_count, article_subject.applicability,
            article_subject.severity, article_subject.priority,
        ))
        self.assertEqual((3, "context_dependent", "low", "review"), (
            contextual_subject.article_count, contextual_subject.applicability,
            contextual_subject.severity, contextual_subject.priority,
        ))
        self.assertEqual(("context_dependent", "low", "review"), (
            missing_type_recommendation.applicability,
            missing_type_recommendation.severity,
            missing_type_recommendation.priority,
        ))
        self.assertIn("recuperação temática", article_subject.impact)
        self.assertIn("não comprova", article_subject.impact)
        self.assertIn("dc:type informado foi Editorial", contextual_subject.articles[0].evidence)
        self.assertIn("dc:type está ausente", contextual_subject.articles[-1].evidence)
        self.assertEqual(self.article.id, article_subject.articles[0].article_id)

    def test_full_campaign_worker_chains_batches_until_the_queue_is_idle(self):
        """Uma solicitação cria uma campanha; o worker encadeia os lotes locais."""
        second_article, _second_observation = self._add_article("campaign-2")
        campaign = request_ojs_html_audit_campaign(self.user, self.journal.id)
        db.session.commit()
        campaign_id, source_id = campaign.id, campaign.source_oai_audit_run_id

        class _LocalClient:
            def fetch(self, url, *, deadline):
                return HtmlFetchResult(
                    url, url, 200, {"content-type": "text/html"},
                    b"<html><head><title>Registro</title><link rel='canonical' href='https://public.example.test/article'></head><body><h1>Registro</h1></body></html>",
                    0, 1,
                )

        # Um limite unitário deixa o encadeamento observável sem alterar os
        # limites reais do produto: cada lote continua usando a mesma seleção
        # determinística e a campanha deve gerar exatamente dois lotes.
        with patch("platform_core.ojs_seo_technical.HTML_AUDIT_MAX_PAGES", 1):
            run_ojs_html_worker(
                self.app, once=False, until_idle=True, max_jobs=1,
                poll_interval=0.01, lease_seconds=120, client_factory=lambda: _LocalClient(),
            )

        # O worker abre um app-context próprio; recarregamos a sessão do teste
        # para observar a confirmação feita por esse processo lógico separado.
        db.session.expire_all()
        completed_campaign = db.session.get(OjsHtmlAuditCampaign, campaign_id)
        runs = list(db.session.scalars(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.campaign_id == campaign_id,
        ).order_by(OjsHtmlAuditRun.created_at, OjsHtmlAuditRun.id)))
        coverage = get_ojs_html_coverage(self.user, self.journal.id, source_oai_audit_run_id=source_id)
        self.assertEqual(
            "completed", completed_campaign.status,
            [(run.status, run.completion_reason, run.analyzed_page_count) for run in runs],
        )
        self.assertEqual(2, completed_campaign.batch_count)
        self.assertEqual(["completed", "completed"], [run.status for run in runs])
        self.assertEqual(2, coverage.verified_article_count)
        self.assertEqual(0, coverage.unprocessed_article_count)
        self.assertEqual(2, db.session.scalar(select(func.count()).select_from(OjsArticleHtmlAnalysis).where(
            OjsArticleHtmlAnalysis.journal_id == self.journal.id,
            OjsArticleHtmlAnalysis.article_id.in_((self.article.id, second_article.id)),
        )))

    def test_full_campaign_retries_only_eligible_failures_then_finishes_with_pending(self):
        campaign = request_ojs_html_audit_campaign(self.user, self.journal.id)
        db.session.commit()

        class _TimeoutClient:
            def fetch(self, url, *, deadline):
                return HtmlFetchResult(
                    url, None, None, {}, None, 0, 1,
                    "connect_timeout", "A conexão com a página HTML excedeu o prazo permitido.",
                )

        first = db.session.scalar(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.campaign_id == campaign.id, OjsHtmlAuditRun.status == "pending",
        ))
        process_ojs_html_audit(first.id, client_factory=lambda: _TimeoutClient(), lease_seconds=120)
        second = db.session.scalar(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.campaign_id == campaign.id, OjsHtmlAuditRun.status == "pending",
        ))
        self.assertIsNotNone(second)
        self.assertEqual(HTML_SELECTION_RETRY_UNVERIFIED, second.parameters_json["selection_mode"])
        process_ojs_html_audit(second.id, client_factory=lambda: _TimeoutClient(), lease_seconds=120)

        finished = db.session.get(OjsHtmlAuditCampaign, campaign.id)
        coverage = get_ojs_html_coverage(self.user, self.journal.id, source_oai_audit_run_id=self.source.id)
        self.assertEqual("completed_with_pending", finished.status)
        self.assertEqual("no_more_eligible_attempts", finished.completion_reason)
        self.assertEqual(2, finished.batch_count)
        self.assertEqual(0, coverage.verified_article_count)
        self.assertEqual(1, coverage.exhausted_unverified_count)
        self.assertIsNone(db.session.scalar(select(OjsHtmlAuditRun.id).where(
            OjsHtmlAuditRun.campaign_id == campaign.id, OjsHtmlAuditRun.status.in_(("pending", "running")),
        )))

    def test_full_campaign_stops_when_its_bibliographic_reference_changes(self):
        campaign = request_ojs_html_audit_campaign(self.user, self.journal.id)
        db.session.commit()
        new_reference = OjsSeoAuditRun(
            journal_id=self.journal.id, status="completed",
            completed_at=utcnow() + timedelta(seconds=1),
        )
        db.session.add(new_reference)
        db.session.commit()

        class _LocalClient:
            def fetch(self, url, *, deadline):
                return HtmlFetchResult(
                    url, url, 200, {"content-type": "text/html"},
                    b"<html><head><title>Registro</title></head><body><h1>Registro</h1></body></html>", 0, 1,
                )

        first = db.session.scalar(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.campaign_id == campaign.id, OjsHtmlAuditRun.status == "pending",
        ))
        process_ojs_html_audit(first.id, client_factory=lambda: _LocalClient(), lease_seconds=120)

        interrupted = db.session.get(OjsHtmlAuditCampaign, campaign.id)
        self.assertEqual("interrupted", interrupted.status)
        self.assertEqual("source_reference_changed", interrupted.completion_reason)
        self.assertEqual(campaign.source_oai_audit_run_id, self.source.id)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(OjsArticleHtmlAnalysis).where(
            OjsArticleHtmlAnalysis.html_audit_run_id == first.id,
        )))
        self.assertIsNone(db.session.scalar(select(OjsHtmlAuditRun.id).where(
            OjsHtmlAuditRun.campaign_id == campaign.id, OjsHtmlAuditRun.status == "pending",
        )))

    def test_full_campaign_does_not_create_a_new_batch_without_page_progress(self):
        campaign = request_ojs_html_audit_campaign(self.user, self.journal.id)
        db.session.commit()
        first = db.session.scalar(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.campaign_id == campaign.id, OjsHtmlAuditRun.status == "pending",
        ))

        class _NeverCalledClient:
            def fetch(self, _url, *, deadline):
                raise AssertionError("A página não deve ser iniciada sem orçamento temporal.")

        with patch("platform_core.ojs_seo_html_executor.time.monotonic", side_effect=(0.0, 211.0)):
            process_ojs_html_audit(first.id, client_factory=lambda: _NeverCalledClient(), lease_seconds=300)

        interrupted = db.session.get(OjsHtmlAuditCampaign, campaign.id)
        self.assertEqual("interrupted", interrupted.status)
        self.assertEqual("no_progress_batch", interrupted.completion_reason)
        self.assertEqual(1, interrupted.no_progress_batch_count)
        self.assertIsNone(db.session.scalar(select(OjsHtmlAuditRun.id).where(
            OjsHtmlAuditRun.campaign_id == campaign.id, OjsHtmlAuditRun.status == "pending",
        )))

    def test_full_campaign_duplicate_request_is_rejected_without_a_second_pending_batch(self):
        campaign = request_ojs_html_audit_campaign(self.user, self.journal.id)
        db.session.commit()

        with self.assertRaises(OjsSeoServiceError) as error:
            request_ojs_html_audit_campaign(self.user, self.journal.id)

        self.assertEqual("ojs_html_campaign_open", error.exception.code)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(OjsHtmlAuditCampaign).where(
            OjsHtmlAuditCampaign.journal_id == self.journal.id,
        )))
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.campaign_id == campaign.id, OjsHtmlAuditRun.status == "pending",
        )))

    def test_expired_campaign_batch_is_interrupted_without_losing_prior_history(self):
        campaign = request_ojs_html_audit_campaign(self.user, self.journal.id)
        db.session.commit()
        run = db.session.scalar(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.campaign_id == campaign.id, OjsHtmlAuditRun.status == "pending",
        ))
        expired_at = utcnow() - timedelta(minutes=6)
        run.status = "running"
        run.started_at = expired_at
        run.claim_token = "b" * 36
        run.claimed_at = expired_at
        run.lease_expires_at = expired_at
        campaign.status = "running"
        campaign.started_at = expired_at
        db.session.commit()

        self.assertEqual(1, recover_expired_ojs_html_audits(now=utcnow()))
        recovered_run = db.session.get(OjsHtmlAuditRun, run.id)
        recovered_campaign = db.session.get(OjsHtmlAuditCampaign, campaign.id)
        self.assertEqual(("failed", "worker_lease_expired", None), (
            recovered_run.status, recovered_run.completion_reason, recovered_run.claim_token,
        ))
        self.assertEqual(("interrupted", "worker_lease_expired"), (
            recovered_campaign.status, recovered_campaign.completion_reason,
        ))
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsArticleHtmlAnalysis)))

    def test_html_worker_cli_preserves_once_and_exposes_until_idle_campaign_mode(self):
        runner = self.app.test_cli_runner()
        with patch("platform_core.ojs_seo_html_executor.run_ojs_html_worker") as worker:
            once = runner.invoke(args=["ojs-seo-html-worker", "--once"])
            self.assertEqual(0, once.exit_code, once.output)
            self.assertTrue(worker.call_args.kwargs["once"])
            self.assertFalse(worker.call_args.kwargs["until_idle"])
            self.assertIsNone(worker.call_args.kwargs["max_jobs"])

            worker.reset_mock()
            campaign = runner.invoke(args=["ojs-seo-html-worker", "--until-idle"])
            self.assertEqual(0, campaign.exit_code, campaign.output)
            self.assertFalse(worker.call_args.kwargs["once"])
            self.assertTrue(worker.call_args.kwargs["until_idle"])

            invalid = runner.invoke(args=["ojs-seo-html-worker", "--once", "--until-idle"])
            self.assertNotEqual(0, invalid.exit_code)
            self.assertIn("não pode ser combinado", invalid.output)

    def test_html_worker_stops_before_claiming_when_shutdown_is_requested(self):
        stopping = Event()
        stopping.set()
        calls = []

        def factory(_endpoint):
            calls.append(_endpoint)
            raise AssertionError("A parada solicitada não pode abrir um cliente HTML")

        run_ojs_html_worker(
            self.app,
            once=False,
            poll_interval=0.01,
            lease_seconds=300,
            client_factory=factory,
            stop_event=stopping,
        )

        self.assertEqual([], calls)

    def test_integrated_operation_enqueues_html_only_after_usable_oai_reference(self):
        operation = request_ojs_integrated_analysis(self.user, self.journal.id)
        db.session.flush()
        self.assertEqual("update", operation.mode)
        self.assertEqual("oai_pending", operation.status)
        run = db.session.get(OjsSeoAuditRun, operation.oai_audit_run_id)
        self.assertEqual("pending", run.status)
        self.assertEqual(operation.id, run.parameters_json["integrated_operation_id"])

        run.status = "completed"
        run.completed_at = utcnow()
        copied = OjsArticleObservation(
            journal_id=self.journal.id, article_id=self.article.id, audit_run_id=run.id,
            record_status="active", dublin_core_json=dict(self.observation.dublin_core_json),
            metadata_hash=self.observation.metadata_hash,
        )
        db.session.add(copied)
        db.session.flush()
        advanced = advance_integrated_after_oai(self.journal, run)
        self.assertEqual("html_pending", advanced.status)
        self.assertIsNotNone(advanced.html_campaign_id)
        campaign = db.session.get(OjsHtmlAuditCampaign, advanced.html_campaign_id)
        # A referência atual é idêntica à anterior. A continuidade deve usar
        # a cobertura da referência anterior, explicitamente vinculada à nova
        # coleta, em vez de repetir páginas já verificadas.
        self.assertEqual(self.source.id, campaign.source_oai_audit_run_id)
        self.assertEqual(run.id, campaign.parameters_json["compatible_with_oai_audit_run_id"])
        self.assertEqual("integrated_unchanged_continuation", campaign.parameters_json["requested_by"])
        self.assertEqual(self.source.id, advanced.parameters_json["html_continuation_reference_oai_audit_run_id"])
        self.assertEqual(1, campaign.batch_count)

    def test_integrated_update_without_bibliographic_changes_reuses_only_the_labeled_prior_coverage(self):
        """A referência anterior é exibida, nunca apresentada como nova verificação."""
        fetched = HtmlFetchResult(
            "https://public.example.test/article", "https://public.example.test/article", 200,
            {"content-type": "text/html"},
            b"<title>HTML</title><h1>HTML</h1><link rel='canonical' href='https://public.example.test/article'>",
            0, 1,
        )
        self._persist_result(self.article, self.observation, fetched)
        operation = request_ojs_integrated_analysis(self.user, self.journal.id)
        current = db.session.get(OjsSeoAuditRun, operation.oai_audit_run_id)
        current.status, current.completed_at = "completed", utcnow()
        db.session.add(OjsArticleObservation(
            journal_id=self.journal.id, article_id=self.article.id, audit_run_id=current.id,
            record_status="active", dublin_core_json=dict(self.observation.dublin_core_json),
            metadata_hash=self.observation.metadata_hash,
        ))
        db.session.flush()

        advanced = advance_integrated_after_oai(self.journal, current)

        self.assertEqual(("completed", "no_bibliographic_changes", None), (
            advanced.status, advanced.completion_reason, advanced.html_campaign_id,
        ))
        self.assertEqual(self.source.id, advanced.parameters_json["html_coverage_reference_oai_audit_run_id"])
        self.assertEqual("not_requested", advanced.parameters_json["html_revalidation"])
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsHtmlEvidenceReuse)))

    def test_integrated_update_reuses_only_unchanged_evidence_and_selects_a_new_article(self):
        """Uma referência nova não força HTTP para o artigo estável já verificado."""
        self._persist_result(
            self.article, self.observation,
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"},
                b"<title>HTML</title><h1>HTML</h1><link rel='canonical' href='https://public.example.test/article'>",
                0, 1,
            ),
        )
        operation, current, advanced, additions = self._completed_updated_reference(
            ((self.article, self.observation, {}),),
            additions=(("2", "b" * 64, "https://public.example.test/article/new-2"),),
        )

        self.assertEqual("html_pending", advanced.status)
        reuse = db.session.scalar(select(OjsHtmlEvidenceReuse))
        self.assertEqual((self.source.id, current.id, self.article.id), (
            reuse.source_oai_audit_run_id, reuse.target_oai_audit_run_id, reuse.article_id,
        ))
        self.assertEqual("stable_article_metadata_and_url_match", reuse.reuse_reason)
        campaign = db.session.get(OjsHtmlAuditCampaign, advanced.html_campaign_id)
        batch = db.session.scalar(select(OjsHtmlAuditRun).where(OjsHtmlAuditRun.campaign_id == campaign.id))
        eligible, candidates = list_html_audit_candidates(self.journal, batch)
        coverage = get_ojs_html_coverage(self.user, self.journal.id, source_oai_audit_run_id=current.id)
        self.assertEqual((2, 1, 1, 0, 1), (
            eligible, len(candidates), coverage.reused_evidence_article_count,
            coverage.verified_article_count, coverage.unprocessed_article_count,
        ))
        self.assertEqual(additions[0][0].id, candidates[0].article.id)
        self.assertEqual(1, operation.parameters_json["html_evidence_reuse"]["created_count"])
        recommendations = list_ojs_recommendations(
            self.user, self.journal.id, audit_run_id=current.id,
        )
        reused_articles = [
            article
            for recommendation in recommendations.recommendations.items
            for article in recommendation.articles
            if article.article_id == self.article.id and article.analysis_id is not None
        ]
        self.assertTrue(reused_articles)
        self.assertTrue(all(article.evidence_reused for article in reused_articles))
        self.assertTrue(all("Evidência HTML reaproveitada" in article.evidence for article in reused_articles))

    def test_integrated_update_rechecks_a_record_with_a_changed_metadata_hash(self):
        """Hash alterado invalida reaproveitamento, sem atingir outros artigos."""
        self._persist_result(
            self.article, self.observation,
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"}, b"<title>HTML</title><h1>HTML</h1>", 0, 1,
            ),
        )
        _operation, current, advanced, _additions = self._completed_updated_reference(
            ((self.article, self.observation, {
                "metadata_hash": "changed" * 9 + "!",
                "dublin_core_json": {
                    "title": ["Título alterado"],
                    "identifier": ["https://public.example.test/article"],
                },
            }),),
        )

        self.assertEqual("html_pending", advanced.status)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsHtmlEvidenceReuse)))
        batch = db.session.scalar(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.campaign_id == advanced.html_campaign_id,
        ))
        _eligible, candidates = list_html_audit_candidates(self.journal, batch)
        self.assertEqual(["https://public.example.test/article"], [item.source_url for item in candidates])
        coverage = get_ojs_html_coverage(self.user, self.journal.id, source_oai_audit_run_id=current.id)
        self.assertEqual((0, 1), (coverage.reused_evidence_article_count, coverage.unprocessed_article_count))

    def test_integrated_update_rechecks_a_record_with_a_changed_public_url(self):
        self._persist_result(
            self.article, self.observation,
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"}, b"<title>HTML</title><h1>HTML</h1>", 0, 1,
            ),
        )
        # A reconciliação normal calcula metadata_hash a partir do registro,
        # portanto uma URL OAI alterada também altera o hash. Esta prova isola
        # a segunda salvaguarda: mesmo que um produtor entregue hash igual por
        # engano, a URL pública normalizada impede o reaproveitamento.
        current = OjsSeoAuditRun(journal_id=self.journal.id, status="completed", completed_at=utcnow())
        db.session.add(current)
        db.session.flush()
        db.session.add(OjsArticleObservation(
            journal_id=self.journal.id, article_id=self.article.id, audit_run_id=current.id,
            record_status="active",
            dublin_core_json={
                "title": ["Título OAI"], "creator": ["Ana"],
                "identifier": ["https://public.example.test/article/changed-url"],
            },
            metadata_hash=self.observation.metadata_hash,
        ))
        db.session.flush()

        self.assertEqual(0, reuse_compatible_html_evidence(
            self.journal, source_oai_audit_run_id=self.source.id, target_oai_audit_run_id=current.id,
        ))
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsHtmlEvidenceReuse)))
        coverage = get_ojs_html_coverage(self.user, self.journal.id, source_oai_audit_run_id=current.id)
        self.assertEqual((0, 1), (coverage.reused_evidence_article_count, coverage.unprocessed_article_count))

    def test_integrated_update_does_not_reuse_a_record_deleted_by_the_source(self):
        self._persist_result(
            self.article, self.observation,
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"}, b"<title>HTML</title><h1>HTML</h1>", 0, 1,
            ),
        )
        _operation, current, advanced, _additions = self._completed_updated_reference(
            ((self.article, self.observation, {"record_status": "deleted"}),),
        )

        self.assertEqual(("completed", "no_html_revalidation_needed", None), (
            advanced.status, advanced.completion_reason, advanced.html_campaign_id,
        ))
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsHtmlEvidenceReuse)))
        coverage = get_ojs_html_coverage(self.user, self.journal.id, source_oai_audit_run_id=current.id)
        self.assertEqual((0, 0, 0), (
            coverage.eligible_article_count, coverage.reused_evidence_article_count,
            coverage.unprocessed_article_count,
        ))

    def test_unchanged_partial_coverage_continues_only_old_unprocessed_articles(self):
        second, second_observation = self._add_article("continuation")
        self._persist_result(
            self.article, self.observation,
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"}, b"<title>HTML</title><h1>HTML</h1>", 0, 1,
            ),
        )
        _operation, current, advanced, _additions = self._completed_updated_reference(
            ((self.article, self.observation, {}), (second, second_observation, {})),
        )

        campaign = db.session.get(OjsHtmlAuditCampaign, advanced.html_campaign_id)
        self.assertEqual(("html_pending", self.source.id, current.id), (
            advanced.status, campaign.source_oai_audit_run_id,
            campaign.parameters_json["compatible_with_oai_audit_run_id"],
        ))
        batch = db.session.scalar(select(OjsHtmlAuditRun).where(OjsHtmlAuditRun.campaign_id == campaign.id))
        _eligible, candidates = list_html_audit_candidates(self.journal, batch)
        self.assertEqual([second.id], [item.article.id for item in candidates])

    def test_unchanged_retryable_failure_continues_only_the_eligible_retry(self):
        timeout = HtmlFetchResult(
            "https://public.example.test/article", None, None, {}, None, 0, 1,
            "connect_timeout", "A conexão com a página HTML excedeu o prazo permitido.",
        )
        self._persist_result(self.article, self.observation, timeout, final_status="partial")
        _operation, _current, advanced, _additions = self._completed_updated_reference(
            ((self.article, self.observation, {}),),
        )
        campaign = db.session.get(OjsHtmlAuditCampaign, advanced.html_campaign_id)
        retry = db.session.scalar(select(OjsHtmlAuditRun).where(OjsHtmlAuditRun.campaign_id == campaign.id))
        self.assertEqual(HTML_SELECTION_RETRY_UNVERIFIED, retry.parameters_json["selection_mode"])
        _eligible, candidates = list_html_audit_candidates(self.journal, retry)
        self.assertEqual([self.article.id], [item.article.id for item in candidates])

    def test_unchanged_exhausted_failures_finish_with_pending_without_a_new_campaign(self):
        timeout = HtmlFetchResult(
            "https://public.example.test/article", None, None, {}, None, 0, 1,
            "connect_timeout", "A conexão com a página HTML excedeu o prazo permitido.",
        )
        self._persist_result(self.article, self.observation, timeout, final_status="partial")
        self._persist_result(self.article, self.observation, timeout, final_status="partial")
        _operation, _current, exhausted, _additions = self._completed_updated_reference(
            ((self.article, self.observation, {}),),
        )
        self.assertEqual(("completed_with_pending", "no_more_eligible_html_attempts", None), (
            exhausted.status, exhausted.completion_reason, exhausted.html_campaign_id,
        ))

    def test_explicit_revalidation_selects_previously_reused_evidence_without_copying_it(self):
        self._persist_result(
            self.article, self.observation,
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"}, b"<title>HTML</title><h1>HTML</h1>", 0, 1,
            ),
        )
        current = OjsSeoAuditRun(journal_id=self.journal.id, status="completed", completed_at=utcnow())
        db.session.add(current)
        db.session.flush()
        current_observation = OjsArticleObservation(
            journal_id=self.journal.id, article_id=self.article.id, audit_run_id=current.id,
            record_status="active", dublin_core_json=dict(self.observation.dublin_core_json),
            metadata_hash=self.observation.metadata_hash,
        )
        db.session.add(current_observation)
        db.session.flush()
        self.assertEqual(1, reuse_compatible_html_evidence(
            self.journal, source_oai_audit_run_id=self.source.id, target_oai_audit_run_id=current.id,
        ))
        reused_coverage = get_ojs_html_coverage(
            self.user, self.journal.id, source_oai_audit_run_id=current.id,
        )
        self.assertEqual((0, 1), (
            reused_coverage.verified_article_count, reused_coverage.reused_evidence_article_count,
        ))

        revalidation = request_ojs_html_audit(
            self.user, self.journal.id, selection_mode=HTML_SELECTION_REVALIDATE,
        )
        _eligible, candidates = list_html_audit_candidates(self.journal, revalidation)
        self.assertEqual([self.article.id], [item.article.id for item in candidates])
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(OjsHtmlEvidenceReuse)))

    def test_explicit_revalidation_can_request_a_fresh_check_of_direct_evidence(self):
        """A revalidação deliberada não depende de alterar o OAI-PMH."""
        self._persist_result(
            self.article, self.observation,
            HtmlFetchResult(
                "https://public.example.test/article", "https://public.example.test/article", 200,
                {"content-type": "text/html"}, b"<title>HTML</title><h1>HTML</h1>", 0, 1,
            ),
        )

        revalidation = request_ojs_html_audit(
            self.user, self.journal.id, selection_mode=HTML_SELECTION_REVALIDATE,
        )
        _eligible, candidates = list_html_audit_candidates(self.journal, revalidation)

        self.assertEqual(HTML_SELECTION_REVALIDATE, revalidation.parameters_json["selection_mode"])
        self.assertTrue(revalidation.parameters_json["force_revalidation"])
        self.assertEqual([self.article.id], [item.article.id for item in candidates])

    def test_integrated_operation_keeps_pending_disclosure_when_its_source_is_partial(self):
        operation = request_ojs_integrated_analysis(self.user, self.journal.id)
        current = db.session.get(OjsSeoAuditRun, operation.oai_audit_run_id)
        current.status, current.completed_at = "partial", utcnow()
        db.session.add(OjsArticleObservation(
            journal_id=self.journal.id, article_id=self.article.id, audit_run_id=current.id,
            record_status="active", dublin_core_json=dict(self.observation.dublin_core_json),
            metadata_hash=self.observation.metadata_hash,
        ))
        db.session.flush()
        advanced = advance_integrated_after_oai(self.journal, current)
        campaign = db.session.get(OjsHtmlAuditCampaign, advanced.html_campaign_id)
        campaign.status, campaign.completion_reason = "completed", "all_eligible_pages_verified"

        completed = advance_integrated_after_html_campaign(self.journal, campaign)

        self.assertEqual(("completed_with_pending", "all_eligible_pages_verified"), (
            completed.status, completed.completion_reason,
        ))

    def test_migration_head_is_single_and_contains_html_tables(self):
        config = Config(); config.set_main_option("script_location", MIGRATIONS)
        self.assertEqual(["91a2b3c4d5e6"], ScriptDirectory.from_config(config).get_heads())
        tables = set(inspect(db.engine).get_table_names())
        self.assertTrue({
            "ojs_html_audit_runs", "ojs_article_html_analyses", "ojs_html_audit_campaigns",
            "ojs_html_evidence_reuses",
        }.issubset(tables))

    def test_html_migration_upgrade_and_downgrade_only_add_technical_tables(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-html-migration-", dir=TEST_TEMP_ROOT) as directory:
            database = Path(directory) / "migration.sqlite3"
            app = create_app({
                "TESTING": True,
                "SECRET_KEY": "ojs-html-migration",
                "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-html-test-secret-with-adequate-length-123456",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": directory,
            })
            with app.app_context():
                upgrade(directory=MIGRATIONS, revision="4e5f60718293")
                before = set(inspect(db.engine).get_table_names())
                try:
                    upgrade(directory=MIGRATIONS, revision="6f708192a4b5")
                    after = set(inspect(db.engine).get_table_names())
                    self.assertEqual(before | {"ojs_html_audit_runs", "ojs_article_html_analyses"}, after)
                    self.assertIn(
                        "uq_ojs_html_audit_one_open_journal",
                        {index["name"] for index in inspect(db.engine).get_indexes("ojs_html_audit_runs")},
                    )

                    # O banco local já contém análises que referenciam a
                    # tabela de execuções. O batch SQLite precisa preservar
                    # uma linha real desse relacionamento, não apenas o
                    # esquema vazio usado pelas migrations anteriores.
                    user = User(name="Migration HTML", email="migration-html@example.test", role="user", status="active")
                    user.set_password("senha-de-teste-segura-123")
                    project = AnalyticsProject(owner=user, module_key="ojs_seo", name="Migração HTML", description="")
                    journal = OjsJournal(
                        analytics_project=project,
                        public_url="https://migration.example.test/journal",
                        discovery_status="identified",
                    )
                    db.session.add_all((user, project, journal))
                    db.session.flush()
                    source = OjsSeoAuditRun(journal_id=journal.id, status="completed", completed_at=utcnow())
                    db.session.add(source)
                    db.session.flush()
                    article = OjsArticle(
                        journal_id=journal.id,
                        oai_identifier="oai:migration:html:1",
                        record_status="active",
                    )
                    db.session.add(article)
                    db.session.flush()
                    observation = OjsArticleObservation(
                        journal_id=journal.id,
                        article_id=article.id,
                        audit_run_id=source.id,
                        record_status="active",
                        dublin_core_json={},
                    )
                    db.session.add(observation)
                    db.session.commit()

                    now = utcnow().isoformat()
                    run_id = str(uuid4())
                    analysis_id = str(uuid4())
                    with db.engine.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO ojs_html_audit_runs ("
                                "id, journal_id, source_oai_audit_run_id, status, created_at, processor_version, "
                                "parameters_json, metrics_json, warnings_json, errors_json, eligible_article_count, "
                                "analyzed_page_count, confirmed_problem_count, potential_problem_count, "
                                "unverified_page_count, attempt_count) VALUES ("
                                ":id, :journal_id, :source_id, 'completed', :created_at, 'test', "
                                ":parameters, :metrics, :warnings, :errors, 1, 1, 0, 0, 0, 0)"
                            ),
                            {
                                "id": run_id, "journal_id": journal.id, "source_id": source.id,
                                "created_at": now, "parameters": json.dumps({}), "metrics": json.dumps({}),
                                "warnings": json.dumps([]), "errors": json.dumps([]),
                            },
                        )
                        connection.execute(
                            text(
                                "INSERT INTO ojs_article_html_analyses ("
                                "id, journal_id, article_id, source_observation_id, html_audit_run_id, status, "
                                "source_url, redirect_count, facts_json, findings_json, confirmed_problem_count, "
                                "potential_problem_count, unverified_count, checked_at, created_at) VALUES ("
                                ":id, :journal_id, :article_id, :observation_id, :run_id, 'analyzed', "
                                "'https://migration.example.test/article/1', 0, :facts, :findings, 0, 0, 0, :checked_at, :created_at)"
                            ),
                            {
                                "id": analysis_id, "journal_id": journal.id, "article_id": article.id,
                                "observation_id": observation.id, "run_id": run_id, "facts": json.dumps({}),
                                "findings": json.dumps([]), "checked_at": now, "created_at": now,
                            },
                        )
                    upgrade(directory=MIGRATIONS, revision="708192a4b5c6")
                    campaign_tables = set(inspect(db.engine).get_table_names())
                    self.assertEqual(after | {"ojs_html_audit_campaigns"}, campaign_tables)
                    self.assertIn("campaign_id", {
                        column["name"] for column in inspect(db.engine).get_columns("ojs_html_audit_runs")
                    })
                    self.assertIn(
                        "uq_ojs_html_audit_one_open_journal",
                        {index["name"] for index in inspect(db.engine).get_indexes("ojs_html_audit_runs")},
                    )
                    self.assertIn(
                        "uq_ojs_html_campaign_active_reference",
                        {index["name"] for index in inspect(db.engine).get_indexes("ojs_html_audit_campaigns")},
                    )
                    upgrade(directory=MIGRATIONS, revision="8192a4b5c6d7")
                    self.assertIn("ojs_seo_analysis_operations", set(inspect(db.engine).get_table_names()))
                    self.assertIn(
                        "uq_ojs_operation_one_open_journal",
                        {index["name"] for index in inspect(db.engine).get_indexes("ojs_seo_analysis_operations")},
                    )
                    upgrade(directory=MIGRATIONS, revision="91a2b3c4d5e6")
                    self.assertIn("ojs_html_evidence_reuses", set(inspect(db.engine).get_table_names()))
                    self.assertIn(
                        "uq_ojs_html_reuse_target_article",
                        {item["name"] for item in inspect(db.engine).get_unique_constraints("ojs_html_evidence_reuses")},
                    )
                    with db.engine.connect() as connection:
                        self.assertEqual("ok", connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())
                        self.assertEqual([], connection.exec_driver_sql("PRAGMA foreign_key_check").all())
                        self.assertEqual(1, connection.exec_driver_sql(
                            "SELECT count(*) FROM ojs_article_html_analyses WHERE id = ?", (analysis_id,)
                        ).scalar_one())
                    downgrade(directory=MIGRATIONS, revision="8192a4b5c6d7")
                    self.assertNotIn("ojs_html_evidence_reuses", set(inspect(db.engine).get_table_names()))
                    downgrade(directory=MIGRATIONS, revision="708192a4b5c6")
                    self.assertEqual(after | {"ojs_html_audit_campaigns"}, set(inspect(db.engine).get_table_names()))
                    downgrade(directory=MIGRATIONS, revision="6f708192a4b5")
                    self.assertEqual(after, set(inspect(db.engine).get_table_names()))
                    downgrade(directory=MIGRATIONS, revision="4e5f60718293")
                    self.assertEqual(before, set(inspect(db.engine).get_table_names()))
                finally:
                    db.session.remove(); db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
