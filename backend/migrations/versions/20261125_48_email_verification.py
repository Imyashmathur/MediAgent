"""Add explicitly requested email ownership verification."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_48'
down_revision = '20261125_47'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('email_verified_at', sa.Float(), nullable=True))
    op.create_table('email_verifications',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False), sa.Column('used_at', sa.Float()),
        sa.Column('delivery_status', sa.String(20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.Float(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False), sa.Column('completed_at', sa.Float()),
        sa.Column('error_code', sa.String(40)),
        sa.CheckConstraint("delivery_status IN ('pending', 'accepted', 'discarded', 'failed')"))
    op.create_index('ix_email_verifications_user_id', 'email_verifications', ['user_id'])
    op.create_index('ix_email_verification_delivery', 'email_verifications', ['delivery_status', 'next_attempt_at'])


def downgrade():
    op.drop_index('ix_email_verification_delivery', table_name='email_verifications')
    op.drop_index('ix_email_verifications_user_id', table_name='email_verifications')
    op.drop_table('email_verifications')
    op.drop_column('users', 'email_verified_at')
