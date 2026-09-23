"""Preserva o estado operacional anterior à exclusão lógica do usuário."""

from alembic import op
import sqlalchemy as sa

revision = "2a6d1c9e4f70"
down_revision = "0d7a4e21c903"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("status_before_deletion", sa.String(length=16), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("status_before_deletion")
