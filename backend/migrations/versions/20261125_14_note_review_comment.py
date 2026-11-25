"""Store clinician rejection rationale with the reviewed note version."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_14"
down_revision = "20261125_13"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("clinical_note_versions", sa.Column("review_comment", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("clinical_note_versions", "review_comment")
