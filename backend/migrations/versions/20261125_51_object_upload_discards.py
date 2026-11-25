"""Track discard requests and continuing object reconciliation."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_51'
down_revision = '20261125_50'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('object_upload_discards',
        sa.Column('intent_id', sa.String(36), sa.ForeignKey('object_document_submissions.intent_id'), primary_key=True),
        sa.Column('requested_at', sa.Float(), nullable=False),
        sa.Column('next_check_at', sa.Float(), nullable=False),
        sa.Column('checked_at', sa.Float(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_result', sa.String(16), nullable=False, server_default='pending'),
        sa.CheckConstraint("last_result IN ('pending', 'absent', 'removed', 'error')"))
    op.create_index('ix_object_upload_discard_due', 'object_upload_discards', ['next_check_at', 'intent_id'])


def downgrade():
    op.drop_index('ix_object_upload_discard_due', table_name='object_upload_discards')
    op.drop_table('object_upload_discards')
