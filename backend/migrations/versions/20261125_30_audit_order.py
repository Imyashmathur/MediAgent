"""Index audit events for chronological keyset pagination."""
from alembic import op

revision = "20261125_30"
down_revision = "20261125_29"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_audit_events_created_id", "audit_events", ["created_at", "id"])


def downgrade():
    op.drop_index("ix_audit_events_created_id", table_name="audit_events")
