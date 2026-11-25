import hashlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.check_restore import inspect_object_references, inspect_restore, local_recovery_failed
from app.db import Base
from app.models import Document, KnowledgeImport, User


@pytest_asyncio.fixture
async def database(tmp_path):
    engine=create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'restore.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine,expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_restore_check_detects_missing_wrong_size_and_changed_knowledge(database,tmp_path):
    uploads=tmp_path/"uploads"; knowledge=tmp_path/"knowledge"
    uploads.mkdir(); knowledge.mkdir()
    user_id,doc_id,deleted_id,import_id=(str(uuid.uuid4()) for _ in range(4))
    async with database() as db:
        db.add(User(id=user_id,email="restore@example.test",full_name="Synthetic",role="patient",password_hash="unused",verification_status="not_applicable"))
        await db.flush()
        db.add_all([Document(id=doc_id,patient_id=user_id,filename="synthetic.pdf",content_type="application/pdf",size=3,sha256=hashlib.sha256(b"doc").hexdigest()),Document(id=deleted_id,patient_id=user_id,filename="deleted.pdf",content_type="application/pdf",size=3,deleted_at=1),KnowledgeImport(id=import_id,created_by=user_id,filename="ref.pdf",media_type="application/pdf",sha256=hashlib.sha256(b"ref").hexdigest())])
        await db.commit()
    (uploads/doc_id).write_bytes(b"doc"); (knowledge/import_id).write_bytes(b"ref")
    async with database() as db:
        assert await inspect_restore(db,uploads,knowledge)=={"missing_storage_roots":0,"active_documents":1,"active_object_documents_not_checked":0,"object_upload_intents":0,"object_document_links":0,"missing_or_wrong_size_documents":0,"hash_mismatch_documents":0,"legacy_documents_without_hash":0,"knowledge_imports":1,"missing_or_changed_knowledge_imports":0}
        (uploads/doc_id).write_bytes(b"dog")
        assert (await inspect_restore(db,uploads,knowledge))["hash_mismatch_documents"]==1
        (uploads/doc_id).write_bytes(b"x")
        (knowledge/import_id).write_bytes(b"bad")
        result=await inspect_restore(db,uploads,knowledge)
        assert result["missing_or_wrong_size_documents"]==1 and result["missing_or_changed_knowledge_imports"]==1
        (uploads/doc_id).unlink(); (uploads/doc_id).symlink_to(knowledge/import_id)
        assert (await inspect_restore(db,uploads,knowledge))["missing_or_wrong_size_documents"]==1


@pytest.mark.asyncio
async def test_restore_check_detects_missing_storage_roots_even_for_empty_database(database,tmp_path):
    async with database() as db:
        result=await inspect_restore(db,tmp_path/"absent-uploads",tmp_path/"absent-knowledge")
        assert result["missing_storage_roots"] == 2


@pytest.mark.asyncio
async def test_restore_check_identifies_legacy_documents_without_hash(database,tmp_path):
    uploads=tmp_path/"uploads"; knowledge=tmp_path/"knowledge"
    uploads.mkdir(); knowledge.mkdir()
    user_id,doc_id=str(uuid.uuid4()),str(uuid.uuid4())
    async with database() as db:
        db.add(User(id=user_id,email="legacy@example.test",full_name="Synthetic",role="patient",password_hash="unused",verification_status="not_applicable")); await db.flush()
        db.add(Document(id=doc_id,patient_id=user_id,filename="old.pdf",content_type="application/pdf",size=3)); await db.commit()
        (uploads/doc_id).write_bytes(b"old")
        result=await inspect_restore(db,uploads,knowledge)
        assert result["legacy_documents_without_hash"]==1
        assert result["hash_mismatch_documents"]==0 and result["missing_or_wrong_size_documents"]==0
        assert local_recovery_failed(result)


