"""Resolve immutable document storage without bypassing caller authorization."""
from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from . import file_integrity
from .models import Document, DocumentObjectLink, ObjectUploadIntent
from .s3_storage import ObjectRef, S3PrivateStorage, StorageIntegrityError


class StorageUnavailable(Exception):
    """A configured storage provider is unavailable or misconfigured."""


@dataclass(frozen=True)
class StorageConfig:
    endpoint: str
    region: str
    bucket: str
    store_id: str
    max_bytes: int


def config() -> StorageConfig:
    endpoint = os.getenv('S3_ENDPOINT_URL', '').strip().rstrip('/')
    region = os.getenv('S3_REGION', '').strip()
    bucket = os.getenv('S3_BUCKET', '').strip()
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
        insecure_test = os.getenv('S3_INSECURE_TEST_ONLY') == 'true' and os.getenv('APP_ENV', 'development') == 'development'
        if (not endpoint or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or parsed.scheme not in (('https', 'http') if insecure_test else ('https',))
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', parsed.hostname)
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', region)):
            raise ValueError
        ObjectRef(bucket, 'documents/00000000-0000-4000-8000-000000000000', 'validation', 1, '0'*64)
        max_bytes = int(os.getenv('MAX_UPLOAD_BYTES', '10485760'))
        if not 1 <= max_bytes <= 100_000_000:
            raise ValueError
        host = parsed.hostname.lower()
        normalized = f'{parsed.scheme}://{host}' + (f':{port}' if port is not None else '')
        identity = hashlib.sha256(json.dumps({'endpoint': normalized, 'region': region, 'bucket': bucket}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return StorageConfig(normalized, region, bucket, identity, max_bytes)
    except (TypeError, ValueError) as exc:
        raise StorageUnavailable('Object storage is not configured safely') from exc


def store(settings: StorageConfig) -> S3PrivateStorage:
    client = boto3.client('s3', endpoint_url=settings.endpoint, region_name=settings.region,
                          config=Config(signature_version='s3v4', s3={'addressing_style':'path'},
                                        connect_timeout=3, read_timeout=10, retries={'mode':'standard','max_attempts':2}))
    return S3PrivateStorage(client, settings.bucket, max_bytes=settings.max_bytes)


@dataclass(frozen=True)
class DocumentLocation:
    path: Path | None = None
    store_id: str | None = None
    ref: ObjectRef | None = None


async def resolve(db, document: Document, upload_dir: Path) -> DocumentLocation:
    # Authorization adds an audit event. Do not flush that write while a
    # provider read is pending, especially on SQLite's single-writer database.
    with db.no_autoflush:
        link = await db.scalar(select(DocumentObjectLink).where(DocumentObjectLink.document_id == document.id).execution_options(populate_existing=True))
        if not link:
            return DocumentLocation(path=upload_dir / document.id)
        intent = await db.scalar(select(ObjectUploadIntent).where(ObjectUploadIntent.id == link.intent_id).execution_options(populate_existing=True))
    if (not intent or intent.status != 'stored' or intent.namespace != 'documents' or intent.object_id != document.id
        or intent.actor_id != document.patient_id or intent.size != document.size or intent.sha256 != document.sha256):
        raise StorageIntegrityError('Document storage association is inconsistent')
    try:
        ref = ObjectRef(intent.bucket, f'documents/{intent.object_id}', intent.version_id, intent.size, intent.sha256)
    except (ValueError, TypeError) as exc:
        raise StorageIntegrityError('Document storage association is invalid') from exc
    return DocumentLocation(store_id=intent.store_id, ref=ref)


def read(location: DocumentLocation, size: int, digest: str | None, limit: int) -> bytes:
    if location.path is not None:
        return file_integrity.read_snapshot(location.path, size, digest, limit)
    if not location.ref or not location.store_id:
        raise StorageIntegrityError('Document storage location is invalid')
    settings = config()
    if settings.store_id != location.store_id or settings.bucket != location.ref.bucket or settings.max_bytes > limit:
        raise StorageUnavailable('Document storage identity does not match configuration')
    try:
        return store(settings).read(location.ref)
    except StorageIntegrityError:
        raise
    except (BotoCoreError, ClientError, OSError) as exc:
        raise StorageUnavailable('Object storage unavailable') from exc


async def resolve_for_purge(db, document: Document) -> DocumentLocation | None:
    """Lock and verify the link for a deleted document; None denotes local storage."""
    if document.deleted_at is None:
        raise StorageIntegrityError('Document is not deleted')
    link = await db.scalar(select(DocumentObjectLink).where(DocumentObjectLink.document_id == document.id).with_for_update())
    if not link:
        return None
    intent = await db.scalar(select(ObjectUploadIntent).where(ObjectUploadIntent.id == link.intent_id).with_for_update())
    if (not intent or intent.status != 'stored' or intent.namespace != 'documents' or intent.object_id != document.id
        or intent.actor_id != document.patient_id or intent.sha256 != document.sha256):
        raise StorageIntegrityError('Document storage association is inconsistent')
    try:
        ref = ObjectRef(intent.bucket, f'documents/{intent.object_id}', intent.version_id, intent.size, intent.sha256)
    except (ValueError, TypeError) as exc:
        raise StorageIntegrityError('Document storage association is invalid') from exc
    return DocumentLocation(store_id=intent.store_id, ref=ref)


def purge(location: DocumentLocation) -> None:
    if not location.ref or not location.store_id or location.path is not None:
        raise StorageIntegrityError('Document object location is invalid')
    settings = config()
    if settings.store_id != location.store_id or settings.bucket != location.ref.bucket:
        raise StorageUnavailable('Document storage identity does not match configuration')
    try:
        store(settings).purge(location.ref)
    except StorageIntegrityError:
        raise
    except (BotoCoreError, ClientError, OSError) as exc:
        raise StorageUnavailable('Object storage unavailable') from exc
