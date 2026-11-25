"""Bind clinical review assignments to the clinician verification revision."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_46'
down_revision = '20261125_45'
branch_labels = None
depends_on = None


def upgrade():
    # Historical reviews cannot establish which verification they relied on.
    op.add_column('knowledge_clinical_reviews', sa.Column('doctor_verification_revision', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('knowledge_clinical_reviews', 'doctor_verification_revision')
