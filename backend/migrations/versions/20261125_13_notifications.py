"""Durable in-app notifications for care workflow events."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_13"
down_revision = "20261125_12"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("notifications", sa.Column("id", sa.String(36), primary_key=True), sa.Column("recipient_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("patient_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("relationship_id", sa.String(36), sa.ForeignKey("care_relationships.id")), sa.Column("required_scope", sa.String(40)), sa.Column("kind", sa.String(40), nullable=False), sa.Column("event_key", sa.String(100), nullable=False), sa.Column("resource_id", sa.String(36), nullable=False), sa.Column("created_at", sa.Float(), nullable=False), sa.Column("read_at", sa.Float()), sa.UniqueConstraint("recipient_id", "event_key"))
    op.create_index("ix_notifications_recipient_id", "notifications", ["recipient_id"])


def downgrade():
    op.drop_table("notifications")
