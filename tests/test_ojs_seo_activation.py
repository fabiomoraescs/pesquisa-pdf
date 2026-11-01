"""Provisionamento local controlado do OJS SEO, inteiramente offline."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from flask_migrate import upgrade
from sqlalchemy import func, select

from app import create_app
from platform_core.commercial_limit_services import effective_subscription_limits
from platform_core.extensions import db
from platform_core.models import AccessGrant, PlanTool, Tool, User, WorkspaceSubscription, utcnow
from platform_core.ojs_oai_client import (
    OaiCollectionResult,
    OaiMetadataFormat,
    OaiPmhAuditResult,
    OaiRepositoryIdentity,
    ValidatedOaiEndpoint,
)
from platform_core.ojs_seo_activation import (
    OJS_SEO_DEVELOPMENT_ACCESS_MODE,
    OJS_SEO_ELIGIBLE_PLAN_IDS,
    OjsSeoActivationError,
    activate_ojs_seo_tool,
    deactivate_ojs_seo_tool,
    prepare_ojs_seo_development_environment,
    provision_ojs_seo_tool,
    set_ojs_seo_navigation_visibility,
    verify_ojs_seo_development_access,
)
from platform_core.ojs_seo_services import OjsSeoQuotaError, list_ojs_seo_subscriptions_for_actor, register_ojs_journal
from platform_core.services import replace_grant, seed_platform


TEST_TEMP_ROOT = r"C:\Temp"
MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


class _StaticOaiClient:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def inspect(self) -> OaiPmhAuditResult:
        return OaiPmhAuditResult(
            endpoint=self.endpoint,
            status="completed",
            identity=OaiRepositoryIdentity(
                repository_name="Revista de teste", base_url=self.endpoint, protocol_version="2.0"
            ),
            metadata_formats=(OaiMetadataFormat("oai_dc", None, None),),
            collection=OaiCollectionResult(
                status="completed", completion_reason=None, pages_fetched=1,
                total_response_bytes=1, records=(), deleted_records=(),
                unique_identifiers=(), duplicate_identifiers=(),
            ),
        )


def _validator(endpoint: str) -> ValidatedOaiEndpoint:
    host = endpoint.split("/")[2]
    return ValidatedOaiEndpoint(
        canonical_url=endpoint, host=host, port=443, path="/oai", addresses=("93.184.216.34",)
    )


@contextmanager
def isolated_activation_platform():
    with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-activation-", dir=TEST_TEMP_ROOT) as directory:
        database = Path(directory) / "activation.sqlite3"
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "ojs-activation-test-only",
            "PESQUISAPDF_CPF_HMAC_SECRET": "ojs-activation-test-secret-with-adequate-length",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "PLATFORM_DATA_DIR": directory,
        })
        with app.app_context():
            upgrade(directory=MIGRATIONS, revision="head")
            seed_platform()
            yield app
            db.session.remove()
            db.engine.dispose()


def _user(name: str, email: str, *, role: str = "user") -> User:
    user = User(name=name, email=email, role=role, status="active")
    user.set_password("senha-de-teste-segura-123")
    db.session.add(user)
    db.session.flush()
    return user


class OjsSeoActivationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_activation_platform()
        self.app = self.scope.__enter__()
        self.admin = _user("Administração", "ojs-activation-admin@example.test", role="admin")
        self.user = _user("Conta de teste", "ojs-activation-user@example.test")
        self.other = _user("Outra conta", "ojs-activation-other@example.test")
        db.session.commit()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def _provision(self):
        result = provision_ojs_seo_tool(self.admin)
        db.session.commit()
        return result

    def _prepare(self):
        result = prepare_ojs_seo_development_environment(
            self.admin,
            test_user_id=self.user.id,
            workspace_label="Conta de validação",
            plan_id="marketing_sites",
        )
        db.session.commit()
        return result

    def test_provisioning_is_explicit_inactive_and_idempotent(self):
        result = self._provision()
        self.assertTrue(result.tool_created)
        self.assertEqual(OJS_SEO_ELIGIBLE_PLAN_IDS, result.plan_links_created)
        tool = db.session.get(Tool, "ojs_seo")
        self.assertFalse(tool.active)
        self.assertEqual(
            set(OJS_SEO_ELIGIBLE_PLAN_IDS),
            {
                row.plan_id for row in db.session.scalars(select(PlanTool).where(PlanTool.tool_id == "ojs_seo"))
            },
        )

        repeated = provision_ojs_seo_tool(self.admin)
        db.session.commit()
        self.assertFalse(repeated.tool_created)
        self.assertEqual((), repeated.plan_links_created)
        self.assertEqual(1, db.session.scalar(select(func.count()).select_from(Tool).where(Tool.id == "ojs_seo")))

    def test_development_subscription_is_dedicated_and_preserves_commercial_layers(self):
        self._provision()
        prepared = self._prepare()
        grant = db.session.get(AccessGrant, prepared.grant_id)
        self.assertEqual(OJS_SEO_DEVELOPMENT_ACCESS_MODE, grant.access_mode)
        subscription = db.session.get(WorkspaceSubscription, prepared.subscription_id)
        self.assertEqual("marketing_sites", subscription.plan_id)
        self.assertEqual(1, effective_subscription_limits(subscription)["resources"]["ojs_journals"]["effective_limit"])

        before_activation = verify_ojs_seo_development_access(self.admin, test_user_id=self.user.id)
        self.assertTrue(before_activation.tool_provisioned)
        self.assertFalse(before_activation.tool_active)
        self.assertFalse(before_activation.operational_access)
        self.assertEqual((subscription.id,), before_activation.eligible_subscription_ids)

        self.assertTrue(activate_ojs_seo_tool(self.admin))
        db.session.commit()
        after_activation = verify_ojs_seo_development_access(self.admin, test_user_id=self.user.id)
        self.assertTrue(after_activation.operational_access)
        self.assertFalse(activate_ojs_seo_tool(self.admin))
        self.assertTrue(deactivate_ojs_seo_tool(self.admin))
        db.session.commit()
        self.assertFalse(verify_ojs_seo_development_access(self.admin, test_user_id=self.user.id).operational_access)

    def test_visibility_is_independent_from_tool_activation_and_authorization(self):
        self._provision()
        self._prepare()
        self.assertTrue(activate_ojs_seo_tool(self.admin))
        self.assertTrue(set_ojs_seo_navigation_visibility(self.admin, scope_type="tool", visible=False))
        db.session.commit()
        self.assertFalse(verify_ojs_seo_development_access(self.admin, test_user_id=self.user.id).operational_access)
        self.assertTrue(set_ojs_seo_navigation_visibility(self.admin, scope_type="tool", visible=True))
        self.assertTrue(set_ojs_seo_navigation_visibility(self.admin, scope_type="group", visible=False))
        db.session.commit()
        self.assertFalse(verify_ojs_seo_development_access(self.admin, test_user_id=self.user.id).operational_access)

    def test_only_the_owner_of_the_development_workspace_sees_its_eligible_subscription(self):
        self._provision()
        self._prepare()
        self.assertTrue(activate_ojs_seo_tool(self.admin))
        replace_grant(self.other, "marketing_sites", "test", "active", self.admin)
        db.session.commit()
        self.assertEqual([], list_ojs_seo_subscriptions_for_actor(self.other))

    def test_expired_subscription_is_not_eligible(self):
        self._provision()
        prepared = self._prepare()
        self.assertTrue(activate_ojs_seo_tool(self.admin))
        subscription = db.session.get(WorkspaceSubscription, prepared.subscription_id)
        subscription.status = "expired"
        subscription.ended_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual([], list_ojs_seo_subscriptions_for_actor(self.user))

    def test_one_journal_capacity_is_applied_by_existing_ojs_reservation_service(self):
        self._provision()
        prepared = self._prepare()
        self.assertTrue(activate_ojs_seo_tool(self.admin))
        db.session.commit()
        first = register_ojs_journal(
            self.user,
            workspace_subscription_id=prepared.subscription_id,
            public_url="https://first.example.test/index",
            oai_endpoint_url="https://first.example.test/oai",
            project_name="Primeiro periódico",
            client_factory=_StaticOaiClient,
            endpoint_validator=_validator,
        )
        self.assertIsNotNone(first.id)
        with self.assertRaises(OjsSeoQuotaError):
            register_ojs_journal(
                self.user,
                workspace_subscription_id=prepared.subscription_id,
                public_url="https://second.example.test/index",
                oai_endpoint_url="https://second.example.test/oai",
                project_name="Segundo periódico",
                client_factory=_StaticOaiClient,
                endpoint_validator=_validator,
            )

    def test_activation_refuses_a_database_without_alembic_ojs_migrations(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-no-migration-", dir=TEST_TEMP_ROOT) as directory:
            database = Path(directory) / "unmigrated.sqlite3"
            app = create_app({
                "TESTING": True,
                "SECRET_KEY": "ojs-no-migration-test-only",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": directory,
            })
            with app.app_context():
                db.create_all()
                seed_platform()
                admin = _user("Sem migration", "ojs-no-migration-admin@example.test", role="admin")
                db.session.commit()
                provision_ojs_seo_tool(admin)
                with self.assertRaisesRegex(OjsSeoActivationError, "migrations OJS SEO"):
                    activate_ojs_seo_tool(admin)
                db.session.rollback()
                db.session.remove()
                db.drop_all()
                db.engine.dispose()

    def test_provisioning_fails_closed_outside_confirmed_development(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-ojs-not-local-", dir=TEST_TEMP_ROOT) as directory:
            database = Path(directory) / "not-local.sqlite3"
            app = create_app({
                "TESTING": False,
                "SECRET_KEY": "ojs-not-local-test-only",
                "PESQUISAPDF_LOCAL_DEVELOPMENT": False,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": directory,
            })
            with app.app_context():
                db.create_all()
                admin = _user("Fora local", "ojs-not-local-admin@example.test", role="admin")
                db.session.commit()
                with self.assertRaisesRegex(OjsSeoActivationError, "PESQUISAPDF_LOCAL_DEVELOPMENT"):
                    provision_ojs_seo_tool(admin)
                self.assertIsNone(db.session.get(Tool, "ojs_seo"))
                db.session.remove()
                db.drop_all()
                db.engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
