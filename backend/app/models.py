import time
import uuid
from datetime import date

from sqlalchemy import JSON, Boolean, CheckConstraint, Date, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from .db import Base


def new_id() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('patient', 'doctor', 'admin')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(255))
    verification_status: Mapped[str] = mapped_column(String(20), default="not_applicable")
    verification_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    email_verified_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class PatientProfile(Base):
    __tablename__ = "patient_profiles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    demographics: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    allergies: Mapped[str | None] = mapped_column(Text, nullable=True)
    medications: Mapped[str | None] = mapped_column(Text, nullable=True)
    medical_history: Mapped[str | None] = mapped_column(Text, nullable=True)


class AssistantPreference(Base):
    __tablename__ = "assistant_preferences"
    __table_args__ = (CheckConstraint("response_style IN ('concise', 'balanced', 'detailed')"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    response_style: Mapped[str] = mapped_column(String(20))
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class DoctorProfile(Base):
    __tablename__ = "doctor_profiles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    specialization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    qualifications: Mapped[str | None] = mapped_column(Text, nullable=True)
    registration_identifier: Mapped[str | None] = mapped_column(String(200), nullable=True)
    experience: Mapped[str | None] = mapped_column(Text, nullable=True)
    organization_affiliation: Mapped[str | None] = mapped_column(String(200), nullable=True)


class DoctorVerificationDecision(Base):
    __tablename__ = "doctor_verification_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    doctor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    profile_revision: Mapped[int] = mapped_column(Integer)
    previous_status: Mapped[str] = mapped_column(String(20))
    decision: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    previous_refresh_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    expires_at: Mapped[float] = mapped_column(Float)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[float] = mapped_column(Float)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ResetDelivery(Base):
    __tablename__ = "reset_deliveries"
    __table_args__ = (CheckConstraint("status IN ('pending', 'accepted', 'discarded', 'failed')"), Index("ix_reset_deliveries_due", "status", "next_attempt_at"))
    reset_id: Mapped[str] = mapped_column(ForeignKey("password_resets.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=time.time)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)


class EmailVerification(Base):
    __tablename__ = "email_verifications"
    __table_args__ = (CheckConstraint("delivery_status IN ('pending', 'accepted', 'discarded', 'failed')"), Index("ix_email_verification_delivery", "delivery_status", "next_attempt_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[float] = mapped_column(Float)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[float] = mapped_column(Float, default=time.time)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)


class IntegrationCredential(Base):
    __tablename__ = "integration_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    patient_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    label: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Relationship(Base):
    __tablename__ = "care_relationships"
    __table_args__ = (UniqueConstraint("patient_id", "doctor_id"), CheckConstraint("status IN ('active', 'ended')"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    doctor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (CheckConstraint("status IN ('active', 'withdrawn')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    relationship_id: Mapped[str] = mapped_column(ForeignKey("care_relationships.id"), index=True)
    scope: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")
    effective_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float] = mapped_column(Float)
    withdrawn_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class CareMessage(Base):
    __tablename__ = "care_messages"
    __table_args__ = (Index("ix_care_messages_relationship_created_id", "relationship_id", "created_at", "id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    relationship_id: Mapped[str] = mapped_column(ForeignKey("care_relationships.id"), index=True)
    sender_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("recipient_id", "event_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recipient_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    relationship_id: Mapped[str | None] = mapped_column(ForeignKey("care_relationships.id"), nullable=True)
    required_scope: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    event_key: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    read_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_purge_due", "purge_status", "purge_after"), Index("ix_documents_patient_created_id", "patient_id", "created_at", "id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(160))
    content_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_status: Mapped[str] = mapped_column(String(20), default="unverified")
    scan_status: Mapped[str] = mapped_column(String(24), default="not_scanned", server_default="not_scanned")
    deleted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    purge_status: Mapped[str] = mapped_column(String(20), default="not_requested", server_default="not_requested")
    purge_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    purge_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    purged_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ObjectUploadIntent(Base):
    """Durable identity for a bounded object write."""
    __tablename__ = "object_upload_intents"
    __table_args__ = (
        CheckConstraint("namespace IN ('documents', 'knowledge-imports')"),
        CheckConstraint("status IN ('pending', 'stored')"),
        CheckConstraint("size > 0"),
        CheckConstraint("(status = 'pending' AND version_id IS NULL) OR (status = 'stored' AND version_id IS NOT NULL)"),
        UniqueConstraint("store_id", "bucket", "namespace", "object_id", name="uq_object_upload_destination"),
        Index("ix_object_upload_pending", "status", "created_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    store_id: Mapped[str] = mapped_column(String(64))
    bucket: Mapped[str] = mapped_column(String(63))
    namespace: Mapped[str] = mapped_column(String(24))
    object_id: Mapped[str] = mapped_column(String(36))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    version_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    stored_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class ObjectDocumentSubmission(Base):
    """Validated metadata captured before an object-backed upload."""
    __tablename__ = "object_document_submissions"
    __table_args__ = (CheckConstraint("scan_status IN ('not_scanned', 'no_threat_detected')"),)
    intent_id: Mapped[str] = mapped_column(ForeignKey("object_upload_intents.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    filename: Mapped[str] = mapped_column(String(160))
    content_type: Mapped[str] = mapped_column(String(80))
    scan_status: Mapped[str] = mapped_column(String(24))
    correlation_id: Mapped[str] = mapped_column(String(36))


class ObjectUploadDiscard(Base):
    """Durable request to monitor and remove an unlinked object upload."""
    __tablename__ = "object_upload_discards"
    __table_args__ = (
        CheckConstraint("last_result IN ('pending', 'absent', 'removed', 'error')"),
        CheckConstraint("error_code IS NULL OR (last_result = 'error' AND error_code IN ('storage_configuration', 'integrity_conflict', 'provider_unavailable', 'unexpected_error'))", name="ck_object_upload_discard_error_code"),
        Index("ix_object_upload_discard_due", "next_check_at", "intent_id"),
        Index("ix_object_upload_discard_error_list", "last_result", "requested_at", "intent_id"),
    )
    intent_id: Mapped[str] = mapped_column(ForeignKey("object_document_submissions.intent_id"), primary_key=True)
    requested_at: Mapped[float] = mapped_column(Float, default=time.time)
    next_check_at: Mapped[float] = mapped_column(Float, default=time.time)
    checked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_result: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DocumentObjectLink(Base):
    """One document can point to one immutable, uniquely assigned upload."""
    __tablename__ = "document_object_links"
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("object_document_submissions.intent_id"), unique=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')"),
        Index("uq_document_active_extraction", "document_id", unique=True, postgresql_where=text("status IN ('queued', 'running')"), sqlite_where=text("status IN ('queued', 'running')")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    request_correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    processor_version: Mapped[str] = mapped_column(String(80), default="native-ocr-v1")
    processor_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    vision_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    vision_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class JobOutbox(Base):
    __tablename__ = "job_outbox"
    __table_args__ = (
        CheckConstraint("kind IN ('document_processing', 'knowledge_import')"),
        CheckConstraint("status IN ('pending', 'published', 'discarded')"),
        UniqueConstraint("kind", "target_id"),
        Index("ix_job_outbox_due", "status", "next_attempt_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=time.time)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    published_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ExtractedPage(Base):
    __tablename__ = "extracted_pages"
    __table_args__ = (UniqueConstraint("job_id", "page_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("extraction_jobs.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    supplemental_ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    vision_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    vision_warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)


class StructuredExtractionVersion(Base):
    __tablename__ = "structured_extraction_versions"
    __table_args__ = (UniqueConstraint("job_id", "version"), CheckConstraint("review_status IN ('unverified', 'reviewed')"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("extraction_jobs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    review_status: Mapped[str] = mapped_column(String(20), default="unverified")
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class DocumentSummary(Base):
    __tablename__ = "document_summaries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("extraction_jobs.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON)
    truncated: Mapped[bool] = mapped_column(Boolean)
    model_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class SummaryGeneration(Base):
    __tablename__ = "summary_generations"
    __table_args__ = (CheckConstraint("status IN ('running', 'completed', 'failed')"),)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("extraction_jobs.id"))
    status: Mapped[str] = mapped_column(String(20))
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[float] = mapped_column(Float)
    summary_id: Mapped[str | None] = mapped_column(ForeignKey("document_summaries.id"), nullable=True, unique=True)


class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"
    __table_args__ = (UniqueConstraint("family_id", "version"), CheckConstraint("status IN ('draft', 'approved', 'retired')"), CheckConstraint("index_status IN ('not_indexed', 'ready')"), Index("uq_knowledge_family_approved", "family_id", unique=True, postgresql_where=text("status = 'approved'"), sqlite_where=text("status = 'approved'")))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    family_id: Mapped[str] = mapped_column(String(36), index=True)
    previous_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_sources.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    publisher: Mapped[str] = mapped_column(String(200))
    provenance: Mapped[str] = mapped_column(String(1000))
    publication_date: Mapped[date] = mapped_column(Date)
    specialty: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(80))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    index_status: Mapped[str] = mapped_column(String(20), default="not_indexed")
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effective_at: Mapped[float] = mapped_column(Float)
    retire_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class KnowledgeIndexJob(Base):
    __tablename__ = "knowledge_index_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')"),
        Index("uq_knowledge_index_active", "source_id", unique=True, postgresql_where=text("status IN ('queued', 'running')"), sqlite_where=text("status IN ('queued', 'running')")),
        Index("ix_knowledge_index_dispatch", "status", "next_dispatch_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("knowledge_sources.id"))
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    request_correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    content_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(200))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    dispatch_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_dispatch_at: Mapped[float] = mapped_column(Float, default=time.time)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class KnowledgeClinicalReview(Base):
    __tablename__ = "knowledge_clinical_reviews"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'recommended', 'declined')"),
        UniqueConstraint("source_id", "doctor_id", name="uq_knowledge_review_source_doctor"),
        Index("ix_knowledge_review_doctor", "doctor_id", "created_at", "id"),
        Index("ix_knowledge_review_source", "source_id", "created_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("knowledge_sources.id"))
    doctor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    assigned_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    doctor_verification_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    reviewed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class KnowledgeImport(Base):
    __tablename__ = "knowledge_imports"
    __table_args__ = (CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')"), Index("ix_knowledge_import_purge_due", "purge_status", "purge_after"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    request_correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    filename: Mapped[str] = mapped_column(String(160))
    media_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    scan_status: Mapped[str] = mapped_column(String(24), default="not_scanned", server_default="not_scanned")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    processor_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    deleted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    purge_status: Mapped[str] = mapped_column(String(20), default="not_requested", server_default="not_requested")
    purge_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    purge_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    purged_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class KnowledgeSourceImport(Base):
    __tablename__ = "knowledge_source_imports"
    source_id: Mapped[str] = mapped_column(ForeignKey("knowledge_sources.id"), primary_key=True)
    import_id: Mapped[str] = mapped_column(ForeignKey("knowledge_imports.id"), index=True)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("source_id", "ordinal"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("knowledge_sources.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(384).with_variant(JSON, "sqlite"))


class Consultation(Base):
    __tablename__ = "consultations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    doctor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    occurred_at: Mapped[float] = mapped_column(Float)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ClinicalNote(Base):
    __tablename__ = "clinical_notes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class NoteConsultation(Base):
    __tablename__ = "note_consultations"
    note_id: Mapped[str] = mapped_column(ForeignKey("clinical_notes.id"), primary_key=True)
    consultation_id: Mapped[str] = mapped_column(ForeignKey("consultations.id"), index=True)


class ClinicalNoteVersion(Base):
    __tablename__ = "clinical_note_versions"
    __table_args__ = (
        UniqueConstraint("note_id", "version"),
        CheckConstraint("source_extraction_version IS NULL OR source_extraction_version > 0", name="ck_note_extraction_version_positive"),
        CheckConstraint("status IN ('draft', 'pending_review', 'approved', 'rejected', 'superseded')"),
        Index("uq_note_open_version", "note_id", unique=True, postgresql_where=text("status IN ('draft', 'pending_review')"), sqlite_where=text("status IN ('draft', 'pending_review')")),
        Index("uq_note_approved_version", "note_id", unique=True, postgresql_where=text("status = 'approved'"), sqlite_where=text("status = 'approved'")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ai_assisted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    note_id: Mapped[str] = mapped_column(ForeignKey("clinical_notes.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    source_processing_job_id: Mapped[str | None] = mapped_column(ForeignKey("extraction_jobs.id"), nullable=True)
    source_extraction_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_fields: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    reviewed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_owner_created_id", "owner_id", "created_at", "id"), Index("ix_conversations_owner_patient_created_id", "owner_id", "patient_id", "created_at", "id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class AssistantTurn(Base):
    __tablename__ = "assistant_turns"
    __table_args__ = (CheckConstraint("status IN ('running', 'completed', 'cancelled', 'failed')"), Index("uq_conversation_running_turn", "conversation_id", unique=True, postgresql_where=text("status = 'running'"), sqlite_where=text("status = 'running'")))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    include_documents: Mapped[bool] = mapped_column(Boolean, default=False)
    include_notes: Mapped[bool] = mapped_column(Boolean, default=False)
    include_consultations: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    include_profile: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (CheckConstraint("role IN ('user', 'assistant')"), Index("ix_conversation_messages_conversation_created_id", "conversation_id", "created_at", "id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("assistant_turns.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class NoteAssistantSource(Base):
    __tablename__ = "note_assistant_sources"
    version_id: Mapped[str] = mapped_column(ForeignKey("clinical_note_versions.id"), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("conversation_messages.id"), index=True)


class NoteGeneration(Base):
    __tablename__ = "note_generations"
    __table_args__ = (CheckConstraint("status IN ('running', 'completed', 'failed')"),)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    message_id: Mapped[str] = mapped_column(ForeignKey("conversation_messages.id"))
    consultation_id: Mapped[str | None] = mapped_column(ForeignKey("consultations.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[float] = mapped_column(Float)
    version_id: Mapped[str | None] = mapped_column(ForeignKey("clinical_note_versions.id"), nullable=True, unique=True)


class SocketTicket(Base):
    __tablename__ = "socket_tickets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    expires_at: Mapped[float] = mapped_column(Float)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_created_id", "created_at", "id"), Index("ix_audit_patient_created_id", "patient_id", "created_at", "id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    patient_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80))
    resource: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str | None] = mapped_column(String(200), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(36), default=new_id)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
