"""Reservas comerciais OJS: banco isolado, sem HTTP ou dados reais."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta, timezone
from multiprocessing import get_context
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from alembic.config import Config
from alembic.script import ScriptDirectory
from flask_migrate import downgrade, upgrade
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from app import create_app
from platform_core.extensions import db
from platform_core.models import (
    AnalyticsProject,
    OjsJournal,
    PlanTool,
    Tool,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceSubscription,
    WorkspaceSubscriptionLimitAdjustment,
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
    OjsSeoAuthorizationError,
    OjsSeoQuotaError,
    OjsSeoServiceError,
    OjsSeoSubscriptionError,
    canonical_ojs_journal_identity,
    register_ojs_journal,
    release_ojs_journal_allocation,
    request_ojs_seo_audit,
)
from platform_core.services import replace_grant, seed_platform


UTC = timezone.utc
TEST_TEMP_ROOT = str(Path(__file__).resolve().parents[1] / ".test-tmp")
MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")
PREVIOUS_REVISION = "3d4e5f607182"
REVISION = "4e5f60718293"
CURRENT_OJS_HEAD = "91a2b3c4d5e6"
ALLOCATION_TABLE = "workspace_subscription_ojs_journals"


class _StaticClient:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def inspect(self) -> OaiPmhAuditResult:
        collection = OaiCollectionResult(
            status="completed", completion_reason=None, pages_fetched=1,
            total_response_bytes=120, records=(), deleted_records=(),
            unique_identifiers=(), duplicate_identifiers=(),
        )
        return OaiPmhAuditResult(
            endpoint=self.endpoint,
            status="completed",
            identity=OaiRepositoryIdentity(
                repository_name="Revista OJS simulada", base_url=self.endpoint, protocol_version="2.0"
            ),
            metadata_formats=(OaiMetadataFormat("oai_dc", None, None),),
            collection=collection,
        )


def _validator(endpoint: str) -> ValidatedOaiEndpoint:
    parsed = urlsplit(endpoint)
    return ValidatedOaiEndpoint(
        canonical_url=endpoint,
        host=parsed.hostname or "journal.example.test",
        port=443,
        path=parsed.path or "/oai",
        addresses=("93.184.216.34",),
    )


def _process_app(database_uri: str, data_dir: str):
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "ojs-allocation-process-test-only",
        "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-allocation-process-test-secret-with-adequate-length",
        "SQLALCHEMY_DATABASE_URI": database_uri,
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 30}},
        "PLATFORM_DATA_DIR": data_dir,
    })


def _register_in_process(
    database_uri: str, data_dir: str, actor_id: str, subscription_id: str,
    endpoint: str, start, result,
) -> None:
    """Alvo Windows spawn: disputa real entre conexões e processos SQLite."""
    app = _process_app(database_uri, data_dir)
    try:
        with app.app_context():
            start.wait(15)
            actor = db.session.get(User, actor_id)
            journal = register_ojs_journal(
                actor,
                workspace_subscription_id=subscription_id,
                public_url=endpoint.removesuffix("/oai") + "/index",
                oai_endpoint_url=endpoint,
                project_name=f"Projeto {endpoint.rsplit('/', 2)[-2]}",
                client_factory=lambda value: _StaticClient(value),
                endpoint_validator=_validator,
            )
            result.put(("ok", journal.id))
            db.session.remove()
            db.engine.dispose()
    except BaseException as exc:  # resultado explícito da disputa controlada
        result.put(("error", getattr(exc, "code", type(exc).__name__)))


@contextmanager
def isolated_allocation_platform(*, named_database: bool = False):
    with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-allocation-", dir=TEST_TEMP_ROOT) as directory:
        database = Path(directory) / "allocation.sqlite3"
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "ojs-allocation-test-only",
            "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-allocation-test-secret-with-adequate-length",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 30}},
            "PLATFORM_DATA_DIR": directory,
        })
        with app.app_context():
            db.create_all()
            seed_platform()
            db.session.add(Tool(id="ojs_seo", name="OJS SEO de teste", route="/test-only-ojs", active=True))
            db.session.flush()
            for plan_id in ("marketing_sites", "marketing_complete", "integral"):
                db.session.add(PlanTool(plan_id=plan_id, tool_id="ojs_seo"))
            db.session.commit()
            yield app, directory, f"sqlite:///{database.as_posix()}"
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


class OjsSeoSubscriptionAllocationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_allocation_platform()
        self.app, self.directory, self.database_uri = self.scope.__enter__()
        self.owner = self.create_user("Titular", "allocation-owner@example.org")
        self.other = self.create_user("Outro", "allocation-other@example.org")

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def create_user(self, name: str, email: str) -> User:
        user = User(name=name, email=email, role="user", status="active")
        user.set_password("senha-de-teste-segura-123")
        db.session.add(user)
        db.session.flush()
        replace_grant(user, "marketing_sites", "admin_courtesy", "active")
        db.session.commit()
        return user

    def subscription_for(self, user: User, *, limit: int = 1, plan_id: str = "marketing_sites") -> WorkspaceSubscription:
        workspace = Workspace(
            name=f"Área {user.id[:8]}", workspace_type="individual", status="active", owner_user_id=user.id
        )
        membership = WorkspaceMembership(workspace=workspace, user=user, role="owner", status="active")
        subscription = WorkspaceSubscription(
            workspace=workspace, plan_id=plan_id, status="active",
            terms_json={"plan_limits": {"instagram_accounts": 0, "ojs_journals": limit}},
        )
        db.session.add_all((workspace, membership, subscription))
        db.session.commit()
        return subscription

    def register(self, owner: User, subscription: WorkspaceSubscription, *, endpoint: str = "https://journal.example.test/oai") -> OjsJournal:
        return register_ojs_journal(
            owner,
            workspace_subscription_id=subscription.id,
            public_url=endpoint.removesuffix("/oai") + "/index",
            oai_endpoint_url=endpoint,
            project_name=f"Projeto {endpoint}",
            client_factory=lambda value: _StaticClient(value),
            endpoint_validator=_validator,
        )

    def allocations(self, subscription: WorkspaceSubscription) -> list[WorkspaceSubscriptionOjsJournal]:
        return list(db.session.scalars(select(WorkspaceSubscriptionOjsJournal).where(
            WorkspaceSubscriptionOjsJournal.workspace_subscription_id == subscription.id
        )))

    def test_eligible_subscription_reserves_project_journal_and_capacity_atomically(self):
        subscription = self.subscription_for(self.owner)
        journal = self.register(self.owner, subscription)
        allocation = self.allocations(subscription)[0]
        self.assertEqual("ojs_seo", journal.analytics_project.module_key)
        self.assertEqual((subscription.id, journal.id, "active"), (
            allocation.workspace_subscription_id, allocation.ojs_journal_id, allocation.status,
        ))
        self.assertEqual(canonical_ojs_journal_identity(journal.oai_endpoint_url), allocation.canonical_identity_hash)

    def test_no_capacity_expired_subscription_and_missing_owner_membership_are_rejected(self):
        no_capacity = self.subscription_for(self.owner, limit=0)
        with self.assertRaises(OjsSeoQuotaError):
            self.register(self.owner, no_capacity)

        expired = self.subscription_for(self.other)
        expired.status = "expired"
        expired.ended_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        with self.assertRaises(OjsSeoSubscriptionError):
            self.register(self.other, expired, endpoint="https://expired.example.test/oai")

        foreign_subscription = self.subscription_for(self.other)
        with self.assertRaises(OjsSeoAuthorizationError):
            self.register(self.owner, foreign_subscription, endpoint="https://foreign.example.test/oai")

    def test_member_or_unrelated_workspace_cannot_consume_subscription_capacity(self):
        subscription = self.subscription_for(self.owner)
        member_workspace = db.session.get(Workspace, subscription.workspace_id)
        db.session.add(WorkspaceMembership(
            workspace=member_workspace, user=self.other, role="member", status="active"
        ))
        db.session.commit()
        with self.assertRaises(OjsSeoAuthorizationError):
            self.register(self.other, subscription)

        other_subscription = self.subscription_for(self.other)
        with self.assertRaises(OjsSeoAuthorizationError):
            self.register(self.owner, other_subscription, endpoint="https://other-workspace.example.test/oai")

    def test_subscription_limit_adjustment_increases_effective_capacity_without_new_logic(self):
        subscription = self.subscription_for(self.owner, limit=1)
        self.register(self.owner, subscription, endpoint="https://one.example.test/oai")
        with self.assertRaises(OjsSeoQuotaError):
            self.register(self.owner, subscription, endpoint="https://two.example.test/oai")
        db.session.add(WorkspaceSubscriptionLimitAdjustment(
            workspace_subscription_id=subscription.id,
            resource_key="ojs_journals",
            additional_units=1,
            status="active",
            starts_at=utcnow(),
            justification="teste isolado",
        ))
        db.session.commit()
        self.register(self.owner, subscription, endpoint="https://two.example.test/oai")
        self.assertEqual(2, len(self.allocations(subscription)))

    def test_valid_trial_grant_uses_the_same_subscription_entitlement_checks(self):
        replace_grant(
            self.owner,
            "marketing_sites",
            "commercial_trial",
            "trial",
            expires_at=utcnow() + timedelta(days=1),
        )
        db.session.commit()
        subscription = self.subscription_for(self.owner)
        journal = self.register(self.owner, subscription)
        self.assertIsNotNone(journal.id)

    def test_duplicate_identity_same_subscription_is_rejected_but_another_customer_can_register_it(self):
        first = self.subscription_for(self.owner)
        self.register(self.owner, first)
        with self.assertRaises(OjsSeoQuotaError) as error:
            self.register(self.owner, first)
        self.assertEqual("ojs_journal_already_allocated", error.exception.code)

        second = self.subscription_for(self.other)
        journal = self.register(self.other, second)
        self.assertEqual("https://journal.example.test/oai", journal.oai_endpoint_url)

    def test_release_preserves_history_and_quota_reduction_does_not_delete_existing_journals(self):
        subscription = self.subscription_for(self.owner, limit=2)
        first = self.register(self.owner, subscription, endpoint="https://one.example.test/oai")
        second = self.register(self.owner, subscription, endpoint="https://two.example.test/oai")
        subscription.terms_json = {"plan_limits": {"instagram_accounts": 0, "ojs_journals": 1}}
        db.session.commit()
        with self.assertRaises(OjsSeoQuotaError):
            self.register(self.owner, subscription, endpoint="https://three.example.test/oai")
        self.assertEqual(2, len(self.allocations(subscription)))
        first_allocation = next(item for item in self.allocations(subscription) if item.ojs_journal_id == first.id)
        released = release_ojs_journal_allocation(self.owner, first_allocation.id)
        self.assertEqual("released", released.status)
        self.assertIsNotNone(released.released_at)
        self.assertIsNotNone(db.session.get(OjsJournal, second.id))
        # A redução não apaga reservas existentes nem libera capacidade enquanto
        # uma reserva ativa ainda ocupa o único limite remanescente.
        with self.assertRaises(OjsSeoQuotaError):
            self.register(self.owner, subscription, endpoint="https://three.example.test/oai")
        second_allocation = next(item for item in self.allocations(subscription) if item.ojs_journal_id == second.id)
        release_ojs_journal_allocation(self.owner, second_allocation.id)
        replacement = self.register(self.owner, subscription, endpoint="https://three.example.test/oai")
        self.assertIsNotNone(replacement.id)
        self.assertEqual(3, len(self.allocations(subscription)))

    def test_active_allocation_prevents_endpoint_mutation_and_ineligible_subscription_blocks_new_audit(self):
        subscription = self.subscription_for(self.owner)
        journal = self.register(self.owner, subscription)
        journal.oai_endpoint_url = "https://changed.example.test/oai"
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        subscription.status = "canceled"
        subscription.ended_at = utcnow()
        db.session.commit()
        with self.assertRaises(OjsSeoSubscriptionError):
            request_ojs_seo_audit(self.owner, journal.id)
        allocation = self.allocations(subscription)[0]
        self.assertEqual("released", release_ojs_journal_allocation(self.owner, allocation.id).status)

    def test_tool_inactive_still_blocks_registration_even_when_subscription_exists(self):
        subscription = self.subscription_for(self.owner)
        tool = db.session.get(Tool, "ojs_seo")
        tool.active = False
        db.session.commit()
        with self.assertRaises(OjsSeoAuthorizationError):
            self.register(self.owner, subscription)

    def test_reservation_failure_and_post_reservation_failure_rollback_everything(self):
        subscription = self.subscription_for(self.owner)
        with patch("platform_core.ojs_seo_services._reserve_journal_in_transaction", side_effect=RuntimeError("falha")):
            with self.assertRaisesRegex(RuntimeError, "falha"):
                self.register(self.owner, subscription)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(AnalyticsProject)))
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsJournal)))
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(WorkspaceSubscriptionOjsJournal)))

        with patch("platform_core.ojs_seo_services.record_audit", side_effect=RuntimeError("auditoria")):
            with self.assertRaisesRegex(RuntimeError, "auditoria"):
                self.register(self.owner, subscription)
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(AnalyticsProject)))
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(OjsJournal)))
        self.assertEqual(0, db.session.scalar(select(func.count()).select_from(WorkspaceSubscriptionOjsJournal)))

    def test_partial_unique_constraints_preserve_history_but_block_two_active_allocations(self):
        subscription = self.subscription_for(self.owner, limit=2)
        journal = self.register(self.owner, subscription)
        current = self.allocations(subscription)[0]
        duplicate = WorkspaceSubscriptionOjsJournal(
            workspace_subscription_id=subscription.id,
            ojs_journal_id=journal.id,
            canonical_identity_hash="b" * 64,
            canonical_endpoint_url="https://other.example.test/oai",
            status="active",
        )
        db.session.add(duplicate)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        release_ojs_journal_allocation(self.owner, current.id)
        historical = WorkspaceSubscriptionOjsJournal(
            workspace_subscription_id=subscription.id,
            ojs_journal_id=journal.id,
            canonical_identity_hash="b" * 64,
            canonical_endpoint_url="https://other.example.test/oai",
            status="released",
            released_at=utcnow(),
        )
        db.session.add(historical)
        db.session.commit()
        self.assertEqual(2, len(self.allocations(subscription)))

    def test_two_processes_cannot_overrun_the_last_sqlite_capacity(self):
        subscription = self.subscription_for(self.owner, limit=1)
        # Libera qualquer leitura local antes da disputa entre processos.
        db.session.rollback()
        ctx = get_context("spawn")
        start = ctx.Event()
        result = ctx.Queue()
        arguments = (self.database_uri, self.directory, self.owner.id, subscription.id)
        first = ctx.Process(target=_register_in_process, args=(*arguments, "https://race-one.example.test/oai", start, result))
        second = ctx.Process(target=_register_in_process, args=(*arguments, "https://race-two.example.test/oai", start, result))
        first.start()
        second.start()
        start.set()
        first.join(45)
        second.join(45)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        outcomes = [result.get(timeout=5), result.get(timeout=5)]
        self.assertEqual(1, sum(status == "ok" for status, _detail in outcomes))
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(WorkspaceSubscriptionOjsJournal).where(
            WorkspaceSubscriptionOjsJournal.workspace_subscription_id == subscription.id,
            WorkspaceSubscriptionOjsJournal.status == "active",
        )))
        self.assertTrue(any(status == "error" for status, _detail in outcomes))


class OjsSeoSubscriptionAllocationMigrationTests(unittest.TestCase):
    def _app(self, root: str):
        database = Path(root) / "ojs-allocation-migration.sqlite3"
        return create_app({
            "TESTING": True,
            "SECRET_KEY": "ojs-allocation-migration-test-only",
            "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-allocation-migration-test-secret-with-adequate-length",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "PLATFORM_DATA_DIR": root,
        })

    def test_upgrade_and_downgrade_only_add_and_remove_the_allocation_schema(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-allocation-migration-", dir=TEST_TEMP_ROOT) as root:
            app = self._app(root)
            with app.app_context():
                upgrade(directory=MIGRATIONS, revision=PREVIOUS_REVISION)
                before = {
                    table: tuple(column["name"] for column in inspect(db.engine).get_columns(table))
                    for table in ("workspace_subscriptions", "ojs_journals", "analytics_projects")
                }
                try:
                    upgrade(directory=MIGRATIONS, revision=REVISION)
                    db.session.remove()
                    inspector = inspect(db.engine)
                    self.assertIn(ALLOCATION_TABLE, inspector.get_table_names())
                    self.assertEqual(before, {
                        table: tuple(column["name"] for column in inspect(db.engine).get_columns(table))
                        for table in before
                    })
                    index_names = {item["name"] for item in inspector.get_indexes(ALLOCATION_TABLE)}
                    self.assertIn("uq_workspace_subscription_ojs_journal_active_journal", index_names)
                    self.assertIn("uq_workspace_subscription_ojs_journal_active_identity", index_names)
                    with db.engine.connect() as connection:
                        self.assertEqual("ok", connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())
                        self.assertEqual([], connection.exec_driver_sql("PRAGMA foreign_key_check").all())
                    downgrade(directory=MIGRATIONS, revision=PREVIOUS_REVISION)
                    db.session.remove()
                    self.assertNotIn(ALLOCATION_TABLE, inspect(db.engine).get_table_names())
                finally:
                    db.session.remove()
                    db.engine.dispose()

    def test_chain_has_one_head(self):
        config = Config()
        config.set_main_option("script_location", MIGRATIONS)
        self.assertEqual([CURRENT_OJS_HEAD], ScriptDirectory.from_config(config).get_heads())


if __name__ == "__main__":
    unittest.main()
