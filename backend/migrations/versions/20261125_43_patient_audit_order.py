"""Index patient-scoped audit history pagination."""
from alembic import op

revision = '20261125_43'
down_revision = '20261125_42'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_audit_patient_created_id', 'audit_events', ['patient_id', 'created_at', 'id'])


def downgrade():
    op.drop_index('ix_audit_patient_created_id', table_name='audit_events')
