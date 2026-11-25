"""Record upload digests for newly accepted documents."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_29"
down_revision = "20261125_28"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("sha256", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("documents", "sha256")
