import hashlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import object_upload_intents as intents
from app.db import Base
from app.models import ObjectUploadIntent, User
from app.s3_storage import ObjectRef


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine=create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/intents.db')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    async with factory() as db:
        db.add(User(id='patient',email='synthetic@example.test',full_name='Synthetic',role='patient',password_hash='unused',verification_status='not_applicable'))
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_intent_persists_before_write_and_records_only_matching_version(sessions):
    object_id=str(uuid.uuid4())
    digest=hashlib.sha256(b'synthetic').hexdigest()
    kwargs={'actor_id':'patient','store_id':'a'*64,'bucket':'synthetic-private-bucket','namespace':'documents','object_id':object_id,'size':9,'sha256':digest}
    intent_id=await intents.prepare(sessions,**kwargs)
    async with sessions() as db:
        intent=await db.get(ObjectUploadIntent,intent_id)
        assert intent.status=='pending' and intent.version_id is None
    ref=ObjectRef(kwargs['bucket'],'documents/'+object_id,'v1',9,digest)
    with pytest.raises(ValueError): await intents.record(sessions,intent_id,store_id='b'*64,ref=ref)
    await intents.record(sessions,intent_id,store_id='a'*64,ref=ref)
    await intents.record(sessions,intent_id,store_id='a'*64,ref=ref)
    with pytest.raises(ValueError): await intents.record(sessions,intent_id,store_id='a'*64,ref=ObjectRef(ref.bucket,ref.key,'v2',ref.size,ref.sha256))
    async with sessions() as db:
        intent=await db.get(ObjectUploadIntent,intent_id)
        assert intent.status=='stored' and intent.version_id=='v1' and intent.stored_at is not None
    with pytest.raises(PermissionError): await intents.prepare(sessions,**{**kwargs,'namespace':'knowledge-imports','object_id':str(uuid.uuid4())})


@pytest.mark.asyncio
async def test_inconclusive_recovery_stays_pending_and_verified_version_can_be_recorded(sessions):
    object_id=str(uuid.uuid4()); digest=hashlib.sha256(b'synthetic').hexdigest()
    intent_id=await intents.prepare(sessions,actor_id='patient',store_id='a'*64,bucket='synthetic-private-bucket',namespace='documents',object_id=object_id,size=9,sha256=digest)
    ref=ObjectRef('synthetic-private-bucket','documents/'+object_id,'v1',9,digest)
    class Store:
        bucket='synthetic-private-bucket'
        found=None
        def discover(self,*args):
            assert args == ('documents',object_id,9,digest)
            return self.found
    store=Store()
    assert await intents.recover(sessions,intent_id,store_id='a'*64,store=store) is None
    async with sessions() as db:
        assert (await db.get(ObjectUploadIntent,intent_id)).status == 'pending'
    store.found=ref
    assert await intents.recover(sessions,intent_id,store_id='a'*64,store=store) == ref
    assert await intents.recover(sessions,intent_id,store_id='a'*64,store=store) == ref
    with pytest.raises(ValueError): await intents.recover(sessions,intent_id,store_id='b'*64,store=store)


@pytest.mark.asyncio
async def test_document_submission_commits_metadata_and_associates_stored_version(sessions):
    import time
    from app import object_document_submissions as documents
    from app.models import AuditEvent, Document, DocumentObjectLink, ObjectDocumentSubmission, Session
    from sqlalchemy import select
    async with sessions() as db:
        db.add(Session(id='session',user_id='patient',refresh_hash='synthetic',expires_at=time.time()+3600))
        await db.commit()
    object_id=str(uuid.uuid4()); digest=hashlib.sha256(b'synthetic').hexdigest(); correlation=str(uuid.uuid4())
    args={'actor_id':'patient','session_id':'session','store_id':'a'*64,'bucket':'synthetic-private-bucket','object_id':object_id,'size':9,'sha256':digest,'filename':'synthetic.pdf','content_type':'application/pdf','scan_status':'no_threat_detected','correlation_id':correlation}
    submission_id=await documents.prepare_document(sessions,**args)
    async with sessions() as db:
        submission=await db.get(ObjectDocumentSubmission,submission_id)
        assert submission.filename == 'synthetic.pdf'
        assert (await db.get(ObjectUploadIntent,submission_id)).status == 'pending'
        assert await db.get(Document,object_id) is None
    with pytest.raises(ValueError): await documents.complete_document(sessions,actor_id='patient',intent_id=submission_id,store_id='a'*64)
    ref=ObjectRef(args['bucket'],'documents/'+object_id,'v1',9,digest)
    await intents.record(sessions,submission_id,store_id='a'*64,ref=ref)
    with pytest.raises(ValueError): await documents.complete_document(sessions,actor_id='patient',intent_id=submission_id,store_id='b'*64)
    assert await documents.complete_document(sessions,actor_id='patient',intent_id=submission_id,store_id='a'*64) == object_id
    assert await documents.complete_document(sessions,actor_id='patient',intent_id=submission_id,store_id='a'*64) == object_id
    async with sessions() as db:
        doc=await db.get(Document,object_id)
        assert doc.patient_id=='patient' and doc.sha256==digest and doc.scan_status=='no_threat_detected'
        assert (await db.get(DocumentObjectLink,object_id)).intent_id==submission_id
        events=(await db.scalars(select(AuditEvent).where(AuditEvent.action=='object_document_upload_completed'))).all()
        assert len(events)==1 and events[0].correlation_id==correlation


