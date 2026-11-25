"""Retain profile-scope requirements in assistant conversations."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_37"
down_revision = "20261125_36"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assistant_turns", sa.Column("include_profile", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("assistant_turns", "include_profile")
