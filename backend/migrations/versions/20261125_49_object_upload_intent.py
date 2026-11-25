"""Persist an object-write intent independently of the storage provider."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_49'
down_revision = '20261125_48'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('object_upload_intents',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('store_id', sa.String(64), nullable=False),
        sa.Column('bucket', sa.String(63), nullable=False),
        sa.Column('namespace', sa.String(24), nullable=False),
        sa.Column('object_id', sa.String(36), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('version_id', sa.String(1024)),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('stored_at', sa.Float()),
        sa.CheckConstraint("namespace IN ('documents', 'knowledge-imports')"),
        sa.CheckConstraint("status IN ('pending', 'stored')"),
        sa.CheckConstraint('size > 0'),
        sa.CheckConstraint("(status = 'pending' AND version_id IS NULL) OR (status = 'stored' AND version_id IS NOT NULL)"),
        sa.UniqueConstraint('store_id', 'bucket', 'namespace', 'object_id', name='uq_object_upload_destination'))
    op.create_index('ix_object_upload_intents_actor_id','object_upload_intents',['actor_id'])
    op.create_index('ix_object_upload_pending','object_upload_intents',['status','created_at','id'])


def downgrade():
    op.drop_index('ix_object_upload_pending', table_name='object_upload_intents')
    op.drop_index('ix_object_upload_intents_actor_id', table_name='object_upload_intents')
    op.drop_table('object_upload_intents')
