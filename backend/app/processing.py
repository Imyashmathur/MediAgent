import asyncio
import hashlib
import os
import stat
import tempfile
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from celery import Celery
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update

from .db import Session, engine
from .models import AuditEvent, Document, ExtractedPage, ExtractionJob, KnowledgeImport, StructuredExtractionVersion, User
from .notifications import queue_notification
from .extraction_schema import LabTest, PARSER_VERSION, bounded_warnings, extract_labeled_fields, validate_evidence
from .document_classification import classify_document
from .parser_subprocess import run_document_parser
from . import document_storage, ocr_provider, vision_provider

ocr_provider.config()

broker = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
celery_app = Celery("mediagent", broker=broker)
celery_app.conf.update(task_serializer="json", accept_content=["json"], task_ignore_result=True,
                       broker_connection_retry_on_startup=True, broker_connection_timeout=2,
                       task_publish_retry=False, worker_prefetch_multiplier=1)


class PageResult(BaseModel):
    page_number: int = Field(ge=1, le=30)
    method: Literal["native", "ocr", "docx"]
    text: str = Field(max_length=30_000)
    supplemental_ocr_text: str | None = Field(default=None, max_length=30_000)
    vision_text: str | None = Field(default=None, max_length=30_000)
    vision_warnings: list[Annotated[str, Field(max_length=500)]] | None = Field(default=None, max_length=10)
    native_table_labs: list[LabTest] = Field(default_factory=list, max_length=101)

    @model_validator(mode="after")
    def validate_layer(self):
        if self.native_table_labs and self.method != "native":
            raise ValueError("Native PDF table rows require native text")
        if self.supplemental_ocr_text is not None and self.method != "native":
            raise ValueError("Supplemental OCR requires native PDF text")
        if (self.vision_text is None) != (self.vision_warnings is None) or (self.method == "docx" and self.vision_text is not None):
            raise ValueError("Invalid vision layer")
        return self


class OCRDetails(BaseModel):
    provider: Literal["tesseract"]
    version: str = Field(min_length=1, max_length=40, pattern=r"^[0-9][A-Za-z0-9.+~_-]*$")
    languages: list[str] = Field(min_length=1, max_length=4)


class VisionDetails(BaseModel):
    provider: Literal["ollama"]
    model: str = Field(min_length=1, max_length=200)
    pages_processed: int = Field(ge=1, le=3)
    truncated: bool


class ProcessorDetails(BaseModel):
    pipeline: Literal["native-ocr-v2", "docx-text-v1", "docx-text-v2", "docx-text-v3", "docx-text-v4"]
    pdf_ocr_mode: Literal["fallback", "all"]
    ocr: OCRDetails | None = None
    vision: VisionDetails | None = None


class ParserResult(BaseModel):
    pages: list[PageResult] = Field(min_length=1, max_length=30)
    processor_details: ProcessorDetails | None = None


def snapshot_file(source: Path, target: Path, expected_hash: str | None, expected_size: int | None = None) -> str:
    """Copy and validate a bounded regular file before any parser sees it."""
    max_bytes = int(os.getenv("MAX_UPLOAD_BYTES", "10485760"))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(source, flags)
    with os.fdopen(fd, "rb") as original:
        info = os.fstat(original.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size < 1 or info.st_size > max_bytes:
            raise ValueError("source_file_limit")
        if expected_size is not None and info.st_size != expected_size:
            raise ValueError("source_size_mismatch")
        digest = hashlib.sha256()
        count = 0
        with target.open("xb") as copy:
            os.chmod(target, 0o600)
            while chunk := original.read(65536):
                count += len(chunk)
                if count > max_bytes or (expected_size is not None and count > expected_size):
                    raise ValueError("source_file_limit")
                digest.update(chunk)
                copy.write(chunk)
        if count != info.st_size or (expected_hash is not None and digest.hexdigest() != expected_hash):
            raise ValueError("source_mismatch")
        return digest.hexdigest()


def verify_source(source: Path, expected_hash: str, expected_size: int | None = None) -> None:
    """Check the original still matches the snapshot after the parser finishes."""
    max_bytes = int(os.getenv("MAX_UPLOAD_BYTES", "10485760"))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(source, flags)
    with os.fdopen(fd, "rb") as original:
        info = os.fstat(original.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size < 1 or info.st_size > max_bytes:
            raise ValueError("source_file_limit")
        if expected_size is not None and info.st_size != expected_size:
            raise ValueError("source_size_mismatch")
        digest, count = hashlib.sha256(), 0
        while chunk := original.read(65536):
            count += len(chunk)
            if count > max_bytes:
                raise ValueError("source_file_limit")
            digest.update(chunk)
        if count != info.st_size or digest.hexdigest() != expected_hash:
            raise ValueError("source_mismatch")


async def lock_processing_owner(db, user_id: str) -> User | None:
    """Serialize final authorization with account deactivation until commit."""
    return await db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))


