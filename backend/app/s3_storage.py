"""Version-addressed S3 primitives; not yet wired into document workflows.

The caller must provide durable upload intent/reconciliation, authorization,
private bucket policy, encryption, and an authenticated, TLS-configured client.
"""
import base64
from dataclasses import dataclass
import hashlib
import hmac
import re
import uuid
from typing import Literal


class StorageIntegrityError(ValueError):
    """Stored bytes or provider metadata do not match the expected object."""


class UntrackedObjectError(RuntimeError):
    """A provider accepted a write without a usable immutable version identifier."""


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    version_id: str
    size: int
    sha256: str

    def __post_init__(self):
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', self.bucket) or '..' in self.bucket:
            raise ValueError('Invalid bucket')
        namespace, separator, identifier = self.key.partition('/')
        try:
            canonical = uuid.UUID(identifier)
        except (ValueError, AttributeError):
            raise ValueError('Invalid object key') from None
        if not separator or namespace not in ('documents', 'knowledge-imports') or canonical.version != 4 or str(canonical) != identifier:
            raise ValueError('Invalid object key')
        if not isinstance(self.version_id, str) or not self.version_id or self.version_id == 'null' or len(self.version_id) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in self.version_id):
            raise ValueError('Immutable version is required')
        if type(self.size) is not int or self.size < 1 or not re.fullmatch(r'[0-9a-f]{64}', self.sha256):
            raise ValueError('Invalid object integrity metadata')


class S3PrivateStorage:
    def __init__(self, client, bucket: str, *, max_bytes: int = 10_485_760):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 100_000_000:
            raise ValueError('Invalid storage limit')
        # Use the same validation as references without permitting arbitrary keys.
        ObjectRef(bucket, 'documents/00000000-0000-4000-8000-000000000000', 'validation', 1, '0' * 64)
        self.client, self.bucket, self.max_bytes = client, bucket, max_bytes

    def put(self, namespace: Literal['documents', 'knowledge-imports'], identifier: str, content: bytes) -> ObjectRef:
        if not isinstance(content, bytes) or not 1 <= len(content) <= self.max_bytes:
            raise ValueError('Invalid object size')
        digest = hashlib.sha256(content).hexdigest()
        key = f'{namespace}/{identifier}'
        # Validate before any request; never embed names or patient details in keys.
        ObjectRef(self.bucket, key, 'validation', len(content), digest)
        if self.client.get_bucket_versioning(Bucket=self.bucket).get('Status') != 'Enabled':
            raise StorageIntegrityError('Bucket versioning must be enabled')
        response = self.client.put_object(Bucket=self.bucket, Key=key, Body=content,
                                          ContentType='application/octet-stream', IfNoneMatch='*',
                                          ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode('ascii'))
        version_id = response.get('VersionId')
        try:
            return ObjectRef(self.bucket, key, version_id, len(content), digest)
        except ValueError as exc:
            # Do not guess how to delete an untracked write; reconciliation is required.
            raise UntrackedObjectError('Provider returned no usable object version') from exc

    def discover(self, namespace: Literal['documents', 'knowledge-imports'], identifier: str, size: int, sha256: str) -> ObjectRef | None:
        """Locate and verify a uniquely matching version after an uncertain write.

        None means no version was observed, not that a delayed write cannot arrive.
        Truncated listings, delete markers, and multiple versions are ambiguous.
        """
        key = f'{namespace}/{identifier}'
        ObjectRef(self.bucket, key, 'validation', size, sha256)
        if size > self.max_bytes:
            raise ValueError('Invalid object size')
        listing = self.client.list_object_versions(Bucket=self.bucket, Prefix=key, MaxKeys=1000)
        if listing.get('IsTruncated') is not False:
            raise StorageIntegrityError('Object version listing is incomplete')
        versions = [item for item in listing.get('Versions', []) if item.get('Key') == key]
        markers = [item for item in listing.get('DeleteMarkers', []) if item.get('Key') == key]
        if markers or len(versions) > 1:
            raise StorageIntegrityError('Object version listing is ambiguous')
        if not versions:
            return None
        try:
            ref = ObjectRef(self.bucket, key, versions[0].get('VersionId'), size, sha256)
        except ValueError as exc:
            raise StorageIntegrityError('Object version is invalid') from exc
        self.read(ref)
        return ref

    def read(self, ref: ObjectRef) -> bytes:
        if ref.bucket != self.bucket or ref.size > self.max_bytes:
            raise ValueError('Object does not belong to this store')
        response = self.client.get_object(Bucket=ref.bucket, Key=ref.key, VersionId=ref.version_id)
        body = response['Body']
        try:
            if response.get('VersionId') != ref.version_id or response.get('ContentLength') != ref.size:
                raise StorageIntegrityError('Object metadata mismatch')
            chunks = bytearray()
            while True:
                chunk = body.read(min(65536, self.max_bytes + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > self.max_bytes or len(chunks) > ref.size:
                    raise StorageIntegrityError('Object exceeds expected size')
            if len(chunks) != ref.size or not hmac.compare_digest(hashlib.sha256(chunks).hexdigest(), ref.sha256):
                raise StorageIntegrityError('Object content mismatch')
            return bytes(chunks)
        finally:
            body.close()

    def delete(self, ref: ObjectRef) -> None:
        if ref.bucket != self.bucket:
            raise ValueError('Object does not belong to this store')
        # Explicit version deletion avoids merely creating a delete marker.
        self.client.delete_object(Bucket=ref.bucket, Key=ref.key, VersionId=ref.version_id)


    def purge(self, ref: ObjectRef) -> None:
        """Delete the recorded version and confirm no exact-key versions remain."""
        self.delete(ref)
        listing = self.client.list_object_versions(Bucket=ref.bucket, Prefix=ref.key, MaxKeys=1000)
        if listing.get('IsTruncated') is not False:
            raise StorageIntegrityError('Object version listing is incomplete')
        if any(item.get('Key') == ref.key for group in ('Versions', 'DeleteMarkers') for item in listing.get(group, [])):
            raise StorageIntegrityError('Other object versions or markers remain')
