"""Bind new clinical reviews to the source content and metadata."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_45'
down_revision = '20261125_44'
branch_labels = None
depends_on = None


def upgrade():
    # Existing reviews cannot be safely bound retroactively to previously reviewed content.
    op.add_column('knowledge_clinical_reviews', sa.Column('source_fingerprint', sa.String(64), nullable=True))


def downgrade():
    op.drop_column('knowledge_clinical_reviews', 'source_fingerprint')