async def process_job(job_id: str) -> None:
    token = str(uuid.uuid4())
    async with Session() as db:
        claimed = await db.execute(update(ExtractionJob).where(ExtractionJob.id == job_id, ExtractionJob.status == "queued").values(status="running", started_at=time.time(), claim_token=token, attempt_count=ExtractionJob.attempt_count + 1).returning(ExtractionJob.id))
        if claimed.scalar_one_or_none() is None:
            await db.rollback()
            return
        await db.commit()
        job = await db.get(ExtractionJob, job_id)
        correlation = job.request_correlation_id or str(uuid.uuid4())
        document = await db.get(Document, job.document_id)
        user = await db.get(User, job.requested_by)
        if not document or document.deleted_at is not None or not user or not user.active or user.role != "patient" or document.patient_id != user.id:
            failed = await db.execute(update(ExtractionJob).where(ExtractionJob.id == job_id, ExtractionJob.status == "running", ExtractionJob.claim_token == token).values(status="failed", error_code="authorization_revoked", completed_at=time.time()).returning(ExtractionJob.id))
            if failed.scalar_one_or_none():
                db.add(AuditEvent(actor_id=job.requested_by, patient_id=document.patient_id if document else None, action="document_processing_failed", resource=f"document:{job.document_id}", outcome="denied", correlation_id=correlation))
            await db.commit()
            return
        document_id, patient_id, media_type, user_id = document.id, document.patient_id, document.content_type, user.id
        source_size, source_digest = document.size, document.sha256
        try:
            upload_dir = Path(os.getenv("UPLOAD_DIR", "/tmp/mediagent-uploads"))
            location = await document_storage.resolve(db, document, upload_dir)
            path = upload_dir / document_id
            with tempfile.TemporaryDirectory(prefix="mediagent-document-") as directory:
                snapshot = Path(directory) / "source"
                if location.path is not None:
                    snapshot_hash = await asyncio.to_thread(snapshot_file, path, snapshot, source_digest, source_size)
                else:
                    content = await asyncio.to_thread(document_storage.read, location, source_size, source_digest, int(os.getenv("MAX_UPLOAD_BYTES", "10485760")))
                    snapshot_hash = hashlib.sha256(content).hexdigest()
                    def write_private_snapshot():
                        fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, 'wb') as target:
                            target.write(content)
                    await asyncio.to_thread(write_private_snapshot)
                parser_args = [sys.executable, "-m", "app.document_parser", str(snapshot), media_type]
                if job.vision_requested:
                    settings = vision_provider.config()
                    if settings.provider != "ollama" or vision_provider.config_fingerprint(settings) != job.vision_config_fingerprint:
                        raise ValueError("vision_configuration_changed")
                    parser_args.append("vision")
                result = await asyncio.to_thread(run_document_parser, parser_args)
                if location.path is not None:
                    await asyncio.to_thread(verify_source, path, snapshot_hash, source_size)
                else:
                    await asyncio.to_thread(document_storage.read, location, source_size, snapshot_hash, int(os.getenv("MAX_UPLOAD_BYTES", "10485760")))
            current_document = await db.scalar(select(Document).where(Document.id == document_id).execution_options(populate_existing=True))
            if (not current_document or current_document.deleted_at is not None
                or (current_document.patient_id, current_document.content_type, current_document.size, current_document.sha256) != (patient_id, media_type, source_size, source_digest)
                or await document_storage.resolve(db, current_document, upload_dir) != location):
                raise ValueError("document_source_changed")
            if len(result.stdout) > 4_000_000:
                raise ValueError("parser output exceeds limit")
            parsed = ParserResult.model_validate_json(result.stdout)
            if [p.page_number for p in parsed.pages] != list(range(1, len(parsed.pages) + 1)):
                raise ValueError("invalid page sequence")
            vision_details = parsed.processor_details.vision if parsed.processor_details else None
            if job.vision_requested:
                expected = min(len(parsed.pages), 3)
                if (not vision_details or vision_details.model != settings.model or vision_details.pages_processed != expected
                    or vision_details.truncated != (len(parsed.pages) > 3)
                    or any((p.vision_text is not None) != (p.page_number <= expected) for p in parsed.pages)):
                    raise ValueError("invalid_vision_output")
            elif vision_details or any(p.vision_text is not None for p in parsed.pages):
                raise ValueError("unexpected_vision_output")
            structured = extract_labeled_fields([p.model_dump() for p in parsed.pages])
            native_table_labs = [lab for page in parsed.pages for lab in page.native_table_labs]
            if native_table_labs:
                available = 100 - len(structured.laboratory_tests)
                structured.laboratory_tests.extend(native_table_labs[:available])
                structured.warnings = bounded_warnings(["Native PDF table rows are unverified, may duplicate other transcriptions, and can be misaligned; check each value and range against the original."] + structured.warnings)
                if len(native_table_labs) > available:
                    structured.warnings = bounded_warnings(structured.warnings + ["Additional native table rows exceeded the transcription limit and were omitted."])
            if job.vision_requested:
                structured.warnings = bounded_warnings(["Vision model text is unverified and is not included in this structured transcription; compare it with the original."] + structured.warnings)
            if any(p.method == "docx" for p in parsed.pages):
                structured.warnings = bounded_warnings(["DOCX text segments are not page numbers; simple table rows are rendered with inserted separators, and headers, footers, images, and some complex content may be omitted."] + structured.warnings)
            validate_evidence(structured, {p.page_number: p.text for p in parsed.pages}, {p.page_number: "text_segment" if p.method == "docx" else "page" for p in parsed.pages}, {p.page_number: p.supplemental_ocr_text for p in parsed.pages if p.supplemental_ocr_text is not None})
            details = parsed.processor_details.model_dump() if parsed.processor_details else None
            if details is not None:
                details["document_type"] = classify_document([p.model_dump() for p in parsed.pages])
                details["structured_parser"] = PARSER_VERSION
            # Recheck the owner's account immediately before writing derived data.
            user = await lock_processing_owner(db, user_id)
            if not user or not user.active or user.role != "patient":
                raise PermissionError("owner inactive")
            updated = await db.execute(update(ExtractionJob).where(ExtractionJob.id == job_id, ExtractionJob.status == "running", ExtractionJob.claim_token == token).values(status="completed", completed_at=time.time(), processor_version=parsed.processor_details.pipeline if parsed.processor_details else job.processor_version, processor_details=details).returning(ExtractionJob.id))
            if updated.scalar_one_or_none() is None:
                await db.rollback()
                return
            db.add_all([ExtractedPage(job_id=job_id, page_number=p.page_number, method=p.method, text=p.text, supplemental_ocr_text=p.supplemental_ocr_text, vision_text=p.vision_text, vision_warnings=p.vision_warnings) for p in parsed.pages])
            db.add(StructuredExtractionVersion(job_id=job_id, version=1, data=structured.model_dump(), review_status="unverified"))
            queue_notification(db, recipient_id=user.id, patient_id=patient_id, kind="processing_completed", event_key=f"processing:{job_id}", resource_id=document_id)
            db.add(AuditEvent(actor_id=user.id, patient_id=patient_id, action="document_processing_complete", resource=f"document:{document_id}", outcome="success", correlation_id=correlation))
            await db.commit()
        except Exception:
            await db.rollback()
            failed = await db.execute(update(ExtractionJob).where(ExtractionJob.id == job_id, ExtractionJob.status == "running", ExtractionJob.claim_token == token).values(status="failed", error_code="processing_failed", completed_at=time.time()).returning(ExtractionJob.id))
            if failed.scalar_one_or_none():
                queue_notification(db, recipient_id=user_id, patient_id=patient_id, kind="processing_failed", event_key=f"processing:{job_id}", resource_id=document_id)
                db.add(AuditEvent(actor_id=user_id, patient_id=patient_id, action="document_processing_failed", resource=f"document:{document_id}", outcome="failed", correlation_id=correlation))
            await db.commit()


