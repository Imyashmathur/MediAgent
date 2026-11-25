"""Store optional transcription field references on note versions."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_41'
down_revision = '20261125_40'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('clinical_note_versions', sa.Column('source_fields', sa.JSON(), nullable=False, server_default='[]'))


def downgrade():
    op.drop_column('clinical_note_versions', 'source_fields')
