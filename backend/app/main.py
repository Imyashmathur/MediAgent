from __future__ import annotations

import base64
import asyncio
import difflib
import hashlib
import hmac
import json
import math
import os
import secrets
import signal
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictBool, UUID4, ValidationError, model_validator
from python_multipart.exceptions import ParseError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .db import Session as SessionFactory, get_db
from .models import AssistantPreference, AssistantTurn, AuditEvent, CareMessage, ClinicalNote, ClinicalNoteVersion, Consent, Consultation, Conversation, ConversationMessage, DoctorProfile, DoctorVerificationDecision, Document, DocumentObjectLink, DocumentSummary, EmailVerification, ExtractedPage, ExtractionJob, IntegrationCredential, JobOutbox, KnowledgeChunk, KnowledgeClinicalReview, KnowledgeImport, KnowledgeIndexJob, KnowledgeSource, KnowledgeSourceImport, NoteAssistantSource, NoteConsultation, NoteGeneration, Notification, ObjectDocumentSubmission, ObjectUploadDiscard, ObjectUploadIntent, PasswordReset, PatientProfile, Relationship, ResetDelivery, Session, SocketTicket, StructuredExtractionVersion, SummaryGeneration, User
from .notifications import queue_notification
from .parser_subprocess import parser_environment
from .extraction_schema import StructuredExtraction, validate_evidence
from . import assistant_provider, auth_rate_limit, document_storage, object_document_submissions, object_staging, object_upload_intents, email_verification, file_integrity, integration_rate_limit, knowledge, malware_scan, observability, password_reset, vision_provider

malware_scan.validate_config()
app = FastAPI(title="MediAgent API", version="0.2.0")
app.add_middleware(observability.ObservabilityMiddleware)
bearer = HTTPBearer(auto_error=False)
SECRET = os.getenv("JWT_SECRET", "local-development-secret-change-me")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/mediagent-uploads"))
KNOWLEDGE_IMPORT_DIR = Path(os.getenv("KNOWLEDGE_IMPORT_DIR", "/tmp/mediagent-knowledge-imports"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", "10485760"))
DOCUMENT_STORAGE_BACKEND = os.getenv("DOCUMENT_STORAGE_BACKEND", "local")
if DOCUMENT_STORAGE_BACKEND not in ("local", "s3"):
    raise RuntimeError("Invalid document storage backend")
if DOCUMENT_STORAGE_BACKEND == "s3":
    document_storage.config()
MAX_MULTIPART_OVERHEAD = 65536
ACCESS_SECONDS = 15 * 60
REFRESH_SECONDS = 7 * 24 * 60 * 60
_review_policy = os.getenv("NOTE_REVIEW_POLICY", "self_or_independent")
if _review_policy not in ("self_or_independent", "independent"):
    raise RuntimeError("Invalid NOTE_REVIEW_POLICY")
NOTE_REVIEW_POLICY = _review_policy
_knowledge_review_policy = os.getenv("KNOWLEDGE_REVIEW_POLICY", "self_or_independent")
if _knowledge_review_policy not in ("self_or_independent", "independent"):
    raise RuntimeError("Invalid KNOWLEDGE_REVIEW_POLICY")
KNOWLEDGE_REVIEW_POLICY = _knowledge_review_policy
_knowledge_clinical_policy = os.getenv("KNOWLEDGE_CLINICAL_REVIEW_POLICY", "optional")
if _knowledge_clinical_policy not in ("optional", "required"):
    raise RuntimeError("Invalid KNOWLEDGE_CLINICAL_REVIEW_POLICY")
KNOWLEDGE_CLINICAL_REVIEW_POLICY = _knowledge_clinical_policy
PREVIEW_SLOTS = asyncio.Semaphore(2)
DOWNLOAD_SLOTS = asyncio.Semaphore(4)
OBJECT_UPLOAD_SLOTS = asyncio.Semaphore(4)
Db = Annotated[AsyncSession, Depends(get_db)]


class Register(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    role: Literal["patient", "doctor"]


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class Refresh(BaseModel):
    refresh_token: str = Field(min_length=30, max_length=200)


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str | None = Field(default=None, max_length=200)


class ResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class EmailVerificationConfirm(BaseModel):
    token: str = Field(min_length=1, max_length=120)


class ResetConfirm(BaseModel):
    token: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=256)


class DeactivateAccount(BaseModel):
    password: str = Field(max_length=256)


class RelationshipRequest(BaseModel):
    doctor_id: str
    purpose: str = Field(min_length=3, max_length=200)


class ProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vision_requested: StrictBool = False
    vision_config_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ConsentRequest(BaseModel):
    relationship_id: str
    scope: Literal["read_documents", "review_documents", "clinical_notes", "assistant", "integration_access", "read_profile", "consultations", "care_messages", "profile_integration", "consultations_integration"]
    purpose: str = Field(min_length=3, max_length=200)
    effective_at: float | None = None
    expires_at: float


class VerificationRequest(BaseModel):
    verified: bool
    expected_profile_revision: int = Field(ge=0)
    expected_verification_revision: int = Field(ge=0)
    expected_status: Literal["pending", "verified", "rejected"]


class CareMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=4000)


class WorklistSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=100)
    patient_cursor: UUID4 | None = None
    upload_cursor: UUID4 | None = None
    note_cursor: UUID4 | None = None
    extraction_cursor: UUID4 | None = None


class PreferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_style: Literal["concise", "balanced", "detailed"]


class AdminUserSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=100)
    role: Literal["patient", "doctor", "admin"] | None = None
    active: bool | None = None
    after: str | None = Field(default=None, max_length=36)
    limit: int = Field(default=25, ge=1, le=50)


class AdminDeactivate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["security", "user_request", "administrative"]


class PatientProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    date_of_birth: date | None = None
    demographics: str | None = Field(default=None, max_length=2000)
    contact_details: str | None = Field(default=None, max_length=2000)
    allergies: str | None = Field(default=None, max_length=4000)
    medications: str | None = Field(default=None, max_length=4000)
    medical_history: str | None = Field(default=None, max_length=8000)

    @model_validator(mode="after")
    def validate_birth_date(self):
        if self.date_of_birth and self.date_of_birth > date.today():
            raise ValueError("Date of birth cannot be in the future")
        return self


class DoctorProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    specialization: str | None = Field(default=None, max_length=200)
    qualifications: str | None = Field(default=None, max_length=4000)
    registration_identifier: str | None = Field(default=None, max_length=200)
    experience: str | None = Field(default=None, max_length=4000)
    organization_affiliation: str | None = Field(default=None, max_length=200)


class NoteSourceField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group: Literal["laboratory_tests", "prescription_items"]
    index: int = Field(ge=0, le=99, strict=True)
    field: Literal["test_name", "observed_value", "unit", "reference_range", "abnormality_flag", "report_date", "laboratory_name", "laboratory_details", "medicine_name", "dosage", "frequency", "duration", "instructions", "prescribing_doctor"]
    note_quote: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_quote(self):
        if self.note_quote is not None and not self.note_quote.strip():
            raise ValueError("Note quotation cannot be blank")
        return self


class NoteDraft(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    source_document_id: str | None = None
    source_processing_job_id: UUID4 | None = None
    source_extraction_version: int | None = Field(default=None, ge=1)
    source_fields: list[NoteSourceField] = Field(default_factory=list, max_length=30)
    consultation_id: str | None = Field(default=None, max_length=36)


class AssistantNoteDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=20000)
    consultation_id: str | None = Field(default=None, max_length=36)


class GeneratedNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    consultation_id: str | None = Field(default=None, max_length=36)


class SummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4


class DraftRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=20000)
    clear_document_source: StrictBool = False


class DocumentDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["DELETE"]


class ConsultationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    occurred_at: float

    @model_validator(mode="after")
    def validate_values(self):
        if not self.title.strip() or not math.isfinite(self.occurred_at) or not 0 < self.occurred_at <= time.time():
            raise ValueError("A title and past or present consultation time are required")
        return self


class NoteReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=2000)


class NoteComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_version_id: str = Field(min_length=36, max_length=36)
    to_version_id: str = Field(min_length=36, max_length=36)


class ExtractionReview(BaseModel):
    expected_version: int = Field(ge=1)
    data: StructuredExtraction


class KnowledgeReviewAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: UUID4


class KnowledgeClinicalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["recommended", "declined"]
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_decline_reason(self):
        if self.decision == "declined" and (not self.comment or len(self.comment) < 3):
            raise ValueError("A reason is required when declining")
        return self


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=200)
    provenance: str = Field(min_length=1, max_length=1000)
    publication_date: date
    specialty: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=50_000)
    effective_at: float
    retire_at: float | None = None
    previous_id: str | None = None
    import_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def validate_source(self):
        if not all(getattr(self, field).strip() for field in ("title", "publisher", "provenance", "specialty", "version", "content")):
            raise ValueError("Source fields cannot be blank")
        if not math.isfinite(self.effective_at) or (self.retire_at is not None and not math.isfinite(self.retire_at)):
            raise ValueError("Dates must be finite")
        if self.retire_at is not None and self.retire_at <= self.effective_at:
            raise ValueError("Retirement must follow the effective time")
        return self


class KnowledgeSearch(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    specialty: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=5, ge=1, le=10)


class ConversationCreate(BaseModel):
    patient_id: str


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["turn"]
    question: str = Field(min_length=1, max_length=4000)
    include_documents: bool = False
    include_notes: bool = False
    include_consultations: bool = False
    include_profile: bool = False
    include_knowledge: bool = True
    consultation_id: UUID4 | None = None

    @model_validator(mode="after")
    def validate_consultation_context(self):
        if self.consultation_id and not (self.include_consultations and self.include_notes):
            raise ValueError("A selected consultation requires consultation and note context")
        return self


class CredentialCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    scopes: list[Literal["documents:read", "knowledge:search", "profile:read", "consultations:read"]] = Field(min_length=1, max_length=4)
    patient_id: str | None = None
    expires_at: float

    @model_validator(mode="after")
    def validate_credential(self):
        if not self.label.strip() or len(set(self.scopes)) != len(self.scopes):
            raise ValueError("A label and unique scopes are required")
        if any(scope in self.scopes for scope in ("documents:read", "profile:read", "consultations:read")) and not self.patient_id:
            raise ValueError("Patient-data scopes require a patient")
        if not math.isfinite(self.expires_at) or not time.time() < self.expires_at <= time.time() + 30 * 86400:
            raise ValueError("Expiry must be within 30 days")
        return self


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    full_name: str
    role: str
    verification_status: str
    email_verified_at: float | None = None


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.urlsafe_b64encode(salt + digest).decode()


def check_password(password: str, encoded: str) -> bool:
    raw = base64.urlsafe_b64decode(encoded.encode())
    return hmac.compare_digest(hash_password(password, raw[:16]), encoded)


# A dummy hash keeps the expensive password check for unknown accounts too.
DUMMY_PASSWORD_HASH = hash_password("not-a-user-password", b"\x00" * 16)


async def enforce_auth_limit(request: Request, operation: str, identity: str, identity_limit: int, address_limit: int) -> None:
    # Forwarded headers are not trusted until a deployment configures trusted proxies.
    address = request.client.host if request.client else "unknown"
    try:
        allowed = await auth_rate_limit.check(operation, address, identity, identity_limit, address_limit)
    except auth_rate_limit.RateLimitUnavailable:
        raise HTTPException(503, "Authentication temporarily unavailable")
    if not allowed:
        raise HTTPException(429, "Too many attempts", headers={"Retry-After": "60"})


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def access_token(user_id: str, session_id: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": user_id, "sid": session_id, "iat": now, "exp": now + ACCESS_SECONDS}, SECRET, algorithm="HS256")


def valid_patient_identifier(value: str | None) -> bool:
    try:
        return value is not None and len(value) == 36 and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def audit(db: AsyncSession, actor_id: str | None, action: str, resource: str, outcome: str, patient_id: str | None = None, purpose: str | None = None) -> None:
    uuid_resources = {"patient", "patient_profile", "user", "session", "relationship", "consent", "document", "processing", "credential", "consultation", "note", "conversation", "turn", "assistant_response", "knowledge", "knowledge_import", "discard_check"}
    prefix, separator, identifier = resource.partition(":")
    if separator and prefix in uuid_resources and not valid_patient_identifier(identifier):
        resource = f"{prefix}:invalid"
    elif len(resource) > 100:
        resource = "invalid_resource"
    if patient_id is not None and not valid_patient_identifier(patient_id):
        patient_id = None
    db.add(AuditEvent(actor_id=actor_id, action=action, resource=resource, outcome=outcome, patient_id=patient_id, purpose=purpose, correlation_id=observability.correlation_id.get() or str(uuid.uuid4())))


async def audit_denial(db: AsyncSession, actor_id: str, action: str, resource: str, patient_id: str | None = None, purpose: str | None = None) -> None:
    # Keep an audit event without committing unrelated work from a denied request.
    await db.rollback()
    audit(db, actor_id, action, resource, "denied", patient_id, purpose)
    await db.commit()


async def enforce_write_limit(request: Request, db: AsyncSession, user: User, operation: str, identity_limit: int, address_limit: int) -> None:
    """Fail closed on throttling or limiter outage, recording only bounded metadata."""
    try:
        await enforce_auth_limit(request, operation, user.id, identity_limit, address_limit)
    except HTTPException as exc:
        audit(db, user.id, f"{operation}_rate_limit", operation, "denied" if exc.status_code == 429 else "failed", user.id if user.role == "patient" else None)
        await db.commit()
        if exc.status_code == 503:
            raise HTTPException(503, "Request temporarily unavailable") from None
        raise


async def actor(db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> User:
    if not credentials:
        raise HTTPException(401, "Authentication required")
    try:
        payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
        return await active_identity(db, payload["sub"], payload["sid"])
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "Invalid or expired session")


async def active_identity(db: AsyncSession, user_id: str, session_id: str) -> User:
    session = await db.get(Session, session_id, populate_existing=True)
    user = await db.get(User, user_id, populate_existing=True)
    if not session or session.user_id != user_id or session.revoked_at is not None or session.expires_at <= time.time() or not user or not user.active:
        raise HTTPException(401, "Invalid or expired session")
    return user


Actor = Annotated[User, Depends(actor)]


async def authorize_patient(db: AsyncSession, user: User, patient_id: str, scope: str, action: str) -> None:
    if not valid_patient_identifier(patient_id):
        await audit_denial(db, user.id, action, "patient:invalid")
        raise HTTPException(403, "Patient access is not authorized")
    # A long-lived request or job may hold a stale ORM identity after an
    # administrator deactivates or unverifies the account. Read current
    # account state rather than trusting that cached object.
    account = (await db.execute(select(User.role, User.active, User.verification_status).where(User.id == user.id))).one_or_none()
    actor_active = bool(account and account.active and account.role == user.role)
    patient_active = await db.scalar(select(User.active).where(User.id == patient_id, User.role == "patient"))
    allowed = actor_active and bool(patient_active) and user.role == "patient" and user.id == patient_id
    purpose = None
    if actor_active and user.role == "doctor" and account.verification_status == "verified":
        now = time.time()
        result = await db.execute(select(Consent.purpose).join(Relationship, Consent.relationship_id == Relationship.id).where(
            Relationship.patient_id == patient_id, Relationship.doctor_id == user.id, Relationship.status == "active",
            Consent.status == "active", Consent.scope == scope, Consent.effective_at <= now, Consent.expires_at > now,
        ).limit(1))
        purpose = result.scalar_one_or_none()
        allowed = bool(patient_active) and purpose is not None
    if not allowed:
        await audit_denial(db, user.id, action, f"patient:{patient_id}", patient_id, purpose)
        raise HTTPException(403, "Patient access is not authorized")
    audit(db, user.id, action, f"patient:{patient_id}", "success", patient_id, purpose)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def readiness(db: Db):
    try:
        await db.execute(select(1))
    except SQLAlchemyError:
        raise HTTPException(503, "Dependency unavailable")
    if not await auth_rate_limit.redis_available():
        raise HTTPException(503, "Dependency unavailable")
    return {"status": "ready"}


@app.post("/auth/register", response_model=UserOut, status_code=201)
async def register(data: Register, request: Request, db: Db):
    email = data.email.strip().lower()
    await enforce_auth_limit(request, "register", email, 5, 100)
    name = data.full_name.strip()
    if "@" not in email or not name:
        raise HTTPException(422, "Valid email and full name are required")
    password_hash = await asyncio.to_thread(hash_password, data.password)
    user = User(email=email, full_name=name, role=data.role, password_hash=password_hash, verification_status="pending" if data.role == "doctor" else "not_applicable")
    db.add(user)
    try:
        await db.flush()
        audit(db, user.id, "register", f"user:{user.id}", "success")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Email already registered")
    return user


@app.post("/auth/login")
async def login(data: Login, request: Request, response: Response, db: Db):
    email = data.email.strip().lower()
    await enforce_auth_limit(request, "login", email, 10, 100)
    user = await db.scalar(select(User).where(User.email == email).with_for_update())
    valid_password = await asyncio.to_thread(check_password, data.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if not user or not user.active or not valid_password:
        audit(db, user.id if user else None, "login", "session", "denied")
        await db.commit()
        raise HTTPException(401, "Invalid credentials")
    refresh = secrets.token_urlsafe(48)
    session = Session(user_id=user.id, refresh_hash=token_hash(refresh), expires_at=time.time() + REFRESH_SECONDS)
    db.add(session)
    await db.flush()
    audit(db, user.id, "login", f"session:{session.id}", "success")
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": access_token(user.id, session.id), "refresh_token": refresh, "token_type": "bearer", "user": UserOut.model_validate(user)}


@app.post("/auth/refresh")
async def refresh_session(data: Refresh, request: Request, response: Response, db: Db):
    await enforce_auth_limit(request, "refresh", data.refresh_token, 20, 200)
    now = time.time()
    new_token = secrets.token_urlsafe(48)
    result = await db.execute(update(Session).where(Session.refresh_hash == token_hash(data.refresh_token), Session.revoked_at.is_(None), Session.expires_at > now).values(previous_refresh_hash=Session.refresh_hash, refresh_hash=token_hash(new_token)).returning(Session.id, Session.user_id))
    row = result.one_or_none()
    if row is None:
        await db.rollback()
        raise HTTPException(401, "Invalid or expired refresh token")
    user = await db.get(User, row.user_id)
    if not user or not user.active:
        await db.rollback()
        raise HTTPException(401, "Invalid or expired refresh token")
    audit(db, user.id, "session_refresh", f"session:{row.id}", "success")
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": access_token(user.id, row.id), "refresh_token": new_token, "token_type": "bearer"}


@app.post("/auth/logout", status_code=204)
async def logout(request: Request, db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)], data: LogoutRequest | None = None):
    now = time.time()
    session_id = None
    subject = None
    if credentials:
        try:
            payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
            session_id, subject = payload["sid"], payload["sub"]
        except (jwt.InvalidTokenError, ValueError, KeyError, TypeError):
            pass
    row = None
    if session_id and subject:
        row = (await db.execute(update(Session).where(Session.id == session_id, Session.user_id == subject, Session.revoked_at.is_(None), Session.expires_at > now).values(revoked_at=now).returning(Session.id, Session.user_id))).one_or_none()
    if row is None and data and data.refresh_token:
        await enforce_auth_limit(request, "logout", data.refresh_token, 20, 200)
        digest = token_hash(data.refresh_token)
        # The previous token can revoke only; it cannot rotate or access records.
        row = (await db.execute(update(Session).where(or_(Session.refresh_hash == digest, Session.previous_refresh_hash == digest), Session.revoked_at.is_(None), Session.expires_at > now).values(revoked_at=now).returning(Session.id, Session.user_id))).one_or_none()
    if row is None:
        await db.rollback()
        raise HTTPException(401, "Session revocation could not be confirmed")
    audit(db, row.user_id, "logout", f"session:{row.id}", "success")
    await db.commit()


@app.post("/auth/password-reset/request", status_code=202)
async def request_password_reset(data: ResetRequest, request: Request, db: Db):
    email = data.email.strip().lower()
    await enforce_auth_limit(request, "reset-request", email, 5, 100)
    user = await db.scalar(select(User).where(User.email == email, User.active.is_(True)).with_for_update())
    if user:
        now = time.time()
        reset = PasswordReset(user_id=user.id, expires_at=now + 900)
        db.add(reset)
        await db.flush()
        db.add(ResetDelivery(reset_id=reset.id))
        audit(db, user.id, "password_reset_requested", "account", "success")
        await db.commit()
    return {"detail": "If the account exists, a reset link will be sent."}


@app.post("/auth/password-reset/confirm")
async def confirm_password_reset(data: ResetConfirm, request: Request, db: Db):
    await enforce_auth_limit(request, "reset-confirm", data.token, 10, 100)
    reset_id = password_reset.valid_token(data.token)
    if reset_id is None:
        raise HTTPException(400, "Invalid or expired reset link")
    encoded = await asyncio.to_thread(hash_password, data.password)
    now = time.time()
    user_id = await db.scalar(select(PasswordReset.user_id).where(PasswordReset.id == reset_id))
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update()) if user_id else None
    if not user or not user.active:
        raise HTTPException(400, "Invalid or expired reset link")
    result = await db.execute(update(PasswordReset).where(PasswordReset.id == reset_id, PasswordReset.used_at.is_(None), PasswordReset.expires_at > now).values(used_at=now).returning(PasswordReset.user_id))
    if result.scalar_one_or_none() is None:
        await db.rollback()
        raise HTTPException(400, "Invalid or expired reset link")
    user.password_hash = encoded
    await db.execute(update(EmailVerification).where(EmailVerification.user_id == user.id, EmailVerification.used_at.is_(None)).values(used_at=now))
    await db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None)).values(used_at=now))
    await db.execute(update(Session).where(Session.user_id == user.id, Session.revoked_at.is_(None)).values(revoked_at=now))
    await db.execute(update(IntegrationCredential).where(IntegrationCredential.owner_id == user.id, IntegrationCredential.revoked_at.is_(None)).values(revoked_at=now))
    audit(db, user.id, "password_reset_completed", "account", "success")
    await db.commit()
    return {"detail": "Password changed. Sign in again."}


@app.post("/auth/email-verification/request", status_code=202)
async def request_email_verification(request: Request, user: Actor, db: Db):
    if os.getenv("EMAIL_VERIFICATION_ENABLED", "false").lower() != "true":
        raise HTTPException(503, "Email verification is not configured")
    await enforce_auth_limit(request, "email-verification-request", user.id, 3, 100)
    locked = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if not locked or not locked.active:
        raise HTTPException(401, "Invalid or expired session")
    if locked.email_verified_at is not None:
        raise HTTPException(409, "Email address is already verified")
    now = time.time()
    await db.execute(update(EmailVerification).where(EmailVerification.user_id == locked.id, EmailVerification.used_at.is_(None)).values(used_at=now))
    db.add(EmailVerification(user_id=locked.id, expires_at=now + 900))
    audit(db, locked.id, "email_verification_requested", "account", "success")
    await db.commit()
    return {"detail": "A verification link has been queued."}


@app.post("/auth/email-verification/confirm")
async def confirm_email_verification(data: EmailVerificationConfirm, request: Request, db: Db):
    await enforce_auth_limit(request, "email-verification-confirm", data.token, 10, 100)
    verification_id = email_verification.valid_token(data.token)
    if not verification_id:
        raise HTTPException(400, "Invalid or expired verification link")
    user_id = await db.scalar(select(EmailVerification.user_id).where(EmailVerification.id == verification_id))
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update()) if user_id else None
    if not user or not user.active:
        raise HTTPException(400, "Invalid or expired verification link")
    now = time.time()
    result = await db.execute(update(EmailVerification).where(EmailVerification.id == verification_id, EmailVerification.used_at.is_(None), EmailVerification.expires_at > now).values(used_at=now).returning(EmailVerification.id))
    if result.scalar_one_or_none() is None or user.email_verified_at is not None:
        await db.rollback()
        raise HTTPException(400, "Invalid or expired verification link")
    user.email_verified_at = now
    await db.execute(update(EmailVerification).where(EmailVerification.user_id == user.id, EmailVerification.used_at.is_(None)).values(used_at=now))
    audit(db, user.id, "email_verified", "account", "success")
    await db.commit()
    return {"detail": "Email address verified. Return to your account."}


@app.get("/me", response_model=UserOut)
async def me(user: Actor):
    return user


def require_assistant_account(user: User) -> None:
    if user.role not in ("patient", "doctor"):
        raise HTTPException(403, "Assistant access is not permitted")


@app.get("/assistant/preferences")
async def read_assistant_preferences(user: Actor, db: Db):
    require_assistant_account(user)
    preference = await db.get(AssistantPreference, user.id)
    audit(db, user.id, "assistant_preferences_read", "preferences", "success")
    await db.commit()
    return {"enabled": preference is not None, "response_style": preference.response_style if preference else None}


