"""Durable, fenced knowledge indexing with authorization rechecks."""
import hashlib
import time
import uuid

from sqlalchemy import delete, select, update

from . import knowledge
from .db import Session as SessionFactory
from .models import AuditEvent, KnowledgeChunk, KnowledgeIndexJob, KnowledgeSource, Session, User


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def process_index(job_id: str, factory=None) -> None:
    token = str(uuid.uuid4())
    async with (factory or SessionFactory)() as db:
        claimed = await db.execute(update(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id, KnowledgeIndexJob.status == "queued").values(status="running", started_at=time.time(), claim_token=token, attempt_count=KnowledgeIndexJob.attempt_count + 1).returning(KnowledgeIndexJob.id))
        if claimed.scalar_one_or_none() is None:
            await db.rollback()
            return
        await db.commit()
        job = await db.get(KnowledgeIndexJob, job_id)
        source_id, actor_id, session_id = job.source_id, job.requested_by, job.session_id
        expected_hash, model = job.content_hash, job.model
        correlation = job.request_correlation_id or str(uuid.uuid4())
        source = await db.get(KnowledgeSource, source_id)
        user = await db.get(User, actor_id)
        session = await db.get(Session, session_id)
        try:
            if not user or not user.active or user.role != "admin" or not session or session.user_id != actor_id or session.revoked_at is not None or session.expires_at <= time.time():
                raise ValueError("authorization_revoked")
            if not source or source.status == "retired" or content_hash(source.content) != expected_hash:
                raise ValueError("source_changed")
            if model != knowledge.embedding_model():
                raise ValueError("model_changed")
            segments = knowledge.chunks(source.content)
            if not segments or len(segments) > 100:
                raise ValueError("invalid_source")
            await db.rollback()
            digest = await knowledge.embedding_digest()
            vectors = await knowledge.embed_texts(segments)
            digest_after = await knowledge.embedding_digest()
            if len(vectors) != len(segments):
                raise ValueError("embedding_invalid")
            # Lock in source -> user -> session -> job order; the worker's token
            # fences it from writing after an expired claim has been recovered.
            source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.id == source_id).with_for_update().execution_options(populate_existing=True))
            user = await db.scalar(select(User).where(User.id == actor_id).with_for_update().execution_options(populate_existing=True))
            session = await db.scalar(select(Session).where(Session.id == session_id).with_for_update().execution_options(populate_existing=True))
            job = await db.scalar(select(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id).with_for_update().execution_options(populate_existing=True))
            if not job or job.status != "running" or job.claim_token != token:
                await db.rollback()
                return
            if not user or not user.active or user.role != "admin" or not session or session.user_id != actor_id or session.revoked_at is not None or session.expires_at <= time.time():
                raise ValueError("authorization_revoked")
            if not source or source.status == "retired" or content_hash(source.content) != expected_hash:
                raise ValueError("source_changed")
            if model != knowledge.embedding_model() or digest != digest_after:
                raise ValueError("model_changed")
            await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))
            db.add_all([KnowledgeChunk(source_id=source_id, ordinal=i, content=segment, embedding=vector) for i, (segment, vector) in enumerate(zip(segments, vectors))])
            source.embedding_model, source.embedding_digest, source.index_status = model, digest, "ready"
            job.status, job.completed_at, job.claim_token = "completed", time.time(), None
            db.add(AuditEvent(actor_id=actor_id, action="knowledge_index_completed", resource=f"knowledge:{source_id}", outcome="success", correlation_id=correlation))
            await db.commit()
        except Exception as exc:
            await db.rollback()
            code = str(exc) if isinstance(exc, ValueError) and str(exc) in {"authorization_revoked", "source_changed", "model_changed", "invalid_source", "embedding_invalid"} else "embedding_unavailable"
            failed = await db.execute(update(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id, KnowledgeIndexJob.status == "running", KnowledgeIndexJob.claim_token == token).values(status="failed", error_code=code, completed_at=time.time(), claim_token=None).returning(KnowledgeIndexJob.id))
            if failed.scalar_one_or_none():
                db.add(AuditEvent(actor_id=actor_id, action="knowledge_index_failed", resource=f"knowledge:{source_id}", outcome="failed", correlation_id=correlation))
            await db.commit()
