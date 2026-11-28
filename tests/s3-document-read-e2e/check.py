"""Synthetic object-backed upload, read, processing, and deletion test."""
import asyncio
import hashlib
import os
import time
import uuid

import httpx
import jwt
import pymupdf
from botocore.exceptions import BotoCoreError, ClientError

from app import document_storage, object_document_submissions
from app.db import Session
from app.main import hash_password
from app.models import ObjectUploadDiscard, User


async def main():
    settings=document_storage.config()
    store=document_storage.store(settings)
    for _ in range(40):
        try:
            await asyncio.to_thread(store.client.list_buckets)
            break
        except (BotoCoreError,ClientError):
            await asyncio.sleep(.5)
    else:
        raise AssertionError('Synthetic object store unavailable')
    await asyncio.to_thread(store.client.create_bucket,Bucket=settings.bucket)
    await asyncio.to_thread(store.client.put_bucket_versioning,Bucket=settings.bucket,VersioningConfiguration={'Status':'Enabled'})
    async with httpx.AsyncClient(base_url='http://api:8000',timeout=5) as http:
        for _ in range(60):
            try:
                if (await http.get('/ready')).status_code==200: break
            except httpx.HTTPError: pass
            await asyncio.sleep(1)
        else: raise AssertionError('API did not become ready')
        async def account(email):
            registration=await http.post('/auth/register',json={'email':email,'password':'synthetic-password-123','full_name':'Synthetic','role':'patient'})
            assert registration.status_code==201,registration.text
            login=await http.post('/auth/login',json={'email':email,'password':'synthetic-password-123'})
            assert login.status_code==200,login.text
            return registration.json()['id'],login.json()['access_token']
        patient_id, token=await account('owner@example.test')
        _, other_token=await account('other@example.test')
        headers={'Authorization':f'Bearer {token}'}
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72,72),'Test name: Synthetic test\nObserved value: 6.2')
            content=pdf.tobytes()
        uploaded=await http.post('/documents',headers=headers,files={'file':('synthetic.pdf',content,'application/pdf')})
        assert uploaded.status_code==201,uploaded.text
        object_id=uploaded.json()['id']
        assert (await http.get('/documents/incomplete-uploads',headers=headers)).json()==[]
        path=f'/documents/{object_id}'
        assert (await http.get(path+'/download',headers={'Authorization':f'Bearer {other_token}'})).status_code==403
        downloaded=await http.get(path+'/download',headers=headers)
        assert downloaded.status_code==200 and downloaded.content==content,downloaded.text
        preview=await http.get(path+'/preview/1',headers=headers)
        assert preview.status_code==200 and preview.content.startswith(b'\x89PNG'),preview.text
        queued=await http.get(path+'/processing',headers=headers)
        assert queued.status_code==200 and queued.json()['status'] in ('queued','running','completed'),queued.text
        for _ in range(60):
            result=(await http.get(path+'/processing',headers=headers)).json()
            if result['status'] in ('completed','failed'): break
            await asyncio.sleep(.5)
        assert result['status']=='completed' and 'Synthetic test' in result['pages'][0]['text'],result
        deletion=await http.post(path+'/delete',headers=headers,json={'confirmation':'DELETE'})
        assert deletion.status_code==202,deletion.text
        assert (await http.get(path+'/download',headers=headers)).status_code==404
        for _ in range(60):
            records=(await http.get('/documents/deletions',headers=headers)).json()
            row=next(item for item in records if item['id']==object_id)
            if row['purge_status']=='complete': break
            await asyncio.sleep(.5)
        assert row['purge_status']=='complete' and row['storage_kind']=='object',row
        key=f'documents/{object_id}'
        versions=await asyncio.to_thread(store.client.list_object_versions,Bucket=settings.bucket,Prefix=key)
        assert not [v for v in versions.get('Versions',[]) if v['Key']==key]
        assert not [v for v in versions.get('DeleteMarkers',[]) if v['Key']==key]
        # An empty first check must not end monitoring: simulate a delayed PUT.
        delayed_id=str(uuid.uuid4())
        session_id=jwt.decode(token,os.environ['JWT_SECRET'],algorithms=['HS256'])['sid']
        intent_id=await object_document_submissions.prepare_document(Session,actor_id=patient_id,session_id=session_id,store_id=settings.store_id,bucket=settings.bucket,object_id=delayed_id,size=len(content),sha256=hashlib.sha256(content).hexdigest(),filename='delayed.pdf',content_type='application/pdf',scan_status='not_scanned',correlation_id=str(uuid.uuid4()))
        discard=f'/documents/incomplete-uploads/{intent_id}/discard'
        assert (await http.post(discard,headers={'Authorization':f'Bearer {other_token}'})).status_code==404
        assert (await http.post(discard,headers=headers)).status_code==202
        for _ in range(40):
            rows=(await http.get('/documents/incomplete-uploads',headers=headers)).json()
            item=next(entry for entry in rows if entry['id']==intent_id)
            if item['cleanup_result']=='absent': break
            await asyncio.sleep(.25)
        assert item['cleanup_result']=='absent',item
        assert (await http.post(f'/documents/incomplete-uploads/{intent_id}/resume',headers=headers)).status_code==409
        await asyncio.to_thread(store.put,'documents',delayed_id,content)
        async with Session() as db:
            marker = await db.get(ObjectUploadDiscard,intent_id)
            marker.last_result, marker.error_code, marker.next_check_at = 'error', 'provider_unavailable', time.time()+3600
            db.add(User(email='operator@example.test',full_name='Synthetic Operator',role='admin',password_hash=hash_password('synthetic-password-123'),verification_status='not_applicable'))
            await db.commit()
        operator=await http.post('/auth/login',json={'email':'operator@example.test','password':'synthetic-password-123'})
        assert operator.status_code==200,operator.text
        operator_headers={'Authorization':f"Bearer {operator.json()['access_token']}"}
        errors_path='/admin/operations/discard-errors'
        assert (await http.get(errors_path,headers=headers)).status_code==403
        errors=await http.get(errors_path,headers=operator_headers)
        assert errors.status_code==200 and any(row['id']==intent_id and row['category']=='provider_unavailable' for row in errors.json()),errors.text
        assert 'delayed.pdf' not in errors.text and patient_id not in errors.text
        assert (await http.post(f'{errors_path}/{intent_id}/retry',headers=operator_headers)).status_code==202
        for _ in range(40):
            rows=(await http.get('/documents/incomplete-uploads',headers=headers)).json()
            item=next(entry for entry in rows if entry['id']==intent_id)
            if item['cleanup_result']=='removed': break
            await asyncio.sleep(.25)
        assert item['cleanup_result']=='removed',item
        versions=await asyncio.to_thread(store.client.list_object_versions,Bucket=settings.bucket,Prefix=f'documents/{delayed_id}')
        assert not [v for v in versions.get('Versions',[]) if v['Key']==f'documents/{delayed_id}']
        # Simulate a provider or external writer creating multiple versions.
        # The reconciler must not choose one or delete any of them.
        ambiguous_id=str(uuid.uuid4())
        ambiguous_intent=await object_document_submissions.prepare_document(Session,actor_id=patient_id,session_id=session_id,store_id=settings.store_id,bucket=settings.bucket,object_id=ambiguous_id,size=len(content),sha256=hashlib.sha256(content).hexdigest(),filename='ambiguous.pdf',content_type='application/pdf',scan_status='not_scanned',correlation_id=str(uuid.uuid4()))
        ambiguous_key=f'documents/{ambiguous_id}'
        for _ in range(2):
            await asyncio.to_thread(store.client.put_object,Bucket=settings.bucket,Key=ambiguous_key,Body=content)
        versions=await asyncio.to_thread(store.client.list_object_versions,Bucket=settings.bucket,Prefix=ambiguous_key)
        expected_versions={v['VersionId'] for v in versions.get('Versions',[]) if v['Key']==ambiguous_key}
        assert len(expected_versions)==2
        assert (await http.post(f'/documents/incomplete-uploads/{ambiguous_intent}/discard',headers=headers)).status_code==202
        for _ in range(40):
            errors=(await http.get(errors_path,headers=operator_headers)).json()
            conflict=next((row for row in errors if row['id']==ambiguous_intent),None)
            if conflict: break
            await asyncio.sleep(.25)
        assert conflict and conflict['category']=='integrity_conflict',conflict
        attempts=conflict['attempts']
        assert (await http.post(f'{errors_path}/{ambiguous_intent}/retry',headers=operator_headers)).status_code==202
        for _ in range(40):
            errors=(await http.get(errors_path,headers=operator_headers)).json()
            conflict=next((row for row in errors if row['id']==ambiguous_intent),None)
            if conflict and conflict['attempts']>attempts: break
            await asyncio.sleep(.25)
        assert conflict and conflict['attempts']>attempts and conflict['category']=='integrity_conflict',conflict
        versions=await asyncio.to_thread(store.client.list_object_versions,Bucket=settings.bucket,Prefix=ambiguous_key)
        assert {v['VersionId'] for v in versions.get('Versions',[]) if v['Key']==ambiguous_key}==expected_versions
        # A single version with different content is also not safe to remove.
        mismatch_id=str(uuid.uuid4())
        mismatch_intent=await object_document_submissions.prepare_document(Session,actor_id=patient_id,session_id=session_id,store_id=settings.store_id,bucket=settings.bucket,object_id=mismatch_id,size=len(content),sha256=hashlib.sha256(content).hexdigest(),filename='mismatch.pdf',content_type='application/pdf',scan_status='not_scanned',correlation_id=str(uuid.uuid4()))
        mismatch_key=f'documents/{mismatch_id}'
        wrong_content=bytes([content[0] ^ 1])+content[1:]
        wrong=await asyncio.to_thread(store.client.put_object,Bucket=settings.bucket,Key=mismatch_key,Body=wrong_content)
        assert (await http.post(f'/documents/incomplete-uploads/{mismatch_intent}/discard',headers=headers)).status_code==202
        for _ in range(40):
            errors=(await http.get(errors_path,headers=operator_headers)).json()
            mismatch=next((row for row in errors if row['id']==mismatch_intent),None)
            if mismatch: break
            await asyncio.sleep(.25)
        assert mismatch and mismatch['category']=='integrity_conflict',mismatch
        versions=await asyncio.to_thread(store.client.list_object_versions,Bucket=settings.bucket,Prefix=mismatch_key)
        assert {v['VersionId'] for v in versions.get('Versions',[]) if v['Key']==mismatch_key}=={wrong['VersionId']}
        print('PASS: object upload, authorization, version purge, late-write reconciliation, and refusal of ambiguous or mismatched versions')
asyncio.run(main())
