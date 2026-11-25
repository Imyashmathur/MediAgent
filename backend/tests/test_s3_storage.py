import base64
import hashlib
import io
import uuid

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
import pytest

from app.s3_storage import ObjectRef, S3PrivateStorage, StorageIntegrityError, UntrackedObjectError


@pytest.fixture
def store():
    client = boto3.client('s3', region_name='us-east-1', aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
    with Stubber(client) as stubber:
        yield S3PrivateStorage(client, 'synthetic-private-bucket'), stubber


def test_versioned_put_read_and_specific_version_delete(store):
    storage, stubber = store
    identifier = str(uuid.uuid4())
    key = 'documents/' + identifier
    content = b'synthetic document'
    digest = hashlib.sha256(content).hexdigest()
    stubber.add_response('get_bucket_versioning', {'Status':'Enabled'}, {'Bucket':storage.bucket})
    stubber.add_response('put_object', {'VersionId':'version-one'}, {'Bucket':storage.bucket,'Key':key,'Body':content,'ContentType':'application/octet-stream','IfNoneMatch':'*','ChecksumSHA256':base64.b64encode(bytes.fromhex(digest)).decode()})
    ref = storage.put('documents', identifier, content)
    assert ref == ObjectRef(storage.bucket,key,'version-one',len(content),digest)
    body = StreamingBody(io.BytesIO(content),len(content))
    stubber.add_response('get_object', {'Body':body,'ContentLength':len(content),'VersionId':'version-one'}, {'Bucket':storage.bucket,'Key':key,'VersionId':'version-one'})
    assert storage.read(ref) == content
    stubber.add_response('delete_object', {'VersionId':'version-one'}, {'Bucket':storage.bucket,'Key':key,'VersionId':'version-one'})
    storage.delete(ref)
    stubber.assert_no_pending_responses()


def test_versioning_and_ambiguous_writes_fail_closed(store):
    storage, stubber = store
    identifier = str(uuid.uuid4())
    stubber.add_response('get_bucket_versioning', {}, {'Bucket':storage.bucket})
    with pytest.raises(StorageIntegrityError): storage.put('documents',identifier,b'synthetic')
    digest=hashlib.sha256(b'synthetic').digest()
    stubber.add_response('get_bucket_versioning', {'Status':'Enabled'}, {'Bucket':storage.bucket})
    stubber.add_response('put_object', {}, {'Bucket':storage.bucket,'Key':'documents/'+identifier,'Body':b'synthetic','ContentType':'application/octet-stream','IfNoneMatch':'*','ChecksumSHA256':base64.b64encode(digest).decode()})
    with pytest.raises(UntrackedObjectError): storage.put('documents',identifier,b'synthetic')
    stubber.assert_no_pending_responses()


def test_reads_reject_tampering_and_wrong_version(store):
    storage, stubber = store
    content=b'synthetic'
    ref=ObjectRef(storage.bucket,'documents/'+str(uuid.uuid4()),'v1',len(content),hashlib.sha256(content).hexdigest())
    for version, payload in [('v2', content), ('v1',b'tampered!')]:
        body=StreamingBody(io.BytesIO(payload),len(payload))
        stubber.add_response('get_object', {'Body':body,'ContentLength':len(content),'VersionId':version}, {'Bucket':storage.bucket,'Key':ref.key,'VersionId':'v1'})
        with pytest.raises(StorageIntegrityError): storage.read(ref)
        assert body._raw_stream.closed
    stubber.assert_no_pending_responses()


def test_rejects_noncanonical_keys_and_wrong_bucket(store):
    storage,_=store
    for identifier in ['../../private', str(uuid.uuid1()), 'not-an-id']:
        with pytest.raises(ValueError): storage.put('documents',identifier,b'foo')
    ref=ObjectRef('other-private-bucket','documents/'+str(uuid.uuid4()),'v1',1,hashlib.sha256(b'x').hexdigest())
    with pytest.raises(ValueError): storage.read(ref)
    with pytest.raises(ValueError): storage.delete(ref)


def test_discovery_requires_one_verified_exact_version(store):
    storage, stubber = store
    identifier = str(uuid.uuid4())
    key = 'documents/' + identifier
    content = b'synthetic'
    digest = hashlib.sha256(content).hexdigest()
    request = {'Bucket':storage.bucket,'Prefix':key,'MaxKeys':1000}
    stubber.add_response('list_object_versions', {'IsTruncated':False,'Versions':[{'Key':key+'-other','VersionId':'other'}]}, request)
    assert storage.discover('documents',identifier,len(content),digest) is None
    stubber.add_response('list_object_versions', {'IsTruncated':False,'Versions':[{'Key':key,'VersionId':'v1'}]}, request)
    stubber.add_response('get_object', {'Body':StreamingBody(io.BytesIO(content),len(content)),'ContentLength':len(content),'VersionId':'v1'}, {'Bucket':storage.bucket,'Key':key,'VersionId':'v1'})
    assert storage.discover('documents',identifier,len(content),digest).version_id == 'v1'
    for result in [
        {'IsTruncated':True},
        {'IsTruncated':False,'Versions':[{'Key':key,'VersionId':'v1'},{'Key':key,'VersionId':'v2'}]},
        {'IsTruncated':False,'DeleteMarkers':[{'Key':key,'VersionId':'marker'}]},
    ]:
        stubber.add_response('list_object_versions',result,request)
        with pytest.raises(StorageIntegrityError): storage.discover('documents',identifier,len(content),digest)
    stubber.assert_no_pending_responses()


def test_purge_deletes_only_recorded_version_and_confirms_absence(store):
    storage, stubber = store
    ref=ObjectRef(storage.bucket,'documents/'+str(uuid.uuid4()),'v1',1,hashlib.sha256(b'x').hexdigest())
    request={'Bucket':ref.bucket,'Key':ref.key,'VersionId':'v1'}
    listing={'Bucket':ref.bucket,'Prefix':ref.key,'MaxKeys':1000}
    stubber.add_response('delete_object',{'VersionId':'v1'},request)
    stubber.add_response('list_object_versions',{'IsTruncated':False,'Versions':[{'Key':ref.key+'-other','VersionId':'other'}]},listing)
    storage.purge(ref)
    for extra in ({'Versions':[{'Key':ref.key,'VersionId':'v2'}]}, {'DeleteMarkers':[{'Key':ref.key,'VersionId':'marker'}]}, {'IsTruncated':True}):
        stubber.add_response('delete_object',{'VersionId':'v1'},request)
        stubber.add_response('list_object_versions',{'IsTruncated':False,**extra},listing)
        with pytest.raises(StorageIntegrityError): storage.purge(ref)
    stubber.assert_no_pending_responses()