@app.post("/assistant/preferences")
async def set_assistant_preferences(data: PreferenceInput, user: Actor, db: Db):
    require_assistant_account(user)
    locked = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if not locked or not locked.active:
        raise HTTPException(401, "Invalid or expired session")
    preference = await db.get(AssistantPreference, user.id)
    if preference is None:
        preference = AssistantPreference(user_id=user.id, response_style=data.response_style)
        db.add(preference)
    else:
        preference.response_style = data.response_style
        preference.updated_at = time.time()
    audit(db, user.id, "assistant_preferences_set", "preferences", "success")
    await db.commit()
    return {"enabled": True, "response_style": preference.response_style}


@app.post("/assistant/preferences/clear", status_code=204)
async def clear_assistant_preferences(user: Actor, db: Db):
    require_assistant_account(user)
    locked = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if not locked or not locked.active:
        raise HTTPException(401, "Invalid or expired session")
    await db.execute(delete(AssistantPreference).where(AssistantPreference.user_id == user.id))
    audit(db, user.id, "assistant_preferences_cleared", "preferences", "success")
    await db.commit()


@app.get("/patients/{patient_id}/profile")
async def read_patient_profile(patient_id: str, user: Actor, db: Db):
    await authorize_patient(db, user, patient_id, "read_profile", "profile_read")
    patient = await db.get(User, patient_id)
    profile = await db.get(PatientProfile, patient_id)
    fields = PatientProfileInput.model_fields
    values = {field: getattr(profile, field) if profile else None for field in fields}
    result = {"user_id": patient.id, "full_name": patient.full_name, **values}
    if user.id == patient_id:
        relationships = (await db.scalars(select(Relationship).where(Relationship.patient_id == patient_id, Relationship.status == "active"))).all()
        result["care_team"] = [{"relationship_id": rel.id, "doctor_id": rel.doctor_id} for rel in relationships]
    await authorize_patient(db, user, patient_id, "read_profile", "profile_read_release")
    await db.commit()
    return result


@app.post("/profile/patient")
async def update_patient_profile(data: PatientProfileInput, user: Actor, db: Db):
    if user.role != "patient":
        raise HTTPException(403, "Patient account required")
    locked = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if not locked or not locked.active:
        raise HTTPException(401, "Invalid or expired session")
    profile = await db.get(PatientProfile, user.id)
    if not profile:
        profile = PatientProfile(user_id=user.id)
        db.add(profile)
    for key, value in data.model_dump().items():
        setattr(profile, key, value if value != "" else None)
    audit(db, user.id, "profile_update", f"patient:{user.id}", "success", user.id)
    await db.commit()
    return {"detail": "Profile saved"}


@app.get("/doctors/{doctor_id}/profile")
async def read_doctor_profile(doctor_id: str, user: Actor, db: Db):
    if user.role != "admin" and user.id != doctor_id:
        raise HTTPException(403, "Profile access is not authorized")
    row = (await db.execute(select(User, DoctorProfile).outerjoin(DoctorProfile, DoctorProfile.user_id == User.id).where(User.id == doctor_id, User.role == "doctor").execution_options(populate_existing=True))).one_or_none()
    if not row:
        raise HTTPException(404, "Doctor not found")
    doctor, profile = row
    values = {field: getattr(profile, field) if profile else None for field in DoctorProfileInput.model_fields}
    audit(db, user.id, "doctor_profile_read", f"user:{doctor_id}", "success")
    await db.commit()
    return {"user_id": doctor.id, "full_name": doctor.full_name, "verification_status": doctor.verification_status, "verification_revision": doctor.verification_revision, "active": doctor.active, "profile_revision": profile.revision if profile else 0, **values}


@app.post("/profile/doctor")
async def update_doctor_profile(data: DoctorProfileInput, user: Actor, db: Db):
    if user.role != "doctor":
        raise HTTPException(403, "Doctor account required")
    doctor = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if not doctor or not doctor.active:
        raise HTTPException(401, "Invalid or expired session")
    profile = await db.get(DoctorProfile, user.id)
    if not profile:
        profile = DoctorProfile(user_id=user.id, revision=0)
        db.add(profile)
    values = {key: value if value != "" else None for key, value in data.model_dump().items()}
    changed = any(getattr(profile, key) != value for key, value in values.items())
    for key, value in values.items():
        setattr(profile, key, value)
    if changed:
        profile.revision += 1
    if changed and doctor.verification_status in ("verified", "rejected"):
        doctor.verification_status = "pending"
        doctor.verification_revision += 1
    audit(db, user.id, "doctor_profile_update", f"user:{user.id}", "success")
    await db.commit()
    return {"detail": "Profile saved", "verification_status": doctor.verification_status, "profile_revision": profile.revision}


async def revoke_account_access(db: AsyncSession, locked: User) -> None:
    now = time.time()
    locked.active = False
    await db.execute(update(Session).where(Session.user_id == locked.id, Session.revoked_at.is_(None)).values(revoked_at=now))
    await db.execute(update(IntegrationCredential).where(IntegrationCredential.owner_id == locked.id, IntegrationCredential.revoked_at.is_(None)).values(revoked_at=now))
    await db.execute(update(PasswordReset).where(PasswordReset.user_id == locked.id, PasswordReset.used_at.is_(None)).values(used_at=now))
    await db.execute(update(EmailVerification).where(EmailVerification.user_id == locked.id, EmailVerification.used_at.is_(None)).values(used_at=now))
    await db.execute(update(SocketTicket).where(SocketTicket.user_id == locked.id, SocketTicket.used_at.is_(None)).values(used_at=now))


@app.post("/account/deactivate", status_code=204)
async def deactivate_account(data: DeactivateAccount, request: Request, user: Actor, db: Db):
    await enforce_auth_limit(request, "deactivate", user.id, 10, 100)
    # Serialize with login and reset so no new session survives the change.
    locked = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    valid = await asyncio.to_thread(check_password, data.password, locked.password_hash if locked else DUMMY_PASSWORD_HASH)
    if not locked or not locked.active or not valid:
        audit(db, user.id, "account_deactivation", "account", "denied")
        await db.commit()
        raise HTTPException(401, "Invalid credentials")
    await revoke_account_access(db, locked)
    audit(db, locked.id, "account_deactivation", f"user:{locked.id}", "success")
    await db.commit()


@app.post("/admin/doctors/{doctor_id}/verification")
async def verify_doctor(doctor_id: str, data: VerificationRequest, user: Actor, db: Db):
    if user.role != "admin":
        raise HTTPException(403, "Administrator role required")
    doctor = await db.scalar(select(User).where(User.id == doctor_id).with_for_update())
    if not doctor or doctor.role != "doctor":
        raise HTTPException(404, "Doctor not found")
    profile = await db.get(DoctorProfile, doctor_id)
    revision = profile.revision if profile else 0
    if not doctor.active or revision != data.expected_profile_revision or doctor.verification_revision != data.expected_verification_revision or doctor.verification_status != data.expected_status:
        audit(db, user.id, "doctor_verification_conflict", f"user:{doctor_id}", "denied")
        await db.commit()
        raise HTTPException(409, "Clinician profile or status changed; review it again")
    previous_status = doctor.verification_status
    doctor.verification_status = "verified" if data.verified else "rejected"
    doctor.verification_revision += 1
    db.add(DoctorVerificationDecision(doctor_id=doctor_id, reviewer_id=user.id, profile_revision=revision, previous_status=previous_status, decision=doctor.verification_status))
    audit(db, user.id, "doctor_verification", f"user:{doctor_id}", "success")
    await db.commit()
    return {"doctor_id": doctor_id, "verification_status": doctor.verification_status, "verification_revision": doctor.verification_revision}


@app.get("/admin/doctors/{doctor_id}/verification-history")
async def doctor_verification_history(doctor_id: str, user: Actor, db: Db):
    if user.role != "admin":
        raise HTTPException(403, "Administrator role required")
    doctor = await db.get(User, doctor_id)
    if not doctor or doctor.role != "doctor":
        raise HTTPException(404, "Doctor not found")
    decisions = (await db.scalars(select(DoctorVerificationDecision).where(DoctorVerificationDecision.doctor_id == doctor_id).order_by(DoctorVerificationDecision.created_at.desc(), DoctorVerificationDecision.id.desc()).limit(100))).all()
    result = [{"id": d.id, "reviewer_id": d.reviewer_id, "profile_revision": d.profile_revision, "previous_status": d.previous_status, "decision": d.decision, "created_at": d.created_at} for d in decisions]
    audit(db, user.id, "doctor_verification_history_read", f"user:{doctor_id}", "success")
    await db.commit()
    return result


@app.post("/relationships", status_code=201)
async def request_relationship(data: RelationshipRequest, request: Request, user: Actor, db: Db):
    if user.role != "patient":
        raise HTTPException(403, "Only patients can create a relationship")
    await enforce_write_limit(request, db, user, "care-access-write", 60, 600)
    doctor = await db.get(User, data.doctor_id)
    if not doctor or doctor.role != "doctor" or not doctor.active or doctor.verification_status != "verified":
        raise HTTPException(404, "Verified doctor not found")
    rel = Relationship(patient_id=user.id, doctor_id=doctor.id, purpose=data.purpose)
    db.add(rel)
    try:
        await db.flush()
        queue_notification(db, recipient_id=doctor.id, patient_id=user.id, relationship_id=rel.id, kind="relationship_created", event_key=f"relationship:{rel.id}", resource_id=rel.id)
        audit(db, user.id, "relationship_create", f"relationship:{rel.id}", "success", user.id, data.purpose)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Relationship already exists")
    return {"id": rel.id, "patient_id": rel.patient_id, "doctor_id": rel.doctor_id, "status": rel.status, "purpose": rel.purpose}


@app.get("/relationships")
async def list_relationships(user: Actor, db: Db):
    if user.role not in ("patient", "doctor"):
        raise HTTPException(403, "Role not permitted")
    condition = Relationship.patient_id == user.id if user.role == "patient" else Relationship.doctor_id == user.id
    rows = (await db.scalars(select(Relationship).where(condition))).all()
    return [{"id": r.id, "patient_id": r.patient_id, "doctor_id": r.doctor_id, "status": r.status, "purpose": r.purpose} for r in rows]


@app.post("/doctor/worklist")
async def doctor_worklist(data: WorklistSearch, response: Response, user: Actor, db: Db):
    if user.role != "doctor" or user.verification_status != "verified":
        audit(db, user.id, "doctor_worklist", "worklist", "denied")
        await db.commit()
        raise HTTPException(403, "Verified doctor authorization required")
    term = data.query.strip()
    now = time.time()
    active_grant = select(Consent.id).where(Consent.relationship_id == Relationship.id, Consent.status == "active", Consent.effective_at <= now, Consent.expires_at > now).exists()
    patient_query = select(User.id, User.full_name, Relationship.id.label("relationship_id")).join(Relationship, Relationship.patient_id == User.id).where(
        User.role == "patient", User.active.is_(True), Relationship.doctor_id == user.id, Relationship.status == "active", active_grant)
    if term:
        patient_query = patient_query.where((User.full_name.icontains(term, autoescape=True)) | (User.id == term))
    if data.patient_cursor is not None:
        anchor = (await db.execute(patient_query.where(User.id == str(data.patient_cursor)))).first()
        if not anchor:
            raise HTTPException(422, "Invalid patient cursor; refresh the worklist")
        patient_query = patient_query.where(or_(User.full_name > anchor.full_name, (User.full_name == anchor.full_name) & (User.id > anchor.id)))
    page = (await db.execute(patient_query.order_by(User.full_name, User.id).limit(101))).all()
    people = page[:100]
    rel_ids = [row.relationship_id for row in people]
    scopes: dict[str, set[str]] = {rel: set() for rel in rel_ids}
    if rel_ids:
        rows = (await db.execute(select(Consent.relationship_id, Consent.scope).where(Consent.relationship_id.in_(rel_ids), Consent.status == "active", Consent.effective_at <= now, Consent.expires_at > now))).all()
        for rel_id, scope in rows:
            scopes[rel_id].add(scope)
    patients = [{"patient_id": person.id, "full_name": person.full_name, "relationship_id": person.relationship_id, "scopes": sorted(scopes[person.relationship_id])} for person in people if scopes[person.relationship_id]]
    document_patients = [person["patient_id"] for person in patients if "read_documents" in person["scopes"]]
    note_patients = [person["patient_id"] for person in patients if "clinical_notes" in person["scopes"]]
    review_patients = [person["patient_id"] for person in patients if {"read_documents", "review_documents"}.issubset(person["scopes"])]
    upload_query = select(Document).where(Document.patient_id.in_(document_patients), Document.deleted_at.is_(None))
    if data.upload_cursor is not None:
        anchor = await db.scalar(select(Document).where(Document.id == str(data.upload_cursor), Document.patient_id.in_(document_patients)))
        if not anchor:
            raise HTTPException(422, "Invalid upload cursor; refresh the worklist")
        upload_query = upload_query.where(or_(Document.created_at < anchor.created_at, (Document.created_at == anchor.created_at) & (Document.id > anchor.id)))
    upload_page = (await db.scalars(upload_query.order_by(Document.created_at.desc(), Document.id).limit(51))).all() if document_patients else []
    uploads = upload_page[:50]
    note_query = select(ClinicalNote, ClinicalNoteVersion).join(ClinicalNoteVersion, ClinicalNoteVersion.note_id == ClinicalNote.id).where(ClinicalNote.patient_id.in_(note_patients), ClinicalNoteVersion.status == "pending_review")
    if NOTE_REVIEW_POLICY == "independent":
        note_query = note_query.where(ClinicalNoteVersion.author_id != user.id)
    if data.note_cursor is not None:
        anchor = await db.scalar(select(ClinicalNoteVersion).join(ClinicalNote).where(ClinicalNoteVersion.id == str(data.note_cursor), ClinicalNote.patient_id.in_(note_patients)))
        if not anchor:
            raise HTTPException(422, "Invalid note cursor; refresh the worklist")
        note_query = note_query.where(or_(ClinicalNoteVersion.created_at < anchor.created_at, (ClinicalNoteVersion.created_at == anchor.created_at) & (ClinicalNoteVersion.id > anchor.id)))
    note_page = (await db.execute(note_query.order_by(ClinicalNoteVersion.created_at.desc(), ClinicalNoteVersion.id).limit(51))).all() if note_patients else []
    pending_notes = note_page[:50]
    pending_extractions = []
    extraction_page = []
    if data.extraction_cursor is not None:
        anchor = await db.scalar(select(ExtractionJob).join(Document).where(ExtractionJob.id == str(data.extraction_cursor), Document.patient_id.in_(review_patients)))
        if not anchor:
            raise HTTPException(422, "Invalid transcription cursor; refresh the worklist")
    if review_patients:
        newest_job = select(ExtractionJob.id).where(ExtractionJob.document_id == Document.id).order_by(ExtractionJob.created_at.desc(), ExtractionJob.id.desc()).limit(1).correlate(Document).scalar_subquery()
        latest_version = select(func.max(StructuredExtractionVersion.version)).where(StructuredExtractionVersion.job_id == ExtractionJob.id).correlate(ExtractionJob).scalar_subquery()
        extraction_query = select(Document, ExtractionJob, StructuredExtractionVersion).join(ExtractionJob, ExtractionJob.id == newest_job).join(StructuredExtractionVersion, StructuredExtractionVersion.job_id == ExtractionJob.id).where(Document.patient_id.in_(review_patients), Document.deleted_at.is_(None), ExtractionJob.status == "completed", StructuredExtractionVersion.version == latest_version, StructuredExtractionVersion.review_status == "unverified")
        if data.extraction_cursor is not None:
            extraction_query = extraction_query.where(or_(ExtractionJob.created_at < anchor.created_at, (ExtractionJob.created_at == anchor.created_at) & (ExtractionJob.id > anchor.id)))
        extraction_page = (await db.execute(extraction_query.order_by(ExtractionJob.created_at.desc(), ExtractionJob.id).limit(51))).all()
        pending_extractions = [{"patient_id": doc.patient_id, "document_id": doc.id, "filename": doc.filename, "job_id": job.id, "created_at": job.created_at} for doc, job, _ in extraction_page[:50]]
    # Recheck accounts and grants after the worklist queries, before release.
    await db.refresh(user)
    if not user.active or user.role != "doctor" or user.verification_status != "verified":
        raise HTTPException(403, "Verified doctor authorization required")
    current: dict[str, set[str]] = {}
    if rel_ids:
        checked_at = time.time()
        valid = (await db.execute(select(Relationship.patient_id, Consent.scope).join(User, User.id == Relationship.patient_id).join(Consent, Consent.relationship_id == Relationship.id).where(Relationship.id.in_(rel_ids), Relationship.doctor_id == user.id, Relationship.status == "active", User.active.is_(True), User.role == "patient", Consent.status == "active", Consent.effective_at <= checked_at, Consent.expires_at > checked_at))).all()
        for patient_id, scope in valid:
            current.setdefault(patient_id, set()).add(scope)
    patients = [{**person, "scopes": sorted(current[person["patient_id"]])} for person in patients if person["patient_id"] in current]
    uploads = [doc for doc in uploads if "read_documents" in current.get(doc.patient_id, set())]
    pending_notes = [(note, version) for note, version in pending_notes if "clinical_notes" in current.get(note.patient_id, set())]
    pending_extractions = [item for item in pending_extractions if {"read_documents", "review_documents"}.issubset(current.get(item["patient_id"], set()))]
    for person in patients:
        audit(db, user.id, "doctor_worklist_patient", f"patient:{person['patient_id']}", "success", person["patient_id"])
    audit(db, user.id, "doctor_worklist", "worklist", "success")
    result = {"patients": patients, "recent_uploads": [{"patient_id": doc.patient_id, "document_id": doc.id, "filename": doc.filename, "created_at": doc.created_at} for doc in uploads],
              "pending_notes": [{"patient_id": note.patient_id, "note_id": note.id, "version_id": version.id, "created_at": version.created_at} for note, version in pending_notes], "pending_extractions": pending_extractions,
              "next_patient_cursor": patients[-1]["patient_id"] if len(page) > 100 and patients else None,
              "next_upload_cursor": uploads[-1].id if len(upload_page) > 50 and uploads else None,
              "next_note_cursor": pending_notes[-1][1].id if len(note_page) > 50 and pending_notes else None,
              "next_extraction_cursor": pending_extractions[-1]["job_id"] if len(extraction_page) > 50 and pending_extractions else None}
    response.headers["Cache-Control"] = "no-store"
    await db.commit()
    return result


@app.post("/relationships/{relationship_id}/end")
async def end_relationship(relationship_id: str, user: Actor, db: Db):
    rel = await db.scalar(select(Relationship).where(Relationship.id == relationship_id).with_for_update())
    if not rel or user.id not in (rel.patient_id, rel.doctor_id):
        raise HTTPException(403, "Only a care team member may end this relationship")
    if rel.status == "active":
        rel.status = "ended"
        audit(db, user.id, "relationship_end", f"relationship:{rel.id}", "success", rel.patient_id, rel.purpose)
        await db.commit()
    return {"id": rel.id, "status": rel.status}


@app.post("/relationships/{relationship_id}/restore")
async def restore_relationship(relationship_id: str, request: Request, user: Actor, db: Db):
    await enforce_write_limit(request, db, user, "care-access-write", 60, 600)
    rel = await db.scalar(select(Relationship).where(Relationship.id == relationship_id).with_for_update())
    if not rel or user.role != "patient" or rel.patient_id != user.id:
        raise HTTPException(403, "Only the patient may restore this relationship")
    if rel.status != "ended":
        raise HTTPException(409, "Only an ended relationship can be restored")
    doctor = await db.get(User, rel.doctor_id)
    if not doctor or not doctor.active or doctor.role != "doctor" or doctor.verification_status != "verified":
        raise HTTPException(409, "Doctor is not currently available for a care relationship")
    # Previous grants, including scheduled grants, must never resume automatically.
    now = time.time()
    grants = (await db.scalars(select(Consent).where(Consent.relationship_id == rel.id, Consent.status == "active").with_for_update())).all()
    for grant in grants:
        grant.status = "withdrawn"
        grant.withdrawn_at = now
        audit(db, user.id, "consent_withdraw_on_relationship_restore", f"consent:{grant.id}", "success", rel.patient_id, grant.purpose)
    rel.status = "active"
    audit(db, user.id, "relationship_restore", f"relationship:{rel.id}", "success", rel.patient_id, rel.purpose)
    queue_notification(db, recipient_id=rel.doctor_id, patient_id=rel.patient_id, relationship_id=rel.id, kind="relationship_restored", event_key=f"relationship-restored:{uuid.uuid4()}", resource_id=rel.id)
    await db.commit()
    return {"id": rel.id, "status": rel.status, "previous_grants_withdrawn": len(grants)}


@app.post("/consents", status_code=201)
async def create_consent(data: ConsentRequest, request: Request, user: Actor, db: Db):
    await enforce_write_limit(request, db, user, "care-access-write", 60, 600)
    rel = await db.scalar(select(Relationship).where(Relationship.id == data.relationship_id).with_for_update())
    if user.role != "patient" or not rel or rel.patient_id != user.id or rel.status != "active":
        raise HTTPException(403, "Only the patient may grant consent for an active relationship")
    now = time.time()
    if not math.isfinite(data.expires_at) or data.expires_at <= now:
        raise HTTPException(422, "Consent expiry must be in the future")
    effective_at = now if data.effective_at is None else data.effective_at
    if not math.isfinite(effective_at) or effective_at < now or effective_at >= data.expires_at:
        raise HTTPException(422, "Consent start must be in the future and before expiry")
    consent = Consent(relationship_id=rel.id, scope=data.scope, purpose=data.purpose, effective_at=effective_at, expires_at=data.expires_at)
    db.add(consent)
    await db.flush()
    queue_notification(db, recipient_id=rel.doctor_id, patient_id=user.id, relationship_id=rel.id, kind="consent_changed", event_key=f"consent-granted:{consent.id}", resource_id=consent.id)
    audit(db, user.id, "consent_grant", f"consent:{consent.id}", "success", user.id, data.purpose)
    await db.commit()
    return {"id": consent.id, "relationship_id": rel.id, "scope": consent.scope, "purpose": consent.purpose, "status": consent.status, "effective_at": consent.effective_at, "expires_at": consent.expires_at}


@app.get("/consents")
async def list_consents(user: Actor, db: Db):
    if user.role != "patient":
        raise HTTPException(403, "Only patients may view their consent history")
    rows = (await db.execute(select(Consent, Relationship).join(Relationship, Consent.relationship_id == Relationship.id).where(Relationship.patient_id == user.id).order_by(Consent.effective_at.desc(), Consent.id))).all()
    return [{"id": c.id, "relationship_id": c.relationship_id, "doctor_id": r.doctor_id, "relationship_status": r.status, "scope": c.scope, "purpose": c.purpose, "status": c.status, "effective_at": c.effective_at, "expires_at": c.expires_at, "withdrawn_at": c.withdrawn_at} for c, r in rows]


@app.post("/consents/{consent_id}/withdraw")
async def withdraw_consent(consent_id: str, user: Actor, db: Db):
    consent = await db.scalar(select(Consent).where(Consent.id == consent_id).with_for_update())
    rel = await db.get(Relationship, consent.relationship_id) if consent else None
    if user.role != "patient" or not rel or rel.patient_id != user.id:
        raise HTTPException(403, "Only the patient may withdraw consent")
    if consent.status == "active":
        consent.status = "withdrawn"
        consent.withdrawn_at = time.time()
        queue_notification(db, recipient_id=rel.doctor_id, patient_id=user.id, relationship_id=rel.id, kind="consent_changed", event_key=f"consent-withdrawn:{consent.id}", resource_id=consent.id)
        audit(db, user.id, "consent_withdraw", f"consent:{consent.id}", "success", user.id, consent.purpose)
        await db.commit()
    return {"id": consent.id, "status": consent.status}


async def authorize_care_messages(db: AsyncSession, user: User, relationship_id: str, *, sending: bool) -> Relationship:
    rel = await db.get(Relationship, relationship_id)
    if not rel or user.role not in ("patient", "doctor") or user.id not in (rel.patient_id, rel.doctor_id):
        await audit_denial(db, user.id, "care_message_access", f"relationship:{relationship_id}")
        raise HTTPException(403, "Care message access is not authorized")
    if user.id == rel.patient_id and not sending:
        # Patients retain access to their own past messages after consent withdrawal.
        return rel
    if user.id == rel.doctor_id:
        await authorize_patient(db, user, rel.patient_id, "care_messages", "care_message_access")
        return rel
    doctor = await db.get(User, rel.doctor_id)
    now = time.time()
    active_grant = await db.scalar(select(Consent.id).where(Consent.relationship_id == rel.id, Consent.scope == "care_messages", Consent.status == "active", Consent.effective_at <= now, Consent.expires_at > now).limit(1))
    if rel.status != "active" or not doctor or not doctor.active or doctor.verification_status != "verified" or not active_grant:
        await audit_denial(db, user.id, "care_message_access", f"relationship:{rel.id}", rel.patient_id)
        raise HTTPException(403, "Care message access is not authorized")
    return rel


