"""Durable background knowledge indexing jobs."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_47"
down_revision = "20261125_46"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_index_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("knowledge_sources.id"), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("request_correlation_id", sa.String(36)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_dispatch_at", sa.Float(), nullable=False),
        sa.Column("claim_token", sa.String(36)), sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.Float(), nullable=False), sa.Column("started_at", sa.Float()),
        sa.Column("completed_at", sa.Float()),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')"))
    op.create_index("uq_knowledge_index_active", "knowledge_index_jobs", ["source_id"], unique=True, postgresql_where=sa.text("status IN ('queued', 'running')"), sqlite_where=sa.text("status IN ('queued', 'running')"))
    op.create_index("ix_knowledge_index_dispatch", "knowledge_index_jobs", ["status", "next_dispatch_at"])


def downgrade():
    op.drop_index("ix_knowledge_index_dispatch", table_name="knowledge_index_jobs")
    op.drop_index("uq_knowledge_index_active", table_name="knowledge_index_jobs")
    op.drop_table("knowledge_index_jobs")
