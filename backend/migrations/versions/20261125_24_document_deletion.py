"""Track patient-requested deletion and durable local file removal."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_24"
down_revision = "20261125_23"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("deleted_at", sa.Float()))
    op.add_column("documents", sa.Column("purge_status", sa.String(20), nullable=False, server_default="not_requested"))
    op.add_column("documents", sa.Column("purge_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("documents", sa.Column("purge_after", sa.Float()))
    op.add_column("documents", sa.Column("purged_at", sa.Float()))
    op.create_index("ix_documents_purge_due", "documents", ["purge_status", "purge_after"])


def downgrade():
    op.drop_index("ix_documents_purge_due", table_name="documents")
    for field in ("purged_at", "purge_after", "purge_attempts", "purge_status", "deleted_at"):
        op.drop_column("documents", field)
