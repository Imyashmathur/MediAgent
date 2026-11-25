"""Allow the immediately previous refresh credential to revoke its session."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_28"
down_revision = "20261125_27"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("previous_refresh_hash", sa.String(64)))
    op.create_index("ix_sessions_previous_refresh_hash", "sessions", ["previous_refresh_hash"])


def downgrade():
    op.drop_index("ix_sessions_previous_refresh_hash", table_name="sessions")
    op.drop_column("sessions", "previous_refresh_hash")
