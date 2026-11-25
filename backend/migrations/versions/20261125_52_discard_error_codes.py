"""Record sanitized error categories for discarded upload reconciliation."""
from alembic import op
import sqlalchemy as sa

revision = '20261125_52'
down_revision = '20261125_51'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('object_upload_discards') as batch:
        batch.add_column(sa.Column('error_code', sa.String(32), nullable=True))
        batch.create_check_constraint('ck_object_upload_discard_error_code', "error_code IS NULL OR (last_result = 'error' AND error_code IN ('storage_configuration', 'integrity_conflict', 'provider_unavailable', 'unexpected_error'))")


def downgrade():
    with op.batch_alter_table('object_upload_discards') as batch:
        batch.drop_constraint('ck_object_upload_discard_error_code', type_='check')
        batch.drop_column('error_code')
