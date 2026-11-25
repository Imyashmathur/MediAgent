"""Add extraction jobs and page-level source text."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_03"
down_revision = "20261125_02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("extraction_jobs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False), sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("processor_version", sa.String(80), nullable=False), sa.Column("error_code", sa.String(80)), sa.Column("created_at", sa.Float(), nullable=False), sa.Column("started_at", sa.Float()), sa.Column("completed_at", sa.Float()), sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')"))
    op.create_index("ix_extraction_jobs_document_id", "extraction_jobs", ["document_id"])
    op.create_index("uq_document_active_extraction", "extraction_jobs", ["document_id"], unique=True, postgresql_where=sa.text("status IN ('queued', 'running')"), sqlite_where=sa.text("status IN ('queued', 'running')"))
    op.create_table("extracted_pages", sa.Column("id", sa.String(36), primary_key=True), sa.Column("job_id", sa.String(36), sa.ForeignKey("extraction_jobs.id"), nullable=False), sa.Column("page_number", sa.Integer(), nullable=False), sa.Column("method", sa.String(20), nullable=False), sa.Column("text", sa.Text(), nullable=False), sa.UniqueConstraint("job_id", "page_number"))
    op.create_index("ix_extracted_pages_job_id", "extracted_pages", ["job_id"])


def downgrade():
    op.drop_table("extracted_pages")
    op.drop_table("extraction_jobs")
