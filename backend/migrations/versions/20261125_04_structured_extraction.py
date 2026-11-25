"""Versioned structured transcriptions and clinician review."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_04"
down_revision = "20261125_03"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("structured_extraction_versions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("job_id", sa.String(36), sa.ForeignKey("extraction_jobs.id"), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("data", sa.JSON(), nullable=False), sa.Column("review_status", sa.String(20), nullable=False), sa.Column("reviewer_id", sa.String(36), sa.ForeignKey("users.id")), sa.Column("reviewed_at", sa.Float()), sa.Column("created_at", sa.Float(), nullable=False), sa.UniqueConstraint("job_id", "version"), sa.CheckConstraint("review_status IN ('unverified', 'reviewed')"))
    op.create_index("ix_structured_extraction_versions_job_id", "structured_extraction_versions", ["job_id"])


def downgrade():
    op.drop_table("structured_extraction_versions")
