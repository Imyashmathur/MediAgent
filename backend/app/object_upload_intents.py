"""Durable identities and conservative reconciliation for future object writes.

Internal service only. Callers must authorize the requested upload, persist the
intent before sending bytes, and atomically associate an object with a record.
Document upload and recovery endpoints use this service.
"""
import asyncio
import re
import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import ObjectUploadDiscard, ObjectUploadIntent, User
from .s3_storage import ObjectRef, S3PrivateStorage


def validate_store_id(store_id: str) -> None:
    if not isinstance(store_id, str) or not re.fullmatch(r'[0-9a-f]{64}', store_id):
        raise ValueError('Invalid storage identity')


async def prepare(factory: async_sessionmaker, *, actor_id: str, store_id: str, bucket: str, namespace: str, object_id: str, size: int, sha256: str) -> str:
    """Commit the intended destination and digest before an external write."""
    validate_store_id(store_id)
    ObjectRef(bucket, f'{namespace}/{object_id}', 'validation', size, sha256)
    async with factory() as db:
        actor = await db.scalar(select(User).where(User.id == actor_id).with_for_update())
        required_role = 'patient' if namespace == 'documents' else 'admin'
        if not actor or not actor.active or actor.role != required_role:
            raise PermissionError('Actor cannot initiate this object write')
        intent = ObjectUploadIntent(id=str(uuid.uuid4()), actor_id=actor_id, store_id=store_id, bucket=bucket, namespace=namespace, object_id=object_id, size=size, sha256=sha256, status='pending')
        db.add(intent)
        await db.commit()
        return intent.id


def matches(intent: ObjectUploadIntent, store_id: str, ref: ObjectRef) -> bool:
    return (intent.store_id == store_id and intent.bucket == ref.bucket and f'{intent.namespace}/{intent.object_id}' == ref.key and intent.size == ref.size and intent.sha256 == ref.sha256)


async def record(factory: async_sessionmaker, intent_id: str, *, store_id: str, ref: ObjectRef) -> None:
    """Idempotently record a returned or independently verified version."""
    validate_store_id(store_id)
    async with factory() as db:
        intent = await db.scalar(select(ObjectUploadIntent).where(ObjectUploadIntent.id == intent_id).with_for_update())
        if not intent or not matches(intent, store_id, ref):
            raise ValueError('Object does not match its upload intent')
        if await db.get(ObjectUploadDiscard, intent_id):
            raise ValueError('Upload was discarded')
        if intent.status == 'stored':
            if intent.version_id != ref.version_id:
                raise ValueError('A different version was already recorded')
            return
        if intent.status != 'pending':
            raise ValueError('Upload intent is not pending')
        intent.status, intent.version_id, intent.stored_at = 'stored', ref.version_id, time.time()
        await db.commit()


async def recover(factory: async_sessionmaker, intent_id: str, *, store_id: str, store: S3PrivateStorage) -> ObjectRef | None:
    """Adopt one fully verified version; absence leaves the intent pending."""
    validate_store_id(store_id)
    async with factory() as db:
        intent = await db.get(ObjectUploadIntent, intent_id)
        if not intent or intent.store_id != store_id or intent.bucket != store.bucket:
            raise ValueError('Storage identity does not match upload intent')
        if await db.get(ObjectUploadDiscard, intent_id):
            raise ValueError('Upload was discarded')
        if intent.status == 'stored':
            return ObjectRef(intent.bucket, f'{intent.namespace}/{intent.object_id}', intent.version_id, intent.size, intent.sha256)
        if intent.status != 'pending':
            raise ValueError('Upload intent is not pending')
        namespace, object_id, size, sha256 = intent.namespace, intent.object_id, intent.size, intent.sha256
    ref = await asyncio.to_thread(store.discover, namespace, object_id, size, sha256)
    if ref is not None:
        await record(factory, intent_id, store_id=store_id, ref=ref)
    return ref
