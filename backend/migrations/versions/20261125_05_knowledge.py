"""Approved knowledge sources and vector chunks."""
from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa

revision = "20261125_05"
down_revision = "20261125_04"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table("knowledge_sources", sa.Column("id", sa.String(36), primary_key=True), sa.Column("family_id", sa.String(36), nullable=False), sa.Column("previous_id", sa.String(36), sa.ForeignKey("knowledge_sources.id")), sa.Column("title", sa.String(300), nullable=False), sa.Column("publisher", sa.String(200), nullable=False), sa.Column("provenance", sa.String(1000), nullable=False), sa.Column("publication_date", sa.Date(), nullable=False), sa.Column("specialty", sa.String(100), nullable=False), sa.Column("version", sa.String(80), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("index_status", sa.String(20), nullable=False), sa.Column("embedding_model", sa.String(200)), sa.Column("effective_at", sa.Float(), nullable=False), sa.Column("retire_at", sa.Float()), sa.Column("reviewer_id", sa.String(36), sa.ForeignKey("users.id")), sa.Column("reviewed_at", sa.Float()), sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("created_at", sa.Float(), nullable=False), sa.UniqueConstraint("family_id", "version"), sa.CheckConstraint("status IN ('draft', 'approved', 'retired')"), sa.CheckConstraint("index_status IN ('not_indexed', 'ready')"))
    for field in ("family_id", "specialty", "status"):
        op.create_index(f"ix_knowledge_sources_{field}", "knowledge_sources", [field])
    op.create_index("uq_knowledge_family_approved", "knowledge_sources", ["family_id"], unique=True, postgresql_where=sa.text("status = 'approved'"), sqlite_where=sa.text("status = 'approved'"))
    op.create_table("knowledge_chunks", sa.Column("id", sa.String(36), primary_key=True), sa.Column("source_id", sa.String(36), sa.ForeignKey("knowledge_sources.id"), nullable=False), sa.Column("ordinal", sa.Integer(), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("embedding", Vector(384).with_variant(sa.JSON(), "sqlite"), nullable=False), sa.UniqueConstraint("source_id", "ordinal"))
    op.create_index("ix_knowledge_chunks_source_id", "knowledge_chunks", ["source_id"])


def downgrade():
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_sources")
