"""Record upload metadata and immutable document-object association."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_50'
down_revision = '20261125_49'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('object_document_submissions',
        sa.Column('intent_id', sa.String(36), sa.ForeignKey('object_upload_intents.id'), primary_key=True),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sessions.id'), nullable=False),
        sa.Column('filename', sa.String(160), nullable=False),
        sa.Column('content_type', sa.String(80), nullable=False),
        sa.Column('scan_status', sa.String(24), nullable=False),
        sa.Column('correlation_id', sa.String(36), nullable=False),
        sa.CheckConstraint("scan_status IN ('not_scanned', 'no_threat_detected')"))
    op.create_table('document_object_links',
        sa.Column('document_id', sa.String(36), sa.ForeignKey('documents.id'), primary_key=True),
        sa.Column('intent_id', sa.String(36), sa.ForeignKey('object_document_submissions.intent_id'), nullable=False, unique=True),
        sa.Column('created_at', sa.Float(), nullable=False))


def downgrade():
    op.drop_table('document_object_links')
    op.drop_table('object_document_submissions')
