"""Rotas OJS SEO em SQLite descartável, sem ativar o catálogo real."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask_migrate import upgrade
from sqlalchemy import func, inspect, select

from app import create_app
from platform_core.extensions import db
from platform_core.models import (
    AnalyticsProject,
    AuditLog,
    OjsArticle,
    OjsArticleHtmlAnalysis,
    OjsArticleObservation,
    OjsHtmlAuditCampaign,
    OjsHtmlAuditRun,
    OjsJournal,
    OjsSeoAnalysisOperation,
    OjsSeoAuditRun,
    PlanTool,
    Tool,
    Workspace,
    WorkspaceMembership,
    WorkspaceSubscription,
    WorkspaceSubscriptionOjsJournal,
    utcnow,
)
from platform_core.ojs_oai_client import (
    OaiCollectionResult,
    OaiMetadataFormat,
    OaiPmhAuditResult,
    OaiRepositoryIdentity,
    ValidatedOaiEndpoint,
)
from platform_core.ojs_seo_diagnostics import FILTER_LABELS
from platform_core.platform_help import tutorial_for
from platform_core.services import replace_grant, seed_platform
from platform_core.ojs_seo_services import (
    archive_ojs_seo_project,
    delete_ojs_seo_project_permanently,
    register_ojs_journal,
)
from platform_helpers import csrf_from


TEST_TEMP_ROOT = str(Path(__file__).resolve().parents[1] / ".test-tmp")
MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


class _FakeOaiClient:
    def inspect(self) -> OaiPmhAuditResult:
        return OaiPmhAuditResult(
            endpoint="https://journal.example.test/index/oai",
            status="completed",
            identity=OaiRepositoryIdentity(
                repository_name="Revista OJS de teste",
                base_url="https://journal.example.test/index/oai",
                protocol_version="2.0",
            ),
            metadata_formats=(OaiMetadataFormat("oai_dc", None, None),),
            collection=OaiCollectionResult(
                status="completed",
                completion_reason=None,
                pages_fetched=1,
                total_response_bytes=128,
                records=(),
                deleted_records=(),
                unique_identifiers=(),
                duplicate_identifiers=(),
            ),
        )


def _fake_validator(endpoint: str) -> ValidatedOaiEndpoint:
    return ValidatedOaiEndpoint(
        canonical_url=endpoint,
        host="journal.example.test",
        port=443,
        path="/index/oai",
        addresses=("93.184.216.34",),
    )


@contextmanager
def isolated_ojs_routes_platform():
    with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-seo-routes-", dir=TEST_TEMP_ROOT) as directory:
        database = Path(directory) / "ojs-routes.sqlite3"
        app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": True,
            "SECRET_KEY": "ojs-seo-route-test-only-secret",
            "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-seo-route-test-secret-with-adequate-length",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "PLATFORM_DATA_DIR": directory,
        })
        app.extensions["ojs_seo_client_factory"] = lambda _endpoint: _FakeOaiClient()
        app.extensions["ojs_seo_endpoint_validator"] = _fake_validator
        with app.app_context():
            upgrade(directory=MIGRATIONS, revision="head")
            if not {"ojs_journals", "workspace_subscription_ojs_journals"}.issubset(
                set(inspect(db.engine).get_table_names())
            ):
                raise AssertionError("A cadeia Alembic temporária não criou as tabelas OJS esperadas.")
            db.session.remove()
            db.engine.dispose()
        try:
            yield app
        finally:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


def _user(name: str, email: str):
    from platform_core.models import User

    user = User(name=name, email=email, role="user", status="active")
    user.set_password("senha-de-teste-segura-123")
    db.session.add(user)
    db.session.flush()
    replace_grant(user, "marketing_sites", "route_test", "active")
    db.session.commit()
    return user


def _subscription(owner, *, status: str = "active", limit: int = 2) -> WorkspaceSubscription:
    workspace = Workspace(
        name=f"Área {owner.id[:8]}", workspace_type="individual", status="active", owner_user_id=owner.id
    )
    membership = WorkspaceMembership(workspace=workspace, user=owner, role="owner", status="active")
    subscription = WorkspaceSubscription(
        workspace=workspace,
        plan_id="marketing_sites",
        status=status,
        terms_json={"plan_limits": {"instagram_accounts": 0, "ojs_journals": limit}},
    )
    db.session.add_all((workspace, membership, subscription))
    db.session.commit()
    return subscription


def _journal(owner, *, name: str = "Projeto OJS") -> OjsJournal:
    project = AnalyticsProject(owner_user_id=owner.id, module_key="ojs_seo", name=name, description="")
    journal = OjsJournal(
        analytics_project=project,
        public_url="https://journal.example.test/index",
        oai_endpoint_url="https://journal.example.test/index/oai",
        repository_name="Revista OJS de teste",
        discovery_status="identified",
    )
    db.session.add(journal)
    db.session.commit()
    return journal


def _allocate(subscription: WorkspaceSubscription, journal: OjsJournal) -> WorkspaceSubscriptionOjsJournal:
    import hashlib

    allocation = WorkspaceSubscriptionOjsJournal(
        workspace_subscription_id=subscription.id,
        ojs_journal_id=journal.id,
        canonical_identity_hash=hashlib.sha256(f"route-allocation:{journal.id}".encode()).hexdigest(),
        canonical_endpoint_url=journal.oai_endpoint_url,
        status="active",
    )
    db.session.add(allocation)
    db.session.commit()
    return allocation


def _diagnostic_record(
    journal: OjsJournal,
    run: OjsSeoAuditRun,
    identifier: str,
    fields: dict[str, list[str]],
    *,
    doi: str | None = None,
    status: str = "active",
) -> OjsArticle:
    """Cria uma evidência histórica sem depender de HTTP nos testes de rota."""
    article = OjsArticle(
        journal_id=journal.id,
        oai_identifier=identifier,
        title=(fields.get("title") or [None])[0],
        titles_json=fields.get("title", []),
        creators_json=fields.get("creator", []),
        subjects_json=fields.get("subject", []),
        descriptions_json=fields.get("description", []),
        identifiers_json=fields.get("identifier", []),
        dates_json=fields.get("date", []),
        languages_json=fields.get("language", []),
        types_json=fields.get("type", []),
        doi=doi,
        dois_json=[doi] if doi else [],
        record_status=status,
        last_observed_at=utcnow(),
    )
    db.session.add(article)
    db.session.flush()
    db.session.add(OjsArticleObservation(
        journal_id=journal.id,
        article_id=article.id,
        audit_run_id=run.id,
        record_status=status,
        dublin_core_json=fields,
        doi_observed=doi,
        dois_observed_json=[doi] if doi else [],
        observed_at=utcnow(),
    ))
    return article


class OjsSeoRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A cadeia Alembic inteira é validada uma vez no SQLite descartável.
        # Cada método recebe um novo app context abaixo, para que ``g`` e a
        # sessão Flask-Login nunca vazem entre clientes de teste.
        cls.scope = isolated_ojs_routes_platform()
        cls.app = cls.scope.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.scope.__exit__(None, None, None)

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        seed_platform()
        db.session.add(Tool(
            id="ojs_seo", name="OJS SEO de teste", route="/analytics/ojs-seo", active=True
        ))
        # ``PlanTool`` carrega somente FKs escalares (sem relationship ORM),
        # então o ``Tool`` efêmero precisa ser confirmado antes da associação.
        # Isso espelha a ordem produzida por uma migration em banco isolado.
        db.session.commit()
        db.session.add(PlanTool(plan_id="marketing_sites", tool_id="ojs_seo"))
        db.session.commit()
        self.owner = _user("Titular OJS", "route-owner@example.org")
        self.other = _user("Outra conta", "route-other@example.org")
        self.subscription = _subscription(self.owner)
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _login_owner(self):
        self._login_client(self.client, self.owner.email)

    @staticmethod
    def _login_client(client, email: str):
        # A fixture mantém um app context de banco durante cada caso. Usar a
        # rota de login nesse contexto faria o ``g`` e o token CSRF do cliente
        # anterior participarem do mesmo request-context. Estes testes cobrem
        # as rotas OJS, não o fluxo de autenticação: estabelecemos a sessão
        # Flask-Login diretamente e mantemos os POSTs OJS protegidos por CSRF.
        from flask import g
        from platform_core.models import User

        g.pop("_login_user", None)
        user = db.session.scalar(select(User).where(User.email == email))
        if user is None:
            raise AssertionError(f"Usuário de fixture ausente: {email}")
        with client.session_transaction() as session:
            session["_user_id"] = user.id
            session["_fresh"] = True

    def _csrf(self, path: str) -> str:
        return csrf_from(self.client.get(path))

    def _archive(self, journal: OjsJournal):
        return self.client.post(
            f"/analytics/ojs-seo/journals/{journal.id}/archive",
            data={
                "csrf_token": self._csrf("/analytics/ojs-seo"),
                "confirm": "yes",
            },
        )

    def test_unauthenticated_and_users_without_ojs_access_are_blocked(self):
        response = self.client.get("/analytics/ojs-seo")
        self.assertEqual(302, response.status_code)

        from platform_core.models import User

        no_grant = User(name="Sem plano OJS", email="route-no-grant@example.org", role="user", status="active")
        no_grant.set_password("senha-de-teste-segura-123")
        db.session.add(no_grant)
        db.session.commit()
        no_grant_client = self.app.test_client()
        self._login_client(no_grant_client, no_grant.email)
        self.assertEqual(403, no_grant_client.get("/analytics/ojs-seo").status_code)

        self._login_owner()
        tool = db.session.get(Tool, "ojs_seo")
        tool.active = False
        db.session.commit()
        response = self.client.get("/analytics/ojs-seo")
        self.assertEqual(403, response.status_code)

    def test_journal_list_and_add_form_use_the_shared_layout_and_eligible_subscription(self):
        self._login_owner()
        response = self.client.get("/analytics/ojs-seo")
        self.assertEqual(200, response.status_code)
        html = response.get_data(as_text=True)
        self.assertIn("Ainda não há periódicos ativos para consolidar.", html)
        self.assertIn('href="/analytics/ojs-seo/journals/new"', html)
        self.assertIn('href="/analytics/ojs-seo/journals/archived"', html)
        self.assertIn("Gestão e análise de periódicos científicos", html)
        self.assertIn("Adicionar periódico", html)
        self.assertIn("Periódicos arquivados", html)
        self.assertIn("Capacidade de periódicos", html)
        self.assertIn("Panorama dos periódicos", html)
        self.assertIn("Vagas utilizadas", html)
        self.assertIn("Limite contratado", html)
        self.assertIn("Vagas disponíveis", html)

        response = self.client.get("/analytics/ojs-seo/journals/new")
        html = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn('name="project_name"', html)
        self.assertIn('name="public_url"', html)
        self.assertIn(self.subscription.id, html)
        self.assertNotIn('name="oai_endpoint_url"', html)
        self.assertIn('ojs-seo-action-group', html)

    def test_journal_list_uses_archive_confirmation_and_delete_is_archived_only(self):
        journal = _journal(self.owner, name="Projeto para exclusão")
        self._login_owner()
        delete_path = f"/analytics/ojs-seo/journals/{journal.id}/delete"
        archive_path = f"/analytics/ojs-seo/journals/{journal.id}/archive"

        listing = self.client.get("/analytics/ojs-seo")
        html = listing.get_data(as_text=True)
        self.assertEqual(200, listing.status_code)
        self.assertIn(f'href="{archive_path}"', html)
        self.assertIn('title="Arquivar periódico"', html)
        self.assertIn('title="Abrir visão geral"', html)
        self.assertIn('aria-label="Abrir visão geral de Projeto para exclusão"', html)
        self.assertNotIn('id="ojs-seo-journal-list"', html)
        self.assertNotIn(f'href="{delete_path}"', html)
        self.assertNotIn('title="Excluir periódico Projeto para exclusão definitivamente"', html)
        self.assertIn(f'/analytics/ojs-seo/journals/{journal.id}"', html)

        archive_confirmation = self.client.get(archive_path)
        self.assertEqual(200, archive_confirmation.status_code)
        self.assertIn(
            "O periódico será removido da lista de ativos. Seu histórico será preservado e continuará ocupando uma vaga da assinatura.",
            archive_confirmation.get_data(as_text=True),
        )

        self.assertEqual(400, self.client.get(delete_path).status_code)
        archived = self._archive(journal)
        self.assertEqual(302, archived.status_code)
        self.assertTrue(archived.headers["Location"].endswith("#ojs-seo-consolidated-title"))

        confirmation = self.client.get(delete_path)
        self.assertEqual(200, confirmation.status_code)
        self.assertIn('Digite <strong>deletar</strong> para confirmar', confirmation.get_data(as_text=True))
        self.assertIn('data-delete-confirm="final-delete-button"', confirmation.get_data(as_text=True))
        archived_list = self.client.get("/analytics/ojs-seo/journals/archived")
        archived_html = archived_list.get_data(as_text=True)
        self.assertIn("Projeto para exclusão", archived_html)
        self.assertIn('title="Restaurar periódico"', archived_html)
        self.assertIn('title="Excluir periódico Projeto para exclusão definitivamente"', archived_html)
        self.assertIn("A vaga comercial desta assinatura continua ocupada", archived_html)
        self.assertIsNotNone(db.session.get(OjsJournal, journal.id))
        self.assertIsNotNone(db.session.get(AnalyticsProject, journal.analytics_project_id))

    def test_project_delete_requires_csrf_confirmation_and_owner_scope(self):
        journal = _journal(self.owner)
        self._login_owner()
        self.assertEqual(302, self._archive(journal).status_code)
        delete_path = f"/analytics/ojs-seo/journals/{journal.id}/delete"

        missing_csrf = self.client.post(delete_path, data={"confirmation": "deletar"})
        self.assertEqual(400, missing_csrf.status_code)
        self.assertIsNotNone(db.session.get(OjsJournal, journal.id))

        wrong_confirmation = self.client.post(delete_path, data={
            "csrf_token": self._csrf(delete_path), "confirmation": "remover",
        })
        self.assertEqual(400, wrong_confirmation.status_code)
        self.assertIn("Digite exatamente", wrong_confirmation.get_data(as_text=True))
        self.assertIsNotNone(db.session.get(OjsJournal, journal.id))

        foreign = _journal(self.other, name="Projeto de terceiro")
        token = self._csrf("/analytics/ojs-seo")
        response = self.client.post(
            f"/analytics/ojs-seo/journals/{foreign.id}/delete",
            data={"csrf_token": token, "confirmation": "deletar"},
        )
        self.assertEqual(404, response.status_code)
        self.assertIsNotNone(db.session.get(OjsJournal, foreign.id))

    def test_project_delete_removes_only_ojs_dependencies_and_frees_subscription_capacity(self):
        journal = _journal(self.owner, name="Projeto completo")
        journal_id = journal.id
        project_id = journal.analytics_project_id
        allocation = _allocate(self.subscription, journal)
        run = OjsSeoAuditRun(
            journal_id=journal.id,
            status="completed",
            started_at=utcnow(),
            completed_at=utcnow(),
            record_count=1,
        )
        article = OjsArticle(
            journal_id=journal.id,
            oai_identifier="oai:journal.example.test:delete:1",
            title="Registro a excluir",
            record_status="active",
            last_observed_at=utcnow(),
        )
        db.session.add_all((run, article))
        db.session.flush()
        observation = OjsArticleObservation(
            journal_id=journal.id,
            article_id=article.id,
            audit_run_id=run.id,
            record_status="active",
            dublin_core_json={"title": ["Registro a excluir"]},
            observed_at=utcnow(),
        )
        db.session.add(observation)
        db.session.commit()
        run_id = run.id
        article_id = article.id
        observation_id = observation.id
        allocation_id = allocation.id

        self._login_owner()
        self.assertEqual(302, self._archive(journal).status_code)
        db.session.expire_all()
        archived_project = db.session.get(AnalyticsProject, project_id)
        self.assertEqual("archived", archived_project.status)
        self.assertIsNotNone(archived_project.archived_at)
        self.assertIsNotNone(db.session.get(OjsSeoAuditRun, run_id))
        self.assertIsNotNone(db.session.get(OjsArticle, article_id))
        self.assertIsNotNone(db.session.get(OjsArticleObservation, observation_id))
        self.assertEqual("active", db.session.get(WorkspaceSubscriptionOjsJournal, allocation_id).status)
        delete_path = f"/analytics/ojs-seo/journals/{journal_id}/delete"
        response = self.client.post(delete_path, data={
            "csrf_token": self._csrf(delete_path), "confirmation": "deletar",
        })
        self.assertEqual(302, response.status_code)
        self.assertEqual(
            "/analytics/ojs-seo/journals/archived#ojs-seo-archived-journal-list",
            response.headers["Location"],
        )
        listing_after_delete = self.client.get(response.headers["Location"])
        self.assertEqual(200, listing_after_delete.status_code)
        self.assertNotIn("Projeto completo", listing_after_delete.get_data(as_text=True))
        self.assertIsNone(db.session.get(OjsJournal, journal_id))
        self.assertIsNone(db.session.get(AnalyticsProject, project_id))
        self.assertIsNone(db.session.get(OjsSeoAuditRun, run_id))
        self.assertIsNone(db.session.get(OjsArticle, article_id))
        self.assertIsNone(db.session.get(OjsArticleObservation, observation_id))
        self.assertIsNone(db.session.get(WorkspaceSubscriptionOjsJournal, allocation_id))
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(
            WorkspaceSubscriptionOjsJournal
        ).where(WorkspaceSubscriptionOjsJournal.workspace_subscription_id == self.subscription.id,
                WorkspaceSubscriptionOjsJournal.status == "active")))
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(
            AuditLog
        ).where(AuditLog.action == "ojs_seo_project_permanently_deleted")))

        replacement = register_ojs_journal(
            self.owner,
            workspace_subscription_id=self.subscription.id,
            public_url="https://journal.example.test/index",
            oai_endpoint_url="https://journal.example.test/index/oai",
            project_name="Projeto após exclusão",
            client_factory=lambda _endpoint: _FakeOaiClient(),
            endpoint_validator=_fake_validator,
        )
        self.assertIsNotNone(replacement.id)

    def test_project_delete_blocks_pending_or_running_audits_without_releasing_allocation(self):
        for status in ("pending", "running"):
            with self.subTest(status=status):
                journal = _journal(self.owner, name=f"Projeto {status}")
                allocation = _allocate(self.subscription, journal)
                run = OjsSeoAuditRun(journal_id=journal.id, status=status)
                if status == "running":
                    now = utcnow()
                    run.started_at = now
                    run.claim_token = str(uuid4())
                    run.claimed_at = now
                    run.lease_expires_at = now + timedelta(minutes=5)
                db.session.add(run)
                db.session.commit()

                self._login_owner()
                archive_path = f"/analytics/ojs-seo/journals/{journal.id}/archive"
                archive = self.client.post(archive_path, data={
                    "csrf_token": self._csrf("/analytics/ojs-seo"), "confirm": "yes",
                })
                self.assertEqual(302, archive.status_code)
                self.assertIn("auditoria pendente ou em execução", self.client.get(
                    archive.headers["Location"]
                ).get_data(as_text=True))
                self.assertEqual("active", db.session.get(AnalyticsProject, journal.analytics_project_id).status)

                project = db.session.get(AnalyticsProject, journal.analytics_project_id)
                project.status = "archived"
                project.archived_at = utcnow()
                db.session.commit()
                delete_path = f"/analytics/ojs-seo/journals/{journal.id}/delete"
                response = self.client.post(delete_path, data={
                    "csrf_token": self._csrf(delete_path), "confirmation": "deletar",
                })
                self.assertEqual(400, response.status_code)
                self.assertIn("auditoria pendente ou em execução", response.get_data(as_text=True))
                self.assertIsNotNone(db.session.get(OjsJournal, journal.id))
                self.assertEqual("active", db.session.get(WorkspaceSubscriptionOjsJournal, allocation.id).status)

    def test_project_delete_rolls_back_all_ojs_data_when_final_audit_fails(self):
        journal = _journal(self.owner, name="Projeto com falha")
        project_id = journal.analytics_project_id
        allocation = _allocate(self.subscription, journal)
        run = OjsSeoAuditRun(journal_id=journal.id, status="completed")
        article = OjsArticle(
            journal_id=journal.id,
            oai_identifier="oai:journal.example.test:rollback:1",
            record_status="active",
        )
        db.session.add_all((run, article))
        db.session.flush()
        observation = OjsArticleObservation(
            journal_id=journal.id,
            article_id=article.id,
            audit_run_id=run.id,
            record_status="active",
            dublin_core_json={},
        )
        db.session.add(observation)
        db.session.commit()

        self._login_owner()
        self.assertEqual(302, self._archive(journal).status_code)

        from platform_core import ojs_seo_services

        original_record_audit = ojs_seo_services.record_audit

        def interrupt_final_audit(actor, action, target_type, target_id, before=None, after=None):
            if action == "ojs_seo_project_permanently_deleted":
                raise RuntimeError("falha simulada após remover dependências")
            return original_record_audit(actor, action, target_type, target_id, before, after)

        with patch("platform_core.ojs_seo_services.record_audit", side_effect=interrupt_final_audit):
            with self.assertRaisesRegex(RuntimeError, "falha simulada"):
                delete_ojs_seo_project_permanently(self.owner, journal.id, "deletar")

        self.assertIsNotNone(db.session.get(OjsJournal, journal.id))
        self.assertIsNotNone(db.session.get(AnalyticsProject, project_id))
        self.assertIsNotNone(db.session.get(OjsSeoAuditRun, run.id))
        self.assertIsNotNone(db.session.get(OjsArticle, article.id))
        self.assertIsNotNone(db.session.get(OjsArticleObservation, observation.id))
        self.assertEqual("active", db.session.get(WorkspaceSubscriptionOjsJournal, allocation.id).status)

    def test_archive_restore_preserves_ojs_data_and_does_not_release_capacity(self):
        journal = _journal(self.owner, name="Projeto para arquivar")
        project_id = journal.analytics_project_id
        allocation = _allocate(self.subscription, journal)
        run = OjsSeoAuditRun(journal_id=journal.id, status="completed", record_count=1)
        article = OjsArticle(
            journal_id=journal.id,
            oai_identifier="oai:journal.example.test:archive:1",
            title="Registro preservado",
            record_status="active",
            last_observed_at=utcnow(),
        )
        db.session.add_all((run, article))
        db.session.flush()
        observation = OjsArticleObservation(
            journal_id=journal.id,
            article_id=article.id,
            audit_run_id=run.id,
            record_status="active",
            dublin_core_json={"title": ["Registro preservado"]},
        )
        db.session.add(observation)
        db.session.commit()
        run_id, article_id, observation_id, allocation_id = run.id, article.id, observation.id, allocation.id

        self._login_owner()
        archive = self._archive(journal)
        self.assertEqual(302, archive.status_code)
        db.session.expire_all()
        project = db.session.get(AnalyticsProject, project_id)
        self.assertEqual("archived", project.status)
        self.assertIsNotNone(project.archived_at)
        self.assertIsNotNone(db.session.get(OjsSeoAuditRun, run_id))
        self.assertIsNotNone(db.session.get(OjsArticle, article_id))
        self.assertIsNotNone(db.session.get(OjsArticleObservation, observation_id))
        self.assertEqual("active", db.session.get(WorkspaceSubscriptionOjsJournal, allocation_id).status)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(
            WorkspaceSubscriptionOjsJournal
        ).where(WorkspaceSubscriptionOjsJournal.workspace_subscription_id == self.subscription.id,
                WorkspaceSubscriptionOjsJournal.status == "active")))
        self.assertNotIn("Projeto para arquivar", self.client.get("/analytics/ojs-seo").get_data(as_text=True))

        archived = self.client.get("/analytics/ojs-seo/journals/archived")
        archived_html = archived.get_data(as_text=True)
        self.assertIn("Projeto para arquivar", archived_html)
        self.assertIn("Ver histórico", archived_html)
        self.assertEqual(404, self.client.post(
            f"/analytics/ojs-seo/journals/{journal.id}/audits",
            data={"csrf_token": csrf_from(archived)},
        ).status_code)
        restore = self.client.post(
            f"/analytics/ojs-seo/journals/{journal.id}/restore",
            data={"csrf_token": csrf_from(archived), "confirm": "yes"},
        )
        self.assertEqual(302, restore.status_code)
        self.assertTrue(restore.headers["Location"].endswith("#ojs-seo-archived-journal-list"))
        db.session.expire_all()
        self.assertEqual("active", db.session.get(AnalyticsProject, project_id).status)
        self.assertIsNone(db.session.get(AnalyticsProject, project_id).archived_at)
        self.assertIsNotNone(db.session.get(OjsSeoAuditRun, run_id))
        self.assertIsNotNone(db.session.get(OjsArticle, article_id))
        self.assertIsNotNone(db.session.get(OjsArticleObservation, observation_id))
        self.assertEqual("active", db.session.get(WorkspaceSubscriptionOjsJournal, allocation_id).status)
        self.assertIn("Projeto para arquivar", self.client.get("/analytics/ojs-seo").get_data(as_text=True))

    def test_archived_list_is_scoped_to_the_authorized_owner(self):
        own = _journal(self.owner, name="Arquivo do titular")
        foreign = _journal(self.other, name="Arquivo de terceiro")
        own_project = own.analytics_project
        foreign_project = foreign.analytics_project
        own_project.status = foreign_project.status = "archived"
        own_project.archived_at = foreign_project.archived_at = utcnow()
        db.session.commit()

        self._login_owner()
        response = self.client.get("/analytics/ojs-seo/journals/archived")
        self.assertEqual(200, response.status_code)
        html = response.get_data(as_text=True)
        self.assertIn("Arquivo do titular", html)
        self.assertNotIn("Arquivo de terceiro", html)

    def test_archive_restore_and_delete_are_owner_scoped_and_post_protected(self):
        journal = _journal(self.owner)
        self._login_owner()
        archive_path = f"/analytics/ojs-seo/journals/{journal.id}/archive"
        restore_path = f"/analytics/ojs-seo/journals/{journal.id}/restore"
        confirmation = self.client.get(archive_path)
        self.assertEqual(200, confirmation.status_code)
        self.assertIn("Arquivar periódico", confirmation.get_data(as_text=True))
        self.assertEqual(404, self.client.post(restore_path, data={
            "csrf_token": self._csrf("/analytics/ojs-seo"), "confirm": "yes",
        }).status_code)
        self.assertEqual(400, self.client.post(archive_path, data={"confirm": "yes"}).status_code)

        foreign = _journal(self.other, name="Projeto arquivamento de terceiro")
        self.assertEqual(404, self.client.post(
            f"/analytics/ojs-seo/journals/{foreign.id}/archive",
            data={"csrf_token": self._csrf("/analytics/ojs-seo"), "confirm": "yes"},
        ).status_code)
        self.assertEqual(404, self.client.post(
            f"/analytics/ojs-seo/journals/{foreign.id}/restore",
            data={"csrf_token": self._csrf("/analytics/ojs-seo"), "confirm": "yes"},
        ).status_code)
        self.assertEqual("active", db.session.get(AnalyticsProject, journal.analytics_project_id).status)

    def test_journal_list_counts_archived_reservations_and_uses_last_completed_audit(self):
        active_journal = _journal(self.owner, name="Periódico ativo")
        archived_journal = _journal(self.owner, name="Periódico arquivado")
        self.subscription.workspace.name = "OJS SEO — desenvolvimento — OJS SEO - teste local"
        _allocate(self.subscription, active_journal)
        _allocate(self.subscription, archived_journal)
        archived_project = archived_journal.analytics_project
        archived_project.status = "archived"
        archived_project.archived_at = utcnow()
        db.session.add_all((
            OjsSeoAuditRun(
                journal_id=active_journal.id,
                status="completed",
                started_at=utcnow() - timedelta(minutes=2),
                completed_at=utcnow() - timedelta(minutes=1),
                record_count=383,
                doi_count=337,
            ),
            OjsSeoAuditRun(journal_id=active_journal.id, status="pending"),
        ))
        db.session.commit()

        self._login_owner()
        html = self.client.get("/analytics/ojs-seo").get_data(as_text=True)
        self.assertIn("Periódico ativo", html)
        self.assertNotIn("Periódico arquivado", html)
        self.assertIn("Última coleta concluída", html)
        self.assertIn("Registros recuperados: 383", html)
        self.assertIn("DOIs identificados: 337", html)
        # Ambas as alocações permanecem ativas; o arquivamento não abre uma
        # vaga nem a interface deriva o uso da quantidade de cartões ativos.
        self.assertIn("<td>2</td><td>2</td><td>0</td>", html)
        self.assertIn(
            '<span title="OJS SEO — desenvolvimento — OJS SEO - teste local · OJS" '
            'aria-label="Assinatura OJS SEO — desenvolvimento; detalhes OJS SEO — desenvolvimento — OJS SEO - teste local · OJS">'
            "OJS SEO — desenvolvimento</span>",
            html,
        )
        full_form = self.client.get("/analytics/ojs-seo/journals/new").get_data(as_text=True)
        self.assertIn(
            "O limite de periódicos da sua assinatura foi atingido. Periódicos arquivados também ocupam vagas.",
            full_form,
        )

    def test_restore_requires_the_existing_subscription_to_remain_eligible(self):
        journal = _journal(self.owner, name="Projeto sem assinatura ativa")
        allocation = _allocate(self.subscription, journal)
        self._login_owner()
        self.assertEqual(302, self._archive(journal).status_code)
        self.subscription.status = "expired"
        db.session.commit()

        archived = self.client.get("/analytics/ojs-seo/journals/archived")
        restore = self.client.post(
            f"/analytics/ojs-seo/journals/{journal.id}/restore",
            data={"csrf_token": csrf_from(archived), "confirm": "yes"},
        )
        self.assertEqual(302, restore.status_code)
        self.assertIn("assinatura não está ativa", self.client.get(
            restore.headers["Location"]
        ).get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual("archived", db.session.get(AnalyticsProject, journal.analytics_project_id).status)
        self.assertEqual("active", db.session.get(WorkspaceSubscriptionOjsJournal, allocation.id).status)

    def test_archived_project_is_never_claimed_by_the_ojs_worker(self):
        journal = _journal(self.owner, name="Projeto arquivado sem claim")
        _allocate(self.subscription, journal)
        run = OjsSeoAuditRun(journal_id=journal.id, status="pending")
        db.session.add(run)
        db.session.flush()
        project = journal.analytics_project
        project.status = "archived"
        project.archived_at = utcnow()
        db.session.commit()

        from platform_core.ojs_seo_executor import claim_ojs_seo_audit

        self.assertIsNone(claim_ojs_seo_audit(run.id))
        db.session.expire_all()
        self.assertEqual("pending", db.session.get(OjsSeoAuditRun, run.id).status)
        self._login_owner()
        detail_path = f"/analytics/ojs-seo/journals/{journal.id}"
        audit_path = f"{detail_path}/audits/{run.id}"
        cancel_path = f"{audit_path}/cancel"
        self.assertNotIn("Cancelar auditoria", self.client.get(detail_path).get_data(as_text=True))
        self.assertNotIn("Cancelar auditoria", self.client.get(audit_path).get_data(as_text=True))
        self.assertEqual(404, self.client.get(cancel_path).status_code)

    def test_archive_rollback_preserves_the_active_project_and_allocation(self):
        journal = _journal(self.owner, name="Projeto com falha ao arquivar")
        allocation = _allocate(self.subscription, journal)
        self._login_owner()

        from platform_core import ojs_seo_services

        original_record_audit = ojs_seo_services.record_audit

        def interrupt_archive(actor, action, target_type, target_id, before=None, after=None):
            if action == "ojs_seo_project_archived":
                raise RuntimeError("falha simulada ao arquivar")
            return original_record_audit(actor, action, target_type, target_id, before, after)

        with patch("platform_core.ojs_seo_services.record_audit", side_effect=interrupt_archive):
            with self.assertRaisesRegex(RuntimeError, "falha simulada"):
                archive_ojs_seo_project(self.owner, journal.id)

        db.session.expire_all()
        project = db.session.get(AnalyticsProject, journal.analytics_project_id)
        self.assertEqual("active", project.status)
        self.assertIsNone(project.archived_at)
        self.assertEqual("active", db.session.get(WorkspaceSubscriptionOjsJournal, allocation.id).status)

    def test_registration_derives_the_conventional_endpoint_and_redirects_to_the_journal(self):
        self._login_owner()
        token = self._csrf("/analytics/ojs-seo/journals/new")
        response = self.client.post("/analytics/ojs-seo/journals", data={
            "csrf_token": token,
            "workspace_subscription_id": self.subscription.id,
            "project_name": "SEO da Revista de Teste",
            "public_url": "https://journal.example.test/index",
        })
        self.assertEqual(302, response.status_code)
        journal = db.session.scalar(select(OjsJournal).where(OjsJournal.analytics_project.has(
            AnalyticsProject.name == "SEO da Revista de Teste"
        )))
        self.assertIsNotNone(journal)
        self.assertEqual("https://journal.example.test/index/oai", journal.oai_endpoint_url)
        self.assertIn(f"/journals/{journal.id}", response.headers["Location"])

        detail = self.client.get(response.headers["Location"])
        self.assertEqual(200, detail.status_code)
        self.assertIn("OJS SEO: SEO da Revista de Teste", detail.get_data(as_text=True))

    def test_registration_rejects_missing_csrf_and_another_workspace_subscription(self):
        self._login_owner()
        response = self.client.post("/analytics/ojs-seo/journals", data={
            "workspace_subscription_id": self.subscription.id,
            "project_name": "Sem CSRF",
            "public_url": "https://journal.example.test/index",
        })
        self.assertEqual(400, response.status_code)

        foreign_subscription = _subscription(self.other)
        token = self._csrf("/analytics/ojs-seo/journals/new")
        response = self.client.post("/analytics/ojs-seo/journals", data={
            "csrf_token": token,
            "workspace_subscription_id": foreign_subscription.id,
            "project_name": "Não autorizado",
            "public_url": "https://journal.example.test/index",
        })
        self.assertEqual(404, response.status_code)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsJournal)))

    def test_expired_or_capacityless_subscription_cannot_create_a_project(self):
        expired = _subscription(self.owner, status="expired")
        capacityless = _subscription(self.owner, limit=0)
        self._login_owner()
        form = self.client.get("/analytics/ojs-seo/journals/new").get_data(as_text=True)
        self.assertNotIn(expired.id, form)
        self.assertNotIn(capacityless.id, form)
        token = self._csrf("/analytics/ojs-seo/journals/new")
        response = self.client.post("/analytics/ojs-seo/journals", data={
            "csrf_token": token,
            "workspace_subscription_id": expired.id,
            "project_name": "Assinatura expirada",
            "public_url": "https://journal.example.test/index",
        })
        self.assertEqual(302, response.status_code)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsJournal)))

        token = self._csrf("/analytics/ojs-seo/journals/new")
        response = self.client.post("/analytics/ojs-seo/journals", data={
            "csrf_token": token,
            "workspace_subscription_id": capacityless.id,
            "project_name": "Sem capacidade",
            "public_url": "https://journal.example.test/index",
        })
        self.assertEqual(302, response.status_code)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsJournal)))

    def test_audit_is_requested_without_http_and_the_status_is_owner_scoped(self):
        journal = _journal(self.owner)
        from platform_core.models import WorkspaceSubscriptionOjsJournal
        import hashlib
        db.session.add(WorkspaceSubscriptionOjsJournal(
            workspace_subscription_id=self.subscription.id,
            ojs_journal_id=journal.id,
            canonical_identity_hash=hashlib.sha256(b"journal-route-test").hexdigest(),
            canonical_endpoint_url=journal.oai_endpoint_url,
            status="active",
        ))
        db.session.commit()
        self._login_owner()
        token = self._csrf(f"/analytics/ojs-seo/journals/{journal.id}")
        response = self.client.post(f"/analytics/ojs-seo/journals/{journal.id}/audits", data={"csrf_token": token})
        self.assertEqual(302, response.status_code)
        run = db.session.scalar(select(OjsSeoAuditRun).where(OjsSeoAuditRun.journal_id == journal.id))
        self.assertEqual("pending", run.status)

        status = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/audits/{run.id}/status")
        self.assertEqual(200, status.status_code)
        self.assertEqual("pending", status.get_json()["status"])

        other_client = self.app.test_client()
        self._login_client(other_client, self.other.email)
        hidden = other_client.get(f"/analytics/ojs-seo/journals/{journal.id}/audits/{run.id}/status")
        self.assertEqual(404, hidden.status_code)

    def test_completed_metrics_remain_visible_while_latest_audit_is_pending_or_running(self):
        journal = _journal(self.owner, name="Indicadores persistidos")
        completed = OjsSeoAuditRun(
            journal_id=journal.id,
            status="completed",
            created_at=utcnow() - timedelta(minutes=2),
            started_at=utcnow() - timedelta(minutes=2),
            completed_at=utcnow() - timedelta(minutes=1),
            record_count=383,
            deleted_record_count=38,
            doi_count=337,
            page_count=4,
            metrics_json={
                "collection": {"active_records": 345, "unique_identifiers": 383},
                "coverage": {"title": 345, "creator": 345},
            },
        )
        pending = OjsSeoAuditRun(journal_id=journal.id, status="pending")
        db.session.add_all((completed, pending))
        db.session.commit()
        self._login_owner()

        journal_path = f"/analytics/ojs-seo/journals/{journal.id}"
        cancel_path = f"{journal_path}/audits/{pending.id}/cancel"
        pending_page = self.client.get(journal_path)
        pending_html = pending_page.get_data(as_text=True)
        self.assertEqual(200, pending_page.status_code)
        self.assertIn("Indicadores da última coleta concluída", pending_html)
        self.assertIn("Problemas identificados", pending_html)
        self.assertNotIn('id="ojs-dashboard-coverage-title"', pending_html)
        self.assertNotIn('id="ojs-dashboard-priorities-title"', pending_html)
        self.assertNotIn('id="ojs-seo-next-action-title"', pending_html)
        self.assertNotIn(">Ver publicações</a>", pending_html)
        self.assertNotIn(">Ver o que corrigir</a>", pending_html)
        self.assertIn("383", pending_html)
        self.assertIn("345", pending_html)
        self.assertIn("Pendente", pending_html)
        pending_history_html = self.client.get(f"{journal_path}/history").get_data(as_text=True)
        self.assertIn(f'href="{cancel_path}"', pending_history_html)
        self.assertIn("Uma análise dos dados bibliográficos está pendente", pending_history_html)

        pending.status = "running"
        pending.started_at = utcnow()
        pending.claimed_at = utcnow()
        pending.lease_expires_at = utcnow() + timedelta(minutes=5)
        pending.claim_token = str(uuid4())
        db.session.commit()
        running_html = self.client.get(journal_path).get_data(as_text=True)
        self.assertIn("Indicadores da última coleta concluída", running_html)
        self.assertIn("383", running_html)
        self.assertIn("Em execução", running_html)
        running_history_html = self.client.get(f"{journal_path}/history").get_data(as_text=True)
        self.assertNotIn(f'href="{cancel_path}"', running_history_html)
        self.assertIn("Uma análise dos dados bibliográficos está em execução", running_history_html)

    def test_partial_metrics_are_explicit_and_no_audit_uses_an_empty_state(self):
        partial_journal = _journal(self.owner, name="Indicadores parciais")
        db.session.add(OjsSeoAuditRun(
            journal_id=partial_journal.id,
            status="partial",
            started_at=utcnow() - timedelta(minutes=1),
            completed_at=utcnow(),
            record_count=17,
            deleted_record_count=2,
            metrics_json={"collection": {"active_records": 15, "unique_identifiers": 17}},
        ))
        empty_journal = _journal(self.owner, name="Sem auditorias")
        db.session.commit()
        self._login_owner()

        partial_html = self.client.get(
            f"/analytics/ojs-seo/journals/{partial_journal.id}"
        ).get_data(as_text=True)
        self.assertIn("Visão geral", partial_html)
        self.assertIn("Indicadores da coleta parcial", partial_html)
        self.assertIn("17", partial_html)

        empty_html = self.client.get(
            f"/analytics/ojs-seo/journals/{empty_journal.id}"
        ).get_data(as_text=True)
        self.assertIn("Ainda não há uma análise bibliográfica disponível.", empty_html)
        self.assertIn("Histórico e análises", empty_html)

    def test_pending_audit_can_be_confirmed_and_canceled_without_touching_prior_results(self):
        journal = _journal(self.owner, name="Cancelar pendência")
        completed = OjsSeoAuditRun(
            journal_id=journal.id,
            status="completed",
            started_at=utcnow() - timedelta(minutes=2),
            completed_at=utcnow() - timedelta(minutes=1),
            record_count=1,
            metrics_json={"collection": {"active_records": 1, "unique_identifiers": 1}},
        )
        article = OjsArticle(
            journal_id=journal.id,
            oai_identifier="oai:journal.example.test:preserved:1",
            title="Registro preservado",
            record_status="active",
            last_observed_at=utcnow(),
        )
        pending = OjsSeoAuditRun(journal_id=journal.id, status="pending")
        db.session.add_all((completed, article, pending))
        db.session.flush()
        # O diagnóstico individual é sempre fundamentado na observação da
        # coleta concluída, não apenas no estado atual do artigo.
        db.session.add(OjsArticleObservation(
            journal_id=journal.id,
            article_id=article.id,
            audit_run_id=completed.id,
            record_status="active",
            dublin_core_json={"title": ["Registro preservado"]},
            observed_at=utcnow(),
        ))
        db.session.commit()
        self._login_owner()

        cancel_path = f"/analytics/ojs-seo/journals/{journal.id}/audits/{pending.id}/cancel"
        confirmation = self.client.get(cancel_path)
        self.assertEqual(200, confirmation.status_code)
        self.assertIn("Cancelar auditoria?", confirmation.get_data(as_text=True))
        self.assertIn("Registro preservado", self.client.get(
            f"/analytics/ojs-seo/journals/{journal.id}/articles"
        ).get_data(as_text=True))

        self.assertEqual(400, self.client.post(cancel_path).status_code)
        self.assertEqual("pending", db.session.get(OjsSeoAuditRun, pending.id).status)
        canceled = self.client.post(cancel_path, data={"csrf_token": self._csrf(cancel_path)})
        self.assertEqual(302, canceled.status_code)
        db.session.expire_all()
        stored = db.session.get(OjsSeoAuditRun, pending.id)
        self.assertEqual(("canceled", "canceled_by_user"), (stored.status, stored.completion_reason))
        self.assertIsNotNone(stored.completed_at)
        self.assertEqual("completed", db.session.get(OjsSeoAuditRun, completed.id).status)
        self.assertIsNotNone(db.session.get(OjsArticle, article.id))
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "ojs_seo_audit_canceled"
        )))

        repeated = self.client.post(cancel_path, data={"csrf_token": self._csrf(
            f"/analytics/ojs-seo/journals/{journal.id}"
        )})
        self.assertEqual(302, repeated.status_code)
        self.assertEqual("canceled", db.session.get(OjsSeoAuditRun, pending.id).status)

    def test_cancel_rejects_running_audits_and_foreign_projects(self):
        journal = _journal(self.owner, name="Claim protegido")
        running = OjsSeoAuditRun(
            journal_id=journal.id,
            status="running",
            started_at=utcnow(),
            claimed_at=utcnow(),
            lease_expires_at=utcnow() + timedelta(minutes=5),
            claim_token=str(uuid4()),
        )
        foreign_journal = _journal(self.other, name="Pendência alheia")
        foreign_pending = OjsSeoAuditRun(journal_id=foreign_journal.id, status="pending")
        db.session.add_all((running, foreign_pending))
        db.session.commit()
        self._login_owner()

        own_cancel = f"/analytics/ojs-seo/journals/{journal.id}/audits/{running.id}/cancel"
        refused = self.client.post(own_cancel, data={"csrf_token": self._csrf(
            f"/analytics/ojs-seo/journals/{journal.id}"
        )})
        self.assertEqual(302, refused.status_code)
        self.assertEqual(("running", running.claim_token), (
            db.session.get(OjsSeoAuditRun, running.id).status,
            db.session.get(OjsSeoAuditRun, running.id).claim_token,
        ))

        foreign_cancel = (
            f"/analytics/ojs-seo/journals/{foreign_journal.id}/audits/{foreign_pending.id}/cancel"
        )
        denied = self.client.post(foreign_cancel, data={"csrf_token": self._csrf(
            f"/analytics/ojs-seo/journals/{journal.id}"
        )})
        self.assertEqual(404, denied.status_code)
        self.assertEqual("pending", db.session.get(OjsSeoAuditRun, foreign_pending.id).status)

    def test_audit_dashboard_and_article_details_preserve_multivalued_metadata(self):
        journal = _journal(self.owner, name="Auditoria OJS")
        run = OjsSeoAuditRun(
            journal_id=journal.id,
            status="completed",
            started_at=utcnow(),
            completed_at=utcnow(),
            record_count=2,
            deleted_record_count=1,
            doi_count=1,
            page_count=1,
            metrics_json={
                "collection": {"active_records": 1, "unique_identifiers": 2},
                "coverage": {"title": 1, "creator": 1, "description": 0, "subject": 1, "identifier": 1, "date": 1, "language": 1, "type": 1},
            },
        )
        article = OjsArticle(
            journal_id=journal.id,
            oai_identifier="oai:journal.example.test:article:1",
            title="Título de teste",
            titles_json=["Título de teste", "Test title"],
            creators_json=["Ana", "Bruno"],
            subjects_json=["OJS", "SEO"],
            descriptions_json=["Resumo de teste"],
            identifiers_json=["10.1000/teste"],
            dates_json=["2026-10-01"],
            languages_json=["pt-BR"],
            types_json=["Article"],
            doi="10.1000/teste",
            dois_json=["10.1000/teste"],
            record_status="active",
            last_observed_at=utcnow(),
        )
        db.session.add_all((run, article))
        db.session.flush()
        db.session.add(OjsArticleObservation(
            journal_id=journal.id,
            article_id=article.id,
            audit_run_id=run.id,
            record_status="active",
            dublin_core_json={"title": ["Título de teste"], "creator": ["Ana", "Bruno"]},
            doi_observed="10.1000/teste",
            dois_observed_json=["10.1000/teste"],
            observed_at=utcnow(),
        ))
        db.session.commit()
        self._login_owner()

        audit_page = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/audits/{run.id}")
        audit_html = audit_page.get_data(as_text=True)
        self.assertEqual(200, audit_page.status_code)
        self.assertIn("Cobertura Dublin Core", audit_html)
        self.assertIn("OAI-PMH/DUBLIN CORE", audit_html)
        self.assertIn('ojs-seo-heading-actions', audit_html)

        articles_page = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/articles")
        articles_html = articles_page.get_data(as_text=True)
        self.assertEqual(200, articles_page.status_code)
        self.assertIn("Título de teste", articles_html)
        self.assertIn('ojs-seo-heading-actions', articles_html)
        detail = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/articles/{article.id}")
        detail_html = detail.get_data(as_text=True)
        self.assertEqual(200, detail.status_code)
        # O diagnóstico é fundamentado no snapshot da observação, nunca no
        # estado atual mutável de ``OjsArticle``.
        self.assertNotIn("Test title", detail_html)
        self.assertIn("Ana", detail_html)
        self.assertIn("Bruno", detail_html)
        self.assertIn('ojs-seo-heading-actions', detail_html)

    def test_article_diagnostic_uses_last_completed_audit_and_filters_in_database(self):
        journal = _journal(self.owner, name="Diagnóstico individual")
        completed = OjsSeoAuditRun(
            journal_id=journal.id,
            status="completed",
            started_at=utcnow(),
            completed_at=utcnow(),
            record_count=3,
            doi_count=1,
        )
        db.session.add(completed)
        db.session.flush()
        complete_fields = {
            "title": ["Registro completo"], "creator": ["Autora A", "Autor B"],
            "description": ["Resumo"], "subject": ["Metadados"],
            "language": ["pt-BR"], "date": ["2026-10-24"], "type": ["Article"],
            "identifier": ["https://journal.example.test/article/1", "10.1000/completo"],
        }
        incomplete_fields = {
            "title": ["Registro com lacunas"], "creator": ["Autora C"],
            "description": [], "subject": [], "language": ["pt-BR"],
            "date": ["2026-10-24"], "type": ["Article"], "identifier": ["item-2"],
        }
        deleted_fields = {field: [] for field in complete_fields}
        complete_article = _diagnostic_record(
            journal, completed, "oai:journal.example.test:diagnostic:1", complete_fields, doi="10.1000/completo",
        )
        _diagnostic_record(
            journal, completed, "oai:journal.example.test:diagnostic:2", incomplete_fields,
        )
        _diagnostic_record(
            journal, completed, "oai:journal.example.test:diagnostic:3", deleted_fields, status="deleted",
        )
        # Uma pendência mais recente não pode trocar a coleta de referência.
        db.session.add(OjsSeoAuditRun(journal_id=journal.id, status="pending"))
        db.session.commit()
        self._login_owner()

        response = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/articles")
        html = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("Última coleta concluída", html)
        self.assertIn("Registros ativos analisados", html)
        self.assertIn("Registro completo", html)
        self.assertIn("Registro com lacunas", html)
        self.assertNotIn("oai:journal.example.test:diagnostic:3</span>", html)
        self.assertIn("Sem descrição", html)
        self.assertIn("Sem assunto", html)
        self.assertIn("Sem DOI identificado", html)
        self.assertIn('title="Abrir URL pública"', html)
        self.assertIn('aria-label="Abrir URL pública"', html)
        self.assertIn('href="https://journal.example.test/article/1"', html)
        self.assertNotIn(">Abrir</a>", html)

        deleted = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/articles?state=deleted")
        self.assertEqual(200, deleted.status_code)
        self.assertIn("Registros excluídos explicitamente", deleted.get_data(as_text=True))
        self.assertIn("Excluído explicitamente", deleted.get_data(as_text=True))
        self.assertIn("Não se aplica", deleted.get_data(as_text=True))

        # Todas as opções reais do seletor permanecem combinações válidas com
        # tombstones. Filtros de lacuna retornam o estado vazio, pois um
        # tombstone não deve ser transformado em diagnóstico editorial.
        for filter_key in FILTER_LABELS:
            with self.subTest(filter_key=filter_key):
                deleted_filtered = self.client.get(
                    f"/analytics/ojs-seo/journals/{journal.id}/articles",
                    query_string={"filter": filter_key, "state": "deleted", "q": "diagnostic", "page": 1},
                )
                deleted_html = deleted_filtered.get_data(as_text=True)
                self.assertEqual(200, deleted_filtered.status_code)
                if filter_key == "all":
                    self.assertIn("Excluído explicitamente", deleted_html)
                else:
                    self.assertIn("Nenhum registro corresponde aos filtros selecionados.", deleted_html)

        filtered = self.client.get(
            f"/analytics/ojs-seo/journals/{journal.id}/articles?filter=missing_description&q=Autora+C"
        )
        filtered_html = filtered.get_data(as_text=True)
        self.assertEqual(200, filtered.status_code)
        self.assertIn("Registro com lacunas", filtered_html)
        self.assertNotIn("Registro completo", filtered_html)

        detail = self.client.get(
            f"/analytics/ojs-seo/journals/{journal.id}/articles/{complete_article.id}?audit={completed.id}"
        )
        self.assertEqual(200, detail.status_code)
        self.assertIn("https://journal.example.test/article/1", detail.get_data(as_text=True))

    def test_article_diagnostic_partial_fallback_and_deleted_records_are_separate(self):
        journal = _journal(self.owner, name="Coleta parcial")
        partial = OjsSeoAuditRun(
            journal_id=journal.id, status="partial", started_at=utcnow(), completed_at=utcnow(), record_count=1,
        )
        db.session.add(partial)
        db.session.flush()
        fields = {
            "title": ["<script>alert('externo')</script>"], "creator": ["Autor"],
            "description": [], "subject": [], "language": [], "date": [], "type": [], "identifier": [],
        }
        article = _diagnostic_record(
            journal, partial, "oai:journal.example.test:partial:1", fields,
        )
        db.session.commit()
        self._login_owner()

        listing = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/articles")
        self.assertEqual(200, listing.status_code)
        self.assertIn("Dados parciais da coleta", listing.get_data(as_text=True))
        self.assertIn("&lt;script&gt;alert", listing.get_data(as_text=True))
        detail = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/articles/{article.id}")
        detail_html = detail.get_data(as_text=True)
        self.assertEqual(200, detail.status_code)
        self.assertIn("Ausente no OAI-PMH", detail_html)
        self.assertIn("Não foi identificada uma descrição no registro Dublin Core.", detail_html)
        self.assertIn("Não foi possível identificar um DOI nos metadados coletados.", detail_html)

        journal_page = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}")
        journal_html = journal_page.get_data(as_text=True)
        self.assertEqual(200, journal_page.status_code)
        self.assertIn('ojs-seo-heading-actions', journal_html)
        self.assertIn('ojs-seo-action-group', journal_html)

    def test_article_diagnostic_uses_stable_server_pagination_and_ignores_canceled_runs(self):
        journal = _journal(self.owner, name="Paginação diagnóstica")
        completed = OjsSeoAuditRun(
            journal_id=journal.id, status="completed", started_at=utcnow(), completed_at=utcnow(), record_count=21,
        )
        canceled = OjsSeoAuditRun(
            journal_id=journal.id, status="canceled", completed_at=utcnow(), completion_reason="canceled_by_user",
        )
        db.session.add_all((completed, canceled))
        db.session.flush()
        for number in range(21):
            fields = {
                "title": [f"Registro {number:02d}"], "creator": ["Autoria"],
                "description": ["Resumo"], "subject": ["Assunto"], "language": ["pt-BR"],
                "date": ["2026-10-24"], "type": ["Article"], "identifier": [f"item-{number:02d}"],
            }
            _diagnostic_record(
                journal, completed, f"oai:journal.example.test:page:{number:02d}", fields,
                doi=f"10.1000/page-{number:02d}",
            )
        db.session.commit()
        self._login_owner()

        first = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/articles?page=1")
        second = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}/articles?page=2")
        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertIn("Registro 00", first.get_data(as_text=True))
        self.assertNotIn("Registro 20", first.get_data(as_text=True))
        self.assertIn("Registro 20", second.get_data(as_text=True))
        self.assertIn("Última coleta concluída", second.get_data(as_text=True))
        self.assertNotIn("Dados parciais da coleta", second.get_data(as_text=True))

    def test_html_audit_is_requested_without_http_and_technical_results_are_owner_scoped(self):
        journal = _journal(self.owner, name="SEO técnico")
        _allocate(self.subscription, journal)
        source = OjsSeoAuditRun(
            journal_id=journal.id, status="completed", started_at=utcnow(), completed_at=utcnow(), record_count=1,
        )
        db.session.add(source)
        db.session.flush()
        article = _diagnostic_record(journal, source, "oai:technical:1", {
            "title": ["Registro técnico"], "creator": ["Ana"], "description": ["Resumo"],
            "subject": ["OJS"], "language": ["pt-BR"], "date": ["2026-10-25"],
            "type": ["Article"], "identifier": ["https://journal.example.test/article/technical"],
        }, doi="10.1000/technical")
        db.session.commit()
        self._login_owner()
        detail_path = f"/analytics/ojs-seo/journals/{journal.id}"
        detail = self.client.get(detail_path)
        self.assertIn("SEO técnico", detail.get_data(as_text=True))
        response = self.client.post(
            f"{detail_path}/technical-audits", data={"csrf_token": self._csrf(detail_path)},
        )
        self.assertEqual(302, response.status_code)
        html_run = db.session.scalar(select(OjsHtmlAuditRun).where(OjsHtmlAuditRun.journal_id == journal.id))
        self.assertEqual(("pending", source.id), (html_run.status, html_run.source_oai_audit_run_id))
        self.assertEqual(200, self.client.get(response.headers["Location"]).status_code)

        html_run.status = "completed"
        html_run.completed_at = utcnow()
        html_run.eligible_article_count = html_run.analyzed_page_count = 1
        analysis = OjsArticleHtmlAnalysis(
            journal_id=journal.id, article_id=article.id,
            source_observation_id=db.session.scalar(select(OjsArticleObservation.id).where(
                OjsArticleObservation.article_id == article.id
            )), html_audit_run_id=html_run.id, status="analyzed",
            source_url="https://journal.example.test/article/technical",
            final_url="https://journal.example.test/article/technical", http_status=200,
            response_time_ms=10,
            facts_json={
                "citation": {"citation_title": ["Registro técnico"]},
                "oai_html_consistency": {
                    # Registro histórico: a apresentação continua legível sem
                    # reclassificar ou regravar critérios anteriores.
                    "criteria_version": "ojs-html-comparison-v2",
                    "comparisons": [{
                        "field": "title", "label": "Título", "oai_label": "dc:title",
                        "html_label": "citation_title", "oai_values": ["Registro técnico"],
                        "html_values": ["Registro Técnico"], "oai_normalized": ["registro tecnico"],
                        "html_normalized": ["registro tecnico"], "result": "compatible",
                        "reason": "Valores formalmente equivalentes.",
                        "recommendation": "Nenhuma ação é indicada por esta comparação.",
                        "severity": "informative", "applicability": "applicable",
                    }],
                },
            },
            findings_json=[{
                "code": "canonical_missing", "classification": "confirmed", "severity": "medium",
                "applicability": "context_dependent", "evidence": "Evidência local",
                "impact": "Impacto depende do contexto.", "audience": "Administração técnica",
                "recommendation": "Revise.",
            }],
            confirmed_problem_count=1,
        )
        db.session.add(analysis); db.session.commit()
        records = self.client.get(f"{detail_path}/technical-audits/{html_run.id}/records")
        self.assertEqual(200, records.status_code)
        self.assertIn("Registro técnico", records.get_data(as_text=True))
        self.assertIn('title="Abrir URL pública"', records.get_data(as_text=True))
        filtered = self.client.get(
            f"{detail_path}/technical-audits/{html_run.id}/records?filter=confirmed&q=technical"
        )
        self.assertEqual(200, filtered.status_code)
        self.assertIn("Registro técnico", filtered.get_data(as_text=True))
        detailed_current = self.client.get(
            f"{detail_path}/technical-audits/{html_run.id}/records/{analysis.id}"
        ).get_data(as_text=True)
        self.assertIn("Valores normalizados", detailed_current)
        self.assertIn("Registro Técnico", detailed_current)
        self.assertIn("Valores formalmente equivalentes.", detailed_current)
        self.assertIn("Certeza e gravidade", detailed_current)
        self.assertIn("Depende de contexto", detailed_current)
        self.assertIn("Canonical ausente", detailed_current)
        self.assertNotIn("canonical_missing", detailed_current)

        # O resultado resumido de uma execução antiga continua legível, sem
        # regravar nem tentar reconstruir os valores que não foram salvos.
        analysis.facts_json = {"oai_html_consistency": {"title": "match", "authors": "different"}}
        db.session.commit()
        legacy_detail = self.client.get(
            f"{detail_path}/technical-audits/{html_run.id}/records/{analysis.id}"
        ).get_data(as_text=True)
        self.assertIn("valores originais comparados não foram persistidos", legacy_detail)
        self.assertIn("title: Compatível", legacy_detail)

        # A evidência histórica foi registrada antes da correção da mensagem
        # contextual. A rota deve apresentá-la como uma falha da página HTML,
        # sem alterar o dado persistido.
        analysis.status = "not_verified"
        analysis.final_url = analysis.http_status = analysis.content_type = None
        analysis.redirect_count = 0
        analysis.facts_json = {}
        analysis.findings_json = [{
            "code": "connect_timeout",
            "classification": "unverified",
            "evidence": "A conexão com o endpoint OAI-PMH excedeu o prazo permitido.",
            "recommendation": "Tente novamente mais tarde.",
        }]
        analysis.confirmed_problem_count = analysis.potential_problem_count = 0
        analysis.unverified_count = 1
        db.session.commit()
        detail = self.client.get(
            f"{detail_path}/technical-audits/{html_run.id}/records/{analysis.id}"
        )
        detail_html = detail.get_data(as_text=True)
        self.assertEqual(200, detail.status_code)
        self.assertIn("Metadados acadêmicos", detail_html)
        self.assertIn("A conexão com a página HTML excedeu o prazo permitido.", detail_html)
        self.assertNotIn(
            "A conexão com o endpoint OAI-PMH excedeu o prazo permitido.",
            detail_html,
        )
        self.assertIn("JSON-LD</dt><dd class=\"col-sm-8\">Não verificado", detail_html)
        self.assertNotIn("Não identificado", detail_html)
        other_client = self.app.test_client()
        self._login_client(other_client, self.other.email)
        self.assertEqual(404, other_client.get(
            f"{detail_path}/technical-audits/{html_run.id}/records"
        ).status_code)

    def test_html_coverage_is_accumulated_and_full_campaign_is_enqueued_without_http(self):
        journal = _journal(self.owner, name="Cobertura progressiva")
        _allocate(self.subscription, journal)
        source = OjsSeoAuditRun(
            journal_id=journal.id, status="completed", started_at=utcnow(), completed_at=utcnow(), record_count=2,
        )
        db.session.add(source)
        db.session.flush()
        first = _diagnostic_record(journal, source, "oai:coverage:1", {
            "title": ["Primeiro"], "identifier": ["https://journal.example.test/article/coverage-1"],
        })
        second = _diagnostic_record(journal, source, "oai:coverage:2", {
            "title": ["Segundo"], "identifier": ["https://journal.example.test/article/coverage-2"],
        })
        db.session.flush()
        html_run = OjsHtmlAuditRun(
            journal_id=journal.id,
            source_oai_audit_run_id=source.id,
            status="completed",
            completed_at=utcnow(),
            eligible_article_count=2,
            analyzed_page_count=1,
            metrics_json={"records_processed": 1, "verified_pages": 1},
        )
        db.session.add(html_run)
        db.session.flush()
        db.session.add(OjsArticleHtmlAnalysis(
            journal_id=journal.id,
            article_id=first.id,
            source_observation_id=db.session.scalar(select(OjsArticleObservation.id).where(
                OjsArticleObservation.article_id == first.id, OjsArticleObservation.audit_run_id == source.id,
            )),
            html_audit_run_id=html_run.id,
            status="analyzed",
            source_url="https://journal.example.test/article/coverage-1",
            final_url="https://journal.example.test/article/coverage-1",
            http_status=200,
            facts_json={},
            findings_json=[],
        ))
        db.session.commit()
        self._login_owner()
        detail_path = f"/analytics/ojs-seo/journals/{journal.id}"

        page = self.client.get(f"{detail_path}/history")
        html = page.get_data(as_text=True)
        self.assertEqual(200, page.status_code)
        self.assertIn("Análises e resultados", html)
        self.assertIn("ojs-history-split-action", html)
        self.assertIn('popovertarget="ojs-history-advanced-actions"', html)
        self.assertIn('data-temporary-popover', html)
        self.assertIn('data-popover-align-end', html)
        self.assertIn("Dados bibliográficos", html)
        self.assertIn("Cobertura da análise das páginas", html)
        self.assertIn("50% verificado", html)
        self.assertIn('style="width: 50.0000%"', html)
        self.assertIn("Progresso da análise das páginas: 50% efetivamente verificado", html)
        self.assertIn("Páginas verificadas", html)
        self.assertIn("Artigos não processados", html)
        self.assertIn("Analisar todas as páginas", html)
        self.assertIn("Revalidar páginas", html)
        self.assertIn("Campanhas de análise das páginas", html)
        self.assertIn("Nenhuma campanha de análise das páginas foi iniciada.", html)
        self.assertEqual(1, html.count("data-platform-tutorial-open"))
        self.assertEqual(4, html.count('<span class="ojs-html-coverage-legend__dot '))
        self.assertEqual(400, self.client.post(f"{detail_path}/technical-campaigns").status_code)

        response = self.client.post(
            f"{detail_path}/technical-campaigns",
            data={"csrf_token": self._csrf(detail_path)},
        )
        self.assertEqual(302, response.status_code)
        requested = db.session.scalar(select(OjsHtmlAuditRun).where(
            OjsHtmlAuditRun.journal_id == journal.id, OjsHtmlAuditRun.status == "pending",
        ))
        self.assertIsNotNone(requested)
        self.assertIsNotNone(requested.campaign_id)
        self.assertEqual("continue", requested.parameters_json["selection_mode"])
        campaign = db.session.get(OjsHtmlAuditCampaign, requested.campaign_id)
        self.assertEqual((journal.id, source.id, "pending", 1), (
            campaign.journal_id, campaign.source_oai_audit_run_id, campaign.status, campaign.batch_count,
        ))
        status_page = self.client.get(f"{detail_path}/history").get_data(as_text=True)
        self.assertIn("Aguardando processamento", status_page)
        self.assertIn("worker HTML ativo", status_page)

    def test_interrupted_campaign_from_old_reference_does_not_hide_new_campaign_action(self):
        journal = _journal(self.owner, name="Referência HTML renovada")
        _allocate(self.subscription, journal)
        old_source = OjsSeoAuditRun(
            journal_id=journal.id, status="completed", completed_at=utcnow() - timedelta(minutes=2),
        )
        db.session.add(old_source)
        db.session.flush()
        old_campaign = OjsHtmlAuditCampaign(
            journal_id=journal.id,
            source_oai_audit_run_id=old_source.id,
            status="interrupted",
            completion_reason="source_reference_changed",
            completed_at=utcnow() - timedelta(minutes=1),
        )
        new_source = OjsSeoAuditRun(
            journal_id=journal.id, status="completed", completed_at=utcnow(),
        )
        db.session.add_all((old_campaign, new_source))
        db.session.flush()
        _diagnostic_record(journal, new_source, "oai:renewed:1", {
            "title": ["Registro da referência atual"],
            "identifier": ["https://journal.example.test/article/renewed"],
        })
        db.session.commit()
        self._login_owner()
        history_path = f"/analytics/ojs-seo/journals/{journal.id}/history"

        page = self.client.get(history_path)
        html = page.get_data(as_text=True)
        self.assertEqual(200, page.status_code)
        self.assertIn("Analisar todas as páginas", html)
        self.assertNotIn("Retomar análise completa", html)
        self.assertIn("Interrompida ou aguardando retomada", html)

        response = self.client.post(
            f"/analytics/ojs-seo/journals/{journal.id}/technical-campaigns",
            data={"csrf_token": self._csrf(history_path)},
        )
        self.assertEqual(302, response.status_code)
        current = db.session.scalar(select(OjsHtmlAuditCampaign).where(
            OjsHtmlAuditCampaign.journal_id == journal.id,
            OjsHtmlAuditCampaign.source_oai_audit_run_id == new_source.id,
        ))
        self.assertEqual("pending", current.status)

    def test_history_coverage_bar_handles_zero_full_and_no_eligible_articles(self):
        zero_journal = _journal(self.owner, name="Cobertura zero")
        zero_source = OjsSeoAuditRun(
            journal_id=zero_journal.id, status="completed", started_at=utcnow(), completed_at=utcnow(),
        )
        db.session.add(zero_source)
        db.session.flush()
        _diagnostic_record(zero_journal, zero_source, "oai:zero:1", {
            "title": ["Ainda não analisado"],
            "identifier": ["https://journal.example.test/article/zero-1"],
        })

        full_journal = _journal(self.owner, name="Cobertura completa")
        full_source = OjsSeoAuditRun(
            journal_id=full_journal.id, status="completed", started_at=utcnow(), completed_at=utcnow(),
        )
        db.session.add(full_source)
        db.session.flush()
        full_article = _diagnostic_record(full_journal, full_source, "oai:full:1", {
            "title": ["Analisado"],
            "identifier": ["https://journal.example.test/article/full-1"],
        })
        full_run = OjsHtmlAuditRun(
            journal_id=full_journal.id, source_oai_audit_run_id=full_source.id,
            status="completed", completed_at=utcnow(), eligible_article_count=1,
            analyzed_page_count=1, metrics_json={"records_processed": 1, "verified_pages": 1},
        )
        db.session.add(full_run)
        db.session.flush()
        db.session.add(OjsArticleHtmlAnalysis(
            journal_id=full_journal.id,
            article_id=full_article.id,
            source_observation_id=db.session.scalar(select(OjsArticleObservation.id).where(
                OjsArticleObservation.article_id == full_article.id,
                OjsArticleObservation.audit_run_id == full_source.id,
            )),
            html_audit_run_id=full_run.id,
            status="analyzed",
            source_url="https://journal.example.test/article/full-1",
            final_url="https://journal.example.test/article/full-1",
            http_status=200,
            facts_json={},
            findings_json=[],
        ))

        no_eligible_journal = _journal(self.owner, name="Sem páginas elegíveis")
        db.session.add(OjsSeoAuditRun(
            journal_id=no_eligible_journal.id, status="completed", started_at=utcnow(), completed_at=utcnow(),
        ))
        db.session.commit()
        self._login_owner()

        zero_html = self.client.get(
            f"/analytics/ojs-seo/journals/{zero_journal.id}/history"
        ).get_data(as_text=True)
        self.assertIn("0% verificado", zero_html)
        self.assertIn('style="width: 100.0000%"', zero_html)
        self.assertIn("1 artigos não processados; 1 elegíveis", zero_html)

        full_html = self.client.get(
            f"/analytics/ojs-seo/journals/{full_journal.id}/history"
        ).get_data(as_text=True)
        self.assertIn("100% verificado", full_html)
        self.assertIn("1 páginas verificadas; 0 não verificadas; 0 artigos não processados; 1 elegíveis", full_html)

        no_eligible_html = self.client.get(
            f"/analytics/ojs-seo/journals/{no_eligible_journal.id}/history"
        ).get_data(as_text=True)
        self.assertIn("Não há artigos elegíveis para análise das páginas nesta referência bibliográfica.", no_eligible_html)

    def test_recommendations_are_scoped_to_the_reference_and_reuse_global_info_guide(self):
        journal = _journal(self.owner, name="Recomendações OJS")
        source = OjsSeoAuditRun(
            journal_id=journal.id, status="completed", started_at=utcnow(), completed_at=utcnow(), record_count=1,
        )
        db.session.add(source)
        db.session.flush()
        article = _diagnostic_record(journal, source, "oai:recommendation:1", {
            "title": ["Artigo com evidência"],
            "identifier": ["https://journal.example.test/article/recommendation-1"],
        })
        html_run = OjsHtmlAuditRun(
            journal_id=journal.id, source_oai_audit_run_id=source.id, status="completed", completed_at=utcnow(),
        )
        db.session.add(html_run)
        db.session.flush()
        observation_id = db.session.scalar(select(OjsArticleObservation.id).where(
            OjsArticleObservation.article_id == article.id,
            OjsArticleObservation.audit_run_id == source.id,
        ))
        db.session.add(OjsArticleHtmlAnalysis(
            journal_id=journal.id, article_id=article.id, source_observation_id=observation_id,
            html_audit_run_id=html_run.id, status="analyzed",
            source_url="https://journal.example.test/article/recommendation-1",
            final_url="https://journal.example.test/article/recommendation-1", http_status=200,
            facts_json={"oai_html_consistency": {"criteria_version": "ojs-html-comparison-v3"}},
            findings_json=[{
                "code": "canonical_missing", "classification": "confirmed", "severity": "medium",
                "applicability": "applicable", "evidence": "Nenhum canonical foi identificado.",
                "recommendation": "Configure uma URL canonical apropriada.",
                "impact": "A ausência foi confirmada.", "audience": "Administração técnica do OJS.",
            }],
        ))
        db.session.commit()
        self._login_owner()
        path = f"/analytics/ojs-seo/journals/{journal.id}/recommendations"
        response = self.client.get(path)
        html = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("Canonical ausente", html)
        self.assertNotIn("canonical_missing</td>", html)
        self.assertIn("info-icon-button", html)
        self.assertIn('data-platform-tutorial-open', html)
        self.assertIn('id="platform-tutorial-dialog"', html)
        self.assertIn("Cobertura progressiva e retentativas controladas", html)
        self.assertIn("Recomendações editoriais consolidadas", html)
        self.assertIn("platform_tutorial.js", html)
        overview = self.client.get(f"/analytics/ojs-seo/journals/{journal.id}")
        overview_html = overview.get_data(as_text=True)
        self.assertEqual(200, overview.status_code)
        self.assertIn('class="ojs-simple-bars"', overview_html)
        self.assertIn('class="ojs-simple-donut-layout"', overview_html)
        self.assertIn('class="platform-metric-card__value"', overview_html)
        self.assertNotIn('id="ojs-dashboard-chart-data"', overview_html)
        self.assertNotIn("plotly-2.35.2.min.js", overview_html)
        self.assertNotIn('id="ojs-dashboard-coverage-title"', overview_html)
        self.assertNotIn('id="ojs-dashboard-priorities-title"', overview_html)
        self.assertNotIn('id="ojs-seo-next-action-title"', overview_html)
        self.assertNotIn(">Ver publicações</a>", overview_html)
        self.assertNotIn(">Ver o que corrigir</a>", overview_html)
        tutorial = tutorial_for("ojs_seo")
        required_sections = {
            "visao-geral", "primeiro-acesso", "auditoria-bibliografica",
            "auditoria-html", "cobertura-progressiva", "interpretacao-diagnosticos",
            "comparacao-oai-html", "recomendacoes", "historico-e-limitacoes",
            "perguntas-frequentes",
        }
        self.assertTrue(required_sections.issubset({section["id"] for section in tutorial["sections"]}))
        for section_id in required_sections:
            self.assertIn(f'href="#tutorial-{section_id}"', html)
            self.assertIn(f'id="tutorial-{section_id}"', html)
        for guide_text in (
            "OJS Analytics", "até 100 páginas elegíveis", "Analisar todas as páginas",
            "worker HTML separado", "Sobrenome, Prenome",
            "não comprova indexação", "Abra O que corrigir",
            "dc:type declara inequivocamente", "A evidência descreve o que foi observado",
        ):
            self.assertIn(guide_text, html)

        detail = self.client.get(
            f"{path}/{source.id}/canonical_missing"
        )
        self.assertEqual(200, detail.status_code)
        detail_html = detail.get_data(as_text=True)
        self.assertIn("Artigos e evidências relacionadas", detail_html)
        self.assertIn("Aplicabilidade", detail_html)
        other_client = self.app.test_client()
        self._login_client(other_client, self.other.email)
        self.assertEqual(404, other_client.get(path).status_code)

    def test_journal_experience_groups_the_existing_routes_into_four_areas(self):
        journal = _journal(self.owner, name="Revista para navegação simples")
        _allocate(self.subscription, journal)
        source = OjsSeoAuditRun(
            journal_id=journal.id,
            status="completed",
            started_at=utcnow(),
            completed_at=utcnow(),
            record_count=1,
            metrics_json={"collection": {"active_records": 1}},
        )
        db.session.add(source)
        db.session.flush()
        article = _diagnostic_record(journal, source, "oai:navigation:1", {
            "title": ["Publicação para navegação"],
            "creator": ["Autora de teste"],
            "identifier": ["https://journal.example.test/article/navigation-1"],
        })
        db.session.commit()
        self._login_owner()

        overview_path = f"/analytics/ojs-seo/journals/{journal.id}"
        history_path = f"{overview_path}/history"
        recommendation_path = f"{overview_path}/recommendations"
        articles_path = f"{overview_path}/articles"
        overview = self.client.get(overview_path)
        overview_html = overview.get_data(as_text=True)
        self.assertEqual(200, overview.status_code)
        self.assertIn("Visão geral", overview_html)
        self.assertIn("Publicações ativas", overview_html)
        self.assertIn(f'href="{recommendation_path}"', overview_html)
        self.assertIn(f'href="{articles_path}"', overview_html)
        self.assertIn(f'href="{history_path}"', overview_html)
        self.assertNotIn('id="ojs-seo-next-action-title"', overview_html)

        history = self.client.get(history_path)
        self.assertEqual(200, history.status_code)
        history_html = history.get_data(as_text=True)
        self.assertIn("Histórico e análises", history_html)
        self.assertIn("Análises e resultados", history_html)
        self.assertIn("Atualizar dados", history_html)
        self.assertIn("ojs-history-split-action", history_html)
        self.assertIn('popovertarget="ojs-history-advanced-actions"', history_html)
        self.assertIn('data-popover-align-end', history_html)
        self.assertIn("Dados bibliográficos", history_html)
        self.assertIn("Analisar todas as páginas", history_html)
        self.assertLess(
            history_html.index("ojs-history-split-action"),
            history_html.index("data-platform-tutorial-open"),
        )
        self.assertEqual(1, history_html.count("data-platform-tutorial-open"))
        self.assertIn("Análises das páginas dos artigos", history_html)
        self.assertIn(f'href="{overview_path}/audits/{source.id}"', history_html)

        recommendations = self.client.get(recommendation_path)
        self.assertEqual(200, recommendations.status_code)
        self.assertIn("O que corrigir", recommendations.get_data(as_text=True))

        articles = self.client.get(articles_path)
        self.assertEqual(200, articles.status_code)
        self.assertIn("Publicações da coleta de referência", articles.get_data(as_text=True))
        detail = self.client.get(f"{articles_path}/{article.id}")
        self.assertEqual(200, detail.status_code)

    def test_overview_renders_two_completed_bibliographic_audits_in_every_theme(self):
        """A referência real pode ter duas coletas concluídas, como Latitude.

        Este é um teste de rota, e não apenas de agregação: ele percorre o
        parcial do dashboard, a tabela histórica e os novos componentes de
        métrica que dependem da comparação entre auditorias.
        """
        journal = _journal(self.owner, name="Revista com histórico comparável")
        _allocate(self.subscription, journal)
        first = OjsSeoAuditRun(
            journal_id=journal.id,
            status="completed",
            started_at=utcnow() - timedelta(days=2),
            completed_at=utcnow() - timedelta(days=2),
            record_count=382,
            deleted_record_count=37,
            doi_count=336,
            metrics_json={"collection": {"active_records": 345}},
        )
        latest = OjsSeoAuditRun(
            journal_id=journal.id,
            status="completed",
            started_at=utcnow() - timedelta(days=1),
            completed_at=utcnow() - timedelta(days=1),
            record_count=383,
            deleted_record_count=38,
            doi_count=337,
            metrics_json={"collection": {"active_records": 345}},
        )
        db.session.add_all((first, latest))
        db.session.flush()
        _diagnostic_record(journal, latest, "oai:dashboard:reference", {
            "title": ["Referência atual"],
            "creator": ["Autora de teste"],
            "description": ["Resumo"],
            "subject": ["Assunto"],
            "language": ["pt-BR"],
            "date": ["2026-10-29"],
            "type": ["Article"],
            "identifier": ["https://journal.example.test/article/dashboard-reference"],
        }, doi="10.1234/dashboard-reference")
        db.session.commit()
        self._login_owner()

        path = f"/analytics/ojs-seo/journals/{journal.id}"
        for theme in ("light", "dark", "black"):
            with self.subTest(theme=theme):
                with self.client.session_transaction() as session:
                    session["theme"] = theme
                response = self.client.get(path)
                html = response.get_data(as_text=True)
                self.assertEqual(200, response.status_code)
                self.assertIn("Registros bibliográficos recuperados", html)
                self.assertIn("Evolução das análises bibliográficas", html)
                self.assertIn("Visualizar auditoria de", html)
                self.assertIn("+0.3%", html)

    def test_integrated_analysis_button_enqueues_only_the_bibliographic_step(self):
        journal = _journal(self.owner, name="Revista integrada")
        _allocate(self.subscription, journal)
        self._login_owner()
        history_path = f"/analytics/ojs-seo/journals/{journal.id}/history"
        page = self.client.get(history_path)
        self.assertIn("Analisar periódico", page.get_data(as_text=True))
        response = self.client.post(
            f"/analytics/ojs-seo/journals/{journal.id}/integrated-analyses",
            data={"csrf_token": self._csrf(history_path)},
        )
        self.assertEqual(302, response.status_code)
        operation = db.session.scalar(select(OjsSeoAnalysisOperation).where(OjsSeoAnalysisOperation.journal_id == journal.id))
        self.assertEqual("oai_pending", operation.status)
        self.assertIsNotNone(operation.oai_audit_run_id)
        self.assertIsNone(operation.html_campaign_id)

    def test_history_progress_endpoint_reuses_coverage_and_stops_at_terminal_state(self):
        journal = _journal(self.owner, name="Progresso persistido")
        _allocate(self.subscription, journal)
        source = OjsSeoAuditRun(
            journal_id=journal.id, status="completed", started_at=utcnow(), completed_at=utcnow(), record_count=2,
        )
        db.session.add(source)
        db.session.flush()
        first = _diagnostic_record(journal, source, "oai:progress:1", {
            "title": ["Primeiro"], "identifier": ["https://journal.example.test/article/progress-1"],
        })
        second = _diagnostic_record(journal, source, "oai:progress:2", {
            "title": ["Segundo"], "identifier": ["https://journal.example.test/article/progress-2"],
        })
        campaign = OjsHtmlAuditCampaign(
            journal_id=journal.id, source_oai_audit_run_id=source.id, status="running", batch_count=2,
            started_at=utcnow(), last_progress_at=utcnow(),
        )
        db.session.add(campaign)
        db.session.flush()
        html_run = OjsHtmlAuditRun(
            journal_id=journal.id, source_oai_audit_run_id=source.id, campaign_id=campaign.id,
            status="completed", completed_at=utcnow(), eligible_article_count=2, analyzed_page_count=2,
            unverified_page_count=1, metrics_json={"records_processed": 2, "verified_pages": 1},
        )
        db.session.add(html_run)
        db.session.flush()
        for article, status in ((first, "analyzed"), (second, "not_verified")):
            observation_id = db.session.scalar(select(OjsArticleObservation.id).where(
                OjsArticleObservation.article_id == article.id,
                OjsArticleObservation.audit_run_id == source.id,
            ))
            db.session.add(OjsArticleHtmlAnalysis(
                journal_id=journal.id, article_id=article.id, source_observation_id=observation_id,
                html_audit_run_id=html_run.id, status=status,
                source_url=f"https://journal.example.test/article/{article.id}",
                final_url=f"https://journal.example.test/article/{article.id}" if status == "analyzed" else None,
                http_status=200 if status == "analyzed" else None,
                facts_json={}, findings_json=[], unverified_count=0 if status == "analyzed" else 1,
            ))
        operation = OjsSeoAnalysisOperation(
            journal_id=journal.id, mode="initial", status="html_running", oai_audit_run_id=source.id,
            html_campaign_id=campaign.id, started_at=utcnow(),
        )
        db.session.add(operation)
        db.session.commit()

        self._login_owner()
        progress_path = f"/analytics/ojs-seo/journals/{journal.id}/history/progress"
        response = self.client.get(progress_path)
        self.assertEqual(200, response.status_code)
        self.assertEqual("no-store", response.headers["Cache-Control"])
        payload = response.get_json()
        self.assertEqual(source.id, payload["reference"]["oai_audit_run_id"])
        self.assertEqual("html_running", payload["monitoring"]["stage"])
        self.assertTrue(payload["monitoring"]["active"])
        self.assertEqual("running", payload["campaign"]["status"])
        self.assertEqual(2, payload["campaign"]["batch_count"])
        self.assertEqual({
            "eligible_article_count": 2,
            "verified_article_count": 1,
            "reused_evidence_article_count": 0,
            "unverified_article_count": 1,
            "unprocessed_article_count": 0,
            "verified_percentage": 50,
            "available_coverage_percentage": 50,
        }, {key: payload["coverage"][key] for key in (
            "eligible_article_count", "verified_article_count", "reused_evidence_article_count",
            "unverified_article_count", "unprocessed_article_count", "verified_percentage",
            "available_coverage_percentage",
        )})

        retry_run = OjsHtmlAuditRun(
            journal_id=journal.id, source_oai_audit_run_id=source.id, campaign_id=campaign.id,
            status="completed", completed_at=utcnow(), eligible_article_count=2, analyzed_page_count=1,
            metrics_json={"records_processed": 1, "verified_pages": 1},
        )
        db.session.add(retry_run)
        db.session.flush()
        second_observation_id = db.session.scalar(select(OjsArticleObservation.id).where(
            OjsArticleObservation.article_id == second.id,
            OjsArticleObservation.audit_run_id == source.id,
        ))
        db.session.add(OjsArticleHtmlAnalysis(
            journal_id=journal.id, article_id=second.id, source_observation_id=second_observation_id,
            html_audit_run_id=retry_run.id, status="analyzed",
            source_url="https://journal.example.test/article/progress-2",
            final_url="https://journal.example.test/article/progress-2", http_status=200,
            facts_json={}, findings_json=[],
        ))
        campaign.batch_count = 3
        campaign.last_progress_at = utcnow()
        db.session.commit()
        recovered = self.client.get(progress_path).get_json()
        self.assertEqual({
            "verified_article_count": 2,
            "reused_evidence_article_count": 0,
            "unverified_article_count": 0,
            "unprocessed_article_count": 0,
            "verified_percentage": 100,
            "available_coverage_percentage": 100,
        }, {key: recovered["coverage"][key] for key in (
            "verified_article_count", "reused_evidence_article_count", "unverified_article_count",
            "unprocessed_article_count", "verified_percentage", "available_coverage_percentage",
        )})
        self.assertEqual(3, recovered["campaign"]["batch_count"])

        operation.status = "completed_with_pending"
        operation.completed_at = utcnow()
        campaign.status = "completed_with_pending"
        campaign.completed_at = utcnow()
        db.session.commit()
        completed = self.client.get(progress_path).get_json()
        self.assertFalse(completed["monitoring"]["active"])
        self.assertEqual("completed_with_pending", completed["operation"]["status"])
        self.assertEqual("completed_with_pending", completed["campaign"]["status"])

    def test_history_progress_endpoint_keeps_journals_isolated(self):
        own = _journal(self.owner, name="Progresso próprio")
        foreign = _journal(self.other, name="Progresso de terceiro")
        self._login_owner()
        own_response = self.client.get(f"/analytics/ojs-seo/journals/{own.id}/history/progress")
        self.assertEqual(200, own_response.status_code)
        self.assertFalse(own_response.get_json()["monitoring"]["active"])
        self.assertIsNone(own_response.get_json()["coverage"])
        self.assertEqual(404, self.client.get(
            f"/analytics/ojs-seo/journals/{foreign.id}/history/progress"
        ).status_code)

    def test_articles_and_audits_cannot_be_read_through_changed_identifiers(self):
        foreign_journal = _journal(self.other, name="Projeto alheio")
        foreign_run = OjsSeoAuditRun(journal_id=foreign_journal.id, status="pending")
        foreign_article = OjsArticle(journal_id=foreign_journal.id, oai_identifier="oai:other:1", record_status="active")
        db.session.add_all((foreign_run, foreign_article))
        db.session.commit()
        self._login_owner()
        for path in (
            f"/analytics/ojs-seo/journals/{foreign_journal.id}",
            f"/analytics/ojs-seo/journals/{foreign_journal.id}/audits/{foreign_run.id}",
            f"/analytics/ojs-seo/journals/{foreign_journal.id}/articles",
            f"/analytics/ojs-seo/journals/{foreign_journal.id}/articles/{foreign_article.id}",
        ):
            with self.subTest(path=path):
                self.assertEqual(404, self.client.get(path).status_code)

    def test_ojs_templates_reuse_the_shared_loading_overlay_for_forms_and_navigation(self):
        root = Path(__file__).resolve().parents[1]
        overlay = (root / "templates/platform/ojs_seo/_loading_overlay.html").read_text(encoding="utf-8")
        form_script = (root / "static/js/platform_form_loading_overlay.js").read_text(encoding="utf-8")
        partial_script = (root / "static/js/platform_auto_submit.js").read_text(encoding="utf-8")
        pagination = (root / "templates/platform/_pagination.html").read_text(encoding="utf-8")
        delete_confirmation = (
            root / "templates/platform/_permanent_delete_confirmation.html"
        ).read_text(encoding="utf-8")

        self.assertIn("platform/_progress_overlay.html", overlay)
        self.assertIn("platform_form_loading_overlay.js", overlay)
        self.assertIn("data-platform-loading-overlay-form", form_script)
        self.assertIn("data-platform-loading-overlay-link", form_script)
        self.assertIn("PlatformFormLoadingOverlay", form_script)
        self.assertIn("data-platform-loading-overlay-form", partial_script)
        self.assertIn("loadingOverlay.reset(form)", partial_script)
        self.assertIn("loading_overlay_target", pagination)
        self.assertIn("permanent_delete_loading_target", delete_confirmation)

        for template_name in (
            "journal_new.html",
            "journal_archive.html",
            "journal_delete.html",
            "archived_journals.html",
            "journal_detail.html",
            "journal_history.html",
            "audit_cancel.html",
            "articles.html",
            "html_records.html",
        ):
            template = (root / "templates/platform/ojs_seo" / template_name).read_text(encoding="utf-8")
            with self.subTest(template=template_name):
                self.assertIn("platform/ojs_seo/_loading_overlay.html", template)

        for template_name in (
            "journal_new.html",
            "journal_archive.html",
            "archived_journals.html",
            "journal_detail.html",
            "journal_history.html",
            "articles.html",
            "html_records.html",
        ):
            template = (root / "templates/platform/ojs_seo" / template_name).read_text(encoding="utf-8")
            with self.subTest(interaction_template=template_name):
                self.assertIn("data-platform-loading-overlay", template)
                self.assertIn("ojs-seo-overlay-processamento", template)

        node = shutil.which("node")
        if not node:
            self.skipTest("Node não disponível")
        result = subprocess.run(
            [node, "-e", "const assert = require('node:assert/strict');\n" + r"""
