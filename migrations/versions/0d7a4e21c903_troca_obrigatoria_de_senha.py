"""Registra troca de senha obrigatória após redefinição administrativa."""

from alembic import op
import sqlalchemy as sa

revision = "0d7a4e21c903"
down_revision = "e6c8b5f42a10"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("must_change_password")
