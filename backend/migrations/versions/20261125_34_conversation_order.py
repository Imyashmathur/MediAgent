"""Index assistant conversation history for keyset pagination."""
from alembic import op

revision = "20261125_34"
down_revision = "20261125_33"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_conversation_messages_conversation_created_id", "conversation_messages", ["conversation_id", "created_at", "id"])


def downgrade():
    op.drop_index("ix_conversation_messages_conversation_created_id", table_name="conversation_messages")
