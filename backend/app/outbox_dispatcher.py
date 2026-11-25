"""Publish committed jobs at least once, retrying broker failures."""
import asyncio
import logging
import time

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import SQLAlchemyError

from .db import Session, engine
from .models import AuditEvent, Document, ExtractionJob, JobOutbox, KnowledgeImport, KnowledgeIndexJob
from .notifications import queue_notification
from .processing import import_knowledge_document, process_document, index_knowledge_job

log = logging.getLogger(__name__)
TASKS = {"document_processing": (ExtractionJob, process_document), "knowledge_import": (KnowledgeImport, import_knowledge_document)}
LEASE_SECONDS = 300
MAX_CLAIMS = 3


async def publish_task(kind, target_id):
    await asyncio.to_thread(TASKS[kind][1].apply_async, args=[target_id], retry=False)


async def dispatch_one(factory=None, publish=None):
    async with (factory or Session)() as db:
        entry = await db.scalar(select(JobOutbox).where(JobOutbox.status == "pending", JobOutbox.next_attempt_at <= time.time()).order_by(JobOutbox.created_at, JobOutbox.id).limit(1).with_for_update(skip_locked=True))
        if entry is None:
            return False
        job = await db.get(TASKS[entry.kind][0], entry.target_id)
        if job is None or job.status != "queued":
            entry.status = "discarded"
        else:
            entry.attempts += 1
            try:
                await (publish or publish_task)(entry.kind, entry.target_id)
            except Exception:
                entry.last_error = "publish_failed"
                entry.next_attempt_at = time.time() + min(300, 2 ** min(entry.attempts, 8))
            else:
                entry.status = "published"
                entry.published_at = time.time()
                entry.last_error = None
        await db.commit()
        return True


async def recover_stale(factory=None, now=None, target_id=None):
    """Requeue expired claims; token checks fence off the old worker's writes."""
    now = time.time() if now is None else now
    recovered = 0
    async with (factory or Session)() as db:
        for kind, (model, _) in TASKS.items():
            query = select(model).where(model.status == "running", model.started_at < now - LEASE_SECONDS)
            if target_id is not None:
                query = query.where(model.id == target_id)
            jobs = (await db.scalars(query.order_by(model.started_at).limit(50).with_for_update(skip_locked=True))).all()
            for job in jobs:
                entry = await db.scalar(select(JobOutbox).where(JobOutbox.kind == kind, JobOutbox.target_id == job.id).with_for_update())
                if job.attempt_count >= MAX_CLAIMS:
                    job.status = "failed"
                    job.error_code = "worker_lost"
                    job.completed_at = now
                    if entry and entry.status == "pending":
                        entry.status = "discarded"
                    if kind == "document_processing":
                        document = await db.get(Document, job.document_id)
                        if document:
                            queue_notification(db, recipient_id=job.requested_by, patient_id=document.patient_id, kind="processing_failed", event_key=f"processing:{job.id}", resource_id=document.id)
                            db.add(AuditEvent(actor_id=job.requested_by, patient_id=document.patient_id, action="document_processing_failed", resource=f"document:{document.id}", outcome="failed", **({"correlation_id": job.request_correlation_id} if job.request_correlation_id else {})))
                    else:
                        db.add(AuditEvent(actor_id=job.created_by, action="knowledge_import_failed", resource=f"knowledge_import:{job.id}", outcome="failed", **({"correlation_id": job.request_correlation_id} if job.request_correlation_id else {})))
                else:
                    job.status = "queued"
                    if entry is None:
                        entry = JobOutbox(kind=kind, target_id=job.id)
                        db.add(entry)
                    entry.status = "pending"
                    entry.next_attempt_at = now
                    entry.published_at = None
                    entry.last_error = None
                job.claim_token = None
                recovered += 1
        queued_document = exists(select(ExtractionJob.id).where(ExtractionJob.id == JobOutbox.target_id, ExtractionJob.status == "queued"))
        queued_import = exists(select(KnowledgeImport.id).where(KnowledgeImport.id == JobOutbox.target_id, KnowledgeImport.status == "queued"))
        query = select(JobOutbox).where(JobOutbox.status == "published", JobOutbox.published_at < now - LEASE_SECONDS, or_((JobOutbox.kind == "document_processing") & queued_document, (JobOutbox.kind == "knowledge_import") & queued_import))
        if target_id is not None:
            query = query.where(JobOutbox.target_id == target_id)
        entries = (await db.scalars(query.order_by(JobOutbox.published_at).limit(50).with_for_update(skip_locked=True))).all()
        for entry in entries:
            job = await db.get(TASKS[entry.kind][0], entry.target_id)
            if job and job.status == "queued":
                entry.status = "pending"
                entry.next_attempt_at = now
                entry.published_at = None
                entry.last_error = None
                recovered += 1
        await db.commit()
    return recovered


async def dispatch_index_one(factory=None, publish=None, now=None):
    """Publish due index jobs at least once; queued jobs are redelivered after a lease."""
    now = time.time() if now is None else now
    async with (factory or Session)() as db:
        job = await db.scalar(select(KnowledgeIndexJob).where(KnowledgeIndexJob.status == "queued", KnowledgeIndexJob.next_dispatch_at <= now).order_by(KnowledgeIndexJob.next_dispatch_at, KnowledgeIndexJob.id).limit(1).with_for_update(skip_locked=True))
        if not job:
            return False
        job.dispatch_attempts += 1
        try:
            if publish:
                await publish(job.id)
            else:
                await asyncio.to_thread(index_knowledge_job.apply_async, args=[job.id], retry=False)
        except Exception:
            job.next_dispatch_at = now + min(300, 2 ** min(job.dispatch_attempts, 8))
        else:
            job.next_dispatch_at = now + LEASE_SECONDS
        await db.commit()
        return True


async def recover_index_jobs(factory=None, now=None):
    now = time.time() if now is None else now
    async with (factory or Session)() as db:
        jobs = (await db.scalars(select(KnowledgeIndexJob).where(KnowledgeIndexJob.status == "running", KnowledgeIndexJob.started_at < now - LEASE_SECONDS).order_by(KnowledgeIndexJob.started_at).limit(50).with_for_update(skip_locked=True))).all()
        for job in jobs:
            job.claim_token = None
            if job.attempt_count >= MAX_CLAIMS:
                job.status, job.error_code, job.completed_at = "failed", "worker_lost", now
                db.add(AuditEvent(actor_id=job.requested_by, action="knowledge_index_failed", resource=f"knowledge:{job.source_id}", outcome="failed", **({"correlation_id": job.request_correlation_id} if job.request_correlation_id else {})))
            else:
                job.status, job.next_dispatch_at = "queued", now
        await db.commit()
        return len(jobs)


async def run():
    try:
        next_recovery = 0.0
        while True:
            try:
                if time.monotonic() >= next_recovery:
                    await recover_stale()
                    await recover_index_jobs()
                    next_recovery = time.monotonic() + 30
                dispatched = await dispatch_one()
                indexed = await dispatch_index_one()
                if not dispatched and not indexed:
                    await asyncio.sleep(1)
            except SQLAlchemyError:
                log.warning("Job dispatcher database unavailable; retrying")
                await asyncio.sleep(2)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
