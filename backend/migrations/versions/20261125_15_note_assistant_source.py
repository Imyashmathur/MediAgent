"""Link a clinician-authored note version to an assistant response."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_15"
down_revision = "20261125_14"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("note_assistant_sources", sa.Column("version_id", sa.String(36), sa.ForeignKey("clinical_note_versions.id"), primary_key=True), sa.Column("message_id", sa.String(36), sa.ForeignKey("conversation_messages.id"), nullable=False))
    op.create_index("ix_note_assistant_sources_message_id", "note_assistant_sources", ["message_id"])


def downgrade():
    op.drop_table("note_assistant_sources")