const classes = new Set();
const body = {classList:{add:name=>classes.add(name),remove:name=>classes.delete(name)}};
const overlay = {hidden:true, attrs:{}, setAttribute(k,v){this.attrs[k]=v}};
const attrs = {};
let clickHandler, pageshowHandler;
const link = {
  dataset:{loadingOverlayTarget:'ojs-seo-overlay-processamento'},
  getAttribute:name=>name === 'target' ? null : null,
  hasAttribute:()=>false,
  addEventListener:(name, handler)=>{if(name === 'click')clickHandler=handler;},
  setAttribute:(name, value)=>{attrs[name]=value;},
  removeAttribute:name=>{delete attrs[name];},
};
const document = {
  querySelectorAll:selector=>selector === '[data-platform-loading-overlay-form]' ? [] : [link],
  getElementById:id=>id === 'ojs-seo-overlay-processamento' ? overlay : null,
  body,
};
const window = {addEventListener:(name, handler)=>{if(name === 'pageshow')pageshowHandler=handler;}};
""" + form_script + r"""
const first = {defaultPrevented:false, button:0, preventDefault(){this.prevented=true;}};
clickHandler(first);
assert.equal(first.prevented, undefined);
assert.equal(overlay.hidden, false);
assert.equal(overlay.attrs['aria-hidden'], 'false');
assert.equal(attrs['aria-busy'], 'true');
assert.equal(attrs['aria-disabled'], 'true');
assert.equal(classes.has('is-processing'), true);
const duplicate = {defaultPrevented:false, button:0, preventDefault(){this.prevented=true;}};
clickHandler(duplicate);
assert.equal(duplicate.prevented, true);
pageshowHandler();
assert.equal(overlay.hidden, true);
assert.equal(overlay.attrs['aria-hidden'], 'true');
assert.equal(attrs['aria-busy'], undefined);
assert.equal(attrs['aria-disabled'], undefined);
assert.equal(classes.has('is-processing'), false);
"""],
            capture_output=True, text=True, encoding="utf-8", cwd=root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
