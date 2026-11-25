"""Fence and deduplicate AI-assisted note generation requests."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_23"
down_revision = "20261125_22"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("note_generations",
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("conversation_messages.id"), nullable=False),
        sa.Column("consultation_id", sa.String(36), sa.ForeignKey("consultations.id")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("claim_token", sa.String(36)),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("clinical_note_versions.id"), unique=True),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')"))


def downgrade():
    op.drop_table("note_generations")
