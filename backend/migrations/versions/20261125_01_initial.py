"""Create persistent identity, consent, document, and audit tables."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("users", sa.Column("id", sa.String(36), primary_key=True), sa.Column("email", sa.String(254), nullable=False), sa.Column("full_name", sa.String(200), nullable=False), sa.Column("role", sa.String(20), nullable=False), sa.Column("password_hash", sa.String(255), nullable=False), sa.Column("verification_status", sa.String(20), nullable=False), sa.Column("active", sa.Boolean(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False), sa.CheckConstraint("role IN ('patient', 'doctor', 'admin')"))
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table("sessions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("refresh_hash", sa.String(64), nullable=False, unique=True), sa.Column("expires_at", sa.Float(), nullable=False), sa.Column("revoked_at", sa.Float()), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_table("care_relationships", sa.Column("id", sa.String(36), primary_key=True), sa.Column("patient_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("doctor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("purpose", sa.String(200), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("created_at", sa.Float(), nullable=False), sa.UniqueConstraint("patient_id", "doctor_id"), sa.CheckConstraint("status IN ('active', 'ended')"))
    op.create_index("ix_care_relationships_patient_id", "care_relationships", ["patient_id"])
    op.create_index("ix_care_relationships_doctor_id", "care_relationships", ["doctor_id"])
    op.create_table("consents", sa.Column("id", sa.String(36), primary_key=True), sa.Column("relationship_id", sa.String(36), sa.ForeignKey("care_relationships.id"), nullable=False), sa.Column("scope", sa.String(40), nullable=False), sa.Column("purpose", sa.String(200), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("effective_at", sa.Float(), nullable=False), sa.Column("expires_at", sa.Float(), nullable=False), sa.Column("withdrawn_at", sa.Float()), sa.CheckConstraint("status IN ('active', 'withdrawn')"))
    op.create_index("ix_consents_relationship_id", "consents", ["relationship_id"])
    op.create_table("documents", sa.Column("id", sa.String(36), primary_key=True), sa.Column("patient_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False), sa.Column("filename", sa.String(160), nullable=False), sa.Column("content_type", sa.String(80), nullable=False), sa.Column("size", sa.Integer(), nullable=False), sa.Column("review_status", sa.String(20), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_documents_patient_id", "documents", ["patient_id"])
    op.create_table("audit_events", sa.Column("id", sa.String(36), primary_key=True), sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id")), sa.Column("patient_id", sa.String(36)), sa.Column("action", sa.String(80), nullable=False), sa.Column("resource", sa.String(100), nullable=False), sa.Column("outcome", sa.String(20), nullable=False), sa.Column("purpose", sa.String(200)), sa.Column("correlation_id", sa.String(36), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])
    op.create_index("ix_audit_events_patient_id", "audit_events", ["patient_id"])


def downgrade():
    op.drop_table("audit_events")
    op.drop_table("documents")
    op.drop_table("consents")
    op.drop_table("care_relationships")
    op.drop_table("sessions")
    op.drop_table("users")