@app.get("/relationships/{relationship_id}/messages")
async def list_care_messages(relationship_id: str, response: Response, user: Actor, db: Db, cursor: str | None = None):
    rel = await authorize_care_messages(db, user, relationship_id, sending=False)
    query = select(CareMessage).where(CareMessage.relationship_id == rel.id)
    if cursor is not None:
        cursor_id = safe_uuid(cursor)
        anchor = await db.get(CareMessage, cursor_id) if cursor_id else None
        if not anchor or anchor.relationship_id != rel.id:
            raise HTTPException(422, "Invalid message cursor")
        query = query.where(or_(CareMessage.created_at < anchor.created_at, (CareMessage.created_at == anchor.created_at) & (CareMessage.id < anchor.id)))
    rows = (await db.scalars(query.order_by(CareMessage.created_at.desc(), CareMessage.id.desc()).limit(101))).all()
    if len(rows) > 100:
        response.headers["X-Next-Cursor"] = rows[99].id
    response.headers["Cache-Control"] = "no-store"
    audit(db, user.id, "care_messages_read", f"relationship:{rel.id}", "success", rel.patient_id)
    await db.commit()
    return [{"id": row.id, "sender_id": row.sender_id, "content": row.content, "created_at": row.created_at} for row in reversed(rows[:100])]


@app.post("/relationships/{relationship_id}/messages", status_code=201)
async def send_care_message(relationship_id: str, data: CareMessageInput, request: Request, user: Actor, db: Db):
    await enforce_auth_limit(request, "care-message-send", user.id, 30, 200)
    rel = await authorize_care_messages(db, user, relationship_id, sending=True)
    message = CareMessage(relationship_id=rel.id, sender_id=user.id, content=data.content)
    db.add(message)
    await db.flush()
    recipient = rel.doctor_id if user.id == rel.patient_id else rel.patient_id
    queue_notification(db, recipient_id=recipient, patient_id=rel.patient_id, relationship_id=rel.id, required_scope="care_messages", kind="care_message_received", event_key=f"care-message:{message.id}", resource_id=rel.id)
    audit(db, user.id, "care_message_created", f"relationship:{rel.id}", "success", rel.patient_id)
    await db.commit()
    return {"id": message.id, "sender_id": message.sender_id, "content": message.content, "created_at": message.created_at}


class MultipartTooLarge(MultiPartException):
    pass


async def bounded_multipart_stream(request: Request):
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD:
            raise MultipartTooLarge("Request too large")
        yield chunk


class SafeMultipartParser(MultiPartParser):
    async def parse(self):
        try:
            return await super().parse()
        except BaseException:
            # Starlette's parser closes these on multipart errors but not all
            # disconnects or cancellations in the pinned version.
            for file in self._files_to_close_on_error:
                file.close()
            raise


async def parse_upload_form(request: Request):
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data;"):
        raise HTTPException(415, "Multipart upload required")
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise HTTPException(400, "Invalid content length")
        if int(length) > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD:
            raise HTTPException(413, "Request too large")
    parser = SafeMultipartParser(request.headers, bounded_multipart_stream(request), max_files=1, max_fields=0)
    try:
        form = await parser.parse()
    except MultipartTooLarge:
        raise HTTPException(413, "Request too large")
    except MultiPartException:
        raise HTTPException(400, "Invalid multipart upload")
    except ParseError:
        raise HTTPException(400, "Invalid multipart upload")
    items = form.multi_items()
    if len(items) != 1 or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
        await form.close()
        raise HTTPException(422, "Exactly one file is required")
    return form, items[0][1]


@app.post("/documents", status_code=201, openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}}}}}})
async def upload_document(request: Request, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    if user.role != "patient":
        raise HTTPException(403, "Only patients may upload")
    await enforce_write_limit(request, db, user, "document-upload", 20, 200)
    form, file = await parse_upload_form(request)
    try:
        session_id = None
        if DOCUMENT_STORAGE_BACKEND == "s3":
            try:
                payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
                if payload["sub"] != user.id:
                    raise ValueError("session subject mismatch")
                session_id = payload["sid"]
            except (jwt.InvalidTokenError, ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(401, "Invalid or expired session")
        return await save_document(file, user, db, session_id=session_id)
    finally:
        await form.close()


async def scan_upload(path: Path, user: User, db: AsyncSession, resource: str) -> str:
    try:
        return await malware_scan.scan(path)
    except malware_scan.ThreatDetected:
        audit(db, user.id, "upload_scan", resource, "denied", user.id if user.role == "patient" else None)
        await db.commit()
        raise HTTPException(422, "File rejected by malware scanner")
    except malware_scan.ScannerUnavailable:
        audit(db, user.id, "upload_scan", resource, "failed", user.id if user.role == "patient" else None)
        await db.commit()
        raise HTTPException(503, "Malware scanner unavailable")


async def save_document(file: UploadFile, user: User, db: AsyncSession, *, session_id: str | None = None):
    allowed = {"application/pdf": (b"%PDF-", ".pdf"), "image/jpeg": (b"\xff\xd8\xff", ".jpg"), "image/png": (b"\x89PNG\r\n\x1a\n", ".png"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04", ".docx")}
    if file.content_type not in allowed:
        raise HTTPException(415, "Only PDF, DOCX, JPEG, and PNG are supported")
    name = file.filename or ""
    if not name or Path(name).name != name or "\\" in name or len(name) > 160 or any(ord(c) < 32 for c in name):
        raise HTTPException(400, "Unsafe filename")
    signature, extension = allowed[file.content_type]
    if Path(name).suffix.lower() not in ({".jpg", ".jpeg"} if extension == ".jpg" else {extension}):
        raise HTTPException(415, "File extension does not match MIME type")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    doc_id = str(uuid.uuid4())
    directory = UPLOAD_DIR / object_staging.STAGING_SUBDIR if DOCUMENT_STORAGE_BACKEND == "s3" else UPLOAD_DIR
    if DOCUMENT_STORAGE_BACKEND == "s3":
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or not directory.is_dir():
            raise HTTPException(503, "Object staging is unavailable")
    path = directory / doc_id
    size = 0
    digest = hashlib.sha256()
    cleanup_transferred = False
    staging_fd = None
    file_created = False
    try:
        with path.open("xb") as dest:
            file_created = True
            if DOCUMENT_STORAGE_BACKEND == "s3":
                staging_fd = object_staging.lock_upload(dest.fileno())
            os.chmod(path, 0o600)
            first = await file.read(65536)
            if not first.startswith(signature):
                raise HTTPException(415, "File signature does not match MIME type")
            chunk = first
            while chunk:
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File too large")
                dest.write(chunk)
                digest.update(chunk)
                chunk = await file.read(65536)
        scan_status = await scan_upload(path, user, db, "document_upload")
        await db.refresh(user)
        if not user.active or user.role != "patient":
            raise HTTPException(403, "Active patient account required")
        actor_id, media_type = user.id, file.content_type
        if DOCUMENT_STORAGE_BACKEND == "s3":
            if not session_id:
                raise HTTPException(401, "Invalid or expired session")
            settings = document_storage.config()
            try:
                await asyncio.wait_for(OBJECT_UPLOAD_SLOTS.acquire(), timeout=2)
            except asyncio.TimeoutError:
                raise HTTPException(503, "Object upload is busy") from None
            task = None
            try:
                correlation = observability.correlation_id.get() or str(uuid.uuid4())
                await db.rollback()  # Do not retain the request transaction during remote I/O.
                try:
                    intent_id = await object_document_submissions.prepare_document(SessionFactory, actor_id=actor_id, session_id=session_id, store_id=settings.store_id, bucket=settings.bucket, object_id=doc_id, size=size, sha256=digest.hexdigest(), filename=name, content_type=media_type, scan_status=scan_status, correlation_id=correlation)
                except PermissionError:
                    raise HTTPException(401, "Session no longer authorized") from None
                async def store_and_complete():
                    try:
                        # Check the same bounded bytes after scanning before sending them.
                        content = await asyncio.to_thread(file_integrity.read_snapshot, path, size, digest.hexdigest(), MAX_UPLOAD_BYTES)
                        provider = await asyncio.to_thread(document_storage.store, settings)
                        ref = await asyncio.to_thread(provider.put, 'documents', doc_id, content)
                        await object_upload_intents.record(SessionFactory, intent_id, store_id=settings.store_id, ref=ref)
                        await object_document_submissions.complete_document(SessionFactory, actor_id=actor_id, intent_id=intent_id, store_id=settings.store_id)
                    finally:
                        try:
                            path.unlink(missing_ok=True)
                        finally:
                            if staging_fd is not None:
                                os.close(staging_fd)
                task = asyncio.create_task(store_and_complete())
                cleanup_transferred = True
                def observe(done: asyncio.Task) -> None:
                    OBJECT_UPLOAD_SLOTS.release()
                    if not done.cancelled():
                        done.exception()
                task.add_done_callback(observe)
                try:
                    await asyncio.shield(task)
                except PermissionError:
                    raise HTTPException(401, "Session no longer authorized; upload remains incomplete") from None
                except Exception:
                    raise HTTPException(503, "Upload outcome is uncertain; check documents and incomplete uploads") from None
            finally:
                if task is None:
                    OBJECT_UPLOAD_SLOTS.release()
        else:
            document = Document(id=doc_id, patient_id=actor_id, filename=name, content_type=media_type, size=size, sha256=digest.hexdigest(), scan_status=scan_status)
            db.add(document)
            audit(db, actor_id, "document_upload", f"document:{doc_id}", "success", actor_id)
            await db.commit()
    except BaseException:
        if not cleanup_transferred and file_created:
            try:
                path.unlink(missing_ok=True)
            finally:
                if staging_fd is not None:
                    os.close(staging_fd)
        raise
    finally:
        await file.close()
    return {"id": doc_id, "patient_id": actor_id, "filename": name, "content_type": media_type, "size": size, "review_status": "unverified", "scan_status": scan_status}


@app.get("/documents/incomplete-uploads")
async def incomplete_document_uploads(response: Response, user: Actor, db: Db, cursor: UUID4 | None = None):
    if user.role != "patient":
        raise HTTPException(403, "Patient account required")
    query = (select(ObjectUploadIntent, ObjectDocumentSubmission, ObjectUploadDiscard)
        .join(ObjectDocumentSubmission, ObjectDocumentSubmission.intent_id == ObjectUploadIntent.id)
        .outerjoin(DocumentObjectLink, DocumentObjectLink.intent_id == ObjectUploadIntent.id)
        .outerjoin(ObjectUploadDiscard, ObjectUploadDiscard.intent_id == ObjectUploadIntent.id)
        .where(ObjectUploadIntent.actor_id == user.id, ObjectUploadIntent.namespace == "documents", DocumentObjectLink.document_id.is_(None)))
    if cursor is not None:
        anchor = await db.scalar(select(ObjectUploadIntent).join(ObjectDocumentSubmission).where(ObjectUploadIntent.id == str(cursor), ObjectUploadIntent.actor_id == user.id, ObjectUploadIntent.namespace == "documents"))
        if not anchor:
            raise HTTPException(422, "Invalid upload cursor")
        query = query.where(or_(ObjectUploadIntent.created_at < anchor.created_at, (ObjectUploadIntent.created_at == anchor.created_at) & (ObjectUploadIntent.id < anchor.id)))
    rows = (await db.execute(query.order_by(ObjectUploadIntent.created_at.desc(), ObjectUploadIntent.id.desc()).limit(51))).all()
    if len(rows) > 50:
        response.headers["X-Next-Cursor"] = rows[49][0].id
    result = [{"id": intent.id, "filename": submission.filename, "status": "discarding" if discard else intent.status, "created_at": intent.created_at, "cleanup_result": discard.last_result if discard else None, "cleanup_checked_at": discard.checked_at if discard else None, "cleanup_next_check_at": discard.next_check_at if discard else None} for intent, submission, discard in rows[:50]]
    audit(db, user.id, "object_document_incomplete_list", "documents", "success", user.id)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@app.post("/documents/incomplete-uploads/{intent_id}/resume")
async def resume_document_upload(intent_id: UUID4, request: Request, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    if user.role != "patient":
        raise HTTPException(403, "Patient account required")
    await enforce_auth_limit(request, "document-upload-resume", user.id, 20, 100)
    intent = await db.get(ObjectUploadIntent, str(intent_id))
    if not intent or intent.actor_id != user.id or intent.namespace != "documents" or not await db.get(ObjectDocumentSubmission, intent.id):
        raise HTTPException(404, "Incomplete upload not found")
    if await db.get(ObjectUploadDiscard, intent.id):
        raise HTTPException(409, "Upload has been discarded")
    try:
        token = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
        if token["sub"] != user.id:
            raise ValueError
        settings = document_storage.config()
        if intent.store_id != settings.store_id or intent.bucket != settings.bucket:
            raise document_storage.StorageUnavailable("Storage identity changed")
        actor_id, submission_id, session_id = user.id, intent.id, token["sid"]
        correlation = observability.correlation_id.get() or str(uuid.uuid4())
        await db.rollback()
        await object_document_submissions.rebind_session(SessionFactory, actor_id=actor_id, session_id=session_id, intent_id=submission_id, store_id=settings.store_id, correlation_id=correlation)
        try:
            await asyncio.wait_for(OBJECT_UPLOAD_SLOTS.acquire(), timeout=2)
        except asyncio.TimeoutError:
            raise HTTPException(503, "Object upload is busy") from None
        try:
            async def recover_object():
                provider = await asyncio.to_thread(document_storage.store, settings)
                return await object_upload_intents.recover(SessionFactory, submission_id, store_id=settings.store_id, store=provider)
            recovery = asyncio.create_task(recover_object())
        except BaseException:
            OBJECT_UPLOAD_SLOTS.release()
            raise
        def release_recovery(done: asyncio.Task) -> None:
            OBJECT_UPLOAD_SLOTS.release()
            if not done.cancelled():
                done.exception()
        recovery.add_done_callback(release_recovery)
        ref = await asyncio.shield(recovery)
        if ref is None:
            raise HTTPException(409, "No stored object was found yet; retry later or upload again")
        document_id = await object_document_submissions.complete_document(SessionFactory, actor_id=actor_id, intent_id=submission_id, store_id=settings.store_id)
        return {"id": document_id, "status": "completed"}
    except HTTPException:
        raise
    except PermissionError:
        raise HTTPException(401, "Session no longer authorized") from None
    except (jwt.InvalidTokenError, KeyError, TypeError, AttributeError):
        raise HTTPException(401, "Invalid or expired session") from None
    except Exception:
        raise HTTPException(503, "Upload could not be resumed; try again later") from None


@app.post("/documents/incomplete-uploads/{intent_id}/discard", status_code=202)
async def discard_document_upload(intent_id: UUID4, request: Request, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    if user.role != "patient":
        raise HTTPException(403, "Patient account required")
    await enforce_auth_limit(request, "document-upload-discard", user.id, 20, 100)
    try:
        token = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
        if token["sub"] != user.id:
            raise ValueError("Session identity changed")
        actor_id, session_id = user.id, token["sid"]
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(401, "Invalid or expired session") from None
    correlation = observability.correlation_id.get() or str(uuid.uuid4())
    await db.rollback()
    try:
        await object_document_submissions.discard_document(SessionFactory, actor_id=actor_id, session_id=session_id, intent_id=str(intent_id), correlation_id=correlation)
    except LookupError:
        raise HTTPException(404, "Incomplete upload not found") from None
    except PermissionError:
        raise HTTPException(401, "Session no longer authorized") from None
    except ValueError:
        raise HTTPException(409, "Upload is already associated with a document") from None
    return {"status": "monitoring"}


@app.get("/patients/{patient_id}/documents")
async def list_documents(patient_id: str, response: Response, user: Actor, db: Db, cursor: UUID4 | None = None):
    await authorize_patient(db, user, patient_id, "read_documents", "document_list")
    query = select(Document).where(Document.patient_id == patient_id, Document.deleted_at.is_(None))
    if cursor is not None:
        anchor = await db.scalar(select(Document).where(Document.id == str(cursor), Document.patient_id == patient_id))
        if not anchor:
            raise HTTPException(422, "Invalid document cursor")
        query = query.where(or_(Document.created_at < anchor.created_at, (Document.created_at == anchor.created_at) & (Document.id < anchor.id)))
    rows = (await db.scalars(query.order_by(Document.created_at.desc(), Document.id.desc()).limit(101))).all()
    if len(rows) > 100:
        response.headers["X-Next-Cursor"] = rows[99].id
    response.headers["Cache-Control"] = "no-store"
    await authorize_patient(db, user, patient_id, "read_documents", "document_list_release")
    await db.commit()
    return [{"id": d.id, "patient_id": d.patient_id, "filename": d.filename, "content_type": d.content_type, "size": d.size, "review_status": d.review_status, "scan_status": d.scan_status} for d in rows[:100]]


@app.post("/documents/{document_id}/delete", status_code=202)
async def delete_document(document_id: str, data: DocumentDeletion, request: Request, user: Actor, db: Db):
    await enforce_auth_limit(request, "document-delete", user.id, 20, 100)
    doc = await db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if not doc or user.role != "patient" or doc.patient_id != user.id:
        audit(db, user.id, "document_deletion", f"document:{document_id}", "denied", doc.patient_id if doc else None)
        await db.commit()
        raise HTTPException(403, "Only the document owner can request deletion")
    if doc.deleted_at is None:
        now = time.time()
        doc.deleted_at, doc.purge_status, doc.purge_after = now, "pending", now
        doc.filename, doc.size = "Deleted document", 0
        job_ids = select(ExtractionJob.id).where(ExtractionJob.document_id == doc.id)
        await db.execute(update(ExtractionJob).where(ExtractionJob.document_id == doc.id, ExtractionJob.status.in_(["queued", "running"])).values(status="failed", error_code="document_deleted", claim_token=None, completed_at=now))
        await db.execute(delete(SummaryGeneration).where(SummaryGeneration.document_id == doc.id))
        await db.execute(delete(DocumentSummary).where(DocumentSummary.document_id == doc.id))
        await db.execute(delete(ExtractedPage).where(ExtractedPage.job_id.in_(job_ids)))
        await db.execute(delete(StructuredExtractionVersion).where(StructuredExtractionVersion.job_id.in_(job_ids)))
        audit(db, user.id, "document_deletion_requested", f"document:{doc.id}", "success", doc.patient_id)
        await db.commit()
    return {"id": doc.id, "deleted_at": doc.deleted_at, "purge_status": doc.purge_status}


@app.get("/documents/deletions")
async def document_deletions(user: Actor, db: Db):
    if user.role != "patient":
        raise HTTPException(403, "Only patients may view deletion status")
    rows = (await db.execute(select(Document, DocumentObjectLink.document_id).outerjoin(DocumentObjectLink, DocumentObjectLink.document_id == Document.id).where(Document.patient_id == user.id, Document.deleted_at.is_not(None)).order_by(Document.deleted_at.desc()).limit(100))).all()
    result = [{"id": doc.id, "deleted_at": doc.deleted_at, "purge_status": doc.purge_status, "purged_at": doc.purged_at, "storage_kind": "object" if object_id else "local"} for doc, object_id in rows]
    audit(db, user.id, "document_deletions_read", "documents", "success", user.id)
    await db.commit()
    return result


@app.get("/patients/{patient_id}/extracted-records")
async def extracted_records(patient_id: str, response: Response, user: Actor, db: Db, cursor: UUID4 | None = None):
    await authorize_patient(db, user, patient_id, "read_documents", "extracted_records_read")
    latest_job = select(ExtractionJob.id).where(ExtractionJob.document_id == Document.id).order_by(ExtractionJob.created_at.desc(), ExtractionJob.id.desc()).limit(1).correlate(Document).scalar_subquery()
    latest_version = select(StructuredExtractionVersion.id).where(StructuredExtractionVersion.job_id == ExtractionJob.id).order_by(StructuredExtractionVersion.version.desc()).limit(1).correlate(ExtractionJob).scalar_subquery()
    query = select(Document, ExtractionJob, StructuredExtractionVersion).join(ExtractionJob, ExtractionJob.id == latest_job).join(StructuredExtractionVersion, StructuredExtractionVersion.id == latest_version).where(Document.patient_id == patient_id, Document.deleted_at.is_(None), ExtractionJob.status == "completed")
    if cursor is not None:
        anchor = await db.scalar(select(Document).where(Document.id == str(cursor), Document.patient_id == patient_id))
        if not anchor:
            raise HTTPException(422, "Invalid document cursor")
        query = query.where(or_(Document.created_at < anchor.created_at, (Document.created_at == anchor.created_at) & (Document.id < anchor.id)))
    rows = (await db.execute(query.order_by(Document.created_at.desc(), Document.id.desc()).limit(26))).all()
    result = [{"document_id": document.id, "filename": document.filename, "job_id": job.id, "version": version.version, "review_status": version.review_status, "reviewer_id": version.reviewer_id, "data": version.data} for document, job, version in rows[:25]]
    await authorize_patient(db, user, patient_id, "read_documents", "extracted_records_read_release")
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"documents": result, "truncated": len(rows) > 25, "next_cursor": rows[24][0].id if len(rows) > 25 else None}


@app.get("/documents/{document_id}/download")
async def download_document(document_id: str, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    doc = await db.get(Document, document_id)
    if not doc or doc.deleted_at is not None:
        raise HTTPException(404, "Document not found")
    await authorize_patient(db, user, doc.patient_id, "read_documents", "document_download")
    patient_id, filename, size, digest = doc.patient_id, doc.filename, doc.size, doc.sha256
    try:
        location = await document_storage.resolve(db, doc, UPLOAD_DIR)
    except (ValueError, TypeError):
        raise HTTPException(409, "Document storage association is invalid")
    try:
        await asyncio.wait_for(DOWNLOAD_SLOTS.acquire(), timeout=2)
    except asyncio.TimeoutError:
        raise HTTPException(503, "Document download is busy")
    task = asyncio.create_task(asyncio.to_thread(document_storage.read, location, size, digest, MAX_UPLOAD_BYTES))
    def release_slot(done: asyncio.Task) -> None:
        DOWNLOAD_SLOTS.release()
        if not done.cancelled():
            done.exception()
    task.add_done_callback(release_slot)
    try:
        content = await asyncio.shield(task)
    except document_storage.StorageUnavailable:
        raise HTTPException(503, "Document storage unavailable")
    except (OSError, ValueError):
        audit(db, user.id, "document_download_integrity", f"document:{document_id}", "failed", patient_id)
        await db.commit()
        raise HTTPException(409, "Document is unavailable or has changed")
    try:
        token = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "Invalid or expired session")
    db.expire_all()
    current = await active_identity(db, token["sub"], token["sid"])
    await authorize_patient(db, current, patient_id, "read_documents", "document_download_release")
    current_document = await db.get(Document, document_id)
    if not current_document or current_document.deleted_at is not None or (current_document.patient_id, current_document.filename, current_document.size, current_document.sha256) != (patient_id, filename, size, digest):
        raise HTTPException(409, "Document changed during download")
    try:
        if await document_storage.resolve(db, current_document, UPLOAD_DIR) != location:
            raise HTTPException(409, "Document storage changed during download")
    except (ValueError, TypeError):
        raise HTTPException(409, "Document storage association is invalid")
    await db.commit()
    return Response(content=content, media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(filename, safe='')}", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


async def communicate_preview(process: asyncio.subprocess.Process, content: bytes, limit: int = 12_000_000) -> bytes:
    """Feed the snapshot and bound renderer output while it is produced."""
    async def feed():
        process.stdin.write(content)
        try:
            await process.stdin.drain()
        finally:
            process.stdin.close()

    async def read():
        output = bytearray()
        while chunk := await process.stdout.read(65536):
            if len(chunk) > limit - len(output):
                raise ValueError("preview_output_limit")
            output.extend(chunk)
        return bytes(output)

    tasks = [asyncio.create_task(feed()), asyncio.create_task(read())]
    try:
        _, output = await asyncio.gather(*tasks)
        await process.wait()
        return output
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # A full stdout pipe can prevent asyncio's subprocess transport from
        # completing wait() even after the child has exited. Drain and discard.
        async def reap():
            while await process.stdout.read(65536):
                pass
            await process.wait()
        try:
            await asyncio.wait_for(reap(), timeout=3)
        except asyncio.TimeoutError:
            # An escaped descendant could keep the pipe open; close the
            # transport rather than holding a preview slot indefinitely.
            process._transport.close()
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@app.get("/documents/{document_id}/preview/{page_number}")
async def preview_document(document_id: str, page_number: int, request: Request, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    await enforce_auth_limit(request, "document-preview", user.id, 60, 300)
    if not 1 <= page_number <= 30:
        raise HTTPException(422, "Page must be between 1 and 30")
    doc = await db.get(Document, document_id)
    if not doc or doc.deleted_at is not None:
        raise HTTPException(404, "Document not found")
    await authorize_patient(db, user, doc.patient_id, "read_documents", "document_preview")
    if doc.content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        raise HTTPException(415, "DOCX visual preview is not supported; use extracted text and download")
    patient_id, media_type, size, digest = doc.patient_id, doc.content_type, doc.size, doc.sha256
    try:
        location = await document_storage.resolve(db, doc, UPLOAD_DIR)
    except (ValueError, TypeError):
        raise HTTPException(409, "Document storage association is invalid")
    try:
        await asyncio.wait_for(PREVIEW_SLOTS.acquire(), timeout=2)
    except asyncio.TimeoutError:
        raise HTTPException(503, "Page preview is busy")
    slot_transferred = False
    try:
        task = asyncio.create_task(asyncio.to_thread(document_storage.read, location, size, digest, MAX_UPLOAD_BYTES))
        try:
            content = await asyncio.shield(task)
        except asyncio.CancelledError:
            slot_transferred = True
            def finish_read(done: asyncio.Task) -> None:
                PREVIEW_SLOTS.release()
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finish_read)
            raise
        except document_storage.StorageUnavailable:
            raise HTTPException(503, "Document storage unavailable")
        except (OSError, ValueError):
            audit(db, user.id, "document_preview_integrity", f"document:{document_id}", "failed", patient_id)
            await db.commit()
            raise HTTPException(409, "Document is unavailable or has changed")
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.document_preview", "--stdin", media_type, str(page_number), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=parser_environment(max_upload_bytes=MAX_UPLOAD_BYTES), start_new_session=True)
        try:
            output = await asyncio.wait_for(communicate_preview(process, content), timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError, ValueError, BrokenPipeError, ConnectionResetError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, asyncio.TimeoutError):
                raise HTTPException(503, "Page preview timed out")
            raise HTTPException(422, "Page preview unavailable")
    finally:
        if not slot_transferred:
            PREVIEW_SLOTS.release()
    if process.returncode != 0 or len(output) > 12_000_000 or not output.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "Page preview unavailable")
    # Recheck the account, session and consent after rendering, before release.
    try:
        payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid or expired session")
    db.expire_all()
    current = await active_identity(db, payload["sub"], payload["sid"])
    await authorize_patient(db, current, patient_id, "read_documents", "document_preview_release")
    current_document = await db.get(Document, document_id)
    if not current_document or current_document.deleted_at is not None:
        raise HTTPException(404, "Document unavailable")
    if (current_document.patient_id, current_document.content_type, current_document.size, current_document.sha256) != (patient_id, media_type, size, digest):
        raise HTTPException(409, "Document changed during preview")
    try:
        if await document_storage.resolve(db, current_document, UPLOAD_DIR) != location:
            raise HTTPException(409, "Document storage changed during preview")
    except (ValueError, TypeError):
        raise HTTPException(409, "Document storage association is invalid")
    await db.commit()
    return Response(content=output, media_type="image/png", headers={"Cache-Control":"no-store", "X-Content-Type-Options":"nosniff", "Content-Security-Policy":"default-src 'none'; sandbox"})


@app.get("/document-processing/capabilities")
async def document_processing_capabilities(user: Actor):
    try:
        settings = vision_provider.config()
    except ValueError:
        return {"vision_available": False}
    if settings.provider != "ollama":
        return {"vision_available": False}
    return {"vision_available": True, "provider": settings.provider, "model": settings.model, "service_url": settings.base_url, "configuration": vision_provider.config_fingerprint(settings), "max_pages": 3}


@app.post("/documents/{document_id}/processing", status_code=202)
async def request_document_processing(document_id: str, request: Request, user: Actor, db: Db, data: ProcessingRequest | None = None):
    await enforce_write_limit(request, db, user, "document-processing", 20, 200)
    doc = await db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if not doc or doc.deleted_at is not None or user.role != "patient" or doc.patient_id != user.id:
        audit(db, user.id, "document_processing_request", f"document:{document_id}", "denied", doc.patient_id if doc else None)
        await db.commit()
        raise HTTPException(403, "Only the document owner can request processing")
    wants_vision = bool(data and data.vision_requested)
    fingerprint = None
    if wants_vision:
        if doc.content_type not in ("application/pdf", "image/jpeg", "image/png"):
            raise HTTPException(415, "Vision transcription supports PDF and image documents")
        try:
            settings = vision_provider.config()
        except ValueError:
            raise HTTPException(503, "Vision provider is not configured")
        if settings.provider != "ollama":
            raise HTTPException(503, "Vision provider is not configured")
        fingerprint = vision_provider.config_fingerprint(settings)
        if not data.vision_config_fingerprint or data.vision_config_fingerprint != fingerprint:
            raise HTTPException(409, "Vision provider configuration changed; reload before requesting")
    elif data and data.vision_config_fingerprint:
        raise HTTPException(422, "Vision configuration requires a vision request")
    # A crashed worker can leave a claim behind; after five minutes a patient can retry.
    now = time.time()
    stale = await db.execute(update(ExtractionJob).where(ExtractionJob.document_id == document_id, ExtractionJob.status == "running", ExtractionJob.started_at < now - 300).values(status="failed", error_code="stale_job", completed_at=now, claim_token=None).returning(ExtractionJob.id))
    for stale_id in stale.scalars():
        queue_notification(db, recipient_id=user.id, patient_id=doc.patient_id, kind="processing_failed", event_key=f"processing:{stale_id}", resource_id=doc.id)
    job = ExtractionJob(document_id=doc.id, requested_by=user.id, request_correlation_id=observability.correlation_id.get(), processor_version="docx-text-v4" if doc.content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" else "native-ocr-v2", vision_requested=wants_vision, vision_config_fingerprint=fingerprint)
    db.add(job)
    try:
        await db.flush()
        db.add(JobOutbox(kind="document_processing", target_id=job.id))
        audit(db, user.id, "document_processing_request", f"document:{document_id}", "success", doc.patient_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Document processing is already queued or running")
    return {"job_id": job.id, "status": "queued", "review_status": "unverified"}


@app.get("/documents/{document_id}/processing")
async def document_processing_results(document_id: str, response: Response, user: Actor, db: Db, job_id: UUID4 | None = None):
    response.headers["Cache-Control"] = "no-store"
    doc = await db.get(Document, document_id)
    if not doc or doc.deleted_at is not None:
        raise HTTPException(404, "Document not found")
    await authorize_patient(db, user, doc.patient_id, "read_documents", "document_processing_read")
    latest = await db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == doc.id).order_by(ExtractionJob.created_at.desc(), ExtractionJob.id.desc()).limit(1))
    job = latest if job_id is None else await db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == doc.id, ExtractionJob.id == str(job_id)))
    if job_id is not None and not job:
        raise HTTPException(404, "Processing job not found")
    if not job:
        await db.commit()
        return {"status": "not_requested", "review_status": "unverified", "pages": [], "document": {"id": doc.id, "filename": doc.filename, "content_type": doc.content_type, "review_status": doc.review_status, "scan_status": doc.scan_status}}
    pages = (await db.scalars(select(ExtractedPage).where(ExtractedPage.job_id == job.id).order_by(ExtractedPage.page_number))).all()
    versions = (await db.scalars(select(StructuredExtractionVersion).where(StructuredExtractionVersion.job_id == job.id).order_by(StructuredExtractionVersion.version.desc()))).all()
    structured = [{"id": v.id, "version": v.version, "review_status": v.review_status, "reviewer_id": v.reviewer_id, "reviewed_at": v.reviewed_at, "data": v.data} for v in versions]
    result = {"document": {"id": doc.id, "filename": doc.filename, "content_type": doc.content_type, "review_status": doc.review_status, "scan_status": doc.scan_status}, "job_id": job.id, "is_latest": job.id == latest.id, "status": job.status, "review_status": "unverified", "processor_version": job.processor_version, "processor_details": job.processor_details, "created_at": job.created_at,
              "error_code": job.error_code, "vision_requested": job.vision_requested, "pages": [{"page_number": p.page_number, "method": p.method, "text": p.text, **({"supplemental_ocr_text": p.supplemental_ocr_text} if p.supplemental_ocr_text is not None else {}), **({"vision_text": p.vision_text, "vision_warnings": p.vision_warnings} if p.vision_text is not None else {})} for p in pages], "structured_versions": structured}
    await db.commit()
    return result


@app.get("/documents/{document_id}/processing/history")
async def document_processing_history(document_id: str, response: Response, user: Actor, db: Db, before: UUID4 | None = None):
    response.headers["Cache-Control"] = "no-store"
    doc = await db.get(Document, document_id)
    if not doc or doc.deleted_at is not None:
        raise HTTPException(404, "Document not found")
    await authorize_patient(db, user, doc.patient_id, "read_documents", "document_processing_history_read")
    query = select(ExtractionJob).where(ExtractionJob.document_id == doc.id)
    if before is not None:
        cursor = await db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == doc.id, ExtractionJob.id == str(before)))
        if not cursor:
            raise HTTPException(404, "Processing job not found")
        query = query.where(or_(ExtractionJob.created_at < cursor.created_at, (ExtractionJob.created_at == cursor.created_at) & (ExtractionJob.id < cursor.id)))
    jobs = (await db.scalars(query.order_by(ExtractionJob.created_at.desc(), ExtractionJob.id.desc()).limit(21))).all()
    items = jobs[:20]
    result = {"items": [{"id": job.id, "status": job.status, "created_at": job.created_at} for job in items], "next_before": items[-1].id if len(jobs) > 20 else None}
    await db.commit()
    return result


