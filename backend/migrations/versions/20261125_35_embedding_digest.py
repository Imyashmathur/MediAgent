"""Track embedding model digests for knowledge indexes."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_35"
down_revision = "20261125_34"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_sources", sa.Column("embedding_digest", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("knowledge_sources", "embedding_digest")
