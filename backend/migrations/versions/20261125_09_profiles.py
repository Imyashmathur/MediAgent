"""Self-reported patient and clinician profiles."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_09"
down_revision = "20261125_08"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("patient_profiles", sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True), sa.Column("date_of_birth", sa.Date()), sa.Column("demographics", sa.Text()), sa.Column("contact_details", sa.Text()), sa.Column("allergies", sa.Text()), sa.Column("medications", sa.Text()), sa.Column("medical_history", sa.Text()))
    op.create_table("doctor_profiles", sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True), sa.Column("specialization", sa.String(200)), sa.Column("qualifications", sa.Text()), sa.Column("registration_identifier", sa.String(200)), sa.Column("experience", sa.Text()), sa.Column("organization_affiliation", sa.String(200)))


def downgrade():
    op.drop_table("doctor_profiles")
    op.drop_table("patient_profiles")