@app.post("/documents/{document_id}/processing/{job_id}/review")
async def review_extraction(document_id: str, job_id: str, data: ExtractionReview, user: Actor, db: Db):
    doc = await db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if not doc or doc.deleted_at is not None:
        raise HTTPException(404, "Document not found")
    if user.role != "doctor":
        audit(db, user.id, "document_extraction_review", f"document:{document_id}", "denied", doc.patient_id)
        await db.commit()
        raise HTTPException(403, "Verified doctor authorization required")
    await authorize_patient(db, user, doc.patient_id, "review_documents", "document_extraction_review")
    await authorize_patient(db, user, doc.patient_id, "read_documents", "document_extraction_source_read")
    latest_job = await db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == doc.id).order_by(ExtractionJob.created_at.desc(), ExtractionJob.id.desc()).limit(1))
    if not latest_job or latest_job.id != job_id or latest_job.status != "completed":
        raise HTTPException(409, "Review requires the latest completed processing job")
    versions = await db.scalar(select(StructuredExtractionVersion).where(StructuredExtractionVersion.job_id == job_id).order_by(StructuredExtractionVersion.version.desc()).limit(1))
    if not versions or versions.version != data.expected_version:
        raise HTTPException(409, "Extraction changed; reload before reviewing")
    pages = (await db.scalars(select(ExtractedPage).where(ExtractedPage.job_id == job_id))).all()
    try:
        validate_evidence(data.data, {p.page_number: p.text for p in pages}, {p.page_number: "text_segment" if p.method == "docx" else "page" for p in pages}, {p.page_number: p.supplemental_ocr_text for p in pages if p.supplemental_ocr_text is not None})
    except ValueError:
        raise HTTPException(422, "Evidence must match the specified extracted page and layer")
    reviewed = StructuredExtractionVersion(job_id=job_id, version=versions.version + 1, data=data.data.model_dump(), review_status="reviewed", reviewer_id=user.id, reviewed_at=time.time())
    db.add(reviewed)
    try:
        await db.flush()
        audit(db, user.id, "document_extraction_reviewed", f"document:{doc.id}", "success", doc.patient_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Extraction changed; reload before reviewing")
    return {"id": reviewed.id, "version": reviewed.version, "review_status": reviewed.review_status, "reviewer_id": reviewed.reviewer_id, "reviewed_at": reviewed.reviewed_at, "data": reviewed.data}


def summary_view(summary: DocumentSummary) -> dict:
    return {"id": summary.id, "job_id": summary.job_id, "content": summary.content, "citations": summary.citations, "truncated": summary.truncated, "model_name": summary.model_name, "created_by": summary.created_by, "created_at": summary.created_at, "review_status": "unverified"}


@app.get("/documents/{document_id}/processing/{job_id}/summaries")
async def list_document_summaries(document_id: str, job_id: str, response: Response, user: Actor, db: Db):
    doc = await db.get(Document, document_id)
    if not doc or doc.deleted_at is not None:
        raise HTTPException(404, "Document not found")
    await authorize_patient(db, user, doc.patient_id, "read_documents", "document_summary_read")
    await authorize_patient(db, user, doc.patient_id, "assistant", "document_summary_read")
    job = await db.get(ExtractionJob, job_id)
    if not job or job.document_id != doc.id:
        raise HTTPException(404, "Processing job not found")
    rows = (await db.scalars(select(DocumentSummary).where(DocumentSummary.document_id == document_id, DocumentSummary.job_id == job_id).order_by(DocumentSummary.created_at.desc(), DocumentSummary.id.desc()).limit(20))).all()
    result = [summary_view(item) for item in rows]
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@app.post("/documents/{document_id}/processing/{job_id}/summaries", status_code=201)
async def generate_document_summary(document_id: str, job_id: str, data: SummaryRequest, request: Request, response: Response, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    await enforce_auth_limit(request, "document-summary", user.id, 10, 100)
    doc = await db.get(Document, document_id)
    if not doc or doc.deleted_at is not None:
        raise HTTPException(404, "Document not found")
    patient_id = doc.patient_id
    await authorize_patient(db, user, patient_id, "read_documents", "document_summary_generate")
    await authorize_patient(db, user, patient_id, "assistant", "document_summary_generate")
    job = await db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == doc.id).order_by(ExtractionJob.created_at.desc(), ExtractionJob.id.desc()).limit(1))
    if not job or job.id != job_id or job.status != "completed":
        raise HTTPException(409, "Summary requires the latest completed processing job")
    pages = (await db.scalars(select(ExtractedPage).where(ExtractedPage.job_id == job_id).order_by(ExtractedPage.page_number))).all()
    sections = [(page, layer, content) for page in pages for layer, content in (("primary", page.text), ("supplemental_ocr", page.supplemental_ocr_text or "")) if content.strip()]
    if not sections:
        raise HTTPException(422, "No extracted text is available to summarize")
    selected = sections[:5]
    truncated = len(sections) > 5 or any(len(content) > 2000 for _, _, content in selected)
    sources = [{"id": f"document:{doc.id}:{'text_segment' if page.method == 'docx' else 'page'}:{page.page_number}:{layer}", "kind": "unverified_patient_document", "title": doc.filename, "text_segment" if page.method == "docx" else "page": page.page_number, "method": "ocr" if layer == "supplemental_ocr" else page.method, "layer": layer, "text": content[:2000]} for page, layer, content in selected]
    model_name = os.getenv("OLLAMA_CHAT_MODEL", "").strip()
    actor_id, request_id, claim = user.id, str(data.request_id), str(uuid.uuid4())
    audience = "doctor" if user.role == "doctor" else "patient"
    db.add(SummaryGeneration(actor_id=actor_id, request_id=request_id, document_id=document_id, job_id=job_id, status="running", claim_token=claim, started_at=time.time()))
    try:
        await db.flush()
        audit(db, actor_id, "document_summary_requested", f"document:{document_id}", "success", patient_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.get(SummaryGeneration, (actor_id, request_id))
        if not existing or (existing.document_id, existing.job_id) != (document_id, job_id):
            raise HTTPException(409, "Request ID is already bound to different inputs")
        if existing.status == "completed":
            summary = await db.get(DocumentSummary, existing.summary_id)
            if not summary or summary.document_id != document_id:
                raise HTTPException(409, "Summary is unavailable")
            result = summary_view(summary)
            audit(db, actor_id, "document_summary_replayed", f"document:{document_id}", "success", patient_id)
            await db.commit()
            response.status_code = 200
            response.headers["Cache-Control"] = "no-store"
            return result
        claimed = await db.execute(update(SummaryGeneration).where(SummaryGeneration.actor_id == actor_id, SummaryGeneration.request_id == request_id, or_(SummaryGeneration.status == "failed", (SummaryGeneration.status == "running") & (SummaryGeneration.started_at < time.time() - 180))).values(status="running", claim_token=claim, started_at=time.time()).returning(SummaryGeneration.actor_id))
        if claimed.scalar_one_or_none() is None:
            await db.rollback()
            raise HTTPException(409, "Summary generation is already in progress")
        await db.commit()
    try:
        if not model_name or len(model_name) > 200:
            raise HTTPException(503, "Summary provider is not configured")
        try:
            draft = await asyncio.wait_for(assistant_provider.draft_answer("Summarize only the provided unverified document text. State uncertainty and missing information. Do not infer a diagnosis, reference range, or treatment decision.", [], sources, audience), timeout=120)
        except (assistant_provider.AssistantUnavailable, asyncio.TimeoutError):
            raise HTTPException(503, "Document summary unavailable")
        valid_ids = {source["id"] for source in sources}
        if not draft.answer.strip() or not draft.citations or any(item not in valid_ids for item in draft.citations):
            raise HTTPException(503, "Document summary unavailable")
        try:
            token = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
        except jwt.InvalidTokenError:
            raise HTTPException(401, "Invalid or expired session")
        db.expire_all()
        current = await active_identity(db, token["sub"], token["sid"])
        await authorize_patient(db, current, patient_id, "read_documents", "document_summary_release")
        await authorize_patient(db, current, patient_id, "assistant", "document_summary_release")
        locked = await db.scalar(select(Document).where(Document.id == document_id).with_for_update())
        if not locked or locked.deleted_at is not None or locked.patient_id != patient_id:
            raise HTTPException(404, "Document no longer available")
        latest = await db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == document_id).order_by(ExtractionJob.created_at.desc(), ExtractionJob.id.desc()).limit(1))
        if not latest or latest.id != job_id or latest.status != "completed":
            raise HTTPException(409, "Document processing changed during generation")
        citations = [{k: v for k, v in source.items() if k != "text"} for source in sources if source["id"] in draft.citations]
        summary = DocumentSummary(document_id=document_id, job_id=job_id, created_by=actor_id, content=draft.answer, citations=citations, truncated=truncated, model_name=model_name)
        db.add(summary)
        await db.flush()
        result = await db.execute(update(SummaryGeneration).where(SummaryGeneration.actor_id == actor_id, SummaryGeneration.request_id == request_id, SummaryGeneration.status == "running", SummaryGeneration.claim_token == claim).values(status="completed", claim_token=None, summary_id=summary.id).returning(SummaryGeneration.actor_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(409, "Summary claim expired; retry with the same request ID")
        audit(db, actor_id, "document_summary_created", f"document:{document_id}", "success", patient_id)
        await db.commit()
        response.headers["Cache-Control"] = "no-store"
        return summary_view(summary)
    except BaseException:
        try:
            await db.rollback()
            failed = await db.execute(update(SummaryGeneration).where(SummaryGeneration.actor_id == actor_id, SummaryGeneration.request_id == request_id, SummaryGeneration.status == "running", SummaryGeneration.claim_token == claim).values(status="failed", claim_token=None).returning(SummaryGeneration.actor_id))
            if failed.scalar_one_or_none():
                audit(db, actor_id, "document_summary_failed", f"document:{document_id}", "failed", patient_id)
            await db.commit()
        except SQLAlchemyError:
            await db.rollback()
        raise


@app.get("/audit/self")
async def audit_self(user: Actor, db: Db):
    rows = (await db.scalars(select(AuditEvent).where(AuditEvent.actor_id == user.id).order_by(AuditEvent.created_at.desc()))).all()
    return [{"id": a.id, "action": a.action, "resource": a.resource, "outcome": a.outcome, "patient_id": a.patient_id, "created_at": a.created_at} for a in rows]


# These are authorization checks, not proof that data was delivered or read.
PATIENT_ACCESS_ACTIONS = {
    "profile_read", "document_list", "extracted_records_read", "document_download_release",
    "document_preview_release", "document_processing_read", "document_processing_history_read",
    "document_extraction_review", "document_extraction_reviewed", "document_summary_read",
    "document_summary_release", "consultation_list", "consultation_create", "note_list",
    "note_create", "note_amend", "note_draft_revision", "note_submit", "note_review",
    "care_messages_read", "care_message_created", "assistant_access", "assistant_context",
    "assistant_output", "integration_documents", "integration_patient_profile",
    "integration_consultations", "integration_document_processing", "integration_document_processing_history",
}


@app.get("/audit/patient-access")
async def patient_access_history(response: Response, user: Actor, db: Db, cursor: UUID4 | None = None):
    if user.role != "patient":
        raise HTTPException(403, "Only patients can view their access history")
    query = select(AuditEvent).join(User, User.id == AuditEvent.actor_id).where(
        AuditEvent.patient_id == user.id, User.role == "doctor", AuditEvent.outcome == "success",
        AuditEvent.action.in_(PATIENT_ACCESS_ACTIONS))
    if cursor is not None:
        anchor = await db.scalar(select(AuditEvent).join(User, User.id == AuditEvent.actor_id).where(
            AuditEvent.id == str(cursor), AuditEvent.patient_id == user.id, User.role == "doctor",
            AuditEvent.outcome == "success", AuditEvent.action.in_(PATIENT_ACCESS_ACTIONS)))
        if not anchor:
            raise HTTPException(422, "Invalid access history cursor")
        query = query.where(or_(AuditEvent.created_at < anchor.created_at, (AuditEvent.created_at == anchor.created_at) & (AuditEvent.id < anchor.id)))
    rows = (await db.scalars(query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(51))).all()
    if len(rows) > 50:
        response.headers["X-Next-Cursor"] = rows[49].id
    response.headers["Cache-Control"] = "no-store"
    result = [{"id": event.id, "actor_id": event.actor_id, "action": event.action, "created_at": event.created_at} for event in rows[:50]]
    audit(db, user.id, "patient_access_history_read", "audit", "success")
    await db.commit()
    return result


def visible_notifications(user: User):
    query = select(Notification).where(Notification.recipient_id == user.id)
    if user.role == "patient":
        return query.where(Notification.patient_id == user.id)
    if user.role != "doctor" or user.verification_status != "verified":
        return query.where(Notification.id == "")
    now = time.time()
    relationship = select(Relationship.id).join(User, User.id == Relationship.patient_id).where(Relationship.id == Notification.relationship_id, Relationship.patient_id == Notification.patient_id, Relationship.doctor_id == user.id, Relationship.status == "active", User.role == "patient", User.active.is_(True)).correlate(Notification).exists()
    scope = select(Consent.id).where(Consent.relationship_id == Notification.relationship_id, Consent.scope == Notification.required_scope, Consent.status == "active", Consent.effective_at <= now, Consent.expires_at > now).correlate(Notification).exists()
    return query.where(relationship, or_(Notification.required_scope.is_(None), scope))


def notification_view(item: Notification) -> dict:
    result = {"id": item.id, "kind": item.kind, "patient_id": item.patient_id, "resource_id": item.resource_id, "created_at": item.created_at, "read_at": item.read_at}
    if item.kind in ("processing_completed", "processing_failed") and item.event_key.startswith("processing:"):
        candidate = item.event_key.removeprefix("processing:")
        try:
            identifier = uuid.UUID(candidate)
            if identifier.version == 4 and str(identifier) == candidate:
                result["processing_job_id"] = candidate
        except ValueError:
            pass
    return result


@app.get("/notifications")
async def list_notifications(response: Response, user: Actor, db: Db, cursor: UUID4 | None = None):
    await db.refresh(user)
    if not user.active:
        raise HTTPException(401, "Invalid or expired session")
    query = visible_notifications(user)
    if cursor is not None:
        anchor = await db.scalar(visible_notifications(user).where(Notification.id == str(cursor)))
        if not anchor:
            raise HTTPException(422, "Invalid or no longer accessible notification cursor")
        query = query.where(or_(Notification.created_at < anchor.created_at, (Notification.created_at == anchor.created_at) & (Notification.id < anchor.id)))
    rows = (await db.scalars(query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(101))).all()
    if len(rows) > 100:
        response.headers["X-Next-Cursor"] = rows[99].id
    response.headers["Cache-Control"] = "no-store"
    result = [notification_view(item) for item in rows[:100]]
    audit(db, user.id, "notifications_read", "notifications", "success")
    await db.commit()
    return result


@app.post("/notifications/{notification_id}/read")
async def read_notification(notification_id: str, user: Actor, db: Db):
    await db.refresh(user)
    if not user.active:
        raise HTTPException(401, "Invalid or expired session")
    item = await db.scalar(visible_notifications(user).where(Notification.id == notification_id).with_for_update())
    if not item:
        raise HTTPException(404, "Notification not found")
    if item.read_at is None:
        item.read_at = time.time()
        audit(db, user.id, "notification_mark_read", "notification", "success", item.patient_id)
        await db.commit()
    return notification_view(item)


def require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Administrator role required")


@app.post("/admin/users/search")
async def admin_user_search(data: AdminUserSearch, user: Actor, db: Db):
    require_admin(user)
    query = select(User)
    term = data.query.strip()
    if term:
        query = query.where(or_(User.full_name.icontains(term, autoescape=True), User.email.icontains(term, autoescape=True), User.id == term))
    if data.role:
        query = query.where(User.role == data.role)
    if data.active is not None:
        query = query.where(User.active.is_(data.active))
    if data.after:
        cursor = safe_uuid(data.after)
        if cursor is None:
            raise HTTPException(422, "Invalid user cursor")
        query = query.where(User.id > cursor)
    rows = (await db.scalars(query.order_by(User.id).limit(data.limit + 1))).all()
    selected = rows[:data.limit]
    result = {"users": [{"id": item.id, "email": item.email, "full_name": item.full_name, "role": item.role, "active": item.active, "verification_status": item.verification_status, "created_at": item.created_at} for item in selected],
              "next_cursor": selected[-1].id if len(rows) > data.limit else None}
    audit(db, user.id, "admin_users_search", "users", "success")
    await db.commit()
    return result


@app.post("/admin/users/{user_id}/deactivate")
async def admin_deactivate_user(user_id: str, data: AdminDeactivate, request: Request, user: Actor, db: Db):
    require_admin(user)
    await enforce_auth_limit(request, "admin-deactivate", user.id, 30, 100)
    target = await db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    if not target or target.role not in ("patient", "doctor"):
        audit(db, user.id, "admin_account_deactivation", "account", "denied")
        await db.commit()
        raise HTTPException(404, "Patient or doctor account not found")
    if target.active:
        await revoke_account_access(db, target)
        audit(db, user.id, "admin_account_deactivation", f"user:{target.id}", "success", target.id if target.role == "patient" else None, data.reason)
    else:
        audit(db, user.id, "admin_account_deactivation_noop", f"user:{target.id}", "success", target.id if target.role == "patient" else None, data.reason)
    await db.commit()
    return {"id": target.id, "active": False}


def safe_uuid(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None


@app.get("/admin/audit")
async def admin_audit(response: Response, user: Actor, db: Db, limit: int = 100, action: str | None = None, outcome: Literal["success", "denied", "failed", "cancelled"] | None = None, cursor: str | None = None):
    require_admin(user)
    if not 1 <= limit <= 100 or (action is not None and (not action or len(action) > 80 or not all(c.isascii() and (c.isalnum() or c == "_") for c in action))):
        raise HTTPException(422, "Invalid audit filter")
    query = select(AuditEvent)
    if action:
        query = query.where(AuditEvent.action == action)
    if outcome:
        query = query.where(AuditEvent.outcome == outcome)
    if cursor is not None:
        cursor_id = safe_uuid(cursor)
        anchor = await db.get(AuditEvent, cursor_id) if cursor_id else None
        if not anchor or (action and anchor.action != action) or (outcome and anchor.outcome != outcome):
            raise HTTPException(422, "Invalid audit cursor")
        query = query.where(or_(AuditEvent.created_at < anchor.created_at, (AuditEvent.created_at == anchor.created_at) & (AuditEvent.id < anchor.id)))
    rows = (await db.scalars(query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1))).all()
    events = rows[:limit]
    if len(rows) > limit:
        response.headers["X-Next-Cursor"] = events[-1].id
    response.headers["Cache-Control"] = "no-store"
    result = []
    for event in events:
        kind = event.resource.split(":", 1)[0]
        # Stored resource and purpose can contain user input, so the operational
        # view exposes only a safe resource category and validated identifiers.
        kind = kind if kind and len(kind) <= 40 and all(c.isascii() and (c.isalnum() or c == "_") for c in kind) else "other"
        result.append({"id": event.id, "actor_id": safe_uuid(event.actor_id), "patient_id": safe_uuid(event.patient_id), "action": event.action, "resource_type": kind, "outcome": event.outcome, "correlation_id": event.correlation_id, "created_at": event.created_at})
    audit(db, user.id, "admin_audit_read", "audit", "success")
    await db.commit()
    return result


@app.get("/admin/operations")
async def admin_operations(user: Actor, db: Db):
    require_admin(user)
    counts = (await db.execute(select(ExtractionJob.status, func.count()).group_by(ExtractionJob.status))).all()
    stale = await db.scalar(select(func.count()).select_from(ExtractionJob).where(ExtractionJob.status.in_(["queued", "running"]), ExtractionJob.created_at < time.time() - 300))
    import_counts = (await db.execute(select(KnowledgeImport.status, func.count()).group_by(KnowledgeImport.status))).all()
    index_counts = (await db.execute(select(KnowledgeIndexJob.status, func.count()).group_by(KnowledgeIndexJob.status))).all()
    stale_indexes = await db.scalar(select(func.count()).select_from(KnowledgeIndexJob).where(KnowledgeIndexJob.status.in_(["queued", "running"]), KnowledgeIndexJob.created_at < time.time() - 300))
    dispatch_counts = (await db.execute(select(JobOutbox.status, func.count()).group_by(JobOutbox.status))).all()
    reset_counts = (await db.execute(select(ResetDelivery.status, func.count()).group_by(ResetDelivery.status))).all()
    purge_counts = (await db.execute(select(Document.purge_status, func.count()).where(Document.deleted_at.is_not(None)).group_by(Document.purge_status))).all()
    incomplete = (select(ObjectUploadIntent).join(ObjectDocumentSubmission).outerjoin(DocumentObjectLink).outerjoin(ObjectUploadDiscard).where(ObjectUploadIntent.namespace == "documents", DocumentObjectLink.document_id.is_(None), ObjectUploadDiscard.intent_id.is_(None)))
    incomplete_counts = (await db.execute(incomplete.with_only_columns(ObjectUploadIntent.status, func.count()).group_by(ObjectUploadIntent.status))).all()
    stale_uploads = await db.scalar(incomplete.with_only_columns(func.count()).where(ObjectUploadIntent.created_at < time.time() - 86400))
    discarded_counts = (await db.execute(select(ObjectUploadDiscard.last_result, func.count()).group_by(ObjectUploadDiscard.last_result))).all()
    error_code = func.coalesce(ObjectUploadDiscard.error_code, "unclassified")
    discarded_errors = (await db.execute(select(error_code, func.count()).where(ObjectUploadDiscard.last_result == "error").group_by(error_code))).all()
    checked_at = time.time()
    overdue_discards, oldest_due = (await db.execute(select(func.count(), func.min(ObjectUploadDiscard.next_check_at)).where(ObjectUploadDiscard.next_check_at < checked_at - 300))).one()
    redis_ok = await auth_rate_limit.redis_available()
    audit(db, user.id, "admin_operations_read", "operations", "success")
    await db.commit()
    return {"database": "connected", "redis": "connected" if redis_ok else "unavailable", "document_jobs": {status: count for status, count in counts}, "knowledge_import_jobs": {status: count for status, count in import_counts}, "knowledge_index_jobs": {status: count for status, count in index_counts}, "possibly_stale_index_jobs": stale_indexes, "job_dispatch": {status: count for status, count in dispatch_counts}, "reset_mail": {status: count for status, count in reset_counts}, "document_purges": {status: count for status, count in purge_counts}, "incomplete_object_uploads": {status: count for status, count in incomplete_counts}, "old_incomplete_object_uploads": stale_uploads, "discarded_object_uploads": {status: count for status, count in discarded_counts}, "discard_error_categories": {code: count for code, count in discarded_errors}, "overdue_discard_checks": overdue_discards, "oldest_discard_check_lag_seconds": max(0, int(checked_at - oldest_due)) if oldest_due is not None else 0, "api_metrics": observability.snapshot(), "possibly_stale_jobs": stale, "stale_after_seconds": 300}


@app.get("/admin/operations/discard-errors")
async def admin_discard_errors(response: Response, user: Actor, db: Db, cursor: UUID4 | None = None):
    require_admin(user)
    query = select(ObjectUploadDiscard).where(ObjectUploadDiscard.last_result == "error")
    if cursor is not None:
        anchor = await db.get(ObjectUploadDiscard, str(cursor))
        if not anchor:
            raise HTTPException(422, "Invalid discard cursor")
        query = query.where(or_(ObjectUploadDiscard.requested_at < anchor.requested_at, (ObjectUploadDiscard.requested_at == anchor.requested_at) & (ObjectUploadDiscard.intent_id < anchor.intent_id)))
    rows = (await db.scalars(query.order_by(ObjectUploadDiscard.requested_at.desc(), ObjectUploadDiscard.intent_id.desc()).limit(51))).all()
    if len(rows) > 50:
        response.headers["X-Next-Cursor"] = rows[49].intent_id
    response.headers["Cache-Control"] = "no-store"
    result = [{"id": row.intent_id, "category": row.error_code or "unclassified", "attempts": row.attempts, "checked_at": row.checked_at, "next_check_at": row.next_check_at} for row in rows[:50]]
    audit(db, user.id, "admin_discard_errors_read", "discard_checks", "success")
    await db.commit()
    return result


@app.post("/admin/operations/discard-errors/{intent_id}/retry", status_code=202)
async def retry_discard_error(intent_id: UUID4, request: Request, user: Actor, db: Db):
    require_admin(user)
    await enforce_write_limit(request, db, user, "discard-check-retry", 20, 100)
    discard = await db.scalar(select(ObjectUploadDiscard).where(ObjectUploadDiscard.intent_id == str(intent_id)).with_for_update())
    if not discard:
        raise HTTPException(404, "Discard check not found")
    if discard.last_result != "error":
        raise HTTPException(409, "Discard check is not in an error state")
    discard.next_check_at = time.time()
    audit(db, user.id, "admin_discard_check_retry", f"discard_check:{discard.intent_id}", "success")
    await db.commit()
    return {"status": "scheduled"}


def knowledge_view(source: KnowledgeSource, include_content: bool = False) -> dict:
    result = {"id": source.id, "family_id": source.family_id, "previous_id": source.previous_id,
              "title": source.title, "publisher": source.publisher, "provenance": source.provenance,
              "publication_date": source.publication_date.isoformat(), "specialty": source.specialty,
              "version": source.version, "status": source.status, "index_status": source.index_status,
              "embedding_model": source.embedding_model, "embedding_digest": source.embedding_digest, "effective_at": source.effective_at,
              "retire_at": source.retire_at, "reviewer_id": source.reviewer_id, "reviewed_at": source.reviewed_at}
    if include_content:
        result["content"] = source.content
    return result


def knowledge_import_view(job: KnowledgeImport, include_text: bool = False) -> dict:
    result = {"id": job.id, "filename": job.filename, "media_type": job.media_type, "sha256": job.sha256, "scan_status": job.scan_status, "status": job.status, "error_code": job.error_code, "processor_details": job.processor_details, "created_at": job.created_at, "completed_at": job.completed_at, "deleted_at": job.deleted_at, "purge_status": job.purge_status, "purged_at": job.purged_at}
    if include_text:
        result["extracted_text"] = job.extracted_text
    return result


@app.post("/admin/knowledge/imports", status_code=202, openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}}}}}})
async def create_knowledge_import(request: Request, user: Actor, db: Db):
    require_admin(user)
    await enforce_auth_limit(request, "knowledge-import", user.id, 10, 50)
    form, file = await parse_upload_form(request)
    try:
        allowed = {"application/pdf": (b"%PDF-", ".pdf"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04", ".docx")}
        if file.content_type not in allowed:
            raise HTTPException(415, "Only PDF and DOCX reference files are supported")
        name = file.filename or ""
        signature, suffix = allowed[file.content_type]
        if not name or Path(name).name != name or "\\" in name or len(name) > 160 or any(ord(c) < 32 for c in name) or Path(name).suffix.lower() != suffix:
            raise HTTPException(400, "Invalid reference filename")
        job_id = str(uuid.uuid4())
        KNOWLEDGE_IMPORT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = KNOWLEDGE_IMPORT_DIR / job_id
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("xb") as dest:
                os.chmod(path, 0o600)
                chunk = await file.read(65536)
                if not chunk.startswith(signature):
                    raise HTTPException(415, "File signature does not match MIME type")
                while chunk:
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "File too large")
                    digest.update(chunk)
                    dest.write(chunk)
                    chunk = await file.read(65536)
            scan_status = await scan_upload(path, user, db, "knowledge_import")
            await db.refresh(user)
            if not user.active or user.role != "admin":
                raise HTTPException(403, "Administrator role required")
            job = KnowledgeImport(id=job_id, created_by=user.id, request_correlation_id=observability.correlation_id.get(), filename=name, media_type=file.content_type, sha256=digest.hexdigest(), status="queued", scan_status=scan_status)
            db.add(job)
            db.add(JobOutbox(kind="knowledge_import", target_id=job_id))
            audit(db, user.id, "knowledge_import_requested", f"knowledge_import:{job_id}", "success")
            await db.commit()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return knowledge_import_view(job)
    finally:
        await form.close()


@app.get("/admin/knowledge/imports")
async def list_knowledge_imports(user: Actor, db: Db):
    require_admin(user)
    jobs = (await db.scalars(select(KnowledgeImport).order_by(KnowledgeImport.created_at.desc(), KnowledgeImport.id.desc()).limit(100))).all()
    audit(db, user.id, "knowledge_import_list", "knowledge_import", "success")
    await db.commit()
    return [knowledge_import_view(job) for job in jobs]


@app.get("/admin/knowledge/imports/{import_id}")
async def read_knowledge_import(import_id: str, user: Actor, db: Db):
    require_admin(user)
    job = await db.get(KnowledgeImport, import_id)
    if not job or job.deleted_at is not None:
        raise HTTPException(404, "Reference import not found")
    audit(db, user.id, "knowledge_import_read", f"knowledge_import:{job.id}", "success")
    await db.commit()
    return knowledge_import_view(job, include_text=True)


@app.get("/admin/knowledge/imports/{import_id}/download")
async def download_knowledge_import(import_id: str, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    require_admin(user)
    job = await db.get(KnowledgeImport, import_id)
    if not job or job.deleted_at is not None:
        raise HTTPException(404, "Reference import not found")
    filename, digest = job.filename, job.sha256
    try:
        await asyncio.wait_for(DOWNLOAD_SLOTS.acquire(), timeout=2)
    except asyncio.TimeoutError:
        raise HTTPException(503, "Reference download is busy")
    task = asyncio.create_task(asyncio.to_thread(file_integrity.read_snapshot, KNOWLEDGE_IMPORT_DIR / job.id, None, digest, MAX_UPLOAD_BYTES))
    def release_slot(done: asyncio.Task) -> None:
        DOWNLOAD_SLOTS.release()
        if not done.cancelled():
            done.exception()
    task.add_done_callback(release_slot)
    try:
        content = await asyncio.shield(task)
    except (OSError, ValueError):
        audit(db, user.id, "knowledge_import_download_integrity", f"knowledge_import:{import_id}", "failed")
        await db.commit()
        raise HTTPException(409, "Reference file is unavailable or has changed")
    try:
        token = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "Invalid or expired session")
    db.expire_all()
    current = await active_identity(db, token["sub"], token["sid"])
    require_admin(current)
    current_job = await db.get(KnowledgeImport, import_id)
    if not current_job or current_job.deleted_at is not None or (current_job.filename, current_job.sha256) != (filename, digest):
        raise HTTPException(409, "Reference import changed during download")
    audit(db, current.id, "knowledge_import_download", f"knowledge_import:{import_id}", "success")
    await db.commit()
    return Response(content=content, media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(filename, safe='')}", "X-Content-Type-Options":"nosniff", "Cache-Control":"no-store"})


@app.post("/admin/knowledge/imports/{import_id}/retry", status_code=202)
async def retry_knowledge_import(import_id: str, request: Request, user: Actor, db: Db):
    require_admin(user)
    await enforce_auth_limit(request, "knowledge-import-retry", user.id, 10, 50)
    job = await db.scalar(select(KnowledgeImport).where(KnowledgeImport.id == import_id).with_for_update())
    if not job or job.deleted_at is not None:
        raise HTTPException(404, "Reference import not found")
    if job.status != "failed":
        raise HTTPException(409, "Only failed imports can be retried")
    creator = await db.get(User, job.created_by)
    if not creator or not creator.active or creator.role != "admin":
        raise HTTPException(409, "Original importing administrator is no longer active")
    entry = await db.scalar(select(JobOutbox).where(JobOutbox.kind == "knowledge_import", JobOutbox.target_id == job.id).with_for_update())
    if entry is None:
        entry = JobOutbox(kind="knowledge_import", target_id=job.id)
        db.add(entry)
    now = time.time()
    job.status, job.error_code, job.started_at, job.completed_at, job.claim_token = "queued", None, None, None, None
    job.attempt_count = 0
    job.request_correlation_id = observability.correlation_id.get()
    entry.status, entry.attempts, entry.next_attempt_at, entry.published_at, entry.last_error = "pending", 0, now, None, None
    audit(db, user.id, "knowledge_import_retry_requested", f"knowledge_import:{job.id}", "success")
    await db.commit()
    return knowledge_import_view(job)


@app.post("/admin/knowledge/imports/{import_id}/delete", status_code=202)
async def delete_knowledge_import(import_id: str, data: DocumentDeletion, request: Request, user: Actor, db: Db):
    require_admin(user)
    await enforce_auth_limit(request, "knowledge-import-delete", user.id, 20, 100)
    job = await db.scalar(select(KnowledgeImport).where(KnowledgeImport.id == import_id).with_for_update())
    if not job:
        raise HTTPException(404, "Reference import not found")
    if job.deleted_at is None:
        now = time.time()
        job.deleted_at, job.purge_status, job.purge_after = now, "pending", now
        job.extracted_text, job.processor_details = None, None
        if job.status in ("queued", "running"):
            job.status, job.error_code, job.completed_at, job.claim_token = "failed", "import_deleted", now, None
        audit(db, user.id, "knowledge_import_deletion_requested", f"knowledge_import:{job.id}", "success")
        await db.commit()
    return {"id": job.id, "deleted_at": job.deleted_at, "purge_status": job.purge_status, "purged_at": job.purged_at}


@app.post("/admin/knowledge", status_code=201)
async def create_knowledge_source(data: KnowledgeCreate, user: Actor, db: Db):
    require_admin(user)
    previous = await db.get(KnowledgeSource, data.previous_id) if data.previous_id else None
    if data.previous_id and not previous:
        raise HTTPException(404, "Previous source version not found")
    imported = await db.scalar(select(KnowledgeImport).where(KnowledgeImport.id == data.import_id).with_for_update()) if data.import_id else None
    if data.import_id and (not imported or imported.deleted_at is not None or imported.status != "completed" or imported.extracted_text != data.content):
        raise HTTPException(409, "Reference import is unavailable or its text was changed")
    source = KnowledgeSource(family_id=previous.family_id if previous else str(uuid.uuid4()), previous_id=data.previous_id,
                             title=data.title.strip(), publisher=data.publisher.strip(), provenance=data.provenance.strip(),
                             publication_date=data.publication_date, specialty=data.specialty.strip(), version=data.version.strip(),
                             content=data.content, effective_at=data.effective_at, retire_at=data.retire_at, created_by=user.id)
    db.add(source)
    try:
        await db.flush()
        if imported:
            db.add(KnowledgeSourceImport(source_id=source.id, import_id=imported.id))
        audit(db, user.id, "knowledge_create", f"knowledge:{source.id}", "success")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Source version already exists")
    return {**knowledge_view(source), "import_id": imported.id if imported else None}


@app.get("/admin/knowledge")
async def list_knowledge_sources(user: Actor, db: Db):
    require_admin(user)
    rows = (await db.scalars(select(KnowledgeSource).order_by(KnowledgeSource.created_at.desc()).limit(200))).all()
    source_ids = [source.id for source in rows]
    links = dict((await db.execute(select(KnowledgeSourceImport.source_id, KnowledgeSourceImport.import_id).where(KnowledgeSourceImport.source_id.in_(source_ids)))).all()) if source_ids else {}
    return [{**knowledge_view(source, include_content=True), "created_by": source.created_by, "import_id": links.get(source.id)} for source in rows]


@app.get("/admin/knowledge/review-policy")
async def knowledge_review_policy(user: Actor):
    require_admin(user)
    return {"knowledge_review_policy": KNOWLEDGE_REVIEW_POLICY, "clinical_review_policy": KNOWLEDGE_CLINICAL_REVIEW_POLICY, "administrator_id": user.id}


def knowledge_review_fingerprint(source: KnowledgeSource) -> str:
    # Include all content and publication metadata the reviewer evaluates.
    payload = {"id": source.id, "family_id": source.family_id, "previous_id": source.previous_id,
               "title": source.title, "publisher": source.publisher, "provenance": source.provenance,
               "publication_date": source.publication_date.isoformat(), "specialty": source.specialty,
               "version": source.version, "content": source.content, "effective_at": source.effective_at,
               "retire_at": source.retire_at}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def knowledge_clinical_review_view(review: KnowledgeClinicalReview, source: KnowledgeSource, doctor: User) -> dict:
    matches = bool(review.source_fingerprint and hmac.compare_digest(review.source_fingerprint, knowledge_review_fingerprint(source)))
    return {"id": review.id, "source_id": review.source_id, "doctor_id": review.doctor_id, "assigned_by": review.assigned_by, "status": review.status, "comment": review.comment, "created_at": review.created_at, "reviewed_at": review.reviewed_at, "source_matches": matches, "verification_matches": bool(doctor.active and doctor.role == "doctor" and doctor.verification_status == "verified" and review.doctor_verification_revision is not None and review.doctor_verification_revision == doctor.verification_revision)}


@app.post("/admin/knowledge/{source_id}/clinical-reviews", status_code=201)
async def assign_knowledge_clinical_review(source_id: str, data: KnowledgeReviewAssignment, request: Request, user: Actor, db: Db):
    require_admin(user)
    await enforce_auth_limit(request, "knowledge-review-assignment", user.id, 30, 100)
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.id == source_id).with_for_update())
    if not source:
        raise HTTPException(404, "Knowledge source not found")
    if source.status != "draft":
        raise HTTPException(409, "Only draft sources can be assigned")
    doctor = await db.scalar(select(User).where(User.id == str(data.doctor_id)).with_for_update())
    if not doctor or not doctor.active or doctor.role != "doctor" or doctor.verification_status != "verified":
        raise HTTPException(422, "An active verified doctor is required")
    review = KnowledgeClinicalReview(source_id=source.id, doctor_id=doctor.id, assigned_by=user.id, source_fingerprint=knowledge_review_fingerprint(source), doctor_verification_revision=doctor.verification_revision)
    db.add(review)
    try:
        await db.flush()
        audit(db, user.id, "knowledge_clinical_review_assigned", f"knowledge:{source.id}", "success")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This doctor has already been assigned to this source")
    return knowledge_clinical_review_view(review, source, doctor)


