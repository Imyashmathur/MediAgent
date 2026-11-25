"""Short-lived, single-use password reset requests."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_08"
down_revision = "20261125_07"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("password_resets", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("expires_at", sa.Float(), nullable=False), sa.Column("used_at", sa.Float()), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_password_resets_user_id", "password_resets", ["user_id"])


def downgrade():
    op.drop_table("password_resets")
