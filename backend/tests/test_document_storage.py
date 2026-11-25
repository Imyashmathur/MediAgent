import pytest
from app import document_storage


def test_object_storage_config_requires_explicit_tls_and_stable_identity(monkeypatch):
    monkeypatch.setenv('S3_ENDPOINT_URL','https://store.example.test:9000/')
    monkeypatch.setenv('S3_REGION','us-east-1')
    monkeypatch.setenv('S3_BUCKET','synthetic-private-bucket')
    first=document_storage.config()
    assert first.endpoint=='https://store.example.test:9000' and len(first.store_id)==64
    monkeypatch.setenv('S3_ENDPOINT_URL','https://STORE.example.test:9000')
    assert document_storage.config().store_id==first.store_id
    monkeypatch.setenv('S3_ENDPOINT_URL','http://store.example.test:9000')
    with pytest.raises(document_storage.StorageUnavailable): document_storage.config()
    monkeypatch.setenv('S3_INSECURE_TEST_ONLY','true')
    monkeypatch.setenv('APP_ENV','development')
    assert document_storage.config().endpoint=='http://store.example.test:9000'
    for environment in ('production', 'Production', 'prod', '', 'development '):
        monkeypatch.setenv('APP_ENV', environment)
        with pytest.raises(document_storage.StorageUnavailable): document_storage.config()