@app.get("/admin/knowledge/{source_id}/clinical-reviews")
async def list_knowledge_clinical_reviews(source_id: str, response: Response, user: Actor, db: Db):
    require_admin(user)
    source = await db.get(KnowledgeSource, source_id)
    if not source:
        raise HTTPException(404, "Knowledge source not found")
    rows = (await db.execute(select(KnowledgeClinicalReview, User).join(User, User.id == KnowledgeClinicalReview.doctor_id).where(KnowledgeClinicalReview.source_id == source_id).order_by(KnowledgeClinicalReview.created_at.desc(), KnowledgeClinicalReview.id.desc()).limit(100))).all()
    audit(db, user.id, "knowledge_clinical_reviews_read", f"knowledge:{source_id}", "success")
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return [knowledge_clinical_review_view(review, source, doctor) for review, doctor in rows]


@app.get("/knowledge/clinical-reviews")
async def my_knowledge_clinical_reviews(response: Response, user: Actor, db: Db):
    await db.refresh(user)
    if user.role != "doctor" or not user.active or user.verification_status != "verified":
        raise HTTPException(403, "An active verified doctor is required")
    rows = (await db.execute(select(KnowledgeClinicalReview, KnowledgeSource).join(KnowledgeSource, KnowledgeSource.id == KnowledgeClinicalReview.source_id).where(KnowledgeClinicalReview.doctor_id == user.id, KnowledgeClinicalReview.status == "pending", KnowledgeSource.status == "draft").order_by(KnowledgeClinicalReview.created_at.desc(), KnowledgeClinicalReview.id.desc()).limit(50))).all()
    result = [{**knowledge_clinical_review_view(review, source, user), "source": knowledge_view(source, include_content=True)} for review, source in rows]
    audit(db, user.id, "knowledge_clinical_assignments_read", "knowledge", "success")
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@app.post("/knowledge/clinical-reviews/{review_id}/decision")
async def decide_knowledge_clinical_review(review_id: str, data: KnowledgeClinicalDecision, request: Request, user: Actor, db: Db):
    await enforce_auth_limit(request, "knowledge-clinical-review", user.id, 30, 100)
    review = await db.scalar(select(KnowledgeClinicalReview).where(KnowledgeClinicalReview.id == review_id).with_for_update())
    if not review or review.doctor_id != user.id:
        raise HTTPException(404, "Review assignment not found")
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.id == review.source_id).with_for_update())
    current = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if not current or not current.active or current.role != "doctor" or current.verification_status != "verified":
        raise HTTPException(403, "An active verified doctor is required")
    if not source or source.status != "draft" or review.status != "pending":
        raise HTTPException(409, "Review assignment is no longer pending for a draft source")
    if not review.source_fingerprint or not hmac.compare_digest(review.source_fingerprint, knowledge_review_fingerprint(source)):
        raise HTTPException(409, "Source changed since assignment; a new source version and review are required")
    if review.doctor_verification_revision is None or review.doctor_verification_revision != current.verification_revision:
        raise HTTPException(409, "Clinician verification changed since assignment; a new source version and review are required")
    review.status, review.comment, review.reviewed_at = data.decision, data.comment or None, time.time()
    audit(db, current.id, "knowledge_clinical_review_decided", f"knowledge:{source.id}", "success")
    await db.commit()
    return knowledge_clinical_review_view(review, source, current)


