"""Executor OJS SEO: claims duráveis, recuperação e persistência offline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace
import socket
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from sqlalchemy import func, select, update

from app import create_app
from platform_core.extensions import db
from platform_core.models import (
    AnalyticsProject,
    OjsArticle,
    OjsArticleObservation,
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
    OaiClientConfig,
    OaiCollectionResult,
    OaiDeletedRecord,
    OaiIssue,
    OaiMetadataFormat,
    OaiPmhAuditResult,
    OaiRecord,
    OaiRepositoryIdentity,
)
from platform_core.ojs_seo_executor import (
    OJS_SEO_WORKER_VERSION,
    claim_next_ojs_seo_audit,
    claim_ojs_seo_audit,
    process_claimed_ojs_seo_audit,
    process_next_ojs_seo_audit,
    process_ojs_seo_audit,
    recover_expired_ojs_seo_audits,
    run_ojs_seo_worker,
)
from platform_core.ojs_seo_persistence import OjsPersistenceError
from platform_core.ojs_seo_services import (
    OjsSeoAuditCancellationError,
    cancel_pending_ojs_seo_audit,
)
from platform_core.services import replace_grant, seed_platform


UTC = timezone.utc
TEST_TEMP_ROOT = str(Path(__file__).parents[1] / ".test-tmp")


def _create_process_app(database_uri: str, data_dir: str):
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "ojs-seo-worker-process-test-only",
        "PESQUISAPDF_CPF_HMAC_SECRET": "test-only-secret-with-adequate-length-123456",
        "SQLALCHEMY_DATABASE_URI": database_uri,
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 30}},
        "PLATFORM_DATA_DIR": data_dir,
    })


def _claim_in_process(database_uri: str, data_dir: str, start, result) -> None:
    """Alvo de processo separado para comprovar exclusão entre workers."""
    app = _create_process_app(database_uri, data_dir)
    try:
        with app.app_context():
            start.wait(15)
            claim = claim_next_ojs_seo_audit(lease_seconds=120)
            result.put((claim.audit_run_id, claim.journal_id) if claim else None)
            db.session.remove()
            db.engine.dispose()
    except BaseException as exc:  # A exceção só é uma evidência de teste, sem dados OAI.
        result.put(("error", type(exc).__name__))


def _reject_invalid_pending_in_process(database_uri: str, data_dir: str, run_id: str, start, result) -> None:
    """Dois processos disputam a finalização CAS de uma pendência inválida."""
    app = _create_process_app(database_uri, data_dir)
    try:
        with app.app_context():
            start.wait(15)
            http_calls: list[str] = []

            def no_http(endpoint: str):
                http_calls.append(endpoint)
                raise AssertionError("Uma auditoria fora do domínio OJS SEO nunca pode iniciar HTTP")

            run = process_ojs_seo_audit(run_id, client_factory=no_http, lease_seconds=120)
            result.put((run.status, run.completion_reason, len(http_calls)) if run else None)
            db.session.remove()
            db.engine.dispose()
    except BaseException as exc:  # Evidência local para o teste de concorrência.
        result.put(("error", type(exc).__name__))


def _cancel_pending_in_process(
    database_uri: str,
    data_dir: str,
    actor_id: str,
    run_id: str,
    start,
    result,
) -> None:
    """Cancela em outro processo para disputar de fato a posse do worker."""
    app = _create_process_app(database_uri, data_dir)
    try:
        with app.app_context():
            start.wait(15)
            actor = db.session.get(User, actor_id)
            if actor is None:
                raise AssertionError("Ator de teste ausente")
            try:
                run = cancel_pending_ojs_seo_audit(actor, run_id)
                result.put(("canceled", run.id))
            except OjsSeoAuditCancellationError as exc:
                result.put(("cancel_refused", exc.code))
            finally:
                db.session.remove()
                db.engine.dispose()
    except BaseException as exc:  # Evidência de concorrência, sem rede OAI.
        result.put(("error", type(exc).__name__))


def _active(identifier: str, *, title: str = "Título de teste", doi: str = "10.1000/teste") -> OaiRecord:
    return OaiRecord(
        identifier=identifier,
        datestamp="2026-10-02",
        set_specs=("articles",),
        metadata_present=True,
        fields={
            "title": (title,),
            "creator": ("Ana",),
            "subject": ("OJS",),
            "description": ("Resumo",),
            "identifier": (doi,),
            "date": ("2026-10-02",),
            "language": ("pt-BR",),
            "type": ("Article",),
        },
        dois=(doi,),
    )


def _deleted(identifier: str) -> OaiDeletedRecord:
    return OaiDeletedRecord(identifier=identifier, datestamp="2026-10-03", set_specs=("articles",))


def _collection(
    *,
    status: str = "completed",
    records: tuple[OaiRecord, ...] = (),
    deleted_records: tuple[OaiDeletedRecord, ...] = (),
    reason: str | None = None,
    errors: tuple[OaiIssue, ...] = (),
) -> OaiCollectionResult:
    identifiers = tuple(item.identifier for item in (*records, *deleted_records))
    return OaiCollectionResult(
        status=status,
        completion_reason=reason,
        pages_fetched=0 if status == "failed" else 1,
        total_response_bytes=0 if status == "failed" else 321,
        records=records,
        deleted_records=deleted_records,
        unique_identifiers=identifiers,
        duplicate_identifiers=(),
        errors=errors,
        elapsed_seconds=0.01,
    )


def _audit_result(collection: OaiCollectionResult | None, *, error: OaiIssue | None = None) -> OaiPmhAuditResult:
    return OaiPmhAuditResult(
        endpoint="https://journal.example.test/oai",
        status=collection.status if collection else "failed",
        identity=OaiRepositoryIdentity(
            repository_name="Repositório de teste",
            base_url="https://journal.example.test/oai",
            protocol_version="2.0",
        ) if collection else None,
        metadata_formats=(OaiMetadataFormat("oai_dc", None, None),) if collection else (),
        collection=collection,
        errors=(error,) if error else (),
    )


class _FakeClient:
    def __init__(self, result: OaiPmhAuditResult, *, max_execution_seconds: int = 1) -> None:
        self.result = result
        self.config = SimpleNamespace(max_execution_seconds=max_execution_seconds)
        self.called = False

    def inspect(self) -> OaiPmhAuditResult:
        self.called = True
        return self.result


class OjsSeoExecutorTests(unittest.TestCase):
    def setUp(self):
        self.scope = tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-seo-worker-", dir=TEST_TEMP_ROOT)
        self.database_path = Path(self.scope.name) / "ojs-worker.sqlite3"
        self.database_uri = f"sqlite:///{self.database_path.as_posix()}"
        self.app = _create_process_app(self.database_uri, self.scope.name)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        seed_platform()
        self.user = User(name="Executor OJS", email="executor-ojs@example.org", role="user", status="active")
        self.user.set_password("senha-de-teste-segura-123")
        db.session.add(self.user)
        db.session.flush()
        replace_grant(self.user, "marketing_sites", "executor_test", "active")
        workspace = Workspace(
            name="Área executor OJS", workspace_type="individual", status="active", owner_user_id=self.user.id
        )
        membership = WorkspaceMembership(workspace=workspace, user=self.user, role="owner", status="active")
        self.subscription = WorkspaceSubscription(
            workspace=workspace,
            plan_id="marketing_sites",
            status="active",
            terms_json={"plan_limits": {"instagram_accounts": 0, "ojs_journals": 10}},
        )
        db.session.add_all((
            Tool(id="ojs_seo", name="OJS SEO de teste", route="/test-only-ojs", active=True),
            workspace,
            membership,
            self.subscription,
        ))
        db.session.flush()
        db.session.add(PlanTool(plan_id="marketing_sites", tool_id="ojs_seo"))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.context.pop()
        self.scope.cleanup()

    def journal(self, *, endpoint: str = "https://journal.example.test/oai") -> OjsJournal:
        project = AnalyticsProject(
            owner_user_id=self.user.id,
            module_key="ojs_seo",
            name=f"Projeto OJS {self.user.id[:8]}",
            description="",
        )
        journal = OjsJournal(
            analytics_project=project,
            public_url="https://journal.example.test/index",
            oai_endpoint_url=endpoint,
            discovery_status="pending",
        )
        db.session.add(journal)
        db.session.commit()
        db.session.add(WorkspaceSubscriptionOjsJournal(
            workspace_subscription_id=self.subscription.id,
            ojs_journal_id=journal.id,
            canonical_identity_hash=hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
            canonical_endpoint_url=endpoint,
            status="active",
        ))
        db.session.commit()
        return journal

    def administrative_journal(self, *, endpoint: str = "https://periodicos.ufal.br/latitude/oai") -> OjsJournal:
        administrator = User(
            name="Administrador OJS", email="executor-admin-ojs@example.org", role="admin", status="active"
        )
        administrator.set_password("senha-de-teste-segura-123")
        project = AnalyticsProject(
            owner=administrator,
            module_key="ojs_seo",
            name="Revista Latitude",
            description="",
        )
        journal = OjsJournal(
            analytics_project=project,
            public_url="https://periodicos.ufal.br/latitude",
            oai_endpoint_url=endpoint,
            discovery_status="identified",
        )
        db.session.add_all((administrator, project, journal))
        db.session.commit()
        return journal

    @staticmethod
    def run_for(journal: OjsJournal) -> OjsSeoAuditRun:
        run = OjsSeoAuditRun(
            journal_id=journal.id,
            status="pending",
            parameters_json={"metadata_prefix": "oai_dc"},
        )
        db.session.add(run)
        db.session.commit()
        return run

    def client_factory(self, result: OaiPmhAuditResult, *, max_execution_seconds: int = 1):
        clients: list[_FakeClient] = []

        def factory(_endpoint: str) -> _FakeClient:
            client = _FakeClient(result, max_execution_seconds=max_execution_seconds)
            clients.append(client)
            return client

        return factory, clients

    @staticmethod
    def stored(run_id: str) -> OjsSeoAuditRun:
        db.session.expire_all()
        return db.session.get(OjsSeoAuditRun, run_id)

    def test_pending_claim_is_durable_and_running_has_token_lease_and_attempt(self):
        run = self.run_for(self.journal())
        claim = claim_ojs_seo_audit(run.id, lease_seconds=120)
        self.assertIsNotNone(claim)
        stored = self.stored(run.id)
        self.assertEqual("running", stored.status)
        self.assertEqual(claim.token, stored.claim_token)
        self.assertIsNotNone(stored.claimed_at)
        self.assertGreater(stored.lease_expires_at, stored.claimed_at)
        self.assertEqual(1, stored.attempt_count)

    def test_pending_running_completed_persists_articles_discovery_and_metrics(self):
        journal = self.journal()
        run = self.run_for(journal)
        factory, clients = self.client_factory(_audit_result(_collection(records=(_active("oai:test:one"),))))
        final = process_ojs_seo_audit(run.id, client_factory=factory, lease_seconds=120)
        stored = self.stored(run.id)
        article = db.session.scalar(select(OjsArticle).where(OjsArticle.journal_id == journal.id))
        self.assertEqual("completed", final.status)
        self.assertEqual(("completed", 1, 1), (stored.status, stored.record_count, stored.doi_count))
        self.assertIsNone(stored.claim_token)
        self.assertIsNone(stored.lease_expires_at)
        self.assertEqual(OJS_SEO_WORKER_VERSION, stored.processor_version)
        self.assertEqual("oai:test:one", article.oai_identifier)
        self.assertEqual("identified", db.session.get(OjsJournal, journal.id).discovery_status)
        self.assertTrue(clients[0].called)

    def test_partial_failed_and_explicit_tombstone_follow_collection_state(self):
        journal = self.journal()
        active_run = self.run_for(journal)
        process_ojs_seo_audit(
            active_run.id,
            client_factory=self.client_factory(_audit_result(_collection(records=(_active("oai:test:gone"),))))[0],
            lease_seconds=120,
        )
        tombstone_run = self.run_for(journal)
        process_ojs_seo_audit(
            tombstone_run.id,
            client_factory=self.client_factory(_audit_result(_collection(
                status="partial", deleted_records=(_deleted("oai:test:gone"),), reason="page_limit_reached"
            )))[0],
            lease_seconds=120,
        )
        article = db.session.scalar(select(OjsArticle).where(OjsArticle.journal_id == journal.id))
        self.assertEqual("deleted", article.record_status)
        self.assertEqual("partial", self.stored(tombstone_run.id).status)

        failed_run = self.run_for(journal)
        failed = _audit_result(None, error=OaiIssue("oai_discovery_failed", "Falha segura de descoberta."))
        process_ojs_seo_audit(failed_run.id, client_factory=self.client_factory(failed)[0], lease_seconds=120)
        stored = self.stored(failed_run.id)
        self.assertEqual(("failed", "oai_discovery_failed"), (stored.status, stored.completion_reason))
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(OjsArticle)))

    def test_partial_transport_failure_persists_all_prior_records_without_duplicates(self):
        """Uma página posterior irrecuperável não descarta páginas já coletadas."""
        journal = self.journal()
        records = tuple(
            _active(f"oai:test:latitude-{number}", doi=f"10.1000/latitude-{number}")
            for number in range(300)
        )
        result = _audit_result(_collection(
            status="partial",
            records=records,
            reason="read_timeout",
            errors=(OaiIssue("read_timeout", "A resposta do endpoint OAI-PMH excedeu o prazo permitido."),),
        ))
        run = self.run_for(journal)
        process_ojs_seo_audit(run.id, client_factory=self.client_factory(result)[0], lease_seconds=120)
        stored = self.stored(run.id)
        self.assertEqual(("partial", "read_timeout", 300), (
            stored.status, stored.completion_reason, stored.record_count
        ))
        self.assertEqual(300, db.session.scalar(select(func.count()).select_from(OjsArticle)))
        self.assertEqual(300, db.session.scalar(select(func.count()).select_from(OjsArticleObservation)))

    def test_partial_or_complete_absence_never_deletes_prior_article(self):
        journal = self.journal()
        first = self.run_for(journal)
        process_ojs_seo_audit(first.id, client_factory=self.client_factory(
            _audit_result(_collection(records=(_active("oai:test:retained"),)))
        )[0], lease_seconds=120)
        for status in ("partial", "completed"):
            run = self.run_for(journal)
            process_ojs_seo_audit(run.id, client_factory=self.client_factory(
                _audit_result(_collection(status=status, reason="page_limit_reached" if status == "partial" else None))
            )[0], lease_seconds=120)
        article = db.session.scalar(select(OjsArticle).where(OjsArticle.oai_identifier == "oai:test:retained"))
        self.assertEqual("active", article.record_status)

    def test_persistence_failure_rolls_back_articles_and_records_failure_separately(self):
        journal = self.journal()
        run = self.run_for(journal)
        factory, _clients = self.client_factory(_audit_result(_collection(records=(_active("oai:test:rollback"),))))
        with patch("platform_core.ojs_seo_executor.persist_oai_collection", side_effect=OjsPersistenceError(
            "simulated_persistence_failure", "Falha de teste."
        )):
            process_ojs_seo_audit(run.id, client_factory=factory, lease_seconds=120)
        stored = self.stored(run.id)
        self.assertEqual("failed", stored.status)
        self.assertEqual("persistence_failed", stored.completion_reason)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsArticle)))

    def test_same_execution_is_not_reopened_or_duplicated_after_completion(self):
        journal = self.journal()
        run = self.run_for(journal)
        result = _audit_result(_collection(records=(_active("oai:test:idempotent"),)))
        first = process_ojs_seo_audit(run.id, client_factory=self.client_factory(result)[0], lease_seconds=120)
        self.assertEqual("completed", first.status)
        self.assertIsNone(process_ojs_seo_audit(run.id, client_factory=self.client_factory(result)[0], lease_seconds=120))
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(OjsArticleObservation)))

    def test_same_claim_cannot_persist_twice_and_final_state_is_not_reopened(self):
        run = self.run_for(self.journal())
        claim = claim_ojs_seo_audit(run.id, lease_seconds=120)
        result = _audit_result(_collection(records=(_active("oai:test:claim-once"),)))
        first = process_claimed_ojs_seo_audit(claim, client_factory=self.client_factory(result)[0], lease_seconds=120)
        second = process_claimed_ojs_seo_audit(claim, client_factory=self.client_factory(result)[0], lease_seconds=120)
        self.assertEqual("completed", first.status)
        self.assertEqual("completed", second.status)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(OjsArticleObservation)))

    def test_claim_rejects_final_or_duplicate_run_and_enforces_one_running_per_journal(self):
        journal = self.journal()
        first = self.run_for(journal)
        second = self.run_for(journal)
        canceled = OjsSeoAuditRun(journal_id=journal.id, status="canceled")
        db.session.add(canceled)
        db.session.commit()
        self.assertIsNotNone(claim_ojs_seo_audit(first.id, lease_seconds=120))
        self.assertIsNone(claim_ojs_seo_audit(second.id, lease_seconds=120))
        self.assertIsNone(claim_ojs_seo_audit(first.id, lease_seconds=120))
        self.assertIsNone(claim_ojs_seo_audit(canceled.id, lease_seconds=120))

    def test_two_processes_claim_only_one_audit_for_same_journal(self):
        journal = self.journal()
        first, second = self.run_for(journal), self.run_for(journal)
        run_ids = {first.id, second.id}
        db.session.remove()
        context = get_context("spawn")
        start, output = context.Event(), context.Queue()
        workers = [context.Process(target=_claim_in_process, args=(self.database_uri, self.scope.name, start, output)) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.set()
        values = [output.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(20)
            self.assertEqual(0, worker.exitcode)
        claimed_ids = [value[0] for value in values if value is not None and value[0] != "error"]
        self.assertEqual(1, len(claimed_ids))
        self.assertIn(claimed_ids[0], run_ids)

    def test_two_processes_can_claim_audits_of_different_journals(self):
        first, second = self.run_for(self.journal()), self.run_for(self.journal(endpoint="https://other.example.test/oai"))
        run_ids = {first.id, second.id}
        db.session.remove()
        context = get_context("spawn")
        start, output = context.Event(), context.Queue()
        workers = [context.Process(target=_claim_in_process, args=(self.database_uri, self.scope.name, start, output)) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.set()
        values = [output.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(20)
            self.assertEqual(0, worker.exitcode)
        self.assertEqual(run_ids, {value[0] for value in values})

    def test_expired_lease_is_recovered_once_and_old_worker_loses_possession(self):
        run = self.run_for(self.journal())
        claim = claim_ojs_seo_audit(run.id, lease_seconds=120)
        stored = self.stored(run.id)
        stored.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(1, recover_expired_ojs_seo_audits())
        self.assertEqual("failed", self.stored(run.id).status)
        result = _audit_result(_collection(records=(_active("oai:test:late"),)))
        self.assertEqual("failed", process_claimed_ojs_seo_audit(
            claim, client_factory=self.client_factory(result)[0], lease_seconds=120
        ).status)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsArticle)))

    def test_lease_expiring_during_external_collection_cannot_persist_after_http_returns(self):
        """A transação B precisa recusar posse perdida entre HTTP e gravação."""
        journal = self.journal()
        run = self.run_for(journal)
        claim = claim_ojs_seo_audit(run.id, lease_seconds=120)
        result = _audit_result(_collection(records=(_active("oai:test:expired-during-http"),)))

        class _ExpiryDuringInspectClient(_FakeClient):
            def inspect(self):
                db.session.execute(update(OjsSeoAuditRun).where(
                    OjsSeoAuditRun.id == run.id,
                    OjsSeoAuditRun.claim_token == claim.token,
                ).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
                db.session.commit()
                return super().inspect()

        final = process_claimed_ojs_seo_audit(
            claim,
            client_factory=lambda _endpoint: _ExpiryDuringInspectClient(result),
            lease_seconds=120,
        )
        self.assertEqual("running", final.status)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsArticle)))
        self.assertEqual(1, recover_expired_ojs_seo_audits())
        self.assertEqual("failed", self.stored(run.id).status)

    def test_worker_once_and_cli_start_without_pending_work_do_not_make_http_requests(self):
        calls = []

        def factory(_endpoint):
            calls.append(_endpoint)
            raise AssertionError("No pending run can instantiate an OAI client")

        run_ojs_seo_worker(self.app, once=True, poll_interval=0.01, lease_seconds=120, client_factory=factory)
        self.assertEqual([], calls)
        invoked = self.app.test_cli_runner().invoke(args=["ojs-seo-worker", "--once"])
        self.assertEqual(0, invoked.exit_code, invoked.output)

    def test_worker_once_claims_a_persisted_pending_audit_and_records_the_result(self):
        journal = self.journal()
        run = self.run_for(journal)
        factory, clients = self.client_factory(
            _audit_result(_collection(records=(_active("oai:test:worker-once"),)))
        )

        run_ojs_seo_worker(
            self.app,
            once=True,
            poll_interval=0.01,
            lease_seconds=120,
            client_factory=factory,
        )

        stored = self.stored(run.id)
        self.assertEqual(("completed", 1, 1), (stored.status, stored.record_count, stored.attempt_count))
        self.assertIsNotNone(stored.started_at)
        self.assertIsNotNone(stored.completed_at)
        self.assertTrue(clients[0].called)

    def test_worker_continuous_mode_processes_the_configured_queue_limit_before_exiting(self):
        first = self.run_for(self.journal(endpoint="https://first.example.test/oai"))
        second = self.run_for(self.journal(endpoint="https://second.example.test/oai"))
        factory, clients = self.client_factory(
            _audit_result(_collection(records=(_active("oai:test:continuous-worker"),)))
        )

        run_ojs_seo_worker(
            self.app,
            once=False,
            max_jobs=2,
            poll_interval=0.01,
            lease_seconds=120,
            client_factory=factory,
        )

        self.assertEqual("completed", self.stored(first.id).status)
        self.assertEqual("completed", self.stored(second.id).status)
        self.assertEqual(2, len(clients))

    def test_worker_stops_after_persisting_the_current_claim_without_starting_another(self):
        first = self.run_for(self.journal(endpoint="https://first.example.test/oai"))
        second = self.run_for(self.journal(endpoint="https://second.example.test/oai"))
        stopping = Event()
        result = _audit_result(_collection(records=(_active("oai:test:graceful-stop"),)))
        clients: list[_FakeClient] = []

        class _StopAfterInspectClient(_FakeClient):
            def inspect(self) -> OaiPmhAuditResult:
                inspected = super().inspect()
                stopping.set()
                return inspected

        def factory(_endpoint: str) -> _FakeClient:
            client = _StopAfterInspectClient(result)
            clients.append(client)
            return client

        run_ojs_seo_worker(
            self.app,
            once=False,
            poll_interval=0.01,
            lease_seconds=120,
            client_factory=factory,
            stop_event=stopping,
        )

        self.assertEqual("completed", self.stored(first.id).status)
        self.assertEqual("pending", self.stored(second.id).status)
        self.assertEqual(1, len(clients))

    def test_lease_shorter_than_client_budget_fails_without_articles(self):
        run = self.run_for(self.journal())
        factory, clients = self.client_factory(
            _audit_result(_collection(records=(_active("oai:test:budget"),))), max_execution_seconds=91
        )
        process_ojs_seo_audit(run.id, client_factory=factory, lease_seconds=120)
        stored = self.stored(run.id)
        self.assertEqual(("failed", "lease_too_short"), (stored.status, stored.completion_reason))
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsArticle)))
        self.assertFalse(clients[0].called)

    def test_executor_rejects_journal_outside_the_ojs_seo_project_domain_without_http(self):
        journal = self.journal()
        journal.analytics_project.module_key = "instagram"
        db.session.commit()
        run = self.run_for(journal)
        calls = []

        def factory(endpoint):
            calls.append(endpoint)
            raise AssertionError("A validação de domínio precisa ocorrer antes do cliente HTTP")

        process_ojs_seo_audit(run.id, client_factory=factory, lease_seconds=120)
        stored = self.stored(run.id)
        self.assertEqual(("failed", "invalid_ojs_project"), (stored.status, stored.completion_reason))
        self.assertEqual([], calls)

    def test_archived_pending_audit_is_canceled_without_http_or_reactivating_project(self):
        journal = self.journal()
        project = journal.analytics_project
        project.status = "archived"
        project.archived_at = utcnow()
        db.session.commit()
        run = self.run_for(journal)
        calls = []

        def factory(endpoint):
            calls.append(endpoint)
            raise AssertionError("Um projeto arquivado não pode iniciar coleta OAI-PMH")

        final = process_ojs_seo_audit(run.id, client_factory=factory, lease_seconds=120)
        stored = self.stored(run.id)
        self.assertEqual(("canceled", "project_archived"), (final.status, final.completion_reason))
        self.assertEqual(("canceled", "project_archived"), (stored.status, stored.completion_reason))
        self.assertIsNone(stored.claim_token)
        self.assertIsNone(stored.lease_expires_at)
        self.assertEqual(("archived", project.archived_at is not None), (
            db.session.get(AnalyticsProject, project.id).status,
            db.session.get(AnalyticsProject, project.id).archived_at is not None,
        ))
        self.assertEqual([], calls)

    def test_rejected_pending_treatment_is_idempotent_and_never_overwrites_running_claim(self):
        journal = self.journal()
        journal.analytics_project.module_key = "instagram"
        db.session.commit()
        run = self.run_for(journal)
        first = process_ojs_seo_audit(run.id, client_factory=lambda _endpoint: self.fail("HTTP inesperado"), lease_seconds=120)
        second = process_ojs_seo_audit(run.id, client_factory=lambda _endpoint: self.fail("HTTP inesperado"), lease_seconds=120)
        stored = self.stored(run.id)
        self.assertEqual(("failed", "invalid_ojs_project"), (first.status, first.completion_reason))
        self.assertIsNone(second)
        self.assertEqual(1, sum(1 for error in stored.errors_json if error["code"] == "invalid_ojs_project"))

        protected = self.run_for(journal)
        protected.status = "running"
        protected.claim_token = "claim-protegido"
        protected.claimed_at = utcnow()
        protected.lease_expires_at = utcnow() + timedelta(minutes=5)
        db.session.commit()
        self.assertIsNone(process_ojs_seo_audit(
            protected.id,
            client_factory=lambda _endpoint: self.fail("HTTP inesperado"),
            lease_seconds=120,
        ))
        stored_protected = self.stored(protected.id)
        self.assertEqual(("running", "claim-protegido"), (stored_protected.status, stored_protected.claim_token))

    def test_invalid_pending_entry_does_not_block_valid_queue_work(self):
        invalid_journal = self.journal()
        invalid_journal.analytics_project.module_key = "instagram"
        invalid = self.run_for(invalid_journal)
        invalid.created_at = utcnow() - timedelta(minutes=1)
        valid_journal = self.journal(endpoint="https://valid.example.test/oai")
        valid = self.run_for(valid_journal)
        db.session.commit()
        factory, clients = self.client_factory(_audit_result(_collection(records=(_active("oai:test:valid-after-invalid"),))))

        final = process_next_ojs_seo_audit(client_factory=factory, lease_seconds=120)
        self.assertEqual(valid.id, final.id)
        self.assertEqual(("failed", "invalid_ojs_project"), (
            self.stored(invalid.id).status, self.stored(invalid.id).completion_reason
        ))
        self.assertTrue(clients[0].called)

    def test_two_processes_finalize_invalid_pending_once_without_http(self):
        journal = self.journal()
        journal.analytics_project.module_key = "instagram"
        db.session.commit()
        run = self.run_for(journal)
        run_id = run.id
        db.session.remove()
        context = get_context("spawn")
        start, output = context.Event(), context.Queue()
        workers = [
            context.Process(
                target=_reject_invalid_pending_in_process,
                args=(self.database_uri, self.scope.name, run_id, start, output),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        start.set()
        values = [output.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(20)
            self.assertEqual(0, worker.exitcode)
        self.assertEqual(1, values.count(("failed", "invalid_ojs_project", 0)))
        self.assertEqual(1, values.count(None))
        stored = self.stored(run_id)
        self.assertEqual(("failed", "invalid_ojs_project"), (stored.status, stored.completion_reason))
        self.assertEqual(1, sum(1 for error in stored.errors_json if error["code"] == "invalid_ojs_project"))

    def test_cancel_and_worker_claim_are_mutually_exclusive_between_processes(self):
        """A confirmação de cancelamento nunca pode substituir um claim válido."""
        run = self.run_for(self.journal())
        run_id, user_id = run.id, self.user.id
        db.session.remove()
        context = get_context("spawn")
        start, output = context.Event(), context.Queue()
        worker = context.Process(
            target=_claim_in_process,
            args=(self.database_uri, self.scope.name, start, output),
        )
        canceler = context.Process(
            target=_cancel_pending_in_process,
            args=(self.database_uri, self.scope.name, user_id, run_id, start, output),
        )
        worker.start()
        canceler.start()
        start.set()
        results = [output.get(timeout=25) for _ in range(2)]
        worker.join(25)
        canceler.join(25)
        self.assertEqual(0, worker.exitcode)
        self.assertEqual(0, canceler.exitcode)
        self.assertFalse(any(isinstance(value, tuple) and value[0] == "error" for value in results), results)

        stored = self.stored(run_id)
        if stored.status == "canceled":
            self.assertIn(("canceled", run_id), results)
            self.assertIn(None, results)
            self.assertIsNone(stored.claim_token)
            self.assertEqual("canceled_by_user", stored.completion_reason)
        else:
            self.assertEqual("running", stored.status)
            self.assertTrue(any(value == (run_id, stored.journal_id) for value in results))
            self.assertTrue(any(
                isinstance(value, tuple)
                and value[0] == "cancel_refused"
                and value[1] in {"ojs_audit_not_pending", "ojs_audit_claimed_before_cancel"}
                for value in results
            ))
            self.assertIsNotNone(stored.claim_token)

    def test_pending_audit_is_failed_without_http_after_subscription_becomes_ineligible(self):
        journal = self.journal()
        run = self.run_for(journal)
        self.subscription.status = "canceled"
        self.subscription.ended_at = utcnow()
        db.session.commit()
        calls = []

        def factory(endpoint):
            calls.append(endpoint)
            raise AssertionError("Uma assinatura inelegível não pode iniciar HTTP OAI-PMH")

        process_ojs_seo_audit(run.id, client_factory=factory, lease_seconds=120)
        stored = self.stored(run.id)
        self.assertEqual(("failed", "ojs_subscription_ineligible"), (stored.status, stored.completion_reason))
        self.assertEqual([], calls)

    def test_administrative_journal_without_subscription_is_processed_without_creating_commercial_allocation(self):
        journal = self.administrative_journal()
        run = self.run_for(journal)
        result = _audit_result(_collection(records=(_active("oai:latitude:admin"),)))

        processed = process_ojs_seo_audit(
            run.id, client_factory=self.client_factory(result)[0], lease_seconds=120
        )

        self.assertEqual("completed", processed.status)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(
            WorkspaceSubscriptionOjsJournal
        ).where(WorkspaceSubscriptionOjsJournal.ojs_journal_id == journal.id)))

    def test_process_next_is_conservative_and_leaves_second_pending_for_next_worker_cycle(self):
        journal = self.journal()
        first, second = self.run_for(journal), self.run_for(journal)
        result = _audit_result(_collection(records=(_active("oai:test:next"),)))
        processed = process_next_ojs_seo_audit(client_factory=self.client_factory(result)[0], lease_seconds=120)
        self.assertEqual(first.id, processed.id)
        self.assertEqual("pending", self.stored(second.id).status)


if __name__ == "__main__":
    unittest.main()
