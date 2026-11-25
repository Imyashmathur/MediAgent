"""Document/object association for authorized uploads and recovery.

The caller must validate file signature and malware-scan a bounded snapshot before
preparing the submission. Reads and purges use the recorded storage identity.
"""
import os
import time
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import AuditEvent, Document, DocumentObjectLink, ObjectDocumentSubmission, ObjectUploadDiscard, ObjectUploadIntent, Session, User
from .object_upload_intents import validate_store_id
from .s3_storage import ObjectRef

MEDIA_EXTENSIONS = {
    'application/pdf': {'.pdf'},
    'image/jpeg': {'.jpg', '.jpeg'},
    'image/png': {'.png'},
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': {'.docx'},
}


def validate_metadata(filename: str, content_type: str, scan_status: str) -> None:
    if (not isinstance(filename, str) or not filename or len(filename) > 160 or Path(filename).name != filename
        or '\\' in filename or any(ord(c) < 32 for c in filename)
        or content_type not in MEDIA_EXTENSIONS or Path(filename).suffix.lower() not in MEDIA_EXTENSIONS[content_type]
        or scan_status not in ('not_scanned', 'no_threat_detected')):
        raise ValueError('Invalid document submission metadata')


async def locked_patient_session(db, actor_id: str, session_id: str) -> None:
    actor = await db.scalar(select(User).where(User.id == actor_id).with_for_update().execution_options(populate_existing=True))
    session = await db.scalar(select(Session).where(Session.id == session_id).with_for_update().execution_options(populate_existing=True))
    if not actor or not actor.active or actor.role != 'patient' or not session or session.user_id != actor_id or session.revoked_at is not None or session.expires_at <= time.time():
        raise PermissionError('Patient session is no longer authorized')


async def prepare_document(factory: async_sessionmaker, *, actor_id: str, session_id: str, store_id: str, bucket: str, object_id: str, size: int, sha256: str, filename: str, content_type: str, scan_status: str, correlation_id: str) -> str:
    """Commit the write identity and validated metadata before object storage."""
    validate_store_id(store_id)
    ObjectRef(bucket, f'documents/{object_id}', 'validation', size, sha256)
    if size > int(os.getenv('MAX_UPLOAD_BYTES', '10485760')):
        raise ValueError('Document exceeds upload limit')
    validate_metadata(filename, content_type, scan_status)
    try:
        if str(uuid.UUID(correlation_id)) != correlation_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid correlation ID') from None
    async with factory() as db:
        await locked_patient_session(db, actor_id, session_id)
        intent = ObjectUploadIntent(id=str(uuid.uuid4()), actor_id=actor_id, store_id=store_id, bucket=bucket, namespace='documents', object_id=object_id, size=size, sha256=sha256, status='pending')
        db.add(intent)
        await db.flush()
        db.add(ObjectDocumentSubmission(intent_id=intent.id, session_id=session_id, filename=filename, content_type=content_type, scan_status=scan_status, correlation_id=correlation_id))
        db.add(AuditEvent(actor_id=actor_id, patient_id=actor_id, action='object_document_upload_prepared', resource=f'document:{object_id}', outcome='success', correlation_id=correlation_id))
        await db.commit()
        return intent.id