@celery_app.task(name="mediagent.process_document", acks_late=True, reject_on_worker_lost=True)
def process_document(job_id: str) -> None:
    async def run():
        try:
            await process_job(job_id)
        finally:
            await engine.dispose()
    asyncio.run(run())


async def process_knowledge_import(import_id: str) -> None:
    token = str(uuid.uuid4())
    async with Session() as db:
        claimed = await db.execute(update(KnowledgeImport).where(KnowledgeImport.id == import_id, KnowledgeImport.status == "queued", KnowledgeImport.deleted_at.is_(None)).values(status="running", started_at=time.time(), claim_token=token, attempt_count=KnowledgeImport.attempt_count + 1).returning(KnowledgeImport.id))
        if claimed.scalar_one_or_none() is None:
            await db.rollback()
            return
        await db.commit()
        job = await db.get(KnowledgeImport, import_id)
        correlation = job.request_correlation_id or str(uuid.uuid4())
        creator_id = job.created_by
        user = await db.get(User, creator_id)
        try:
            if not user or not user.active or user.role != "admin":
                raise PermissionError("creator unavailable")
            path = Path(os.getenv("KNOWLEDGE_IMPORT_DIR", "/tmp/mediagent-knowledge-imports")) / import_id
            with tempfile.TemporaryDirectory(prefix="mediagent-reference-") as directory:
                snapshot = Path(directory) / "source"
                snapshot_hash = await asyncio.to_thread(snapshot_file, path, snapshot, job.sha256)
                result = await asyncio.to_thread(run_document_parser, [sys.executable, "-m", "app.document_parser", str(snapshot), job.media_type])
                await asyncio.to_thread(verify_source, path, snapshot_hash)
            if len(result.stdout) > 4_000_000:
                raise ValueError("parser output limit")
            parsed = ParserResult.model_validate_json(result.stdout)
            if [p.page_number for p in parsed.pages] != list(range(1, len(parsed.pages)+1)):
                raise ValueError("invalid segments")
            sections = []
            for page in parsed.pages:
                if page.text.strip():
                    sections.append(page.text.strip())
                if page.supplemental_ocr_text and page.supplemental_ocr_text.strip():
                    sections.append(f"Supplemental OCR for page {page.page_number} (may duplicate native text):\n{page.supplemental_ocr_text.strip()}")
            text = "\n\n".join(sections)
            if not text or len(text) > 50_000:
                raise ValueError("source text limit")
            await db.refresh(user)
            if not user.active or user.role != "admin":
                raise PermissionError("creator unavailable")
            updated = await db.execute(update(KnowledgeImport).where(KnowledgeImport.id == import_id, KnowledgeImport.status == "running", KnowledgeImport.claim_token == token, KnowledgeImport.deleted_at.is_(None)).values(status="completed", extracted_text=text, processor_details=parsed.processor_details.model_dump() if parsed.processor_details else None, completed_at=time.time()).returning(KnowledgeImport.id))
            if updated.scalar_one_or_none() is None:
                await db.rollback()
                return
            db.add(AuditEvent(actor_id=user.id, action="knowledge_import_completed", resource=f"knowledge_import:{import_id}", outcome="success", correlation_id=correlation))
            await db.commit()
        except Exception:
            await db.rollback()
            failed = await db.execute(update(KnowledgeImport).where(KnowledgeImport.id == import_id, KnowledgeImport.status == "running", KnowledgeImport.claim_token == token).values(status="failed", error_code="import_failed", completed_at=time.time()).returning(KnowledgeImport.id))
            if failed.scalar_one_or_none():
                db.add(AuditEvent(actor_id=creator_id, action="knowledge_import_failed", resource=f"knowledge_import:{import_id}", outcome="failed", correlation_id=correlation))
            await db.commit()


@celery_app.task(name="mediagent.process_knowledge_import", acks_late=True, reject_on_worker_lost=True)
def import_knowledge_document(import_id: str) -> None:
    async def run():
        try:
            await process_knowledge_import(import_id)
        finally:
            await engine.dispose()
    asyncio.run(run())


@celery_app.task(name="mediagent.deliver_password_reset", acks_late=True, reject_on_worker_lost=True)
def deliver_password_reset(reset_id: str) -> None:
    from .password_reset import deliver_reset
    async def run():
        try:
            await deliver_reset(reset_id)
        finally:
            await engine.dispose()
    # Do not log SMTP exception details: they can include recipient data.
    try:
        asyncio.run(run())
    except Exception:
        return


@celery_app.task(name="mediagent.index_knowledge", acks_late=True, reject_on_worker_lost=True)
def index_knowledge_job(job_id: str) -> None:
    from .knowledge_indexing import process_index
    async def run():
        try:
            await process_index(job_id)
        finally:
            await engine.dispose()
    asyncio.run(run())
