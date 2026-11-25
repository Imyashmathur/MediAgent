"""Patient-scoped assistant conversations and one-use WebSocket tickets."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_06"
down_revision = "20261125_05"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("conversations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("patient_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_conversations_patient_id", "conversations", ["patient_id"])
    op.create_index("ix_conversations_owner_id", "conversations", ["owner_id"])
    op.create_table("assistant_turns", sa.Column("id", sa.String(36), primary_key=True), sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("include_documents", sa.Boolean(), nullable=False), sa.Column("include_notes", sa.Boolean(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False), sa.Column("completed_at", sa.Float()), sa.CheckConstraint("status IN ('running', 'completed', 'cancelled', 'failed')"))
    op.create_index("ix_assistant_turns_conversation_id", "assistant_turns", ["conversation_id"])
    op.create_index("uq_conversation_running_turn", "assistant_turns", ["conversation_id"], unique=True, postgresql_where=sa.text("status = 'running'"), sqlite_where=sa.text("status = 'running'"))
    op.create_table("conversation_messages", sa.Column("id", sa.String(36), primary_key=True), sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("turn_id", sa.String(36), sa.ForeignKey("assistant_turns.id"), nullable=False), sa.Column("role", sa.String(20), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("citations", sa.JSON(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False), sa.CheckConstraint("role IN ('user', 'assistant')"))
    op.create_index("ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"])
    op.create_index("ix_conversation_messages_turn_id", "conversation_messages", ["turn_id"])
    op.create_table("socket_tickets", sa.Column("id", sa.String(36), primary_key=True), sa.Column("token_hash", sa.String(64), nullable=False, unique=True), sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False), sa.Column("expires_at", sa.Float(), nullable=False), sa.Column("used_at", sa.Float()))
    op.create_index("ix_socket_tickets_conversation_id", "socket_tickets", ["conversation_id"])


def downgrade():
    op.drop_table("socket_tickets")
    op.drop_table("conversation_messages")
    op.drop_table("assistant_turns")
    op.drop_table("conversations")
