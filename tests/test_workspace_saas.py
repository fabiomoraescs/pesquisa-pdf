"""Núcleo SaaS estrutural: workspaces não alteram a autorização legada."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from flask_migrate import downgrade, upgrade
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import create_app
from platform_helpers import create_user, isolated_platform
from platform_core.extensions import db
from platform_core.models import (
    AccessGrant,
    AnalyticsProject,
    AuditLog,
    Plan,
    PlanTool,
    Project,
    Tool,
    ToolVisibilitySetting,
    User,
    UserToolOverride,
    Workspace,
    WorkspaceMembership,
    WorkspaceSubscription,
)
from platform_core.services import can_use_tool, current_grant
from platform_core.workspace_services import (
    WorkspaceServiceError,
    add_workspace_member,
    create_individual_workspace,
    create_team_workspace,
    create_workspace_subscription,
    current_workspace_subscription,
    deactivate_workspace_membership,
    end_workspace_subscription,
    get_workspace_for_user,
    list_workspace_members,
    list_workspace_subscriptions,
    list_workspaces_for_user,
)


def _pending_user(name: str, email: str) -> User:
    user = User(name=name, email=email, role="user", status="active")
    user.set_password("senha-de-teste-segura-123")
    db.session.add(user)
    db.session.flush()
    return user


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_memberships_are_isolated_and_a_user_can_participate_in_multiple_workspaces(self):
        owner = create_user("Titular", "workspace-owner@example.org")
        member = _pending_user("Membro sem plano", "workspace-member@example.org")
        outsider = create_user("Terceiro", "workspace-outsider@example.org")
        self.assertEqual(list_workspaces_for_user(member), [])

        individual = create_individual_workspace(owner, "Área pessoal")
        team = create_team_workspace(owner, "Equipe de pesquisa")
        membership = add_workspace_member(owner, team.id, member)
        db.session.commit()

        self.assertEqual(individual.workspace_type, "individual")
        self.assertEqual(team.workspace_type, "team")
        self.assertEqual(membership.role, "member")
        self.assertEqual({item.id for item in list_workspaces_for_user(owner)}, {individual.id, team.id})
        self.assertEqual([item.id for item in list_workspaces_for_user(member)], [team.id])
        self.assertEqual({item.user_id for item in list_workspace_members(member, team.id)}, {owner.id, member.id})
        with self.assertRaises(WorkspaceServiceError):
            get_workspace_for_user(outsider, team.id)
        with self.assertRaises(WorkspaceServiceError):
            list_workspace_members(outsider, team.id)

        with self.assertRaises(WorkspaceServiceError):
            add_workspace_member(owner, team.id, member)
        duplicate = WorkspaceMembership(workspace_id=team.id, user_id=member.id, role="member", status="active")
        db.session.add(duplicate)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_membership_does_not_grant_operational_access_and_legacy_grant_is_preserved(self):
        owner = create_user("Titular", "workspace-grant-owner@example.org", plan="student")
        pending = _pending_user("Pendente", "workspace-grant-pending@example.org")
        visibility = ToolVisibilitySetting(scope_type="tool", scope_id="pdf_scraper", visible=False)
        db.session.add(visibility)
        db.session.commit()

        team = create_team_workspace(owner, "Equipe sem assinatura")
        add_workspace_member(owner, team.id, pending)
        db.session.commit()

        self.assertIsNone(current_grant(pending))
        self.assertFalse(can_use_tool(pending, "pdf_scraper"))
        self.assertTrue(can_use_tool(owner, "pdf_scraper"))
        self.assertEqual(db.session.get(ToolVisibilitySetting, visibility.id).visible, False)
        self.assertEqual(
            db.session.scalar(select(func.count()).select_from(AccessGrant).where(
                AccessGrant.user_id == owner.id, AccessGrant.status == "active"
            )),
            1,
        )

    def test_membership_history_preserves_owner_and_audits_only_effective_changes(self):
        owner = create_user("Titular", "workspace-history-owner@example.org")
        member = _pending_user("Membro", "workspace-history-member@example.org")
        workspace = create_team_workspace(owner, "Equipe histórica")
        add_workspace_member(owner, workspace.id, member)
        db.session.commit()

        ended = deactivate_workspace_membership(owner, workspace.id, member.id)
        db.session.commit()
        self.assertEqual(ended.status, "ended")
        self.assertIsNotNone(ended.ended_at)
        self.assertEqual([row.user_id for row in list_workspace_members(owner, workspace.id)], [owner.id])
        self.assertEqual(len(list_workspace_members(owner, workspace.id, include_ended=True)), 2)
        with self.assertRaises(WorkspaceServiceError):
            deactivate_workspace_membership(owner, workspace.id, owner.id)
        actions = set(db.session.scalars(select(AuditLog.action)).all())
        self.assertTrue({"workspace_created", "workspace_member_added", "workspace_member_ended"}.issubset(actions))

    def test_subscription_snapshots_terms_preserves_history_and_never_changes_access(self):
        owner = create_user("Titular", "workspace-subscription-owner@example.org", plan="student")
        administrator = _pending_user("Admin", "workspace-subscription-admin@example.org")
        administrator.role = "admin"
        workspace = create_individual_workspace(owner, "Assinatura individual")
        plan = db.session.get(Plan, "marketing_instagram")
        plan.limits_json = {"instagram_accounts": 1}
        db.session.commit()

        with self.assertRaises(WorkspaceServiceError):
            create_workspace_subscription(owner, workspace.id, "marketing_instagram")
        subscription = create_workspace_subscription(
            administrator,
            workspace.id,
            "marketing_instagram",
            terms={"additional_instagram_accounts": 2},
        )
        db.session.commit()
        self.assertEqual(subscription.terms_json["policy_version"], "plan-limits-v1")
        self.assertEqual(subscription.terms_json["plan_policy_version"], 1)
        self.assertIsNotNone(subscription.terms_json["plan_policy_id"])
        self.assertEqual(subscription.terms_json["plan_limits"], {
            "instagram_accounts": 1, "ojs_journals": 0,
        })
        self.assertEqual(subscription.terms_json["additional_limits"], {
            "additional_instagram_accounts": 2,
        })
        self.assertEqual(subscription.assigned_by_id, administrator.id)
        self.assertIs(current_workspace_subscription(workspace.id), subscription)
        self.assertFalse(can_use_tool(owner, "analytics_instagram"))
        plan.limits_json = {"instagram_accounts": 5}
        db.session.commit()
        self.assertEqual(subscription.terms_json["plan_limits"], {
            "instagram_accounts": 1, "ojs_journals": 0,
        })
        with self.assertRaises(WorkspaceServiceError):
            create_workspace_subscription(administrator, workspace.id, "research_quali")

        ended = end_workspace_subscription(administrator, workspace.id)
        db.session.commit()
        self.assertEqual(ended.status, "canceled")
        replacement = create_workspace_subscription(administrator, workspace.id, "research_quali")
        db.session.commit()
        self.assertEqual(current_workspace_subscription(workspace.id).id, replacement.id)
        self.assertEqual(len(list_workspace_subscriptions(owner, workspace.id)), 2)

        duplicate = WorkspaceSubscription(
            workspace_id=workspace.id,
            plan_id="research_quali",
            status="active",
            terms_json={},
        )
        db.session.add(duplicate)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_administrator_can_maintain_workspace_without_a_grant(self):
        admin = _pending_user("Admin", "workspace-admin@example.org")
        admin.role = "admin"
        owner = create_user("Titular", "workspace-admin-owner@example.org")
        workspace = create_team_workspace(owner, "Área administrável")
        db.session.commit()

        self.assertIsNone(current_grant(admin))
        self.assertEqual(get_workspace_for_user(admin, workspace.id).id, workspace.id)
        member = _pending_user("Membro", "workspace-admin-member@example.org")
        added = add_workspace_member(admin, workspace.id, member)
        db.session.commit()
        self.assertEqual(added.user_id, member.id)


class WorkspaceMigrationTests(unittest.TestCase):
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")
    previous_revision = "b5c6d7e8f90"
    commercial_revision = "c6d7e8f901a2"
    labels_revision = "d7e8f901a2b3"
    revision = "e8f901a2b3c4"

    def _app(self, root: str):
        database = Path(root) / "platform.sqlite3"
        return create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "PLATFORM_DATA_DIR": root,
            "SECRET_KEY": "workspace-saas-migration-test-only",
        })

    def _insert_legacy_records(self) -> tuple[str, str]:
        legacy = Plan(id="student", name="Estudante", description="Legado", active=True)
        tool = Tool(id="pdf_scraper", name="Raspagem por termos", route="/raspagem-livre", active=True)
        user = _pending_user("Legado", "workspace-migration@example.org")
        db.session.add_all((legacy, tool))
        db.session.flush()
        db.session.add_all((
            PlanTool(plan_id="student", tool_id="pdf_scraper"),
            AccessGrant(user_id=user.id, plan_id="student", access_mode="student_free", status="active"),
            UserToolOverride(user_id=user.id, tool_id="pdf_scraper", decision="deny"),
            Project(owner_user_id=user.id, name="Projeto legado", description="Preservar"),
            AnalyticsProject(
                owner_user_id=user.id,
                module_key="instagram",
                name="Projeto Instagram legado",
                description="Preservar",
            ),
        ))
        db.session.commit()
        return user.id, "student"

    def test_upgrade_from_each_predecessor_preserves_existing_domains_and_creates_workspace_schema(self):
        for source in (self.previous_revision, self.commercial_revision, self.labels_revision):
            with self.subTest(source=source), tempfile.TemporaryDirectory(
                prefix="pesquisapdf-workspace-migration-"
            ) as root:
                app = self._app(root)
                with app.app_context():
                    upgrade(directory=self.migrations, revision=source)
                    user_id, plan_id = self._insert_legacy_records()
                    upgrade(directory=self.migrations, revision=self.revision)
                    db.session.expire_all()

                    self.assertIsNotNone(db.session.get(User, user_id))
                    self.assertIsNotNone(db.session.get(Plan, plan_id))
                    self.assertEqual(current_grant(db.session.get(User, user_id)).plan_id, plan_id)
                    self.assertIsNotNone(db.session.get(UserToolOverride, (user_id, "pdf_scraper")))
                    self.assertEqual(db.session.scalar(select(func.count()).select_from(Workspace)), 0)
                    self.assertEqual(
                        db.session.scalar(select(func.count()).select_from(Project).where(
                            Project.owner_user_id == user_id
                        )),
                        1,
                    )
                    self.assertEqual(
                        db.session.scalar(select(func.count()).select_from(AnalyticsProject).where(
                            AnalyticsProject.owner_user_id == user_id
                        )),
                        1,
                    )
                    with db.engine.connect() as connection:
                        tables = {
                            row[0] for row in connection.exec_driver_sql(
                                "SELECT name FROM sqlite_master WHERE type = 'table'"
                            )
                        }
                        self.assertTrue({"workspaces", "workspace_memberships", "workspace_subscriptions"}.issubset(tables))
                        self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                        self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                        self.assertEqual(
                            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                            self.revision,
                        )

                    user = db.session.get(User, user_id)
                    workspace = create_individual_workspace(user, "Estrutura migrada")
                    administrator = _pending_user("Admin", f"workspace-admin-{source}@example.org")
                    administrator.role = "admin"
                    create_workspace_subscription(administrator, workspace.id, "student")
                    db.session.commit()
                    self.assertIsNotNone(current_workspace_subscription(workspace.id))
                    db.session.remove()
                    db.engine.dispose()

    def test_downgrade_to_labels_revision_removes_only_workspace_schema(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-workspace-downgrade-") as root:
            app = self._app(root)
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.labels_revision)
                user_id, _plan_id = self._insert_legacy_records()
                upgrade(directory=self.migrations, revision=self.revision)
                user = db.session.get(User, user_id)
                workspace = create_individual_workspace(user, "Estrutura temporária")
                db.session.commit()
                self.assertIsNotNone(db.session.get(type(workspace), workspace.id))

                downgrade(directory=self.migrations, revision=self.labels_revision)
                db.session.remove()
                with db.engine.connect() as connection:
                    tables = {
                        row[0] for row in connection.exec_driver_sql(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    self.assertFalse({"workspaces", "workspace_memberships", "workspace_subscriptions"} & tables)
                    self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                    self.assertEqual(
                        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                        self.labels_revision,
                    )
                self.assertIsNotNone(db.session.get(User, user_id))
                self.assertEqual(
                    db.session.scalar(select(func.count()).select_from(Project).where(Project.owner_user_id == user_id)),
                    1,
                )
                db.session.remove()
                db.engine.dispose()