@pytest.mark.asyncio
async def test_local_restore_rejects_object_references_even_with_local_shadow(database,tmp_path):
    import time
    from app.models import DocumentObjectLink, ObjectDocumentSubmission, ObjectUploadIntent, Session
    uploads=tmp_path/'uploads'; knowledge=tmp_path/'knowledge'
    uploads.mkdir(); knowledge.mkdir()
    user_id, session_id, object_id, intent_id = (str(uuid.uuid4()) for _ in range(4))
    async with database() as db:
        db.add(User(id=user_id,email='object-restore@example.test',full_name='Synthetic',role='patient',password_hash='unused',verification_status='not_applicable'))
        await db.flush()
        db.add(Session(id=session_id,user_id=user_id,refresh_hash='synthetic',expires_at=time.time()+3600))
        db.add(Document(id=object_id,patient_id=user_id,filename='synthetic.pdf',content_type='application/pdf',size=3,sha256=hashlib.sha256(b'doc').hexdigest()))
        db.add(ObjectUploadIntent(id=intent_id,actor_id=user_id,store_id='a'*64,bucket='synthetic-bucket',namespace='documents',object_id=object_id,size=3,sha256=hashlib.sha256(b'doc').hexdigest(),status='stored',version_id='v1'))
        await db.flush()
        db.add(ObjectDocumentSubmission(intent_id=intent_id,session_id=session_id,filename='synthetic.pdf',content_type='application/pdf',scan_status='not_scanned',correlation_id=str(uuid.uuid4())))
        await db.flush()
        db.add(DocumentObjectLink(document_id=object_id,intent_id=intent_id))
        await db.commit()
        (uploads/object_id).write_bytes(b'doc')
        preflight=await inspect_object_references(db)
        assert preflight == {'object_upload_intents':1,'object_document_links':1}
        assert local_recovery_failed(preflight)
        result=await inspect_restore(db,uploads,knowledge)
        assert result['active_object_documents_not_checked']==1 and result['missing_or_wrong_size_documents']==0
        assert local_recovery_failed(result)


@pytest.mark.asyncio
async def test_local_restore_preflight_rejects_unlinked_intent_and_ignores_deleted_import(database,tmp_path):
    from app.models import ObjectUploadIntent
    uploads=tmp_path/'uploads'; knowledge=tmp_path/'knowledge'
    uploads.mkdir(); knowledge.mkdir()
    user_id=str(uuid.uuid4())
    async with database() as db:
        db.add(User(id=user_id,email='pending-restore@example.test',full_name='Synthetic',role='patient',password_hash='unused',verification_status='not_applicable'))
        await db.flush()
        db.add(ObjectUploadIntent(id=str(uuid.uuid4()),actor_id=user_id,store_id='a'*64,bucket='synthetic-bucket',namespace='documents',object_id=str(uuid.uuid4()),size=3,sha256='b'*64,status='pending'))
        db.add(KnowledgeImport(id=str(uuid.uuid4()),created_by=user_id,filename='deleted.pdf',media_type='application/pdf',sha256='c'*64,deleted_at=1))
        await db.commit()
        result=await inspect_restore(db,uploads,knowledge)
        assert result['object_upload_intents']==1 and result['object_document_links']==0 and local_recovery_failed(result)
        assert result['knowledge_imports']==0 and result['missing_or_changed_knowledge_imports']==0


@pytest.mark.asyncio
async def test_local_only_preflight_cli_rejects_unlinked_object_intents(database):
    import asyncio
    import os
    import subprocess
    import sys
    from app.models import ObjectUploadIntent
    environment={**os.environ, 'DATABASE_URL':str(database.kw['bind'].url)}
    async def preflight():
        return await asyncio.to_thread(subprocess.run, [sys.executable,'-m','app.check_restore','--local-only-preflight'], env=environment, capture_output=True, text=True, timeout=15)
    empty=await preflight()
    assert empty.returncode==0 and 'object_upload_intents: 0' in empty.stdout
    user_id, intent_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with database() as db:
        db.add(User(id=user_id,email='preflight@example.test',full_name='Synthetic',role='patient',password_hash='unused',verification_status='not_applicable'))
        await db.flush()
        db.add(ObjectUploadIntent(id=intent_id,actor_id=user_id,store_id='a'*64,bucket='synthetic-bucket',namespace='documents',object_id=str(uuid.uuid4()),size=3,sha256='b'*64,status='pending'))
        await db.commit()
    blocked=await preflight()
    assert blocked.returncode==1 and 'object_upload_intents: 1' in blocked.stdout
    assert user_id not in blocked.stdout and intent_id not in blocked.stdout
