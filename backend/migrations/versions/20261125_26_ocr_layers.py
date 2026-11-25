"""Preserve supplemental page OCR and processor provenance."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_26"
down_revision = "20261125_25"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("extracted_pages", sa.Column("supplemental_ocr_text", sa.Text()))
    op.add_column("extraction_jobs", sa.Column("processor_details", sa.JSON()))
    op.add_column("knowledge_imports", sa.Column("processor_details", sa.JSON()))


def downgrade():
    op.drop_column("knowledge_imports", "processor_details")
    op.drop_column("extraction_jobs", "processor_details")
    op.drop_column("extracted_pages", "supplemental_ocr_text")
