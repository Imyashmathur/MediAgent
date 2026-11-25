"""Check that restored local files match database references without printing identifiers."""
import asyncio
import argparse
import hashlib
import os
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Session, engine
from .models import Document, DocumentObjectLink, KnowledgeImport, ObjectUploadIntent


def checked_file(root: Path, identifier: str) -> Path | None:
    try:
        if str(uuid.UUID(identifier)) != identifier:
            return None
        path = root / identifier
        if path.is_symlink() or not path.is_file():
            return None
        return path
    except (ValueError, TypeError, OSError):
        return None


def digest(path: Path) -> str:
    hashed=hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            hashed.update(chunk)
    return hashed.hexdigest()


async def inspect_restore(db: AsyncSession, upload_dir: Path, knowledge_dir: Path) -> dict[str,int]:
    documents=(await db.execute(select(Document.id,Document.size,Document.sha256,DocumentObjectLink.document_id).outerjoin(DocumentObjectLink,DocumentObjectLink.document_id == Document.id).where(Document.deleted_at.is_(None)))).all()
    imports=(await db.execute(select(KnowledgeImport.id,KnowledgeImport.sha256).where(KnowledgeImport.deleted_at.is_(None)))).all()
    object_references = await inspect_object_references(db)
    missing_documents=0
    invalid_imports=0
    hash_mismatch_documents=0
    legacy_documents=0
    object_documents=0
    for identifier, size, expected, object_link in documents:
        if object_link is not None:
            object_documents += 1
            continue
        path=checked_file(upload_dir,identifier)
        if expected is None:
            legacy_documents += 1
        try:
            if path is None or path.stat().st_size != size:
                missing_documents += 1
            elif expected is not None and digest(path) != expected:
                hash_mismatch_documents += 1
        except OSError:
            missing_documents += 1
    for identifier, expected in imports:
        path=checked_file(knowledge_dir,identifier)
        try:
            if path is None or digest(path) != expected: invalid_imports += 1
        except OSError:
            invalid_imports += 1
    missing_roots = sum(not root.is_dir() or root.is_symlink() for root in (upload_dir, knowledge_dir))
    return {"missing_storage_roots":missing_roots,"active_documents":len(documents),"active_object_documents_not_checked":object_documents,**object_references,"missing_or_wrong_size_documents":missing_documents,"hash_mismatch_documents":hash_mismatch_documents,"legacy_documents_without_hash":legacy_documents,"knowledge_imports":len(imports),"missing_or_changed_knowledge_imports":invalid_imports}


async def inspect_object_references(db: AsyncSession) -> dict[str,int]:
    """Local archives cannot establish recovery of linked or uncertain objects."""
    intents = await db.scalar(select(func.count()).select_from(ObjectUploadIntent))
    links = await db.scalar(select(func.count()).select_from(DocumentObjectLink))
    return {"object_upload_intents":intents, "object_document_links":links}


def local_recovery_failed(result: dict[str,int]) -> bool:
    return any(result.get(key, 0) for key in ("object_upload_intents", "object_document_links", "missing_storage_roots", "missing_or_wrong_size_documents", "hash_mismatch_documents", "legacy_documents_without_hash", "missing_or_changed_knowledge_imports"))


async def main(*, preflight: bool = False) -> int:
    try:
        async with Session() as db:
            result = await inspect_object_references(db) if preflight else await inspect_restore(db,Path(os.getenv("UPLOAD_DIR","/tmp/mediagent-uploads")),Path(os.getenv("KNOWLEDGE_IMPORT_DIR","/tmp/mediagent-knowledge-imports")))
        for key,value in result.items():
            print(f"{key}: {value}")
        return 1 if local_recovery_failed(result) else 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect local recovery coverage without exposing record identifiers")
    parser.add_argument('--local-only-preflight', action='store_true', help='fail if any object-storage intent or link exists')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(preflight=args.local_only_preflight)))
