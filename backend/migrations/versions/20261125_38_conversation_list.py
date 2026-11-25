"""Index owner- and patient-scoped conversation pagination."""
from alembic import op

revision = "20261125_38"
down_revision = "20261125_37"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_conversations_owner_created_id", "conversations", ["owner_id", "created_at", "id"])
    op.create_index("ix_conversations_owner_patient_created_id", "conversations", ["owner_id", "patient_id", "created_at", "id"])


def downgrade():
    op.drop_index("ix_conversations_owner_patient_created_id", table_name="conversations")
    op.drop_index("ix_conversations_owner_created_id", table_name="conversations")
