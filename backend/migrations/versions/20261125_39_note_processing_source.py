"""Preserve the processing run cited by a clinical note version."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_39'
down_revision = '20261125_38'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('clinical_note_versions') as batch:
            batch.add_column(sa.Column('source_processing_job_id', sa.String(36), nullable=True))
            batch.create_foreign_key('fk_note_version_processing_job', 'extraction_jobs', ['source_processing_job_id'], ['id'])
    else:
        op.add_column('clinical_note_versions', sa.Column('source_processing_job_id', sa.String(36), nullable=True))
        op.create_foreign_key('fk_note_version_processing_job', 'clinical_note_versions', 'extraction_jobs', ['source_processing_job_id'], ['id'])


def downgrade():
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('clinical_note_versions') as batch:
            batch.drop_constraint('fk_note_version_processing_job', type_='foreignkey')
            batch.drop_column('source_processing_job_id')
    else:
        op.drop_constraint('fk_note_version_processing_job', 'clinical_note_versions', type_='foreignkey')
        op.drop_column('clinical_note_versions', 'source_processing_job_id')
