"""Relationship-scoped patient and care team messages."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_11"
down_revision = "20261125_10"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("care_messages", sa.Column("id", sa.String(36), primary_key=True), sa.Column("relationship_id", sa.String(36), sa.ForeignKey("care_relationships.id"), nullable=False), sa.Column("sender_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_care_messages_relationship_id", "care_messages", ["relationship_id"])


def downgrade():
    op.drop_table("care_messages")
