"""Serviços de acesso OJS SEO sem rota, HTTP externo ou banco persistente."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timezone
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import create_app
from platform_core.extensions import db
from platform_core.models import (
    AnalyticsProject,
    AuditLog,
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
from platform_core.ojs_oai_client import (
    OaiCollectionResult,
    OaiMetadataFormat,
    OaiPmhAuditResult,
    OaiRepositoryIdentity,
    ValidatedOaiEndpoint,
)
from platform_core.ojs_seo_services import (
    OjsSeoAuditCancellationError,
    OjsSeoAuthorizationError,
    OjsSeoNotFoundError,
    OjsSeoSubscriptionError,
    cancel_pending_ojs_seo_audit,
    discover_ojs_journal,
    get_ojs_journal,
    get_ojs_seo_audit_result,
    list_ojs_journals,
    list_ojs_seo_audits,
    register_ojs_journal,
    request_ojs_seo_audit,
)
from platform_core.services import replace_grant, seed_platform


TEST_TEMP_ROOT = str(Path(__file__).resolve().parents[1] / ".test-tmp")
UTC = timezone.utc


@contextmanager
def isolated_ojs_service_platform():
    with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-seo-services-", dir=TEST_TEMP_ROOT) as directory:
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "ojs-seo-service-test-only-secret",
            "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-seo-service-test-secret-with-adequate-length",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{Path(directory, 'test.sqlite3').as_posix()}",
            "PLATFORM_DATA_DIR": directory,
        })
        with app.app_context():
            db.create_all()
            seed_platform()
            # O catálogo produtivo ainda mantém OJS SEO sem Tool operacional.
            # Esta linha é uma capacidade isolada da fixture para verificar que
            # o serviço preserva a autorização existente quando a ferramenta
            # for publicada numa fase futura; ela nunca chama seed nem altera
            # catálogo real.
            db.session.add(Tool(
                id="ojs_seo", name="OJS SEO de teste", route="/test-only-ojs-seo", active=True
            ))
            db.session.flush()
            db.session.add(PlanTool(plan_id="marketing_sites", tool_id="ojs_seo"))
            db.session.commit()
            yield app
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def create_user(name: str, email: str) -> User:
    user = User(name=name, email=email, role="user", status="active")
    user.set_password("senha-de-teste-segura-123")
    db.session.add(user)
    db.session.flush()
    replace_grant(user, "marketing_sites", "admin_courtesy", "active")
    db.session.commit()
    return user


def make_journal(owner: User, *, endpoint: str = "https://journal.example.test/oai") -> OjsJournal:
    project = AnalyticsProject(
        owner_user_id=owner.id,
        module_key="ojs_seo",
        name=f"Projeto OJS {owner.id[:8]}",
        description="",
    )
    journal = OjsJournal(
        analytics_project=project,
        public_url="https://journal.example.test/index",
        oai_endpoint_url=endpoint,
        discovery_status="identified",
    )
    db.session.add(journal)
    db.session.commit()
    return journal


def make_subscription(owner: User, *, limit: int = 1) -> WorkspaceSubscription:
    workspace = Workspace(
        name=f"Área OJS {owner.id[:8]}", workspace_type="individual", status="active", owner_user_id=owner.id
    )
    membership = WorkspaceMembership(workspace=workspace, user=owner, role="owner", status="active")
    subscription = WorkspaceSubscription(
        workspace=workspace,
        plan_id="marketing_sites",
        status="active",
        terms_json={"plan_limits": {"instagram_accounts": 0, "ojs_journals": limit}},
    )
    db.session.add_all((workspace, membership, subscription))
    db.session.commit()
    return subscription


def allocate(subscription: WorkspaceSubscription, journal: OjsJournal) -> WorkspaceSubscriptionOjsJournal:
    allocation = WorkspaceSubscriptionOjsJournal(
        workspace_subscription_id=subscription.id,
        ojs_journal_id=journal.id,
        canonical_identity_hash="a" * 64,
        canonical_endpoint_url=journal.oai_endpoint_url or "https://journal.example.test/oai",
        status="active",
    )
    db.session.add(allocation)
    db.session.commit()
    return allocation


class _FakeClient:
    def __init__(self, result: OaiPmhAuditResult):
        self.result = result
        self.inspected_with_open_transaction: bool | None = None

    def inspect(self) -> OaiPmhAuditResult:
        self.inspected_with_open_transaction = db.session().in_transaction()
        return self.result


def fake_validator(endpoint: str) -> ValidatedOaiEndpoint:
    return ValidatedOaiEndpoint(
        canonical_url=endpoint,
        host="journal.example.test",
        port=443,
        path="/oai",
        addresses=("93.184.216.34",),
    )


def successful_discovery() -> OaiPmhAuditResult:
    collection = OaiCollectionResult(
        status="completed",
        completion_reason=None,
        pages_fetched=1,
        total_response_bytes=200,
        records=(),
        deleted_records=(),
        unique_identifiers=(),
        duplicate_identifiers=(),
    )
    return OaiPmhAuditResult(
        endpoint="https://journal.example.test/oai",
        status="completed",
        identity=OaiRepositoryIdentity(
            repository_name="Revista de teste",
            base_url="https://journal.example.test/oai",
            protocol_version="2.0",
        ),
        metadata_formats=(OaiMetadataFormat("oai_dc", None, None),),
        collection=collection,
    )


class OjsSeoServiceTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_ojs_service_platform()
        self.app = self.scope.__enter__()
        self.user = create_user("Titular", "titular-ojs@example.org")
        self.other = create_user("Outra conta", "outra-ojs@example.org")

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_authorized_discovery_uses_the_existing_client_after_the_read_transaction_closes(self):
        client = _FakeClient(successful_discovery())
        discovery = discover_ojs_journal(
            self.user,
            public_url="https://journal.example.test/index",
            oai_endpoint_url="https://journal.example.test/oai",
            client_factory=lambda _endpoint: client,
            endpoint_validator=fake_validator,
        )
        self.assertEqual("Revista de teste", discovery.repository_name)
        self.assertEqual("https://journal.example.test/oai", discovery.endpoint_url)
        self.assertFalse(client.inspected_with_open_transaction)

    def test_registration_creates_project_journal_and_subscription_allocation_atomically(self):
        subscription = make_subscription(self.user)
        journal = register_ojs_journal(
            self.user,
            workspace_subscription_id=subscription.id,
            public_url="https://journal.example.test/index",
            oai_endpoint_url="https://journal.example.test/oai",
            project_name="Periódico teste",
            client_factory=lambda _endpoint: _FakeClient(successful_discovery()),
            endpoint_validator=fake_validator,
        )
        allocation = db.session.scalar(select(WorkspaceSubscriptionOjsJournal).where(
            WorkspaceSubscriptionOjsJournal.ojs_journal_id == journal.id
        ))
        self.assertEqual("ojs_seo", journal.analytics_project.module_key)
        self.assertEqual(subscription.id, allocation.workspace_subscription_id)

    def test_registration_checks_existing_tool_authorization_before_the_quota_block(self):
        unassigned = User(name="Sem plano", email="registro-sem-plano@example.org", role="user", status="active")
        unassigned.set_password("senha-de-teste-segura-123")
        db.session.add(unassigned)
        db.session.commit()
        subscription = make_subscription(self.user)
        with self.assertRaises(OjsSeoAuthorizationError):
            register_ojs_journal(
                unassigned,
                workspace_subscription_id=subscription.id,
                public_url="https://journal.example.test/index",
                oai_endpoint_url="https://journal.example.test/oai",
                project_name="Sem acesso",
            )

    def test_discovery_without_access_does_not_instantiate_the_client(self):
        unassigned = User(name="Sem plano", email="sem-plano-ojs@example.org", role="user", status="active")
        unassigned.set_password("senha-de-teste-segura-123")
        db.session.add(unassigned)
        db.session.commit()
        calls: list[str] = []
        with self.assertRaises(OjsSeoAuthorizationError):
            discover_ojs_journal(
                unassigned,
                public_url="https://journal.example.test/index",
                oai_endpoint_url="https://journal.example.test/oai",
                client_factory=lambda endpoint: calls.append(endpoint),
                endpoint_validator=fake_validator,
            )
        self.assertEqual([], calls)

    def test_journal_identity_is_scoped_to_its_project_and_same_endpoint_is_allowed_for_another_owner(self):
        first = make_journal(self.user)
        second = make_journal(self.other)
        self.assertNotEqual(first.analytics_project_id, second.analytics_project_id)
        self.assertEqual(first.oai_endpoint_url, second.oai_endpoint_url)
        with self.assertRaises(IntegrityError):
            db.session.add(OjsJournal(
                analytics_project_id=first.analytics_project_id,
                public_url="https://another.example.test/index",
                discovery_status="pending",
            ))
            db.session.commit()
        db.session.rollback()

    def test_journal_queries_do_not_expose_another_owners_project_or_audit(self):
        journal = make_journal(self.user)
        run = OjsSeoAuditRun(journal_id=journal.id, status="completed")
        db.session.add(run)
        db.session.commit()
        self.assertEqual([journal.id], [item.id for item in list_ojs_journals(self.user)])
        with self.assertRaises(OjsSeoNotFoundError):
            get_ojs_journal(self.other, journal.id)
        with self.assertRaises(OjsSeoNotFoundError):
            get_ojs_seo_audit_result(self.other, run.id)

    def test_authorized_pending_audit_is_created_once_and_audited_without_http(self):
        journal = make_journal(self.user)
        allocate(make_subscription(self.user), journal)
        run = request_ojs_seo_audit(self.user, journal.id)
        db.session.commit()
        self.assertEqual((journal.id, "pending"), (run.journal_id, run.status))
        self.assertEqual([run.id], [item.id for item in list_ojs_seo_audits(self.user, journal.id)])
        self.assertEqual(run.id, get_ojs_seo_audit_result(self.user, run.id).id)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "ojs_seo_audit_requested"
        )))
        with self.assertRaisesRegex(ValueError, "Já existe uma auditoria"):
            request_ojs_seo_audit(self.user, journal.id)
        with self.assertRaises(OjsSeoNotFoundError):
            request_ojs_seo_audit(self.other, journal.id)

    def test_pending_audit_cancel_is_owner_scoped_idempotent_and_audited(self):
        journal = make_journal(self.user)
        allocate(make_subscription(self.user), journal)
        run = request_ojs_seo_audit(self.user, journal.id)
        db.session.commit()

        canceled = cancel_pending_ojs_seo_audit(self.user, run.id)
        self.assertEqual(("canceled", "canceled_by_user"), (
            canceled.status, canceled.completion_reason,
        ))
        self.assertIsNotNone(canceled.completed_at)
        self.assertIsNone(canceled.claim_token)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "ojs_seo_audit_canceled"
        )))
        with self.assertRaises(OjsSeoAuditCancellationError) as repeated:
            cancel_pending_ojs_seo_audit(self.user, run.id)
        self.assertEqual("ojs_audit_not_pending", repeated.exception.code)
        with self.assertRaises(OjsSeoNotFoundError):
            cancel_pending_ojs_seo_audit(self.other, run.id)

    def test_cancel_preserves_an_archived_project_for_a_residual_pending_audit(self):
        journal = make_journal(self.user)
        allocate(make_subscription(self.user), journal)
        run = request_ojs_seo_audit(self.user, journal.id)
        project = journal.analytics_project
        project.status = "archived"
        project.archived_at = utcnow()
        db.session.commit()

        canceled = cancel_pending_ojs_seo_audit(self.user, run.id)
        self.assertEqual("canceled", canceled.status)
        db.session.expire_all()
        stored_project = db.session.get(AnalyticsProject, project.id)
        self.assertEqual("archived", stored_project.status)
        self.assertIsNotNone(stored_project.archived_at)

    def test_pending_audit_requires_confirmed_oai_endpoint(self):
        project = AnalyticsProject(
            owner_user_id=self.user.id, module_key="ojs_seo", name="Sem descoberta", description=""
        )
        journal = OjsJournal(analytics_project=project, public_url="https://journal.example.test/index")
        db.session.add(journal)
        db.session.commit()
        allocate(make_subscription(self.user), journal)
        with self.assertRaisesRegex(ValueError, "endpoint OAI-PMH confirmado"):
            request_ojs_seo_audit(self.user, journal.id)

    def test_pending_audit_requires_an_active_commercial_allocation(self):
        journal = make_journal(self.user)
        with self.assertRaises(OjsSeoSubscriptionError) as error:
            request_ojs_seo_audit(self.user, journal.id)
        self.assertEqual("ojs_journal_allocation_inactive", error.exception.code)


if __name__ == "__main__":
    unittest.main()
    cancel_pending_ojs_seo_audit,
