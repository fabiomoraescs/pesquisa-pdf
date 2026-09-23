"""Preserva a autoria de auditoria e concessões após exclusão de administradores.

Revision ID: e6c8b5f42a10
Revises: b49c62e8a113
"""

from alembic import op
import sqlalchemy as sa


revision = "e6c8b5f42a10"
down_revision = "b49c62e8a113"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("audit_logs", sa.Column("admin_name_snapshot", sa.String(160), nullable=True))
    op.add_column("access_grants", sa.Column("granted_by_name_snapshot", sa.String(160), nullable=True))

    users = sa.table("users", sa.column("id", sa.String(36)), sa.column("name", sa.String(160)))
    audit = sa.table("audit_logs", sa.column("admin_user_id", sa.String(36)),
                     sa.column("admin_name_snapshot", sa.String(160)))
    grants = sa.table("access_grants", sa.column("granted_by_id", sa.String(36)),
                      sa.column("granted_by_name_snapshot", sa.String(160)))
    connection = op.get_bind()
    connection.execute(
        sa.update(audit).where(audit.c.admin_user_id.is_not(None)).values(
            admin_name_snapshot=sa.select(users.c.name).where(
                users.c.id == audit.c.admin_user_id).scalar_subquery()
        )
    )
    connection.execute(
        sa.update(grants).where(grants.c.granted_by_id.is_not(None)).values(
            granted_by_name_snapshot=sa.select(users.c.name).where(
                users.c.id == grants.c.granted_by_id).scalar_subquery()
        )
    )


def downgrade():
    with op.batch_alter_table("access_grants") as batch_op:
        batch_op.drop_column("granted_by_name_snapshot")
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.drop_column("admin_name_snapshot")
