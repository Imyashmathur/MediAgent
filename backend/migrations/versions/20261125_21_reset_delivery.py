"""Track durable password-reset mail delivery."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_21"
down_revision = "20261125_20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("reset_deliveries",
        sa.Column("reset_id", sa.String(36), sa.ForeignKey("password_resets.id"), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float()),
        sa.Column("error_code", sa.String(40)),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'discarded', 'failed')"))
    op.create_index("ix_reset_deliveries_due", "reset_deliveries", ["status", "next_attempt_at"])


def downgrade():
    op.drop_index("ix_reset_deliveries_due", table_name="reset_deliveries")
    op.drop_table("reset_deliveries")
