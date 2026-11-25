"""Fence worker attempts and track their start times."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_19"
down_revision = "20261125_18"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("extraction_jobs", "knowledge_imports"):
        if table == "knowledge_imports":
            op.add_column(table, sa.Column("started_at", sa.Float(), nullable=True))
        op.add_column(table, sa.Column("claim_token", sa.String(36), nullable=True))
        op.add_column(table, sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
        op.execute(sa.text(f"UPDATE {table} SET started_at = COALESCE(started_at, created_at), attempt_count = 1 WHERE status = 'running'"))


def downgrade():
    for table in ("extraction_jobs", "knowledge_imports"):
        op.drop_column(table, "attempt_count")
        op.drop_column(table, "claim_token")
        if table == "knowledge_imports":
            op.drop_column(table, "started_at")
