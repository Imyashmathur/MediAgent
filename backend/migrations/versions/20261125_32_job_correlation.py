"""Link background jobs to their initiating request when available."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_32"
down_revision = "20261125_31"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("extraction_jobs", "knowledge_imports"):
        op.add_column(table, sa.Column("request_correlation_id", sa.String(36), nullable=True))


def downgrade():
    for table in ("knowledge_imports", "extraction_jobs"):
        op.drop_column(table, "request_correlation_id")
