"""Persistência histórica da fundação Análysis Instagram."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from platform_helpers import create_user, isolated_platform
from platform_core.extensions import db
from platform_core.models import (
    AnalyticsProject,
    AnalyticsRun,
    InstagramAccount,
    InstagramAccountSnapshot,
    InstagramMedia,
    InstagramMediaObservation,
)


UTC = timezone.utc
PREVIOUS = "c3d9e8a7f2b4"
REVISION = "d4e8f0a1b2c3"
MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")


class InstagramPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.user = create_user()

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    @staticmethod
    def moment(day: int, hour: int = 0) -> datetime:
        return datetime(2026, 10, day, hour, tzinfo=UTC)

    def rejects(self, item):
        db.session.add(item)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def project_and_account(self) -> tuple[AnalyticsProject, InstagramAccount]:
        project = AnalyticsProject(owner_user_id=self.user.id, name="Instagram da Revista X")
        account = InstagramAccount(
            analytics_project=project,
            username="@RevistaX",
            username_normalized="revistax",
            display_name="Revista X",
        )
        db.session.add_all((project, account))
        db.session.commit()
        return project, account

    def create_run(self, project: AnalyticsProject, *, observed_day: int, source_hash: str = "a" * 64) -> AnalyticsRun:
        run = AnalyticsRun(
            analytics_project_id=project.id,
            source_kind="manual",
            status="completed",
            period_start=datetime(2026, 9, 1, tzinfo=UTC),
            period_end=datetime(2026, 9, 30, 23, 59, tzinfo=UTC),
            started_at=self.moment(observed_day, 14),
            completed_at=self.moment(observed_day, 14, ),
            processor_version="instagram-v1",
            source_hash=source_hash,
        )
        db.session.add(run)
        db.session.commit()
        return run

    def test_project_and_account_are_separate_from_pdf_models_and_are_one_to_one(self):
        project, account = self.project_and_account()
        self.assertEqual(project.module_key, "instagram")
        self.assertEqual(project.owner_user_id, self.user.id)
        self.assertEqual(account.analytics_project_id, project.id)
        self.assertEqual(account.username_normalized, "revistax")
        self.assertEqual(project.instagram_account.id, account.id)
        self.assertNotIn("delete", AnalyticsProject.runs.property.cascade)
        self.assertNotIn("delete-orphan", AnalyticsProject.runs.property.cascade)

        self.rejects(InstagramAccount(
            analytics_project_id=project.id,
            username="@Outra",
            username_normalized="outra",
        ))

    def test_runs_keep_period_distinct_from_observation_time_and_do_not_use_hash_as_identity(self):
        project, _account = self.project_and_account()
        first = self.create_run(project, observed_day=2)
        second = self.create_run(project, observed_day=10)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.source_hash, second.source_hash)
        self.assertEqual(first.period_end.date().isoformat(), "2026-09-30")
        self.assertEqual(first.started_at.date().isoformat(), "2026-10-02")

        self.rejects(AnalyticsRun(
            analytics_project_id=project.id,
            period_start=self.moment(10),
            period_end=self.moment(2),
        ))
        self.rejects(AnalyticsRun(analytics_project_id=project.id, record_count=-1))

    def test_media_and_account_observations_preserve_two_historical_snapshots(self):
        project, account = self.project_and_account()
        first_run = self.create_run(project, observed_day=2)
        second_run = self.create_run(project, observed_day=10)
        media = InstagramMedia(
            instagram_account_id=account.id,
            external_id="media-123",
            caption_original="Texto original da publicação.",
            published_at=datetime(2026, 9, 3, 12, tzinfo=UTC),
            media_type_raw="REEL",
            first_seen_at=self.moment(2, 14),
            last_seen_at=self.moment(10, 14),
        )
        db.session.add(media)
        db.session.flush()
        first_observation = InstagramMediaObservation(
            instagram_media_id=media.id,
            analytics_run_id=first_run.id,
            observed_at=self.moment(2, 14),
            period_start=first_run.period_start,
            period_end=first_run.period_end,
            reach=1_000,
            likes=50,
        )
        second_observation = InstagramMediaObservation(
            instagram_media_id=media.id,
            analytics_run_id=second_run.id,
            observed_at=self.moment(10, 14),
            period_start=second_run.period_start,
            period_end=second_run.period_end,
            reach=1_450,
            likes=73,
        )
        first_snapshot = InstagramAccountSnapshot(
            instagram_account_id=account.id,
            analytics_run_id=first_run.id,
            observed_at=self.moment(2, 14),
            followers_count=8_200,
        )
        second_snapshot = InstagramAccountSnapshot(
            instagram_account_id=account.id,
            analytics_run_id=second_run.id,
            observed_at=self.moment(10, 14),
            followers_count=8_530,
            new_followers=330,
        )
        db.session.add_all((first_observation, second_observation, first_snapshot, second_snapshot))
        db.session.commit()

        observations = db.session.scalars(
            db.select(InstagramMediaObservation)
            .where(InstagramMediaObservation.instagram_media_id == media.id)
            .order_by(InstagramMediaObservation.observed_at)
        ).all()
        snapshots = db.session.scalars(
            db.select(InstagramAccountSnapshot)
            .where(InstagramAccountSnapshot.instagram_account_id == account.id)
            .order_by(InstagramAccountSnapshot.observed_at)
        ).all()
        self.assertEqual([row.reach for row in observations], [1_000, 1_450])
        self.assertEqual([row.followers_count for row in snapshots], [8_200, 8_530])
        self.assertEqual(observations[0].period_end.date().isoformat(), "2026-09-30")
        self.assertEqual(observations[0].observed_at.date().isoformat(), "2026-10-02")
        self.assertEqual(media.caption_original, "Texto original da publicação.")

        self.rejects(InstagramMediaObservation(
            instagram_media_id=media.id,
            analytics_run_id=first_run.id,
            reach=1_100,
        ))
        self.rejects(InstagramAccountSnapshot(
            instagram_account_id=account.id,
            analytics_run_id=first_run.id,
            followers_count=8_201,
        ))

    def test_identity_and_metric_constraints_reject_ambiguous_or_negative_records(self):
        project, account = self.project_and_account()
        run = self.create_run(project, observed_day=2)
        media = InstagramMedia(instagram_account_id=account.id, source_key="dataset:row:1")
        db.session.add(media)
        db.session.commit()

        self.rejects(InstagramMedia(instagram_account_id=account.id))
        self.rejects(InstagramMedia(instagram_account_id=account.id, source_key="dataset:row:1"))
        self.rejects(InstagramMediaObservation(
            instagram_media_id=media.id,
            analytics_run_id=run.id,
            reach=-1,
        ))
        self.rejects(InstagramAccountSnapshot(
            instagram_account_id=account.id,
            analytics_run_id=run.id,
            followers_count=-1,
        ))


class InstagramPersistenceMigrationTests(unittest.TestCase):
    def test_upgrade_and_downgrade_only_manage_the_analytics_instagram_tables(self):
        from app import create_app

        with tempfile.TemporaryDirectory(prefix="pesquisapdf-instagram-migration-") as root:
            database = Path(root) / "platform.sqlite3"
            app = create_app({
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
                "PLATFORM_DATA_DIR": root,
                "SECRET_KEY": "instagram-migration-test-only",
            })
            with app.app_context():
                upgrade(directory=MIGRATIONS, revision=PREVIOUS)
                before = set(inspect(db.engine).get_table_names())
                self.assertNotIn("analytics_projects", before)

                upgrade(directory=MIGRATIONS, revision=REVISION)
                inspector = inspect(db.engine)
                expected = {
                    "analytics_projects", "analytics_runs", "instagram_accounts", "instagram_media",
                    "instagram_media_observations", "instagram_account_snapshots",
                }
                self.assertTrue(expected.issubset(set(inspector.get_table_names())))
                self.assertEqual(
                    {"owner_user_id", "module_key", "status"},
                    set(inspector.get_indexes("analytics_projects")[0]["column_names"]),
                )
                self.assertIn(
                    "uq_instagram_media_observation_run_media",
                    {item["name"] for item in inspector.get_unique_constraints("instagram_media_observations")},
                )

                downgrade(directory=MIGRATIONS, revision=PREVIOUS)
                remaining = set(inspect(db.engine).get_table_names())
                self.assertTrue(expected.isdisjoint(remaining))
                self.assertIn("users", remaining)
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