async def complete_document(factory: async_sessionmaker, *, actor_id: str, intent_id: str, store_id: str) -> str:
    """Atomically create a document and bind its stored object; safe to retry."""
    validate_store_id(store_id)
    async with factory() as db:
        submission = await db.get(ObjectDocumentSubmission, intent_id)
        if not submission:
            raise ValueError('Document submission not found')
        authorized_session_id = submission.session_id
        await locked_patient_session(db, actor_id, authorized_session_id)
        intent = await db.scalar(select(ObjectUploadIntent).where(ObjectUploadIntent.id == intent_id).with_for_update().execution_options(populate_existing=True))
        submission = await db.scalar(select(ObjectDocumentSubmission).where(ObjectDocumentSubmission.intent_id == intent_id).with_for_update().execution_options(populate_existing=True))
        if submission and submission.session_id != authorized_session_id:
            raise PermissionError('Upload session changed during authorization; retry')
        if await db.get(ObjectUploadDiscard, intent_id):
            raise ValueError('Upload was discarded')
        if not intent or not submission or intent.actor_id != actor_id or intent.store_id != store_id or intent.namespace != 'documents' or intent.status != 'stored':
            raise ValueError('Stored object does not match document submission')
        ObjectRef(intent.bucket, f'documents/{intent.object_id}', intent.version_id, intent.size, intent.sha256)
        validate_metadata(submission.filename, submission.content_type, submission.scan_status)
        document = await db.get(Document, intent.object_id)
        if document:
            link = await db.get(DocumentObjectLink, document.id)
            if (not link or link.intent_id != intent.id or document.patient_id != actor_id or document.deleted_at is not None
                or (document.filename, document.content_type, document.size, document.sha256, document.scan_status)
                != (submission.filename, submission.content_type, intent.size, intent.sha256, submission.scan_status)):
                raise ValueError('Document identity is already in use')
            return document.id
        document = Document(id=intent.object_id, patient_id=actor_id, filename=submission.filename, content_type=submission.content_type, size=intent.size, sha256=intent.sha256, scan_status=submission.scan_status)
        db.add(document)
        await db.flush()
        db.add(DocumentObjectLink(document_id=document.id, intent_id=intent.id))
        db.add(AuditEvent(actor_id=actor_id, patient_id=actor_id, action='object_document_upload_completed', resource=f'document:{document.id}', outcome='success', correlation_id=submission.correlation_id))
        await db.commit()
        return document.id


async def rebind_session(factory: async_sessionmaker, *, actor_id: str, session_id: str, intent_id: str, store_id: str, correlation_id: str) -> None:
    """Explicitly resume an incomplete submission under a current patient session."""
    validate_store_id(store_id)
    async with factory() as db:
        await locked_patient_session(db, actor_id, session_id)
        intent = await db.scalar(select(ObjectUploadIntent).where(ObjectUploadIntent.id == intent_id).with_for_update())
        submission = await db.scalar(select(ObjectDocumentSubmission).where(ObjectDocumentSubmission.intent_id == intent_id).with_for_update())
        if not intent or not submission or intent.actor_id != actor_id or intent.store_id != store_id or intent.namespace != 'documents':
            raise ValueError('Document submission not found')
        if await db.get(ObjectUploadDiscard, intent_id):
            raise ValueError('Upload was discarded')
        link = await db.scalar(select(DocumentObjectLink).where(DocumentObjectLink.intent_id == intent_id))
        if link:
            raise ValueError('Document submission is already complete')
        submission.session_id = session_id
        db.add(AuditEvent(actor_id=actor_id, patient_id=actor_id, action='object_document_upload_resumed', resource=f'document:{intent.object_id}', outcome='success', correlation_id=correlation_id))
        await db.commit()


async def discard_document(factory: async_sessionmaker, *, actor_id: str, session_id: str, intent_id: str, correlation_id: str) -> None:
    """Fence association and request continued cleanup of an unlinked upload."""
    async with factory() as db:
        await locked_patient_session(db, actor_id, session_id)
        intent = await db.scalar(select(ObjectUploadIntent).where(ObjectUploadIntent.id == intent_id).with_for_update())
        submission = await db.scalar(select(ObjectDocumentSubmission).where(ObjectDocumentSubmission.intent_id == intent_id).with_for_update())
        if not intent or not submission or intent.actor_id != actor_id or intent.namespace != 'documents':
            raise LookupError('Document submission not found')
        if await db.scalar(select(DocumentObjectLink).where(DocumentObjectLink.intent_id == intent_id)):
            raise ValueError('Upload is already associated with a document')
        if not await db.get(ObjectUploadDiscard, intent_id):
            now = time.time()
            db.add(ObjectUploadDiscard(intent_id=intent_id, requested_at=now, next_check_at=now))
            submission.filename = 'Discarded upload'
            db.add(AuditEvent(actor_id=actor_id, patient_id=actor_id, action='object_document_upload_discarded', resource=f'document:{intent.object_id}', outcome='success', correlation_id=correlation_id))
            await db.commit()
