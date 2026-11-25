"""Consultation history and stable links from clinical notes."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_10"
down_revision = "20261125_09"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("consultations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("patient_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("doctor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("occurred_at", sa.Float(), nullable=False), sa.Column("title", sa.String(200), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_consultations_patient_id", "consultations", ["patient_id"])
    op.create_index("ix_consultations_doctor_id", "consultations", ["doctor_id"])
    op.create_table("note_consultations", sa.Column("note_id", sa.String(36), sa.ForeignKey("clinical_notes.id"), primary_key=True), sa.Column("consultation_id", sa.String(36), sa.ForeignKey("consultations.id"), nullable=False))
    op.create_index("ix_note_consultations_consultation_id", "note_consultations", ["consultation_id"])


def downgrade():
    op.drop_table("note_consultations")
    op.drop_table("consultations")
