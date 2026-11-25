"""Retain consultation-scope requirements for assistant conversations."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_33"
down_revision = "20261125_32"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assistant_turns", sa.Column("include_consultations", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("assistant_turns", "include_consultations")
