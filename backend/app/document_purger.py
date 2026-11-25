"""Idempotently purge patient-deleted local files or recorded object versions."""
import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .db import Session, engine
from .models import Document, DocumentObjectLink, KnowledgeImport, ObjectUploadDiscard, ObjectUploadIntent
from . import document_storage, object_staging

log = logging.getLogger(__name__)


def discard_check_delay(requested_at: float, checked_at: float, result: str) -> int:
    """Keep monitoring indefinitely while reducing checks for older discards."""
    age = max(0, checked_at - requested_at)
    interval = 300 if age < 3600 else 900 if age < 86400 else 3600 if age < 7 * 86400 else 21600
    return min(interval, 3600) if result == "error" else interval


def reconcile_discard(intent: ObjectUploadIntent) -> str:
    """Remove one uniquely verified version; absence remains under observation."""
    settings = document_storage.config()
    if settings.store_id != intent.store_id or settings.bucket != intent.bucket:
        raise document_storage.StorageUnavailable("Object storage identity changed")
    provider = document_storage.store(settings)
    ref = provider.discover(intent.namespace, intent.object_id, intent.size, intent.sha256)
    if ref is None:
        return "absent"
    if intent.status == "stored" and intent.version_id != ref.version_id:
        raise ValueError("Stored version does not match the discovered version")
    provider.purge(ref)
    return "removed"


async def reconcile_discard_one(factory=None, reconcile=None):
    async with (factory or Session)() as db:
        row = (await db.execute(select(ObjectUploadIntent, ObjectUploadDiscard)
            .join(ObjectUploadDiscard, ObjectUploadDiscard.intent_id == ObjectUploadIntent.id)
            .where(ObjectUploadDiscard.next_check_at <= time.time())
            .order_by(ObjectUploadDiscard.next_check_at, ObjectUploadDiscard.intent_id)
            .limit(1).with_for_update(skip_locked=True))).first()
        if not row:
            return False
        intent, discard = row
        discard.attempts += 1
        try:
            if intent.namespace != "documents" or await db.scalar(select(DocumentObjectLink.document_id).where(DocumentObjectLink.intent_id == intent.id)):
                raise ValueError("Discarded upload is linked or invalid")
            outcome = await asyncio.to_thread(reconcile or reconcile_discard, intent)
            if outcome not in ("absent", "removed"):
                raise ValueError("Invalid reconciliation result")
            discard.last_result = outcome
            discard.error_code = None
        except document_storage.StorageUnavailable:
            discard.last_result, discard.error_code = "error", "storage_configuration"
        except (ValueError, TypeError):
            discard.last_result, discard.error_code = "error", "integrity_conflict"
        except (BotoCoreError, ClientError, OSError):
            discard.last_result, discard.error_code = "error", "provider_unavailable"
        except Exception:
            discard.last_result = "error"
            discard.error_code = "unexpected_error"
        discard.checked_at = time.time()
        # A successful empty listing cannot prove a delayed PUT will never arrive.
        discard.next_check_at = discard.checked_at + discard_check_delay(discard.requested_at, discard.checked_at, discard.last_result)
        await db.commit()
        return True


async def purge_one(factory=None, remove=None):
    async with (factory or Session)() as db:
        doc = await db.scalar(select(Document).where(Document.purge_status == "pending", Document.purge_after <= time.time(), Document.deleted_at.is_not(None)).order_by(Document.purge_after, Document.id).limit(1).with_for_update(skip_locked=True))
        if doc is None:
            return False
        doc.purge_attempts += 1
        try:
            location = await document_storage.resolve_for_purge(db, doc)
            if location is not None:
                await asyncio.to_thread(document_storage.purge, location)
            else:
                try:
                    if str(uuid.UUID(doc.id)) != doc.id:
                        raise ValueError("noncanonical ID")
                except (TypeError, ValueError) as exc:
                    raise OSError("invalid document ID") from exc
                path = Path(os.getenv("UPLOAD_DIR", "/tmp/mediagent-uploads")) / doc.id
                if remove:
                    await asyncio.to_thread(remove, path)
                else:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
        except (OSError, ValueError, document_storage.StorageUnavailable):
            doc.purge_after = time.time() + min(300, 2 ** min(doc.purge_attempts, 8))
        else:
            doc.purge_status = "complete"
            doc.purged_at = time.time()
            doc.purge_after = None
        await db.commit()
        return True


async def purge_import_one(factory=None, remove=None):
    async with (factory or Session)() as db:
        job = await db.scalar(select(KnowledgeImport).where(KnowledgeImport.purge_status == "pending", KnowledgeImport.purge_after <= time.time(), KnowledgeImport.deleted_at.is_not(None)).order_by(KnowledgeImport.purge_after, KnowledgeImport.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return False
        job.purge_attempts += 1
        try:
            if str(uuid.UUID(job.id)) != job.id:
                raise ValueError("noncanonical ID")
            path = Path(os.getenv("KNOWLEDGE_IMPORT_DIR", "/tmp/mediagent-knowledge-imports")) / job.id
            if remove:
                await asyncio.to_thread(remove, path)
            else:
                await asyncio.to_thread(path.unlink, missing_ok=True)
        except (OSError, ValueError, TypeError):
            job.purge_after = time.time() + min(300, 2 ** min(job.purge_attempts, 8))
        else:
            job.purge_status = "complete"
            job.purged_at = time.time()
            job.purge_after = None
        await db.commit()
        return True


async def run():
    next_staging_scan = 0.0
    sweeper = object_staging.StagingSweeper(Path(os.getenv("UPLOAD_DIR", "/tmp/mediagent-uploads")))
    try:
        while True:
            try:
                if time.monotonic() >= next_staging_scan:
                    next_staging_scan = time.monotonic() + 60
                    try:
                        await asyncio.to_thread(sweeper.scan)
                    except OSError:
                        log.warning("Object staging cleanup unavailable; retrying")
                documents = await purge_one()
                imports = await purge_import_one()
                discards = await reconcile_discard_one()
                if not documents and not imports and not discards:
                    await asyncio.sleep(1)
            except SQLAlchemyError:
                log.warning("Document purge database unavailable; retrying")
                await asyncio.sleep(2)
    finally:
        await asyncio.to_thread(sweeper.close)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
