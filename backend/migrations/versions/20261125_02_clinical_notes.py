"""Add clinical note versions and enforce one open and one approved version."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_02"
down_revision = "20261125_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("clinical_notes", sa.Column("id", sa.String(36), primary_key=True), sa.Column("patient_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_clinical_notes_patient_id", "clinical_notes", ["patient_id"])
    op.create_table("clinical_note_versions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("note_id", sa.String(36), sa.ForeignKey("clinical_notes.id"), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("author_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("reviewer_id", sa.String(36), sa.ForeignKey("users.id")), sa.Column("source_document_id", sa.String(36), sa.ForeignKey("documents.id")), sa.Column("created_at", sa.Float(), nullable=False), sa.Column("reviewed_at", sa.Float()), sa.UniqueConstraint("note_id", "version"), sa.CheckConstraint("status IN ('draft', 'pending_review', 'approved', 'rejected', 'superseded')"))
    op.create_index("ix_clinical_note_versions_note_id", "clinical_note_versions", ["note_id"])
    op.create_index("uq_note_open_version", "clinical_note_versions", ["note_id"], unique=True, postgresql_where=sa.text("status IN ('draft', 'pending_review')"), sqlite_where=sa.text("status IN ('draft', 'pending_review')"))
    op.create_index("uq_note_approved_version", "clinical_note_versions", ["note_id"], unique=True, postgresql_where=sa.text("status = 'approved'"), sqlite_where=sa.text("status = 'approved'"))


def downgrade():
    op.drop_table("clinical_note_versions")
    op.drop_table("clinical_notes")
