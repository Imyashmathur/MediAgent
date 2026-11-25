"""Index patient document pagination."""
from alembic import op

revision = '20261125_42'
down_revision = '20261125_41'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_documents_patient_created_id', 'documents', ['patient_id', 'created_at', 'id'])


def downgrade():
    op.drop_index('ix_documents_patient_created_id', table_name='documents')