@pytest.mark.asyncio
async def test_document_submission_rechecks_session_and_rejects_unsafe_metadata(sessions):
    import time
    from app import object_document_submissions as documents
    from app.models import Document, Session
    async with sessions() as db:
        db.add(Session(id='session',user_id='patient',refresh_hash='synthetic',expires_at=time.time()+3600))
        await db.commit()
    object_id=str(uuid.uuid4()); digest=hashlib.sha256(b'synthetic').hexdigest()
    args={'actor_id':'patient','session_id':'session','store_id':'a'*64,'bucket':'synthetic-private-bucket','object_id':object_id,'size':9,'sha256':digest,'filename':'synthetic.pdf','content_type':'application/pdf','scan_status':'not_scanned','correlation_id':str(uuid.uuid4())}
    for change in ({'filename':'../private.pdf'}, {'content_type':'image/png'}, {'scan_status':'threat_detected'}, {'size':10485761}):
        with pytest.raises(ValueError): await documents.prepare_document(sessions,**{**args,**change})
    submission_id=await documents.prepare_document(sessions,**args)
    await intents.record(sessions,submission_id,store_id='a'*64,ref=ObjectRef(args['bucket'],'documents/'+object_id,'v1',9,digest))
    async with sessions() as db:
        (await db.get(Session,'session')).revoked_at=time.time()
        await db.commit()
    with pytest.raises(PermissionError): await documents.complete_document(sessions,actor_id='patient',intent_id=submission_id,store_id='a'*64)
    async with sessions() as db:
        assert await db.get(Document,object_id) is None


@pytest.mark.asyncio
async def test_document_submission_prevents_cross_patient_session_and_identity_collision(sessions):
    import time
    from app import object_document_submissions as documents
    from app.models import Document, DocumentObjectLink, Session
    async with sessions() as db:
        db.add(User(id='other',email='other@example.test',full_name='Synthetic',role='patient',password_hash='unused',verification_status='not_applicable'))
        db.add(Session(id='session',user_id='patient',refresh_hash='synthetic',expires_at=time.time()+3600))
        await db.commit()
    object_id=str(uuid.uuid4()); digest=hashlib.sha256(b'synthetic').hexdigest()
    args={'actor_id':'patient','session_id':'session','store_id':'a'*64,'bucket':'synthetic-private-bucket','object_id':object_id,'size':9,'sha256':digest,'filename':'synthetic.pdf','content_type':'application/pdf','scan_status':'not_scanned','correlation_id':str(uuid.uuid4())}
    with pytest.raises(PermissionError): await documents.prepare_document(sessions,**{**args,'actor_id':'other'})
    submission_id=await documents.prepare_document(sessions,**args)
    await intents.record(sessions,submission_id,store_id='a'*64,ref=ObjectRef(args['bucket'],'documents/'+object_id,'v1',9,digest))
    with pytest.raises(PermissionError): await documents.complete_document(sessions,actor_id='other',intent_id=submission_id,store_id='a'*64)
    async with sessions() as db:
        db.add(Document(id=object_id,patient_id='other',filename='other.pdf',content_type='application/pdf',size=1))
        await db.commit()
    with pytest.raises(ValueError): await documents.complete_document(sessions,actor_id='patient',intent_id=submission_id,store_id='a'*64)
    async with sessions() as db:
        assert (await db.get(Document,object_id)).patient_id=='other'
        assert await db.get(DocumentObjectLink,object_id) is None


