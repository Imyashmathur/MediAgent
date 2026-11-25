"""Support durable removal of staged knowledge import files."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_36"
down_revision = "20261125_35"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_imports", sa.Column("deleted_at", sa.Float()))
    op.add_column("knowledge_imports", sa.Column("purge_status", sa.String(20), nullable=False, server_default="not_requested"))
    op.add_column("knowledge_imports", sa.Column("purge_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("knowledge_imports", sa.Column("purge_after", sa.Float()))
    op.add_column("knowledge_imports", sa.Column("purged_at", sa.Float()))
    op.create_index("ix_knowledge_import_purge_due", "knowledge_imports", ["purge_status", "purge_after"])


def downgrade():
    op.drop_index("ix_knowledge_import_purge_due", table_name="knowledge_imports")
    for field in ("purged_at", "purge_after", "purge_attempts", "purge_status", "deleted_at"):
        op.drop_column("knowledge_imports", field)
