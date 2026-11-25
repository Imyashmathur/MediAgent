"""Track explicitly requested vision processing and unverified page output."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_27"
down_revision = "20261125_26"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("extraction_jobs", sa.Column("vision_requested", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("extraction_jobs", sa.Column("vision_config_fingerprint", sa.String(64)))
    op.add_column("extracted_pages", sa.Column("vision_text", sa.Text()))
    op.add_column("extracted_pages", sa.Column("vision_warnings", sa.JSON()))


def downgrade():
    op.drop_column("extracted_pages", "vision_warnings")
    op.drop_column("extracted_pages", "vision_text")
    op.drop_column("extraction_jobs", "vision_config_fingerprint")
    op.drop_column("extraction_jobs", "vision_requested")
