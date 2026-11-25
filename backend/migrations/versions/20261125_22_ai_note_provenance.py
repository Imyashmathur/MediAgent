"""Retain AI assistance provenance on clinical-note versions."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_22"
down_revision = "20261125_21"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("clinical_note_versions", sa.Column("ai_assisted", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("clinical_note_versions", "ai_assisted")