@pytest.mark.asyncio
async def test_completion_rejects_session_changed_before_lock(sessions, monkeypatch):
    import time
    from app import object_document_submissions as documents
    from app.models import Document, ObjectDocumentSubmission, Session

    async with sessions() as db:
        db.add_all([Session(id='old-session', user_id='patient', refresh_hash='old', expires_at=time.time()+3600),
                    Session(id='new-session', user_id='patient', refresh_hash='new', expires_at=time.time()+3600)])
        await db.commit()
    object_id = str(uuid.uuid4())
    digest = hashlib.sha256(b'synthetic').hexdigest()
    intent_id = await documents.prepare_document(sessions, actor_id='patient', session_id='old-session', store_id='a'*64,
        bucket='synthetic-private-bucket', object_id=object_id, size=9, sha256=digest, filename='synthetic.pdf',
        content_type='application/pdf', scan_status='not_scanned', correlation_id=str(uuid.uuid4()))
    await intents.record(sessions, intent_id, store_id='a'*64,
        ref=ObjectRef('synthetic-private-bucket', 'documents/'+object_id, 'v1', 9, digest))

    original = documents.locked_patient_session
    async def change_session_before_lock(db, actor_id, session_id):
        assert session_id == 'old-session'
        async with sessions() as other:
            (await other.get(ObjectDocumentSubmission, intent_id)).session_id = 'new-session'
            (await other.get(Session, 'new-session')).revoked_at = time.time()
            await other.commit()
        await original(db, actor_id, session_id)
    monkeypatch.setattr(documents, 'locked_patient_session', change_session_before_lock)
    with pytest.raises(PermissionError, match='session changed'):
        await documents.complete_document(sessions, actor_id='patient', intent_id=intent_id, store_id='a'*64)
    monkeypatch.setattr(documents, 'locked_patient_session', original)
    with pytest.raises(PermissionError, match='no longer authorized'):
        await documents.complete_document(sessions, actor_id='patient', intent_id=intent_id, store_id='a'*64)
    async with sessions() as db:
        assert await db.get(Document, object_id) is None


@pytest.mark.asyncio
async def test_discard_fences_stored_submission_without_deleting_linked_documents(sessions):
    import time
    from app import object_document_submissions as documents
    from app.models import Document, ObjectUploadDiscard, Session
    async with sessions() as db:
        db.add(Session(id='session', user_id='patient', refresh_hash='synthetic', expires_at=time.time()+3600))
        await db.commit()
    digest = hashlib.sha256(b'synthetic').hexdigest()
    async def prepared():
        object_id = str(uuid.uuid4())
        intent_id = await documents.prepare_document(sessions, actor_id='patient', session_id='session', store_id='a'*64,
            bucket='synthetic-private-bucket', object_id=object_id, size=9, sha256=digest, filename='synthetic.pdf',
            content_type='application/pdf', scan_status='not_scanned', correlation_id=str(uuid.uuid4()))
        ref = ObjectRef('synthetic-private-bucket', 'documents/'+object_id, 'v1', 9, digest)
        await intents.record(sessions, intent_id, store_id='a'*64, ref=ref)
        return object_id, intent_id, ref
    object_id, intent_id, ref = await prepared()
    await documents.discard_document(sessions, actor_id='patient', session_id='session', intent_id=intent_id, correlation_id=str(uuid.uuid4()))
    with pytest.raises(ValueError, match='discarded'):
        await documents.complete_document(sessions, actor_id='patient', intent_id=intent_id, store_id='a'*64)
    with pytest.raises(ValueError, match='discarded'):
        await intents.record(sessions, intent_id, store_id='a'*64, ref=ref)
    with pytest.raises(ValueError, match='discarded'):
        await documents.rebind_session(sessions, actor_id='patient', session_id='session', intent_id=intent_id, store_id='a'*64, correlation_id=str(uuid.uuid4()))
    linked_id, linked_intent, _ = await prepared()
    assert await documents.complete_document(sessions, actor_id='patient', intent_id=linked_intent, store_id='a'*64) == linked_id
    with pytest.raises(ValueError, match='already associated'):
        await documents.discard_document(sessions, actor_id='patient', session_id='session', intent_id=linked_intent, correlation_id=str(uuid.uuid4()))
    async with sessions() as db:
        assert await db.get(Document, object_id) is None
        assert await db.get(ObjectUploadDiscard, intent_id) is not None
        assert await db.get(Document, linked_id) is not None
        assert await db.get(ObjectUploadDiscard, linked_intent) is None
