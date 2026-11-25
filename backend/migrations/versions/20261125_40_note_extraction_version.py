"""Record the transcription version referenced by a note."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_40'
down_revision = '20261125_39'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('clinical_note_versions') as batch:
            batch.add_column(sa.Column('source_extraction_version', sa.Integer(), nullable=True))
            batch.create_check_constraint('ck_note_extraction_version_positive', 'source_extraction_version IS NULL OR source_extraction_version > 0')
    else:
        op.add_column('clinical_note_versions', sa.Column('source_extraction_version', sa.Integer(), nullable=True))
        op.create_check_constraint('ck_note_extraction_version_positive', 'clinical_note_versions', 'source_extraction_version IS NULL OR source_extraction_version > 0')


def downgrade():
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('clinical_note_versions') as batch:
            batch.drop_constraint('ck_note_extraction_version_positive', type_='check')
            batch.drop_column('source_extraction_version')
    else:
        op.drop_constraint('ck_note_extraction_version_positive', 'clinical_note_versions', type_='check')
        op.drop_column('clinical_note_versions', 'source_extraction_version')
