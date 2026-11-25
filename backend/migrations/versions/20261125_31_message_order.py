"""Index relationship message history for keyset pagination."""
from alembic import op

revision = "20261125_31"
down_revision = "20261125_30"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_care_messages_relationship_created_id", "care_messages", ["relationship_id", "created_at", "id"])


def downgrade():
    op.drop_index("ix_care_messages_relationship_created_id", table_name="care_messages")
