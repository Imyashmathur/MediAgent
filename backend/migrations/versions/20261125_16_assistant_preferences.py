"""Opt-in, user-owned nonclinical assistant preferences."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_16"
down_revision = "20261125_15"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("assistant_preferences", sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True), sa.Column("response_style", sa.String(20), nullable=False), sa.Column("updated_at", sa.Float(), nullable=False), sa.CheckConstraint("response_style IN ('concise', 'balanced', 'detailed')"))


def downgrade():
    op.drop_table("assistant_preferences")
