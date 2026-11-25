"""Index bounded operational listing of discard errors."""
from alembic import op

revision = '20261125_53'
down_revision = '20261125_52'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_object_upload_discard_error_list', 'object_upload_discards', ['last_result', 'requested_at', 'intent_id'])


def downgrade():
    op.drop_index('ix_object_upload_discard_error_list', table_name='object_upload_discards')