def knowledge_index_job_view(job: KnowledgeIndexJob) -> dict:
    return {"id": job.id, "source_id": job.source_id, "status": job.status, "error_code": job.error_code, "attempt_count": job.attempt_count, "created_at": job.created_at, "completed_at": job.completed_at}


@app.post("/admin/knowledge/{source_id}/index-jobs", status_code=202)
async def enqueue_knowledge_index(source_id: str, request: Request, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    from .knowledge_indexing import content_hash
    require_admin(user)
    await enforce_auth_limit(request, "knowledge-index", user.id, 30, 100)
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.id == source_id).with_for_update())
    if not source:
        raise HTTPException(404, "Knowledge source not found")
    if source.status == "retired":
        raise HTTPException(409, "Retired sources cannot be indexed")
    segments = knowledge.chunks(source.content)
    if not segments or len(segments) > 100:
        raise HTTPException(422, "Source has too many chunks or no indexable text")
    token = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
    job = KnowledgeIndexJob(source_id=source.id, requested_by=user.id, session_id=token["sid"], request_correlation_id=observability.correlation_id.get(), content_hash=content_hash(source.content), model=knowledge.embedding_model())
    db.add(job)
    try:
        await db.flush()
        audit(db, user.id, "knowledge_index_requested", f"knowledge:{source.id}", "success")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Indexing is already queued or running for this source")
    return knowledge_index_job_view(job)


@app.get("/admin/knowledge/{source_id}/index-jobs")
async def list_knowledge_index_jobs(source_id: str, response: Response, user: Actor, db: Db):
    require_admin(user)
    if not await db.get(KnowledgeSource, source_id):
        raise HTTPException(404, "Knowledge source not found")
    jobs = (await db.scalars(select(KnowledgeIndexJob).where(KnowledgeIndexJob.source_id == source_id).order_by(KnowledgeIndexJob.created_at.desc(), KnowledgeIndexJob.id.desc()).limit(20))).all()
    audit(db, user.id, "knowledge_index_jobs_read", f"knowledge:{source_id}", "success")
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return [knowledge_index_job_view(job) for job in jobs]


@app.post("/admin/knowledge/{source_id}/index")
async def index_knowledge(source_id: str, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    require_admin(user)
    source = await db.get(KnowledgeSource, source_id)
    if not source:
        raise HTTPException(404, "Knowledge source not found")
    if source.status == "retired":
        raise HTTPException(409, "Retired sources cannot be indexed")
    text_content = source.content
    segments = knowledge.chunks(text_content)
    if not segments or len(segments) > 100:
        raise HTTPException(422, "Source has too many chunks or no indexable text")
    model = knowledge.embedding_model()
    actor_id = user.id
    # Do not keep a database transaction open during an external embedding request.
    await db.rollback()
    try:
        digest = await knowledge.embedding_digest()
        vectors = await knowledge.embed_texts(segments)
        digest_after = await knowledge.embedding_digest()
    except knowledge.EmbeddingUnavailable:
        raise HTTPException(503, "Embedding provider unavailable or not configured")
    try:
        token = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "Invalid or expired session")
    current = await db.scalar(select(User).where(User.id == actor_id).with_for_update().execution_options(populate_existing=True))
    session = await db.scalar(select(Session).where(Session.id == token["sid"], Session.user_id == actor_id).with_for_update().execution_options(populate_existing=True))
    if token["sub"] != actor_id or not current or not current.active or not session or session.revoked_at is not None or session.expires_at <= time.time():
        raise HTTPException(401, "Invalid or expired session")
    require_admin(current)
    if model != knowledge.embedding_model() or digest != digest_after:
        raise HTTPException(409, "Embedding configuration changed while indexing")
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.id == source_id).with_for_update())
    if not source or source.status == "retired" or source.content != text_content:
        raise HTTPException(409, "Source changed while indexing")
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))
    if len(segments) != len(vectors):
        raise HTTPException(503, "Embedding provider returned an invalid response")
    db.add_all([KnowledgeChunk(source_id=source_id, ordinal=i, content=segment, embedding=vector) for i, (segment, vector) in enumerate(zip(segments, vectors))])
    source.embedding_model = model
    source.embedding_digest = digest
    source.index_status = "ready"
    audit(db, actor_id, "knowledge_index", f"knowledge:{source_id}", "success")
    await db.commit()
    return {"source_id": source_id, "index_status": "ready", "chunks": len(segments), "embedding_model": model}


@app.post("/admin/knowledge/{source_id}/approve")
async def approve_knowledge(source_id: str, user: Actor, db: Db):
    require_admin(user)
    source = await db.get(KnowledgeSource, source_id)
    if not source:
        raise HTTPException(404, "Knowledge source not found")
    family = (await db.scalars(select(KnowledgeSource).where(KnowledgeSource.family_id == source.family_id).order_by(KnowledgeSource.id).with_for_update())).all()
    source = next(item for item in family if item.id == source_id)
    if source.status != "draft" or source.index_status != "ready" or not source.embedding_digest:
        raise HTTPException(409, "Only indexed draft sources can be approved")
    if KNOWLEDGE_REVIEW_POLICY == "independent" and source.created_by == user.id:
        audit(db, user.id, "knowledge_approve", f"knowledge:{source.id}", "denied")
        await db.commit()
        raise HTTPException(403, "A different administrator must approve this source")
    if source.retire_at is not None and source.retire_at <= time.time():
        raise HTTPException(409, "Source has already reached its retirement date")
    if KNOWLEDGE_CLINICAL_REVIEW_POLICY == "required":
        review = await db.scalar(select(KnowledgeClinicalReview).where(KnowledgeClinicalReview.source_id == source.id).order_by(KnowledgeClinicalReview.created_at.desc(), KnowledgeClinicalReview.id.desc()).limit(1))
        doctor = await db.scalar(select(User).where(User.id == review.doctor_id).with_for_update().execution_options(populate_existing=True)) if review else None
        if not review or review.status != "recommended" or not review.source_fingerprint or not hmac.compare_digest(review.source_fingerprint, knowledge_review_fingerprint(source)) or not doctor or not doctor.active or doctor.role != "doctor" or doctor.verification_status != "verified" or review.doctor_verification_revision is None or review.doctor_verification_revision != doctor.verification_revision:
            raise HTTPException(409, "The latest assigned review must recommend the source and its doctor must remain verified under the same verification revision")
    approved = [item for item in family if item.status == "approved"]
    if approved and source.effective_at > time.time():
        raise HTTPException(409, "Approve this future version when it becomes effective")
    for item in approved:
        item.status = "retired"
        audit(db, user.id, "knowledge_supersede", f"knowledge:{item.id}", "success")
    await db.flush()
    source.status = "approved"
    source.reviewer_id = user.id
    source.reviewed_at = time.time()
    audit(db, user.id, "knowledge_approve", f"knowledge:{source.id}", "success")
    await db.commit()
    return knowledge_view(source)


@app.post("/admin/knowledge/{source_id}/retire")
async def retire_knowledge(source_id: str, user: Actor, db: Db):
    require_admin(user)
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.id == source_id).with_for_update())
    if not source:
        raise HTTPException(404, "Knowledge source not found")
    if source.status != "retired":
        source.status = "retired"
        audit(db, user.id, "knowledge_retire", f"knowledge:{source.id}", "success")
        await db.commit()
    return knowledge_view(source)


async def search_knowledge(data: KnowledgeSearch, user: Actor, db: Db):
    if not data.query.strip():
        raise HTTPException(422, "Search query cannot be blank")
    if db.bind.dialect.name != "postgresql":
        raise HTTPException(503, "Semantic knowledge search requires PostgreSQL with pgvector")
    model = knowledge.embedding_model()
    try:
        digest = await knowledge.embedding_digest()
        vector = (await knowledge.embed_texts([data.query]))[0]
        digest_after = await knowledge.embedding_digest()
    except knowledge.EmbeddingUnavailable:
        raise HTTPException(503, "Embedding provider unavailable or not configured")
    if digest != digest_after or model != knowledge.embedding_model():
        raise HTTPException(503, "Embedding configuration changed during search")
    now = time.time()
    distance = KnowledgeChunk.embedding.cosine_distance(vector)
    query = select(KnowledgeChunk, KnowledgeSource, distance.label("distance")).join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id).where(
        KnowledgeSource.status == "approved", KnowledgeSource.index_status == "ready", KnowledgeSource.embedding_model == model, KnowledgeSource.embedding_digest == digest,
        KnowledgeSource.effective_at <= now, (KnowledgeSource.retire_at.is_(None) | (KnowledgeSource.retire_at > now)))
    if data.specialty:
        query = query.where(KnowledgeSource.specialty == data.specialty.strip())
    rows = (await db.execute(query.order_by(distance).limit(data.limit))).all()
    audit(db, user.id, "knowledge_search", "approved_knowledge", "success")
    await db.commit()
    return {"results": [{"excerpt": chunk.content, "chunk": chunk.ordinal, "source": knowledge_view(source), "distance": float(score)} for chunk, source, score in rows],
            "notice": "Retrieved reference text is not a diagnosis or treatment recommendation. Verify source relevance and currency."}


@app.post("/knowledge/search")
async def knowledge_search_endpoint(data: KnowledgeSearch, response: Response, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    user_id = user.id
    result = await search_knowledge(data, user, db)
    try:
        payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
        if payload["sub"] != user_id:
            raise ValueError("Session identity changed")
        await active_identity(db, user_id, payload["sid"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError, AttributeError, HTTPException):
        audit(db, user_id, "knowledge_search_release", "approved_knowledge", "denied")
        await db.commit()
        raise HTTPException(401, "Invalid or expired session") from None
    audit(db, user_id, "knowledge_search_release", "approved_knowledge", "success")
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


def credential_view(credential: IntegrationCredential) -> dict:
    return {"id": credential.id, "label": credential.label, "patient_id": credential.patient_id, "scopes": credential.scopes,
            "expires_at": credential.expires_at, "revoked_at": credential.revoked_at, "created_at": credential.created_at}


INTEGRATION_PATIENT_SCOPES = {
    "documents:read": ("read_documents", "integration_access"),
    "profile:read": ("read_profile", "profile_integration"),
    "consultations:read": ("consultations", "consultations_integration"),
}


@app.post("/integration-credentials", status_code=201)
async def create_integration_credential(data: CredentialCreate, response: Response, user: Actor, db: Db):
    if user.role not in ("patient", "doctor"):
        raise HTTPException(403, "Role not permitted")
    try:
        allowed = await integration_rate_limit.check_rate_limit(f"issue-{user.id}", user.id)
    except integration_rate_limit.RateLimitUnavailable:
        raise HTTPException(503, "Integration rate limiter unavailable")
    if not allowed:
        audit(db, user.id, "integration_credential_rate_limited", "credential", "denied", data.patient_id)
        await db.commit()
        raise HTTPException(429, "Integration rate limit exceeded")
    if data.patient_id:
        patient_scopes = [INTEGRATION_PATIENT_SCOPES[scope] for scope in data.scopes if scope in INTEGRATION_PATIENT_SCOPES]
        # Preserve the existing authorization rule for a patient-bound knowledge-only credential.
        for access_scope, delegation_scope in patient_scopes or [INTEGRATION_PATIENT_SCOPES["documents:read"]]:
            await authorize_patient(db, user, data.patient_id, access_scope, "integration_credential_create")
            await authorize_patient(db, user, data.patient_id, delegation_scope, "integration_credential_create")
    token = "mga_" + secrets.token_urlsafe(48)
    credential = IntegrationCredential(owner_id=user.id, patient_id=data.patient_id, label=data.label.strip(), scopes=data.scopes, token_hash=token_hash(token), expires_at=data.expires_at)
    db.add(credential)
    await db.flush()
    audit(db, user.id, "integration_credential_created", f"credential:{credential.id}", "success", data.patient_id)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {**credential_view(credential), "token": token}


@app.get("/integration-credentials")
async def list_integration_credentials(user: Actor, db: Db):
    credentials = (await db.scalars(select(IntegrationCredential).where(IntegrationCredential.owner_id == user.id).order_by(IntegrationCredential.created_at.desc()).limit(100))).all()
    return [credential_view(credential) for credential in credentials]


@app.post("/integration-credentials/{credential_id}/revoke")
async def revoke_integration_credential(credential_id: str, user: Actor, db: Db):
    credential = await db.get(IntegrationCredential, credential_id)
    if not credential or credential.owner_id != user.id:
        raise HTTPException(404, "Credential not found")
    if credential.revoked_at is None:
        credential.revoked_at = time.time()
        audit(db, user.id, "integration_credential_revoked", f"credential:{credential.id}", "success", credential.patient_id)
        await db.commit()
    return credential_view(credential)


async def integration_identity(db: AsyncSession, credentials: HTTPAuthorizationCredentials | None, required_scope: str, patient_id: str | None = None) -> tuple[User, IntegrationCredential]:
    if not credentials or not credentials.credentials.startswith("mga_") or len(credentials.credentials) > 200:
        raise HTTPException(401, "Invalid integration credential")
    credential = await db.scalar(select(IntegrationCredential).where(IntegrationCredential.token_hash == token_hash(credentials.credentials)))
    if not credential or credential.revoked_at is not None or credential.expires_at <= time.time():
        raise HTTPException(401, "Invalid or expired integration credential")
    user = await db.get(User, credential.owner_id)
    if not user or not user.active or user.role not in ("patient", "doctor"):
        raise HTTPException(401, "Invalid or expired integration credential")
    try:
        allowed = await integration_rate_limit.check_rate_limit(credential.id, user.id)
    except integration_rate_limit.RateLimitUnavailable:
        raise HTTPException(503, "Integration rate limiter unavailable")
    if not allowed:
        audit(db, user.id, "integration_rate_limited", f"credential:{credential.id}", "denied", credential.patient_id)
        await db.commit()
        raise HTTPException(429, "Integration rate limit exceeded")
    if required_scope not in credential.scopes or (patient_id is not None and credential.patient_id != patient_id):
        audit(db, user.id, "integration_access", f"credential:{credential.id}", "denied", patient_id)
        await db.commit()
        raise HTTPException(403, "Integration scope does not permit this request")
    if required_scope in INTEGRATION_PATIENT_SCOPES:
        if credential.patient_id is None:
            raise HTTPException(403, "Patient binding is required")
        await authorize_patient(db, user, credential.patient_id, INTEGRATION_PATIENT_SCOPES[required_scope][1], "integration_delegation_access")
    return user, credential


@app.get("/integration/patients/{patient_id}/documents")
async def integration_documents(patient_id: str, response: Response, db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)], cursor: UUID4 | None = None):
    user, credential = await integration_identity(db, credentials, "documents:read", patient_id)
    result = await list_documents(patient_id, response, user, db, cursor)
    audit(db, user.id, "integration_documents", f"credential:{credential.id}", "success", patient_id)
    await db.commit()
    return result


@app.get("/integration/patients/{patient_id}/profile")
async def integration_patient_profile(patient_id: str, response: Response, db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    user, credential = await integration_identity(db, credentials, "profile:read", patient_id)
    result = await read_patient_profile(patient_id, user, db)
    audit(db, user.id, "integration_patient_profile", f"credential:{credential.id}", "success", patient_id)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/integration/patients/{patient_id}/consultations")
async def integration_patient_consultations(patient_id: str, response: Response, db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)], cursor: UUID4 | None = None):
    user, credential = await integration_identity(db, credentials, "consultations:read", patient_id)
    result = await list_consultations(patient_id, response, user, db, cursor)
    audit(db, user.id, "integration_consultations", f"credential:{credential.id}", "success", patient_id)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/integration/documents/{document_id}/processing")
async def integration_document_processing(document_id: str, response: Response, db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)], job_id: UUID4 | None = None):
    user, credential = await integration_identity(db, credentials, "documents:read")
    document = await db.get(Document, document_id)
    if not document or document.deleted_at is not None or credential.patient_id != document.patient_id:
        audit(db, user.id, "integration_document_processing", f"credential:{credential.id}", "denied", credential.patient_id)
        await db.commit()
        raise HTTPException(403, "Integration scope does not permit this request")
    result = await document_processing_results(document_id, response, user, db, job_id)
    audit(db, user.id, "integration_document_processing", f"credential:{credential.id}", "success", document.patient_id)
    await db.commit()
    return result


@app.get("/integration/documents/{document_id}/processing/history")
async def integration_document_processing_history(document_id: str, response: Response, db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)], before: UUID4 | None = None):
    user, credential = await integration_identity(db, credentials, "documents:read")
    document = await db.get(Document, document_id)
    if not document or document.deleted_at is not None or credential.patient_id != document.patient_id:
        audit(db, user.id, "integration_document_processing_history", f"credential:{credential.id}", "denied", credential.patient_id)
        await db.commit()
        raise HTTPException(403, "Integration scope does not permit this request")
    result = await document_processing_history(document_id, response, user, db, before)
    audit(db, user.id, "integration_document_processing_history", f"credential:{credential.id}", "success", document.patient_id)
    await db.commit()
    return result


