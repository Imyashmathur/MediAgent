"""Bind clinician verification decisions to a profile revision."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_12"
down_revision = "20261125_11"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("verification_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("doctor_profiles", sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("doctor_verification_decisions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("doctor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("reviewer_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("profile_revision", sa.Integer(), nullable=False), sa.Column("previous_status", sa.String(20), nullable=False), sa.Column("decision", sa.String(20), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_doctor_verification_decisions_doctor_id", "doctor_verification_decisions", ["doctor_id"])


def downgrade():
    op.drop_table("doctor_verification_decisions")
    op.drop_column("doctor_profiles", "revision")
    op.drop_column("users", "verification_revision")
