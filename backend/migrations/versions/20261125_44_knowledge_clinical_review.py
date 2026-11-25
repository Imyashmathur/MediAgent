"""Add assigned clinician recommendations for knowledge sources."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_44'
down_revision = '20261125_43'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('knowledge_clinical_reviews',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('source_id', sa.String(36), sa.ForeignKey('knowledge_sources.id'), nullable=False),
        sa.Column('doctor_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('assigned_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('reviewed_at', sa.Float(), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'recommended', 'declined')"),
        sa.UniqueConstraint('source_id', 'doctor_id', name='uq_knowledge_review_source_doctor'))
    op.create_index('ix_knowledge_review_doctor', 'knowledge_clinical_reviews', ['doctor_id', 'created_at', 'id'])
    op.create_index('ix_knowledge_review_source', 'knowledge_clinical_reviews', ['source_id', 'created_at', 'id'])


def downgrade():
    op.drop_index('ix_knowledge_review_source', table_name='knowledge_clinical_reviews')
    op.drop_index('ix_knowledge_review_doctor', table_name='knowledge_clinical_reviews')
    op.drop_table('knowledge_clinical_reviews')
