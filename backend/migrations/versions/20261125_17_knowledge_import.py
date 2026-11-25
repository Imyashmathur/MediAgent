"""Separate reference-file ingestion from patient documents."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_17"
down_revision = "20261125_16"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_imports", sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("filename", sa.String(160), nullable=False), sa.Column("media_type", sa.String(100), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("extracted_text", sa.Text()), sa.Column("error_code", sa.String(80)), sa.Column("created_at", sa.Float(), nullable=False), sa.Column("completed_at", sa.Float()), sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')"))
    op.create_index("ix_knowledge_imports_created_by", "knowledge_imports", ["created_by"])
    op.create_table("knowledge_source_imports", sa.Column("source_id", sa.String(36), sa.ForeignKey("knowledge_sources.id"), primary_key=True), sa.Column("import_id", sa.String(36), sa.ForeignKey("knowledge_imports.id"), nullable=False))
    op.create_index("ix_knowledge_source_imports_import_id", "knowledge_source_imports", ["import_id"])


def downgrade():
    op.drop_table("knowledge_source_imports")
    op.drop_table("knowledge_imports")