@app.post("/integration/knowledge/search")
async def integration_knowledge_search(data: KnowledgeSearch, response: Response, db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    user, credential = await integration_identity(db, credentials, "knowledge:search")
    result = await search_knowledge(data, user, db)
    # Embedding is external I/O: do not release results under a credential or
    # account that was revoked while the search was in progress.
    current = await db.scalar(select(IntegrationCredential).where(IntegrationCredential.id == credential.id).execution_options(populate_existing=True))
    owner = await db.scalar(select(User).where(User.id == user.id).execution_options(populate_existing=True))
    if (not current or current.owner_id != user.id or current.revoked_at is not None
        or current.expires_at <= time.time() or not owner or not owner.active or owner.role not in ("patient", "doctor")):
        audit(db, user.id, "integration_knowledge_search", f"credential:{credential.id}", "denied")
        await db.commit()
        raise HTTPException(401, "Invalid or expired integration credential")
    if "knowledge:search" not in current.scopes:
        audit(db, user.id, "integration_knowledge_search", f"credential:{credential.id}", "denied")
        await db.commit()
        raise HTTPException(403, "Integration scope does not permit this request")
    audit(db, user.id, "integration_knowledge_search", f"credential:{credential.id}", "success")
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


def consultation_view(consultation: Consultation) -> dict:
    return {"id": consultation.id, "patient_id": consultation.patient_id, "doctor_id": consultation.doctor_id,
            "title": consultation.title, "occurred_at": consultation.occurred_at, "created_at": consultation.created_at}


@app.post("/patients/{patient_id}/consultations", status_code=201)
async def create_consultation(patient_id: str, data: ConsultationCreate, user: Actor, db: Db):
    if user.role != "doctor":
        raise HTTPException(403, "Verified doctor authorization required")
    await authorize_patient(db, user, patient_id, "consultations", "consultation_create")
    consultation = Consultation(patient_id=patient_id, doctor_id=user.id, title=data.title.strip(), occurred_at=data.occurred_at)
    db.add(consultation)
    await db.flush()
    audit(db, user.id, "consultation_created", f"consultation:{consultation.id}", "success", patient_id)
    await db.commit()
    return consultation_view(consultation)


@app.get("/patients/{patient_id}/consultations")
async def list_consultations(patient_id: str, response: Response, user: Actor, db: Db, cursor: UUID4 | None = None):
    await authorize_patient(db, user, patient_id, "consultations", "consultation_list")
    query = select(Consultation).where(Consultation.patient_id == patient_id)
    if cursor is not None:
        anchor = await db.scalar(select(Consultation).where(Consultation.id == str(cursor), Consultation.patient_id == patient_id))
        if not anchor:
            raise HTTPException(422, "Invalid consultation cursor")
        query = query.where(or_(Consultation.occurred_at < anchor.occurred_at, (Consultation.occurred_at == anchor.occurred_at) & (Consultation.id > anchor.id)))
    rows = (await db.scalars(query.order_by(Consultation.occurred_at.desc(), Consultation.id).limit(501))).all()
    if len(rows) > 500:
        response.headers["X-Next-Cursor"] = rows[499].id
    response.headers["Cache-Control"] = "no-store"
    result = [consultation_view(row) for row in rows[:500]]
    await authorize_patient(db, user, patient_id, "consultations", "consultation_list_release")
    await db.commit()
    return result


def note_view(note: ClinicalNote, version: ClinicalNoteVersion, consultation_id: str | None = None, assistant_referenced: bool = False) -> dict:
    return {"note_id": note.id, "patient_id": note.patient_id, "version_id": version.id,
            "version": version.version, "content": version.content, "status": version.status,
            "author_id": version.author_id, "reviewer_id": version.reviewer_id,
            "source_document_id": version.source_document_id, "source_processing_job_id": version.source_processing_job_id, "source_extraction_version": version.source_extraction_version, "source_fields": version.source_fields, "created_at": version.created_at,
            "reviewed_at": version.reviewed_at, "review_comment": version.review_comment, "consultation_id": consultation_id, "assistant_referenced": assistant_referenced, "ai_assisted": version.ai_assisted}


async def require_note_doctor(db: AsyncSession, user: User, patient_id: str, action: str) -> None:
    if user.role != "doctor":
        await audit_denial(db, user.id, action, f"patient:{patient_id}", patient_id)
        raise HTTPException(403, "Verified doctor authorization required")
    await authorize_patient(db, user, patient_id, "clinical_notes", action)


@app.get("/clinical-review-policy")
async def clinical_review_policy(user: Actor):
    return {"note_review_policy": NOTE_REVIEW_POLICY}


def validate_note_source_quotes(content: str, fields: list[NoteSourceField]) -> None:
    for field in fields:
        if field.note_quote is not None and content.count(field.note_quote) != 1:
            raise HTTPException(422, "Each source quotation must occur exactly once in the note; update it or remove the document references")


async def validate_note_source(db: AsyncSession, user: User, patient_id: str, document_id: str | None, job_id: str | None = None, extraction_version: int | None = None, source_fields: list[NoteSourceField] | None = None) -> None:
    source_fields = source_fields or []
    if source_fields and extraction_version is None:
        raise HTTPException(422, "Field references require a transcription version")
    if len({(field.group, field.index, field.field) for field in source_fields}) != len(source_fields):
        raise HTTPException(422, "Duplicate transcription field reference")
    if extraction_version is not None and job_id is None:
        raise HTTPException(422, "A transcription version requires a processing run")
    if document_id is None:
        if job_id is not None:
            raise HTTPException(422, "A processing run requires a source document")
        return
    await authorize_patient(db, user, patient_id, "read_documents", "note_source_access")
    document = await db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if not document or document.deleted_at is not None or document.patient_id != patient_id:
        raise HTTPException(404, "Source document not found")
    if job_id is not None:
        job = await db.scalar(select(ExtractionJob).where(ExtractionJob.id == job_id, ExtractionJob.document_id == document_id, ExtractionJob.status == "completed"))
        if not job:
            raise HTTPException(404, "Completed processing run not found for this document")
        if extraction_version is not None:
            transcription = await db.scalar(select(StructuredExtractionVersion).where(StructuredExtractionVersion.job_id == job.id, StructuredExtractionVersion.version == extraction_version))
            if not transcription:
                raise HTTPException(404, "Transcription version not found for this processing run")
            try:
                structured = StructuredExtraction.model_validate(transcription.data)
            except ValidationError:
                raise HTTPException(409, "Transcription version is unavailable or invalid")
            for reference in source_fields:
                records = getattr(structured, reference.group)
                if reference.index >= len(records) or reference.field not in type(records[reference.index]).model_fields or getattr(records[reference.index], reference.field) is None:
                    raise HTTPException(422, "Transcription field reference is not present")


@app.post("/patients/{patient_id}/notes", status_code=201)
async def create_note(patient_id: str, data: NoteDraft, user: Actor, db: Db):
    await require_note_doctor(db, user, patient_id, "note_create")
    if not data.content.strip():
        raise HTTPException(422, "Note content cannot be blank")
    validate_note_source_quotes(data.content, data.source_fields)
    await validate_note_source(db, user, patient_id, data.source_document_id, str(data.source_processing_job_id) if data.source_processing_job_id else None, data.source_extraction_version, data.source_fields)
    if data.consultation_id:
        await authorize_patient(db, user, patient_id, "consultations", "note_consultation_access")
        consultation = await db.get(Consultation, data.consultation_id)
        if not consultation or consultation.patient_id != patient_id:
            raise HTTPException(404, "Consultation not found")
    note = ClinicalNote(patient_id=patient_id)
    db.add(note)
    await db.flush()
    version = ClinicalNoteVersion(note_id=note.id, version=1, content=data.content, author_id=user.id, source_document_id=data.source_document_id, source_processing_job_id=str(data.source_processing_job_id) if data.source_processing_job_id else None, source_extraction_version=data.source_extraction_version, source_fields=[field.model_dump(exclude_none=True) for field in data.source_fields])
    db.add(version)
    if data.consultation_id:
        db.add(NoteConsultation(note_id=note.id, consultation_id=data.consultation_id))
    await db.flush()
    audit(db, user.id, "note_draft_created", f"note:{note.id}", "success", patient_id)
    await db.commit()
    return note_view(note, version, data.consultation_id)


@app.get("/patients/{patient_id}/notes")
async def list_notes(patient_id: str, user: Actor, db: Db):
    await authorize_patient(db, user, patient_id, "clinical_notes", "note_list")
    query = select(ClinicalNote, ClinicalNoteVersion).join(ClinicalNoteVersion, ClinicalNote.id == ClinicalNoteVersion.note_id).where(ClinicalNote.patient_id == patient_id)
    if user.role == "patient":
        query = query.where(ClinicalNoteVersion.status == "approved")
    rows = (await db.execute(query.order_by(ClinicalNote.created_at.desc(), ClinicalNoteVersion.version.desc()))).all()
    links = (await db.execute(select(NoteConsultation.note_id, NoteConsultation.consultation_id).join(ClinicalNote).where(ClinicalNote.patient_id == patient_id))).all()
    mapping = dict(links)
    version_ids = [version.id for _, version in rows]
    referenced = set((await db.scalars(select(NoteAssistantSource.version_id).where(NoteAssistantSource.version_id.in_(version_ids)))).all()) if version_ids else set()
    await authorize_patient(db, user, patient_id, "clinical_notes", "note_list_release")
    await db.commit()
    return [note_view(note, version, mapping.get(note.id), version.id in referenced) for note, version in rows]


async def locked_note(db: AsyncSession, note_id: str) -> ClinicalNote:
    note = await db.scalar(select(ClinicalNote).where(ClinicalNote.id == note_id).with_for_update())
    if not note:
        raise HTTPException(404, "Note not found")
    return note


@app.post("/notes/{note_id}/versions", status_code=201)
async def amend_note(note_id: str, data: NoteDraft, user: Actor, db: Db):
    note = await locked_note(db, note_id)
    await require_note_doctor(db, user, note.patient_id, "note_amend")
    link = await db.get(NoteConsultation, note.id)
    if data.consultation_id is not None and (not link or data.consultation_id != link.consultation_id):
        raise HTTPException(409, "A note's consultation link cannot be changed")
    if not data.content.strip():
        raise HTTPException(422, "Note content cannot be blank")
    latest = await db.scalar(select(ClinicalNoteVersion).where(ClinicalNoteVersion.note_id == note.id).order_by(ClinicalNoteVersion.version.desc()).limit(1))
    if not latest or latest.status not in ("approved", "rejected"):
        raise HTTPException(409, "A new version requires an approved or rejected latest version")
    # An omitted source retains attribution, even if access to the original is later revoked.
    # Explicitly selecting a source requires current access; explicit null removes the link.
    source_id = data.source_document_id if "source_document_id" in data.model_fields_set else latest.source_document_id
    if "source_processing_job_id" in data.model_fields_set:
        job_id = str(data.source_processing_job_id) if data.source_processing_job_id else None
    elif "source_document_id" in data.model_fields_set:
        job_id = None
    else:
        job_id = latest.source_processing_job_id
    if "source_extraction_version" in data.model_fields_set:
        extraction_version = data.source_extraction_version
    elif "source_document_id" in data.model_fields_set or "source_processing_job_id" in data.model_fields_set:
        extraction_version = None
    else:
        extraction_version = latest.source_extraction_version
    if "source_fields" in data.model_fields_set:
        source_fields = data.source_fields
    elif {"source_document_id", "source_processing_job_id", "source_extraction_version"} & data.model_fields_set:
        source_fields = []
    else:
        source_fields = [NoteSourceField.model_validate(field) for field in latest.source_fields]
    validate_note_source_quotes(data.content, source_fields)
    if {"source_document_id", "source_processing_job_id", "source_extraction_version", "source_fields"} & data.model_fields_set:
        await validate_note_source(db, user, note.patient_id, source_id, job_id, extraction_version, source_fields)
    version = ClinicalNoteVersion(note_id=note.id, version=latest.version + 1, content=data.content, author_id=user.id, source_document_id=source_id, source_processing_job_id=job_id, source_extraction_version=extraction_version, source_fields=[field.model_dump(exclude_none=True) for field in source_fields], ai_assisted=latest.ai_assisted)
    db.add(version)
    try:
        await db.flush()
        assistant_source = await db.get(NoteAssistantSource, latest.id)
        if assistant_source:
            db.add(NoteAssistantSource(version_id=version.id, message_id=assistant_source.message_id))
        audit(db, user.id, "note_amendment_created", f"note:{note.id}", "success", note.patient_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Another version already exists")
    return note_view(note, version, link.consultation_id if link else None, assistant_source is not None)


@app.post("/notes/{note_id}/versions/{version_id}/revise", status_code=201)
async def revise_note_draft(note_id: str, version_id: str, data: DraftRevision, user: Actor, db: Db):
    note = await locked_note(db, note_id)
    await require_note_doctor(db, user, note.patient_id, "note_draft_revision")
    old = await db.get(ClinicalNoteVersion, version_id)
    latest = await db.scalar(select(ClinicalNoteVersion).where(ClinicalNoteVersion.note_id == note.id).order_by(ClinicalNoteVersion.version.desc()).limit(1))
    if not old or old.note_id != note.id:
        raise HTTPException(404, "Note version not found")
    if old.author_id != user.id:
        raise HTTPException(403, "Only the author may revise this draft")
    if not latest or latest.id != old.id or old.status != "draft":
        raise HTTPException(409, "Only the latest draft may be revised")
    if not data.content.strip():
        raise HTTPException(422, "Note content cannot be blank")
    source = await db.get(NoteAssistantSource, old.id)
    link = await db.get(NoteConsultation, note.id)
    if not data.clear_document_source:
        validate_note_source_quotes(data.content, [NoteSourceField.model_validate(field) for field in old.source_fields])
    old.status = "superseded"
    await db.flush()
    version = ClinicalNoteVersion(note_id=note.id, version=old.version + 1, content=data.content, author_id=user.id, source_document_id=None if data.clear_document_source else old.source_document_id, source_processing_job_id=None if data.clear_document_source else old.source_processing_job_id, source_extraction_version=None if data.clear_document_source else old.source_extraction_version, source_fields=[] if data.clear_document_source else old.source_fields, ai_assisted=old.ai_assisted)
    db.add(version)
    await db.flush()
    if source:
        db.add(NoteAssistantSource(version_id=version.id, message_id=source.message_id))
    audit(db, user.id, "note_draft_revised", f"note:{note.id}", "success", note.patient_id)
    await db.commit()
    return note_view(note, version, link.consultation_id if link else None, source is not None)


@app.post("/notes/{note_id}/versions/{version_id}/submit")
async def submit_note(note_id: str, version_id: str, user: Actor, db: Db):
    note = await locked_note(db, note_id)
    await require_note_doctor(db, user, note.patient_id, "note_submit")
    version = await db.get(ClinicalNoteVersion, version_id)
    if not version or version.note_id != note.id:
        raise HTTPException(404, "Note version not found")
    if version.author_id != user.id:
        raise HTTPException(403, "Only the author may submit this draft")
    if version.status != "draft":
        raise HTTPException(409, "Only a draft may be submitted")
    version.status = "pending_review"
    link = await db.get(NoteConsultation, note.id)
    assistant_source = await db.get(NoteAssistantSource, version.id)
    now = time.time()
    review_grant = select(Consent.id).where(Consent.relationship_id == Relationship.id, Consent.scope == "clinical_notes", Consent.status == "active", Consent.effective_at <= now, Consent.expires_at > now).exists()
    reviewers = (await db.execute(select(Relationship.id, Relationship.doctor_id).join(User, User.id == Relationship.doctor_id).where(Relationship.patient_id == note.patient_id, Relationship.status == "active", User.role == "doctor", User.active.is_(True), User.verification_status == "verified", review_grant))).all()
    for relationship_id, doctor_id in reviewers:
        if NOTE_REVIEW_POLICY == "independent" and doctor_id == version.author_id:
            continue
        queue_notification(db, recipient_id=doctor_id, patient_id=note.patient_id, relationship_id=relationship_id, required_scope="clinical_notes", kind="note_review_requested", event_key=f"note-review:{version.id}", resource_id=note.id)
    audit(db, user.id, "note_submit", f"note:{note.id}", "success", note.patient_id)
    await db.commit()
    return note_view(note, version, link.consultation_id if link else None, assistant_source is not None)


@app.post("/notes/{note_id}/versions/{version_id}/review")
async def review_note(note_id: str, version_id: str, data: NoteReview, user: Actor, db: Db):
    note = await locked_note(db, note_id)
    await require_note_doctor(db, user, note.patient_id, "note_review")
    version = await db.get(ClinicalNoteVersion, version_id)
    if not version or version.note_id != note.id:
        raise HTTPException(404, "Note version not found")
    if version.status != "pending_review":
        raise HTTPException(409, "Only a pending version may be reviewed")
    if NOTE_REVIEW_POLICY == "independent" and version.author_id == user.id:
        audit(db, user.id, "note_review_independence", f"note:{note.id}", "denied", note.patient_id)
        await db.commit()
        raise HTTPException(403, "An independent doctor must review this note")
    if data.decision == "reject" and (not data.reason or len(data.reason.strip()) < 3):
        raise HTTPException(422, "A rejection reason of at least 3 characters is required")
    if data.decision == "approve" and data.reason:
        raise HTTPException(422, "A rejection reason is only accepted for rejection")
    if data.decision == "approve":
        await db.execute(update(ClinicalNoteVersion).where(ClinicalNoteVersion.note_id == note.id, ClinicalNoteVersion.status == "approved").values(status="superseded"))
    version.status = "approved" if data.decision == "approve" else "rejected"
    version.reviewer_id = user.id
    version.review_comment = data.reason.strip() if data.decision == "reject" and data.reason else None
    version.reviewed_at = time.time()
    link = await db.get(NoteConsultation, note.id)
    assistant_source = await db.get(NoteAssistantSource, version.id)
    audit(db, user.id, "note_approved" if data.decision == "approve" else "note_rejected", f"note:{note.id}", "success", note.patient_id)
    await db.commit()
    return note_view(note, version, link.consultation_id if link else None, assistant_source is not None)


def compare_note_lines(old: str, new: str) -> list[dict[str, str]]:
    before, after = old.splitlines(), new.splitlines()
    if len(before) > 1000 or len(after) > 1000:
        raise ValueError("too many lines")
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=True)
    result = []
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag in ("equal", "delete", "replace"):
            result.extend({"kind": "context" if tag == "equal" else "removed", "text": line} for line in before[a:b])
        if tag in ("insert", "replace"):
            result.extend({"kind": "added", "text": line} for line in after[c:d])
    return result


@app.post("/notes/{note_id}/compare")
async def compare_note_versions(note_id: str, data: NoteComparison, request: Request, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    await enforce_auth_limit(request, "note-comparison", user.id, 30, 200)
    note = await db.get(ClinicalNote, note_id)
    if not note:
        raise HTTPException(404, "Note not found")
    await require_note_doctor(db, user, note.patient_id, "note_compare")
    versions = (await db.scalars(select(ClinicalNoteVersion).where(ClinicalNoteVersion.note_id == note_id, ClinicalNoteVersion.id.in_([data.from_version_id, data.to_version_id])))).all()
    by_id = {version.id: version for version in versions}
    first, second = by_id.get(data.from_version_id), by_id.get(data.to_version_id)
    if not first or not second or first.version >= second.version:
        raise HTTPException(422, "Select two versions in chronological order")
    patient_id, old, new, first_number, second_number = note.patient_id, first.content, second.content, first.version, second.version
    await db.commit()
    try:
        lines = await asyncio.to_thread(compare_note_lines, old, new)
    except ValueError:
        raise HTTPException(422, "Comparison supports up to 1000 lines per version")
    try:
        token = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid or expired session")
    db.expire_all()
    current = await active_identity(db, token["sub"], token["sid"])
    await require_note_doctor(db, current, patient_id, "note_compare_release")
    audit(db, current.id, "note_versions_compared", f"note:{note_id}", "success", patient_id)
    await db.commit()
    return {"from_version":first_number, "to_version":second_number, "lines":lines}


async def conversation_access(db: AsyncSession, user: User, conversation: Conversation, action: str, extra_scopes: tuple[str, ...] = ()) -> None:
    if user.role not in ("patient", "doctor") or conversation.owner_id != user.id:
        await audit_denial(db, user.id, action, f"conversation:{conversation.id}", conversation.patient_id)
        raise HTTPException(403, "Conversation access denied")
    await authorize_patient(db, user, conversation.patient_id, "assistant", action)
    for scope in set(extra_scopes):
        await authorize_patient(db, user, conversation.patient_id, scope, action)


async def conversation_history_scopes(db: AsyncSession, conversation_id: str) -> tuple[str, ...]:
    turns = (await db.scalars(select(AssistantTurn).where(AssistantTurn.conversation_id == conversation_id))).all()
    return tuple(scope for scope, used in (("read_documents", any(t.include_documents for t in turns)), ("clinical_notes", any(t.include_notes for t in turns)), ("consultations", any(t.include_consultations for t in turns)), ("read_profile", any(t.include_profile for t in turns))) if used)


@app.post("/conversations/{conversation_id}/messages/{message_id}/note-draft", status_code=201)
async def create_assistant_referenced_note(conversation_id: str, message_id: str, data: AssistantNoteDraft, user: Actor, db: Db):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    await require_note_doctor(db, user, conversation.patient_id, "assistant_note_create")
    await conversation_access(db, user, conversation, "assistant_note_source", await conversation_history_scopes(db, conversation_id))
    message = await db.get(ConversationMessage, message_id)
    turn = await db.get(AssistantTurn, message.turn_id) if message else None
    if not message or message.conversation_id != conversation.id or message.role != "assistant" or not turn or turn.conversation_id != conversation.id or turn.status != "completed" or not message.citations:
        raise HTTPException(422, "A completed evidence-cited assistant response is required")
    if not data.content.strip():
        raise HTTPException(422, "Note content cannot be blank")
    if data.consultation_id:
        await authorize_patient(db, user, conversation.patient_id, "consultations", "note_consultation_access")
        consultation = await db.get(Consultation, data.consultation_id)
        if not consultation or consultation.patient_id != conversation.patient_id:
            raise HTTPException(404, "Consultation not found")
    note = ClinicalNote(patient_id=conversation.patient_id)
    db.add(note)
    await db.flush()
    version = ClinicalNoteVersion(note_id=note.id, version=1, content=data.content, author_id=user.id)
    db.add(version)
    await db.flush()
    db.add(NoteAssistantSource(version_id=version.id, message_id=message.id))
    if data.consultation_id:
        db.add(NoteConsultation(note_id=note.id, consultation_id=data.consultation_id))
    audit(db, user.id, "assistant_note_draft_created", f"note:{note.id}", "success", conversation.patient_id)
    await db.commit()
    return note_view(note, version, data.consultation_id, True)


@app.post("/conversations/{conversation_id}/messages/{message_id}/generate-note-draft", status_code=201)
async def generate_note_draft(conversation_id: str, message_id: str, data: GeneratedNoteRequest, request: Request, response: Response, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    await enforce_auth_limit(request, "note-generation", user.id, 10, 100)
    conversation = await db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    patient_id = conversation.patient_id
    await require_note_doctor(db, user, patient_id, "assistant_note_generation")
    await conversation_access(db, user, conversation, "assistant_note_generation_source", await conversation_history_scopes(db, conversation_id))
    message = await db.get(ConversationMessage, message_id)
    turn = await db.get(AssistantTurn, message.turn_id) if message else None
    if not message or message.conversation_id != conversation_id or message.role != "assistant" or not turn or turn.conversation_id != conversation_id or turn.status != "completed" or not any(c.get("kind") in ("unverified_patient_document", "doctor_approved_note", "self_reported_patient_profile") for c in message.citations if isinstance(c, dict)):
        raise HTTPException(422, "A completed assistant response citing patient records is required")
    if data.consultation_id:
        await authorize_patient(db, user, patient_id, "consultations", "note_consultation_access")
        consultation = await db.get(Consultation, data.consultation_id)
        if not consultation or consultation.patient_id != patient_id:
            raise HTTPException(404, "Consultation not found")
    source_text = message.content[:5000]
    source_id = f"assistant_response:{message.id}"
    actor_id, request_id, claim = user.id, str(data.request_id), str(uuid.uuid4())
    db.add(NoteGeneration(actor_id=actor_id, request_id=request_id, conversation_id=conversation_id, message_id=message_id, consultation_id=data.consultation_id, status="running", claim_token=claim, started_at=time.time()))
    try:
        await db.flush()
        audit(db, actor_id, "assistant_note_generation_requested", f"conversation:{conversation_id}", "success", patient_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.get(NoteGeneration, (actor_id, request_id))
        if not existing or (existing.conversation_id, existing.message_id, existing.consultation_id) != (conversation_id, message_id, data.consultation_id):
            raise HTTPException(409, "Request ID is already bound to different inputs")
        if existing.status == "completed":
            version = await db.get(ClinicalNoteVersion, existing.version_id)
            note = await db.get(ClinicalNote, version.note_id) if version else None
            if not note or note.patient_id != patient_id:
                raise HTTPException(409, "Generated note is unavailable")
            audit(db, actor_id, "assistant_note_generation_replayed", f"note:{note.id}", "success", patient_id)
            result = note_view(note, version, data.consultation_id, True)
            await db.commit()
            response.status_code = 200
            response.headers["Cache-Control"] = "no-store"
            return result
        claimed = await db.execute(update(NoteGeneration).where(NoteGeneration.actor_id == actor_id, NoteGeneration.request_id == request_id, or_(NoteGeneration.status == "failed", (NoteGeneration.status == "running") & (NoteGeneration.started_at < time.time() - 180))).values(status="running", claim_token=claim, started_at=time.time()).returning(NoteGeneration.actor_id))
        if claimed.scalar_one_or_none() is None:
            await db.rollback()
            raise HTTPException(409, "Generation is already in progress")
        audit(db, actor_id, "assistant_note_generation_retried", f"conversation:{conversation_id}", "success", patient_id)
        await db.commit()
    try:
        try:
            draft = await asyncio.wait_for(assistant_provider.draft_answer("Prepare a concise provisional clinical note draft using only this unreviewed assistant response. Clearly identify missing information and uncertainty. Do not add findings, dates, diagnoses, medication changes or treatment decisions.", [], [{"id": source_id, "kind": "unreviewed_assistant_response", "title": "Unreviewed assistant response", "text": source_text}], "doctor"), timeout=120)
        except (assistant_provider.AssistantUnavailable, asyncio.TimeoutError):
            raise HTTPException(503, "Note draft generation unavailable")
        if not draft.answer.strip() or draft.citations != [source_id]:
            raise HTTPException(503, "Note draft generation unavailable")
        try:
            token = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"], options={"require": ["sub", "sid", "exp", "iat"]})
        except jwt.InvalidTokenError:
            raise HTTPException(401, "Invalid or expired session")
        db.expire_all()
        current = await active_identity(db, token["sub"], token["sid"])
        conversation = await db.get(Conversation, conversation_id)
        if not conversation or conversation.patient_id != patient_id:
            raise HTTPException(404, "Conversation not found")
        await require_note_doctor(db, current, patient_id, "assistant_note_generation_release")
        await conversation_access(db, current, conversation, "assistant_note_generation_source_release", await conversation_history_scopes(db, conversation_id))
        if data.consultation_id:
            await authorize_patient(db, current, patient_id, "consultations", "note_consultation_access_release")
            consultation = await db.get(Consultation, data.consultation_id)
            if not consultation or consultation.patient_id != patient_id:
                raise HTTPException(404, "Consultation not found")
        note = ClinicalNote(patient_id=patient_id)
        db.add(note)
        await db.flush()
        version = ClinicalNoteVersion(note_id=note.id, version=1, content=draft.answer, author_id=current.id, ai_assisted=True)
        db.add(version)
        await db.flush()
        result = await db.execute(update(NoteGeneration).where(NoteGeneration.actor_id == actor_id, NoteGeneration.request_id == request_id, NoteGeneration.status == "running", NoteGeneration.claim_token == claim).values(status="completed", claim_token=None, version_id=version.id).returning(NoteGeneration.actor_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(409, "Generation claim expired; retry with the same request ID")
        db.add(NoteAssistantSource(version_id=version.id, message_id=message_id))
        if data.consultation_id:
            db.add(NoteConsultation(note_id=note.id, consultation_id=data.consultation_id))
        audit(db, current.id, "assistant_note_generated", f"note:{note.id}", "success", patient_id)
        await db.commit()
        response.headers["Cache-Control"] = "no-store"
        return note_view(note, version, data.consultation_id, True)
    except BaseException:
        try:
            await db.rollback()
            failed = await db.execute(update(NoteGeneration).where(NoteGeneration.actor_id == actor_id, NoteGeneration.request_id == request_id, NoteGeneration.status == "running", NoteGeneration.claim_token == claim).values(status="failed", claim_token=None).returning(NoteGeneration.actor_id))
            if failed.scalar_one_or_none():
                audit(db, actor_id, "assistant_note_generation_failed", f"conversation:{conversation_id}", "failed", patient_id)
            await db.commit()
        except SQLAlchemyError:
            await db.rollback()
        raise


@app.get("/notes/{note_id}/versions/{version_id}/assistant-source")
async def read_note_assistant_source(note_id: str, version_id: str, user: Actor, db: Db):
    note = await db.get(ClinicalNote, note_id)
    if not note:
        raise HTTPException(404, "Note not found")
    await require_note_doctor(db, user, note.patient_id, "assistant_note_source_read")
    version = await db.get(ClinicalNoteVersion, version_id)
    source = await db.get(NoteAssistantSource, version_id) if version and version.note_id == note_id else None
    message = await db.get(ConversationMessage, source.message_id) if source else None
    conversation = await db.get(Conversation, message.conversation_id) if message else None
    if not conversation or conversation.patient_id != note.patient_id:
        raise HTTPException(404, "Assistant source not found")
    await conversation_access(db, user, conversation, "assistant_note_source_read", await conversation_history_scopes(db, conversation.id))
    result = {"content": message.content, "citations": message.citations, "review_status": "unreviewed"}
    audit(db, user.id, "assistant_note_source_opened", f"note:{note.id}", "success", note.patient_id)
    await db.commit()
    return result


@app.post("/conversations", status_code=201)
async def create_conversation(data: ConversationCreate, user: Actor, db: Db):
    if user.role not in ("patient", "doctor"):
        raise HTTPException(403, "Role not permitted")
    await authorize_patient(db, user, data.patient_id, "assistant", "conversation_create")
    conversation = Conversation(patient_id=data.patient_id, owner_id=user.id)
    db.add(conversation)
    await db.flush()
    audit(db, user.id, "conversation_created", f"conversation:{conversation.id}", "success", data.patient_id)
    await db.commit()
    return {"id": conversation.id, "patient_id": conversation.patient_id, "created_at": conversation.created_at}


@app.get("/conversations")
async def list_conversations(response: Response, user: Actor, db: Db, patient_id: UUID4 | None = None, cursor: UUID4 | None = None):
    if user.role not in ("patient", "doctor"):
        raise HTTPException(403, "Role not permitted")
    query = select(Conversation).where(Conversation.owner_id == user.id)
    patient_filter = str(patient_id) if patient_id else None
    if patient_filter:
        query = query.where(Conversation.patient_id == patient_filter)
    if cursor:
        anchor = await db.get(Conversation, str(cursor))
        if not anchor or anchor.owner_id != user.id or (patient_filter and anchor.patient_id != patient_filter):
            raise HTTPException(422, "Invalid conversation cursor")
        query = query.where(or_(Conversation.created_at < anchor.created_at, (Conversation.created_at == anchor.created_at) & (Conversation.id < anchor.id)))
    rows = (await db.scalars(query.order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(51))).all()
    if len(rows) > 50:
        response.headers["X-Next-Cursor"] = rows[49].id
    response.headers["Cache-Control"] = "no-store"
    candidates = [{"id": row.id, "patient_id": row.patient_id, "created_at": row.created_at} for row in rows[:50]]
    visible = []
    for item in candidates:
        conversation = await db.get(Conversation, item["id"])
        if conversation is None:
            continue
        try:
            await conversation_access(db, user, conversation, "conversation_list", await conversation_history_scopes(db, conversation.id))
            visible.append(item)
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            await db.refresh(user)
    await db.refresh(user)
    if not user.active:
        raise HTTPException(401, "Invalid or expired session")
    checked = []
    for item in visible:
        conversation = await db.get(Conversation, item["id"])
        if conversation is None:
            continue
        try:
            await conversation_access(db, user, conversation, "conversation_list_release", await conversation_history_scopes(db, conversation.id))
            checked.append(item)
            # A later denied item rolls back its transaction; keep the audit for
            # each already-authorized conversation durable.
            await db.commit()
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            await db.refresh(user)
    await db.commit()
    return checked


@app.get("/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: str, response: Response, user: Actor, db: Db, cursor: str | None = None):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    await conversation_access(db, user, conversation, "conversation_history", await conversation_history_scopes(db, conversation.id))
    query = select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id)
    if cursor is not None:
        cursor_id = safe_uuid(cursor)
        anchor = await db.get(ConversationMessage, cursor_id) if cursor_id else None
        if not anchor or anchor.conversation_id != conversation.id:
            raise HTTPException(422, "Invalid conversation cursor")
        query = query.where(or_(ConversationMessage.created_at < anchor.created_at, (ConversationMessage.created_at == anchor.created_at) & (ConversationMessage.id < anchor.id)))
    rows = (await db.scalars(query.order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(101))).all()
    if len(rows) > 100:
        response.headers["X-Next-Cursor"] = rows[99].id
    response.headers["Cache-Control"] = "no-store"
    result = [{"id": message.id, "role": message.role, "content": message.content, "citations": message.citations, "created_at": message.created_at, "review_status": "unreviewed" if message.role == "assistant" else None} for message in reversed(rows[:100])]
    await db.commit()
    return result


@app.post("/conversations/{conversation_id}/ticket")
async def conversation_ticket(conversation_id: str, response: Response, user: Actor, db: Db, credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    await conversation_access(db, user, conversation, "conversation_ticket", await conversation_history_scopes(db, conversation.id))
    payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"])
    token = secrets.token_urlsafe(48)
    db.add(SocketTicket(token_hash=token_hash(token), conversation_id=conversation.id, user_id=user.id, session_id=payload["sid"], expires_at=time.time() + 30))
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"ticket": token, "expires_in": 30}


async def ensure_documents_available(db: AsyncSession, patient_id: str, document_ids: set[str], lock: bool = False) -> None:
    if not document_ids:
        return
    query = select(Document.id).where(Document.patient_id == patient_id, Document.id.in_(document_ids), Document.deleted_at.is_(None)).order_by(Document.id)
    if lock:
        query = query.with_for_update()
    if set((await db.scalars(query)).all()) != document_ids:
        raise HTTPException(403, "Document source is no longer available")


async def ensure_document_sources_current(db: AsyncSession, snapshots: list[dict]) -> None:
    """Require each extracted section to belong to the latest completed job."""
    latest_jobs: dict[str, ExtractionJob | None] = {}
    for snapshot in snapshots:
        doc_id = snapshot["document_id"]
        if doc_id not in latest_jobs:
            latest_jobs[doc_id] = await db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == doc_id).order_by(ExtractionJob.created_at.desc(), ExtractionJob.id.desc()).limit(1))
        job = latest_jobs[doc_id]
        if not job or job.id != snapshot["job_id"] or job.status != "completed":
            raise HTTPException(409, "Document processing changed during generation")
        page = await db.scalar(select(ExtractedPage).where(ExtractedPage.job_id == job.id, ExtractedPage.page_number == snapshot["page_number"]))
        if not page or page.method != snapshot["method"]:
            raise HTTPException(409, "Document processing changed during generation")
        text = page.text if snapshot["layer"] == "primary" else (page.supplemental_ocr_text or "")
        if text[:2000] != snapshot["text"]:
            raise HTTPException(409, "Document processing changed during generation")


async def ensure_note_sources_current(db: AsyncSession, patient_id: str, snapshots: list[dict], *, lock: bool = False) -> None:
    """Check the exact approved note versions supplied to the model."""
    if not snapshots:
        return
    ids = sorted({snapshot["note_id"] for snapshot in snapshots})
    query = select(ClinicalNote).where(ClinicalNote.id.in_(ids), ClinicalNote.patient_id == patient_id).order_by(ClinicalNote.id)
    if lock:
        # Note approval and amendment acquire the same parent-row lock.
        query = query.with_for_update()
    notes = {note.id for note in (await db.scalars(query)).all()}
    if notes != set(ids):
        raise HTTPException(409, "Clinical note changed during generation")
    for snapshot in snapshots:
        version = await db.scalar(select(ClinicalNoteVersion).where(ClinicalNoteVersion.id == snapshot["version_id"], ClinicalNoteVersion.note_id == snapshot["note_id"]))
        if not version or version.status != "approved" or version.content[:2000] != snapshot["text"]:
            raise HTTPException(409, "Clinical note changed during generation")
        if "consultation_id" in snapshot:
            link = await db.get(NoteConsultation, snapshot["note_id"])
            if not link or link.consultation_id != snapshot["consultation_id"]:
                raise HTTPException(409, "Clinical note consultation changed during generation")


async def ensure_knowledge_sources_current(db: AsyncSession, snapshots: list[dict], *, lock: bool = False) -> None:
    """Ensure every reference supplied to a model remains approved and unchanged."""
    if not snapshots:
        return
    ids = sorted({snapshot["source_id"] for snapshot in snapshots})
    query = select(KnowledgeSource).where(KnowledgeSource.id.in_(ids)).order_by(KnowledgeSource.id)
    if lock:
        query = query.with_for_update()
    sources = {source.id: source for source in (await db.scalars(query)).all()}
    now = time.time()
    for snapshot in snapshots:
        source = sources.get(snapshot["source_id"])
        if (not source or source.status != "approved" or source.index_status != "ready"
            or source.effective_at > now or (source.retire_at is not None and source.retire_at <= now)
            or source.embedding_model != snapshot["embedding_model"]
            or source.embedding_digest != snapshot.get("embedding_digest")
            or source.title != snapshot["title"] or source.provenance != snapshot["provenance"]
            or source.version != snapshot["version"]):
            raise HTTPException(409, "Approved reference changed during generation")
        chunk = await db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id, KnowledgeChunk.ordinal == snapshot["ordinal"]))
        if not chunk or chunk.content[:1500] != snapshot["text"]:
            raise HTTPException(409, "Approved reference changed during generation")


async def ensure_consultation_sources_current(db: AsyncSession, patient_id: str, snapshots: list[dict], *, lock: bool = False) -> None:
    if not snapshots:
        return
    query = select(Consultation).where(Consultation.id.in_([s["id"] for s in snapshots]), Consultation.patient_id == patient_id).order_by(Consultation.id)
    if lock:
        query = query.with_for_update()
    records = {row.id: row for row in (await db.scalars(query)).all()}
    for snapshot in snapshots:
        row = records.get(snapshot["id"])
        if not row or row.title != snapshot["title"] or row.occurred_at != snapshot["occurred_at"] or row.doctor_id != snapshot["doctor_id"]:
            raise HTTPException(409, "Consultation changed during generation")


async def ensure_profile_source_current(db: AsyncSession, patient_id: str, snapshot: dict | None, *, lock: bool = False) -> None:
    if snapshot is None:
        return
    if lock:
        await db.scalar(select(User.id).where(User.id == patient_id).with_for_update())
    profile = await db.get(PatientProfile, patient_id)
    current = {field: (getattr(profile, field) or "")[:2000] if profile else "" for field in ("allergies", "medications", "medical_history")}
    if current != snapshot:
        raise HTTPException(409, "Patient profile changed during generation")


async def socket_access(user_id: str, session_id: str, conversation_id: str, scopes: tuple[str, ...] = (), document_ids: set[str] | None = None, knowledge_snapshots: list[dict] | None = None, note_snapshots: list[dict] | None = None, document_snapshots: list[dict] | None = None, consultation_snapshots: list[dict] | None = None, profile_snapshot: dict | None = None) -> None:
    async with SessionFactory() as db:
        user = await active_identity(db, user_id, session_id)
        conversation = await db.get(Conversation, conversation_id)
        if not conversation:
            raise HTTPException(404, "Conversation not found")
        historical = await conversation_history_scopes(db, conversation_id)
        await conversation_access(db, user, conversation, "assistant_access", tuple(set(scopes + historical)))
        await ensure_documents_available(db, conversation.patient_id, document_ids or set())
        await ensure_document_sources_current(db, document_snapshots or [])
        await ensure_knowledge_sources_current(db, knowledge_snapshots or [])
        await ensure_note_sources_current(db, conversation.patient_id, note_snapshots or [])
        await ensure_consultation_sources_current(db, conversation.patient_id, consultation_snapshots or [])
        await ensure_profile_source_current(db, conversation.patient_id, profile_snapshot)
        await db.commit()


async def finish_turn(turn_id: str, status: str, user_id: str, patient_id: str) -> None:
    async with SessionFactory() as db:
        result = await db.execute(update(AssistantTurn).where(AssistantTurn.id == turn_id, AssistantTurn.status == "running").values(status=status, completed_at=time.time()).returning(AssistantTurn.id))
        if result.scalar_one_or_none():
            audit(db, user_id, f"assistant_turn_{status}", f"turn:{turn_id}", "success" if status == "completed" else status, patient_id)
        await db.commit()


class AssistantTurnLimit(Exception):
    def __init__(self, unavailable: bool):
        self.unavailable = unavailable


async def check_assistant_turn_limit(socket: WebSocket, user_id: str, conversation_id: str, patient_id: str) -> None:
    address = socket.client.host if socket.client else "unknown"
    try:
        allowed = await auth_rate_limit.check("assistant-turn", address, user_id, 20, 200)
        unavailable = False
    except auth_rate_limit.RateLimitUnavailable:
        allowed = False
        unavailable = True
    if not allowed:
        async with SessionFactory() as db:
            audit(db, user_id, "assistant_turn_rate_limit", f"conversation:{conversation_id}", "failed" if unavailable else "denied", patient_id)
            await db.commit()
        raise AssistantTurnLimit(unavailable)


async def run_assistant_turn(socket: WebSocket, user_id: str, session_id: str, conversation_id: str, patient_id: str, turn: TurnInput) -> None:
    turn_id = None
    selected_consultation = str(turn.consultation_id) if turn.consultation_id else None
    scopes = tuple(scope for scope, enabled in (("read_documents", turn.include_documents), ("clinical_notes", turn.include_notes), ("consultations", turn.include_consultations), ("read_profile", turn.include_profile)) if enabled)
    try:
        if not turn.question.strip():
            raise HTTPException(422, "Question cannot be blank")
        await socket_access(user_id, session_id, conversation_id, scopes)
        await check_assistant_turn_limit(socket, user_id, conversation_id, patient_id)
        async with SessionFactory() as db:
            user = await active_identity(db, user_id, session_id)
            conversation = await db.get(Conversation, conversation_id)
            await conversation_access(db, user, conversation, "assistant_turn_start", tuple(set(scopes + await conversation_history_scopes(db, conversation_id))))
            if selected_consultation:
                selected = await db.get(Consultation, selected_consultation)
                if not selected or selected.patient_id != patient_id:
                    raise HTTPException(404, "Consultation not found")
            await db.execute(update(AssistantTurn).where(AssistantTurn.conversation_id == conversation_id, AssistantTurn.status == "running", AssistantTurn.created_at < time.time() - 300).values(status="failed", completed_at=time.time()))
            record = AssistantTurn(conversation_id=conversation_id, include_documents=turn.include_documents, include_notes=turn.include_notes, include_consultations=turn.include_consultations, include_profile=turn.include_profile)
            db.add(record)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                raise HTTPException(409, "A turn is already running")
            turn_id = record.id
            previous = (await db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id).order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(6))).all()
            history = [{"role": m.role, "content": m.content[:2000]} for m in reversed(previous)]
            db.add(ConversationMessage(conversation_id=conversation_id, turn_id=turn_id, role="user", content=turn.question, citations=[]))
            await db.commit()
        await socket.send_json({"type": "status", "message": "Checking requested evidence"})
        routed = assistant_provider.route_question(turn.question)
        sources = []
        source_documents: set[str] = set()
        document_snapshots: list[dict] = []
        knowledge_snapshots: list[dict] = []
        note_snapshots: list[dict] = []
        consultation_snapshots: list[dict] = []
        profile_snapshot: dict | None = None
        knowledge_unavailable = False
        if not routed:
            async with SessionFactory() as db:
                user = await active_identity(db, user_id, session_id)
                conversation = await db.get(Conversation, conversation_id)
                await conversation_access(db, user, conversation, "assistant_context", tuple(set(scopes + await conversation_history_scopes(db, conversation_id))))
                if turn.include_profile:
                    profile = await db.get(PatientProfile, patient_id)
                    values = {field: (getattr(profile, field) or "")[:2000] if profile else "" for field in ("allergies", "medications", "medical_history")}
                    if any(values.values()):
                        profile_snapshot = values
                        truncated = any(len(getattr(profile, field) or "") > 2000 for field in values)
                        sources.append({"id": f"patient_profile:{patient_id}", "kind": "self_reported_patient_profile", "title": "Patient-reported profile", "truncated": truncated, "text": json.dumps(values, ensure_ascii=False)})
                if turn.include_documents:
                    documents = (await db.scalars(select(Document).where(Document.patient_id == patient_id, Document.deleted_at.is_(None)).order_by(Document.created_at.desc()).limit(3))).all()
                    for document in documents:
                        job = await db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == document.id).order_by(ExtractionJob.created_at.desc(), ExtractionJob.id.desc()).limit(1))
                        if job and job.status == "completed":
                            pages = (await db.scalars(select(ExtractedPage).where(ExtractedPage.job_id == job.id).order_by(ExtractedPage.page_number).limit(2))).all()
                            for page in pages:
                                source_documents.add(document.id)
                                location = "text_segment" if page.method == "docx" else "page"
                                document_snapshots.append({"document_id": document.id, "job_id": job.id, "page_number": page.page_number, "method": page.method, "layer": "primary", "text": page.text[:2000]})
                                sources.append({"id": f"document:{document.id}:{location}:{page.page_number}", "kind": "unverified_patient_document", "title": document.filename, "job_id": job.id, location: page.page_number, "text": page.text[:2000]})
                                if page.supplemental_ocr_text and page.supplemental_ocr_text.strip():
                                    document_snapshots.append({"document_id": document.id, "job_id": job.id, "page_number": page.page_number, "method": page.method, "layer": "supplemental_ocr", "text": page.supplemental_ocr_text[:2000]})
                                    sources.append({"id": f"document:{document.id}:page:{page.page_number}:supplemental_ocr", "kind": "unverified_patient_document", "title": document.filename, "job_id": job.id, "page": page.page_number, "layer": "supplemental_ocr", "text": page.supplemental_ocr_text[:2000]})
                if turn.include_notes:
                    query = select(ClinicalNote, ClinicalNoteVersion).join(ClinicalNoteVersion).where(ClinicalNote.patient_id == patient_id, ClinicalNoteVersion.status == "approved")
                    if selected_consultation:
                        query = query.join(NoteConsultation, NoteConsultation.note_id == ClinicalNote.id).where(NoteConsultation.consultation_id == selected_consultation)
                    rows = (await db.execute(query.order_by(ClinicalNoteVersion.created_at.desc(), ClinicalNoteVersion.id.desc()).limit(3))).all()
                    for note, version in rows:
                        snapshot = {"note_id": note.id, "version_id": version.id, "text": version.content[:2000]}
                        source = {"id": f"note:{note.id}:v{version.version}", "kind": "doctor_approved_note", "title": "Approved clinical note", "text": version.content[:2000]}
                        if selected_consultation:
                            snapshot["consultation_id"] = selected_consultation
                            source["consultation_id"] = selected_consultation
                        note_snapshots.append(snapshot)
                        sources.append(source)
                if turn.include_consultations:
                    query = select(Consultation).where(Consultation.patient_id == patient_id)
                    if selected_consultation:
                        query = query.where(Consultation.id == selected_consultation)
                    consultations = (await db.scalars(query.order_by(Consultation.occurred_at.desc(), Consultation.id.desc()).limit(5))).all()
                    if selected_consultation and not consultations:
                        raise HTTPException(404, "Consultation not found")
                    for item in consultations:
                        snapshot = {"id": item.id, "title": item.title, "occurred_at": item.occurred_at, "doctor_id": item.doctor_id}
                        consultation_snapshots.append(snapshot)
                        sources.append({"id": f"consultation:{item.id}", "kind": "consultation_metadata", "title": "Recorded consultation", "text": json.dumps({"title": item.title, "occurred_at_unix_seconds_utc": item.occurred_at, "doctor_id": item.doctor_id})})
                if turn.include_knowledge:
                    try:
                        result = await search_knowledge(KnowledgeSearch(query=turn.question[:1000], limit=3), user, db)
                        for hit in result["results"]:
                            ref = hit["source"]
                            knowledge_snapshots.append({"source_id": ref["id"], "ordinal": hit["chunk"], "text": hit["excerpt"][:1500], "title": ref["title"], "provenance": ref["provenance"], "version": ref["version"], "embedding_model": ref["embedding_model"], "embedding_digest": ref.get("embedding_digest")})
                            sources.append({"id": f"knowledge:{ref['id']}:{hit['chunk']}", "kind": "approved_reference", "title": ref["title"], "provenance": ref["provenance"], "version": ref["version"], "text": hit["excerpt"][:1500]})
                    except HTTPException as exc:
                        if exc.status_code != 503:
                            raise
                        knowledge_unavailable = True
                await db.commit()
            await socket_access(user_id, session_id, conversation_id, scopes, source_documents, knowledge_snapshots, note_snapshots, document_snapshots, consultation_snapshots, profile_snapshot)
            if knowledge_unavailable:
                await socket.send_json({"type": "status", "message": "Approved reference search is unavailable; only other selected evidence can be used"})
            elif turn.include_knowledge and not knowledge_snapshots:
                await socket.send_json({"type": "status", "message": "No approved references were retrieved for this question"})
            await socket.send_json({"type": "status", "message": "Generating and checking an unreviewed draft"})
            async with SessionFactory() as db:
                user = await active_identity(db, user_id, session_id)
                audience = "doctor" if user.role == "doctor" else "patient"
                preference = await db.get(AssistantPreference, user_id)
                style = preference.response_style if preference else None
            draft = await assistant_provider.draft_answer(turn.question, history, sources, audience, style)
            answer, citations = draft.answer, [source for source in sources if source["id"] in draft.citations]
            if knowledge_unavailable:
                answer = "Approved reference search was unavailable for this response. Please verify any medical information with a clinician.\n\n" + answer
            elif turn.include_knowledge and not knowledge_snapshots:
                answer = "No approved medical references were retrieved for this response. Please verify any medical information with a clinician.\n\n" + answer
            elif turn.include_knowledge and not any(source["kind"] == "approved_reference" for source in citations):
                answer = "Approved references were retrieved but not cited in this response. Please verify any medical information with a clinician.\n\n" + answer
        else:
            answer, citations = routed, []
        await socket_access(user_id, session_id, conversation_id, scopes, source_documents, knowledge_snapshots, note_snapshots, document_snapshots, consultation_snapshots, profile_snapshot)
        # Save output only after another authorization check; original source text is not stored in citations.
        safe_citations = [{k: v for k, v in source.items() if k != "text"} for source in citations]
        async with SessionFactory() as db:
            user = await active_identity(db, user_id, session_id)
            conversation = await db.get(Conversation, conversation_id)
            await conversation_access(db, user, conversation, "assistant_output", tuple(set(scopes + await conversation_history_scopes(db, conversation_id))))
            await ensure_documents_available(db, patient_id, source_documents, lock=True)
            await ensure_document_sources_current(db, document_snapshots)
            await ensure_note_sources_current(db, patient_id, note_snapshots, lock=True)
            await ensure_consultation_sources_current(db, patient_id, consultation_snapshots, lock=True)
            await ensure_profile_source_current(db, patient_id, profile_snapshot, lock=True)
            await ensure_knowledge_sources_current(db, knowledge_snapshots, lock=True)
            active = await db.get(AssistantTurn, turn_id)
            if not active or active.status != "running":
                raise HTTPException(409, "Turn no longer active")
            db.add(ConversationMessage(conversation_id=conversation_id, turn_id=turn_id, role="assistant", content=answer, citations=safe_citations))
            active.status = "completed"
            active.completed_at = time.time()
            audit(db, user_id, "assistant_turn_completed", f"turn:{turn_id}", "success", patient_id)
            await db.commit()
        for index in range(0, len(answer), 256):
            await socket_access(user_id, session_id, conversation_id, scopes, source_documents, knowledge_snapshots, note_snapshots, document_snapshots, consultation_snapshots, profile_snapshot)
            await socket.send_json({"type": "chunk", "text": answer[index:index + 256]})
        await socket_access(user_id, session_id, conversation_id, scopes, source_documents, knowledge_snapshots, note_snapshots, document_snapshots, consultation_snapshots, profile_snapshot)
        await socket.send_json({"type": "complete", "citations": safe_citations, "review_status": "unreviewed"})
    except asyncio.CancelledError:
        if turn_id:
            await finish_turn(turn_id, "cancelled", user_id, patient_id)
        raise
    except AssistantTurnLimit as exc:
        event = {"type": "error", "message": "Assistant is temporarily unavailable. If there is immediate danger or severe symptoms, contact local emergency services or seek emergency care now."}
        if not exc.unavailable:
            event["retry_after"] = 60
        await socket.send_json(event)
    except (HTTPException, assistant_provider.AssistantUnavailable):
        if turn_id:
            await finish_turn(turn_id, "failed", user_id, patient_id)
        await socket.send_json({"type": "error", "message": "The turn could not be completed. Check access, selected evidence, and provider configuration."})
    except Exception:
        if turn_id:
            await finish_turn(turn_id, "failed", user_id, patient_id)
        await socket.send_json({"type": "error", "message": "The turn could not be completed."})


