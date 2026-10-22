"""CPF mascarado nos períodos de teste, sem tocar no banco local."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import g
from flask_migrate import upgrade
from sqlalchemy import select, text

from app import create_app
from platform_helpers import create_user, isolated_platform, login
from platform_core.commercial_trial_services import (
    activate_commercial_trial,
    all_commercial_trials,
    mask_cpf,
    normalize_cpf,
    trial_cpf_mask_display,
)
from platform_core.extensions import db
from platform_core.models import AuditLog, CommercialTrial, User, utcnow


VALID_CPF = "529.982.247-25"
EXPECTED_MASK = "*** 982.247 **"


class CommercialTrialMaskTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_platform()
        self.app = self.scope.__enter__()
        self.app.config["PESQUISAPDF_CPF_HMAC_SECRET"] = "trial-mask-test-secret-that-is-long-enough-123456"
        self.client = self.app.test_client()
        self.admin = create_user("Admin", "trial-mask-admin@example.org", role="admin")
        self.pending = User(name="Pendente", email="trial-mask-pending@example.org", role="user", status="active")
        self.pending.set_password("senha-de-teste-segura-123")
        db.session.add(self.pending)
        db.session.commit()
        login(self.client, self.admin.email)

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def test_mask_is_minimized_persisted_and_admin_only(self):
        self.assertEqual(mask_cpf(normalize_cpf(VALID_CPF)), EXPECTED_MASK)

        trial = activate_commercial_trial(self.admin, self.pending, "research_quali", VALID_CPF)
        db.session.commit()
        self.assertEqual(trial.cpf_masked, EXPECTED_MASK)
        self.assertEqual(trial_cpf_mask_display(trial), EXPECTED_MASK)

        row = db.session.execute(text(
            "SELECT cpf_masked, cpf_fingerprint FROM commercial_trials WHERE id = :id"
        ), {"id": trial.id}).one()
        raw_digits = normalize_cpf(VALID_CPF)
        self.assertEqual(row.cpf_masked, EXPECTED_MASK)
        self.assertEqual(len(row.cpf_fingerprint), 64)
        self.assertNotIn(raw_digits, row.cpf_fingerprint)
        self.assertNotIn(raw_digits, row.cpf_masked)

        audit_payload = str(db.session.scalar(select(AuditLog.after_json).where(
            AuditLog.action == "commercial_trial_activated", AuditLog.target_id == trial.id
        )))
        self.assertNotIn(raw_digits, audit_payload)
        self.assertNotIn(VALID_CPF, audit_payload)

        response = self.client.get(f"/admin/usuarios/{self.pending.id}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"CPF: {EXPECTED_MASK}", html)
        self.assertNotIn(VALID_CPF, html)
        self.assertNotIn(raw_digits, html)

        user_client = self.app.test_client()
        # O teste mantém uma sessão independente da do administrador para
        # verificar que a representação minimizada não sai da área admin.
        with user_client.session_transaction() as session:
            session["_user_id"] = self.pending.id
            session["_fresh"] = True
        # ``isolated_platform`` mantém o contexto da aplicação durante o
        # fixture; descartamos o usuário carregado para a sessão do admin
        # antes de atender a nova requisição independente.
        g.pop("_login_user", None)
        self.assertEqual(user_client.get("/admin/periodos-de-teste").status_code, 403)

    def test_form_reuses_aligned_limit_layout_without_auxiliary_text(self):
        html = self.client.get(f"/admin/usuarios/{self.pending.id}").get_data(as_text=True)
        stylesheet = (Path(__file__).resolve().parents[1] / "static" / "css" / "style.css").read_text(
            encoding="utf-8"
        )

        self.assertIn('class="platform-commercial-limit-form"', html)
        self.assertIn('class="platform-commercial-limit-fields platform-commercial-trial-fields"', html)
        self.assertIn('class="platform-commercial-limit-field platform-commercial-trial-action"', html)
        self.assertNotIn("O número é validado e não é armazenado em texto.", html)
        self.assertNotIn("trial-cpf-help", html)
        self.assertIn(".platform-commercial-limit-field .form-control, .platform-commercial-limit-field .form-select { width: 100%; min-height: 2.375rem; }", stylesheet)
        self.assertIn(".platform-commercial-trial-fields { grid-template-columns: repeat(2, minmax(0, 1fr)) auto; }", stylesheet)
        self.assertIn(".platform-commercial-trial-action .btn { min-height: 2.375rem; white-space: nowrap; }", stylesheet)

    def test_legacy_maskless_record_remains_readable_as_unavailable(self):
        legacy = User(name="Histórico", email="trial-mask-legacy@example.org", role="user", status="active")
        legacy.set_password("senha-de-teste-segura-123")
        db.session.add(legacy)
        db.session.flush()
        started = utcnow()
        db.session.add(CommercialTrial(
            cpf_fingerprint="f" * 64,
            cpf_key_version="v1",
            cpf_masked=None,
            commercial_group_id="management",
            user_id=legacy.id,
            plan_id="marketing_instagram",
            started_at=started,
            expires_at=started + timedelta(days=7),
            activated_by_id=self.admin.id,
            activated_by_name_snapshot=self.admin.name,
        ))
        db.session.commit()

        html = self.client.get(f"/admin/usuarios/{legacy.id}").get_data(as_text=True)
        self.assertIn("CPF: Não disponível", html)
        self.assertNotIn(VALID_CPF, html)


class CommercialTrialMaskMigrationTests(unittest.TestCase):
    revision = "1b2c3d4e5f60"
    previous_revision = "0a1b2c3d4e5f"
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")

    def _app(self, root: str):
        database = Path(root) / "commercial-trial-mask.sqlite3"
        return create_app({
            "TESTING": True,
            "SECRET_KEY": "commercial-trial-mask-migration-test-only",
            "PESQUISAPDF_CPF_HMAC_SECRET": "commercial-trial-mask-migration-secret-0123456789",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "PLATFORM_DATA_DIR": root,
        })

    def test_upgrade_preserves_existing_trials_with_null_mask_and_single_head(self):
        with tempfile.TemporaryDirectory(prefix="pesquisapdf-commercial-trial-mask-") as root:
            app = self._app(root)
            with app.app_context():
                upgrade(directory=self.migrations, revision=self.previous_revision)
                now = utcnow()
                db.session.execute(text("""
                    INSERT INTO commercial_trials (
                        id, cpf_fingerprint, cpf_key_version, commercial_group_id,
                        user_id, plan_id, workspace_id, workspace_subscription_id,
                        started_at, expires_at, activated_by_id,
                        activated_by_name_snapshot, created_at
                    ) VALUES (
                        :id, :fingerprint, :key_version, :group_id,
                        NULL, :plan_id, NULL, NULL, :started_at, :expires_at,
                        NULL, NULL, :created_at
                    )
                """), {
                    "id": "legacy-maskless-trial",
                    "fingerprint": "a" * 64,
                    "key_version": "v1",
                    "group_id": "research",
                    "plan_id": "research_quali",
                    "started_at": now.isoformat(),
                    "expires_at": (now + timedelta(days=7)).isoformat(),
                    "created_at": now.isoformat(),
                })
                db.session.commit()

                # O código novo precisa continuar exibindo o histórico quando
                # ainda estiver diante de um banco na revisão anterior.
                legacy_before_upgrade = all_commercial_trials()
                self.assertEqual(len(legacy_before_upgrade), 1)
                self.assertEqual(trial_cpf_mask_display(legacy_before_upgrade[0]), "Não disponível")

                upgrade(directory=self.migrations, revision=self.revision)
                db.session.remove()
                with db.engine.connect() as connection:
                    columns = {
                        row[1] for row in connection.exec_driver_sql("PRAGMA table_info(commercial_trials)")
                    }
                    legacy = connection.exec_driver_sql(
                        "SELECT cpf_masked, cpf_fingerprint FROM commercial_trials WHERE id = 'legacy-maskless-trial'"
                    ).one()
                    self.assertIn("cpf_masked", columns)
                    self.assertIsNone(legacy[0])
                    self.assertEqual(legacy[1], "a" * 64)
                    self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                    self.assertEqual(
                        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                        self.revision,
                    )

                config = Config()
                config.set_main_option("script_location", self.migrations)
                self.assertEqual(ScriptDirectory.from_config(config).get_heads(), [self.revision])
                db.engine.dispose()
