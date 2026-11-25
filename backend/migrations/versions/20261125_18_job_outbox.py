"""Durable delivery of document and knowledge import jobs."""
import time
import uuid

from alembic import op
import sqlalchemy as sa

revision = "20261125_18"
down_revision = "20261125_17"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "job_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("published_at", sa.Float()),
        sa.Column("last_error", sa.String(80)),
        sa.CheckConstraint("kind IN ('document_processing', 'knowledge_import')"),
        sa.CheckConstraint("status IN ('pending', 'published', 'discarded')"),
        sa.UniqueConstraint("kind", "target_id"),
    )
    op.create_index("ix_job_outbox_due", "job_outbox", ["status", "next_attempt_at"])
    now = time.time()
    connection = op.get_bind()
    for kind, name in (("document_processing", "extraction_jobs"), ("knowledge_import", "knowledge_imports")):
        rows = connection.execute(sa.text(f"SELECT id FROM {name} WHERE status = 'queued' AND created_at >= :cutoff"), {"cutoff": now - 300})
        for row in rows:
            connection.execute(table.insert().values(id=str(uuid.uuid4()), kind=kind, target_id=row.id, status="pending", attempts=0, next_attempt_at=now, created_at=now))


def downgrade():
    op.drop_index("ix_job_outbox_due", table_name="job_outbox")
    op.drop_table("job_outbox")
