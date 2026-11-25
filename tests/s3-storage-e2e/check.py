import asyncio, hashlib, time, uuid
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db import Base
from app.models import Document, DocumentObjectLink, ObjectUploadIntent, Session as UserSession, User
from app import object_document_submissions, object_upload_intents
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from app.s3_storage import S3PrivateStorage, StorageIntegrityError
client=boto3.client('s3',endpoint_url='http://object-store:9000',config=Config(s3={'addressing_style':'path'},connect_timeout=2,read_timeout=2,retries={'max_attempts':0}))
for _ in range(40):
    try:
        client.list_buckets()
        break
    except (BotoCoreError,ClientError): time.sleep(.5)
else: raise AssertionError('Synthetic storage server unavailable')
bucket='synthetic-private-test'
client.create_bucket(Bucket=bucket)
store=S3PrivateStorage(client,bucket)
identifier=str(uuid.uuid4())
try:
    store.put('documents',identifier,b'%PDF-synthetic document')
    raise AssertionError('Unversioned bucket accepted')
except StorageIntegrityError: pass
client.put_bucket_versioning(Bucket=bucket,VersioningConfiguration={'Status':'Enabled'})
async def check_intent():
    engine=create_async_engine('sqlite+aiosqlite:////tmp/synthetic-intent.db')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    async with factory() as db:
        db.add(User(id='synthetic',email='synthetic@example.test',full_name='Synthetic',role='patient',password_hash='unused',verification_status='not_applicable'))
        await db.flush()
        db.add(UserSession(id='synthetic-session',user_id='synthetic',refresh_hash='synthetic',expires_at=time.time()+3600))
        await db.commit()
    data=b'%PDF-synthetic document'
    intent_id=await object_document_submissions.prepare_document(factory,actor_id='synthetic',session_id='synthetic-session',store_id='a'*64,bucket=bucket,object_id=identifier,size=len(data),sha256=hashlib.sha256(data).hexdigest(),filename='synthetic.pdf',content_type='application/pdf',scan_status='not_scanned',correlation_id=str(uuid.uuid4()))
    assert await object_upload_intents.recover(factory,intent_id,store_id='a'*64,store=store) is None
    # Simulate loss of the successful PUT response; recover from the durable intent.
    await asyncio.to_thread(store.put,'documents',identifier,data)
    ref=await object_upload_intents.recover(factory,intent_id,store_id='a'*64,store=store)
    assert ref is not None and store.read(ref)==data
    async with factory() as db:
        intent=await db.get(ObjectUploadIntent,intent_id)
        assert intent.status=='stored' and intent.version_id==ref.version_id
    assert await object_document_submissions.complete_document(factory,actor_id='synthetic',intent_id=intent_id,store_id='a'*64)==identifier
    async with factory() as db:
        assert (await db.get(DocumentObjectLink,identifier)).intent_id==intent_id
        assert (await db.get(Document,identifier)).sha256==ref.sha256
    await engine.dispose()
    return ref
ref=asyncio.run(check_intent())
try:
    store.put('documents',identifier,b'duplicate')
    raise AssertionError('Duplicate key accepted')
except ClientError as exc:
    assert exc.response['ResponseMetadata']['HTTPStatusCode'] in (409,412),exc
store.delete(ref)
try:
    client.get_object(Bucket=bucket,Key=ref.key,VersionId=ref.version_id)
    raise AssertionError('Version not removed')
except ClientError as exc:
    assert exc.response['ResponseMetadata']['HTTPStatusCode']==404,exc
print('PASS: private versioned object, lost-response discovery, atomic document link, integrity, conditional create, and exact-version deletion against isolated MinIO')
