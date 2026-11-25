"""Scoped, individually revocable integration credentials."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_07"
down_revision = "20261125_06"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("integration_credentials", sa.Column("id", sa.String(36), primary_key=True), sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("patient_id", sa.String(36), sa.ForeignKey("users.id")), sa.Column("label", sa.String(100), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False, unique=True), sa.Column("scopes", sa.JSON(), nullable=False), sa.Column("expires_at", sa.Float(), nullable=False), sa.Column("revoked_at", sa.Float()), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_integration_credentials_owner_id", "integration_credentials", ["owner_id"])


def downgrade():
    op.drop_table("integration_credentials")
