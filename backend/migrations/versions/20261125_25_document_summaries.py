"""Store unverified, source-cited document summaries and retry claims."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_25"
down_revision = "20261125_24"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("document_summaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("extraction_jobs.id"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False), sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False), sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_document_summaries_document_id", "document_summaries", ["document_id"])
    op.create_table("summary_generations",
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("extraction_jobs.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("claim_token", sa.String(36)),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("summary_id", sa.String(36), sa.ForeignKey("document_summaries.id"), unique=True),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')"))


def downgrade():
    op.drop_table("summary_generations")
    op.drop_index("ix_document_summaries_document_id", table_name="document_summaries")
    op.drop_table("document_summaries")
