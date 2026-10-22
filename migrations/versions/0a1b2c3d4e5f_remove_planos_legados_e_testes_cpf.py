"""Remove planos legados autorizados e registra testes por CPF protegido.

Revision ID: 0a1b2c3d4e5f
Revises: f9a0b1c2d3e4

Os quatro planos anteriores e os vínculos exclusivamente associados a eles
foram autorizados para exclusão definitiva em ambiente de desenvolvimento.
O downgrade é bloqueado: recriar IDs não recuperaria grants, assinaturas ou
políticas removidas.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0a1b2c3d4e5f"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


_LEGACY_PLAN_IDS = ("student", "researcher", "pro", "institutional")


def _legacy_sql_values() -> str:
    return ", ".join(f"'{plan_id}'" for plan_id in _LEGACY_PLAN_IDS)


def _assert_commercial_catalog(bind) -> None:
    expected = {
        "research_quali", "research_quanti", "research_complete",
        "marketing_instagram", "marketing_sites", "marketing_complete", "integral",
    }
    rows = bind.execute(sa.text(
        "SELECT id FROM plans WHERE id IN ("
        + ", ".join(f"'{plan_id}'" for plan_id in sorted(expected)) + ")"
    )).scalars()
    missing = expected - set(rows)
    if missing:
        raise RuntimeError(
            "A remoção dos planos legados exige os sete planos comerciais; ausentes: "
            + ", ".join(sorted(missing))
        )


def upgrade() -> None:
    bind = op.get_bind()
    _assert_commercial_catalog(bind)
    plan_ids = _legacy_sql_values()

    # Ordem explícita filho → pai para funcionar também quando SQLite mantém
    # ``foreign_keys`` ativo. Nenhuma tabela de projetos ou Analytics é tocada.
    bind.execute(sa.text(
        "DELETE FROM workspace_subscription_limit_adjustments WHERE workspace_subscription_id IN "
        f"(SELECT id FROM workspace_subscriptions WHERE plan_id IN ({plan_ids}))"
    ))
    bind.execute(sa.text(
        f"DELETE FROM workspace_subscriptions WHERE plan_id IN ({plan_ids})"
    ))
    bind.execute(sa.text(f"DELETE FROM access_grants WHERE plan_id IN ({plan_ids})"))
    bind.execute(sa.text(f"DELETE FROM plan_limit_policies WHERE plan_id IN ({plan_ids})"))
    bind.execute(sa.text(f"DELETE FROM plan_tools WHERE plan_id IN ({plan_ids})"))
    bind.execute(sa.text(
        "DELETE FROM audit_logs WHERE "
        f"(target_type = 'plan' AND target_id IN ({plan_ids})) OR "
        "(target_type = 'plan_tool' AND ("
        + " OR ".join(f"target_id LIKE '{plan_id}:%'" for plan_id in _LEGACY_PLAN_IDS)
        + "))"
    ))
    bind.execute(sa.text(f"DELETE FROM plans WHERE id IN ({plan_ids})"))

    op.create_table(
        "commercial_trials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cpf_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cpf_key_version", sa.String(length=32), nullable=False),
        sa.Column("commercial_group_id", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("plan_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("workspace_subscription_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_by_id", sa.String(length=36), nullable=True),
        sa.Column("activated_by_name_snapshot", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "commercial_group_id IN ('research', 'management', 'research_management')",
            name="ck_commercial_trial_group",
        ),
        sa.CheckConstraint("expires_at > started_at", name="ck_commercial_trial_period"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["workspace_subscription_id"], ["workspace_subscriptions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["activated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cpf_fingerprint", "commercial_group_id", name="uq_commercial_trial_cpf_group"),
    )
    op.create_index("ix_commercial_trials_user_id", "commercial_trials", ["user_id"])
    op.create_index("ix_commercial_trials_plan_id", "commercial_trials", ["plan_id"])
    op.create_index("ix_commercial_trials_workspace_id", "commercial_trials", ["workspace_id"])
    op.create_index(
        "ix_commercial_trials_workspace_subscription_id",
        "commercial_trials", ["workspace_subscription_id"],
    )
    op.create_index("ix_commercial_trials_activated_by_id", "commercial_trials", ["activated_by_id"])
    op.create_index(
        "ix_commercial_trials_group_expiration",
        "commercial_trials", ["commercial_group_id", "expires_at"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "A remoção dos planos legados e os períodos de teste são irreversíveis: "
        "o downgrade não recria concessões nem dados comerciais eliminados."
    )