@app.websocket("/ws/conversations/{conversation_id}")
async def conversation_socket(socket: WebSocket, conversation_id: str):
    allowed_origins = {origin.strip() for origin in os.getenv("WS_ALLOWED_ORIGINS", "http://localhost:3100,http://127.0.0.1:3100").split(",") if origin.strip()}
    if socket.headers.get("origin") not in allowed_origins:
        await socket.close(code=1008)
        return
    await socket.accept()
    task = None
    try:
        first = await asyncio.wait_for(socket.receive_text(), timeout=10)
        if len(first) > 1000:
            await socket.close(code=1008)
            return
        auth_data = json.loads(first)
        token = auth_data.get("ticket") if isinstance(auth_data, dict) else None
        if not isinstance(token, str) or len(token) > 200:
            await socket.close(code=1008)
            return
        async with SessionFactory() as db:
            result = await db.execute(update(SocketTicket).where(SocketTicket.token_hash == token_hash(token), SocketTicket.conversation_id == conversation_id, SocketTicket.used_at.is_(None), SocketTicket.expires_at > time.time()).values(used_at=time.time()).returning(SocketTicket.user_id, SocketTicket.session_id))
            identity = result.one_or_none()
            await db.commit()
        if not identity:
            await socket.close(code=1008)
            return
        user_id, session_id = identity.user_id, identity.session_id
        await socket_access(user_id, session_id, conversation_id)
        async with SessionFactory() as db:
            conversation = await db.get(Conversation, conversation_id)
            patient_id = conversation.patient_id
        await socket.send_json({"type": "ready"})
        deadline = time.monotonic() + 900
        while True:
            text = await asyncio.wait_for(socket.receive_text(), timeout=max(0.01, deadline - time.monotonic()))
            if len(text) > 16_000:
                await socket.close(code=1009)
                break
            event = json.loads(text)
            if isinstance(event, dict) and event.get("type") == "cancel":
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    await socket.send_json({"type": "cancelled"})
                continue
            try:
                turn = TurnInput.model_validate(event)
            except ValidationError:
                await socket.send_json({"type": "error", "message": "Invalid turn"})
                continue
            if task and not task.done():
                await socket.send_json({"type": "error", "message": "A turn is already running"})
                continue
            task = asyncio.create_task(run_assistant_turn(socket, user_id, session_id, conversation_id, patient_id, turn))
    except (WebSocketDisconnect, asyncio.TimeoutError, HTTPException, ValueError, TypeError):
        pass
    finally:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                pass
        try:
            await socket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
