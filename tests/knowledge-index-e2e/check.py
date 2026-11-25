import asyncio, json, time
import httpx
from sqlalchemy import select
from websockets.asyncio.client import connect
from app.db import Session
from app.main import hash_password
from app.models import AssistantTurn, ConversationMessage, User

async def check():
    async with httpx.AsyncClient(base_url='http://api:8000',timeout=5) as http:
        for _ in range(60):
            try:
                if (await http.get('/ready')).status_code == 200: break
            except httpx.HTTPError: pass
            await asyncio.sleep(1)
        else: raise AssertionError('API did not become ready')
        async with Session() as db:
            db.add(User(email='index-e2e@example.test',full_name='Synthetic Admin',role='admin',password_hash=hash_password('synthetic-password-123'),verification_status='not_applicable'))
            await db.commit()
        login=await http.post('/auth/login',json={'email':'index-e2e@example.test','password':'synthetic-password-123'})
        assert login.status_code == 200, login.text
        headers={'Authorization':'Bearer '+login.json()['access_token']}
        source={'title':'Synthetic reference','publisher':'Synthetic publisher','provenance':'synthetic://reference','publication_date':'2025-01-01','specialty':'general','version':'1','content':'Synthetic clinical reference text','effective_at':time.time()-1}
        created=await http.post('/admin/knowledge',headers=headers,json=source)
        assert created.status_code == 201,created.text
        source_id=created.json()['id']
        queued=await http.post(f'/admin/knowledge/{source_id}/index-jobs',headers=headers)
        assert queued.status_code == 202,queued.text
        for _ in range(40):
            jobs=(await http.get(f'/admin/knowledge/{source_id}/index-jobs',headers=headers)).json()
            if jobs[0]['status'] in ('completed','failed'): break
            await asyncio.sleep(0.5)
        assert jobs[0]['status']=='completed',jobs
        sources=(await http.get('/admin/knowledge',headers=headers)).json()
        result=next(item for item in sources if item['id']==source_id)
        assert result['index_status']=='ready' and result['embedding_digest']=='a'*64,result
        approved=await http.post(f'/admin/knowledge/{source_id}/approve',headers=headers)
        assert approved.status_code==200,approved.text
        draft=await http.post('/admin/knowledge',headers=headers,json={**source,'title':'Unapproved reference','content':'Synthetic unapproved reference'})
        assert draft.status_code==201,draft.text
        draft_id=draft.json()['id']
        assert (await http.post(f'/admin/knowledge/{draft_id}/index-jobs',headers=headers)).status_code==202
        for _ in range(40):
            draft_jobs=(await http.get(f'/admin/knowledge/{draft_id}/index-jobs',headers=headers)).json()
            if draft_jobs[0]['status'] in ('completed','failed'): break
            await asyncio.sleep(.5)
        assert draft_jobs[0]['status']=='completed',draft_jobs
        search=await http.post('/knowledge/search',headers=headers,json={'query':'synthetic'})
        assert search.status_code==200 and search.json()['results'],search.text
        assert [hit['source']['id'] for hit in search.json()['results']]==[source_id]
        ops=await http.get('/admin/operations',headers=headers)
        assert ops.json()['knowledge_index_jobs']['completed']==2,ops.text
        registration=await http.post('/auth/register',json={'email':'assistant-patient@example.test','password':'synthetic-password-123','full_name':'Synthetic Patient','role':'patient'})
        assert registration.status_code==201,registration.text
        patient_id=registration.json()['id']
        login=await http.post('/auth/login',json={'email':'assistant-patient@example.test','password':'synthetic-password-123'})
        patient_headers={'Authorization':'Bearer '+login.json()['access_token']}
        assert (await http.post('/profile/patient',headers=patient_headers,json={'medical_history':'Synthetic patient history'})).status_code==200
        conversation=await http.post('/conversations',headers=patient_headers,json={'patient_id':patient_id})
        assert conversation.status_code==201,conversation.text
        conversation_id=conversation.json()['id']
        ticket=await http.post(f'/conversations/{conversation_id}/ticket',headers=patient_headers)
        assert ticket.status_code==200,ticket.text
        async with connect(f'ws://api:8000/ws/conversations/{conversation_id}',origin='http://localhost:3100',proxy=None) as socket:
            await socket.send(json.dumps({'ticket':ticket.json()['ticket']}))
            assert json.loads(await asyncio.wait_for(socket.recv(),10))['type']=='ready'
            await socket.send(json.dumps({'type':'turn','question':'Explain this synthetic topic','include_profile':True,'include_knowledge':True}))
            chunks=[]
            for _ in range(20):
                event=json.loads(await asyncio.wait_for(socket.recv(),20))
                if event['type']=='error': raise AssertionError(event)
                if event['type']=='chunk': chunks.append(event['text'])
                if event['type']=='complete': break
            else: raise AssertionError('Assistant turn did not complete')
        assert ''.join(chunks)=='Synthetic educational explanation; verify with a clinician.'
        assert event['review_status']=='unreviewed'
        assert {citation['kind'] for citation in event['citations']}=={'approved_reference','self_reported_patient_profile'}
        assert all('text' not in citation for citation in event['citations'])
        history=await http.get(f'/conversations/{conversation_id}/messages',headers=patient_headers)
        assert history.status_code==200 and [message['role'] for message in history.json()]==['user','assistant']
        assert {citation['kind'] for citation in history.json()[-1]['citations']}=={'approved_reference','self_reported_patient_profile'}
        assert 'Synthetic patient history' not in json.dumps(history.json())
        other=await http.post('/auth/register',json={'email':'assistant-other@example.test','password':'synthetic-password-123','full_name':'Synthetic Other','role':'patient'})
        assert other.status_code==201
        other_login=await http.post('/auth/login',json={'email':'assistant-other@example.test','password':'synthetic-password-123'})
        other_headers={'Authorization':'Bearer '+other_login.json()['access_token']}
        assert (await http.get(f'/conversations/{conversation_id}/messages',headers=other_headers)).status_code==403
        # A verified doctor may use the assistant only within both consent scopes.
        doctor=await http.post('/auth/register',json={'email':'assistant-doctor@example.test','password':'synthetic-password-123','full_name':'Synthetic Doctor','role':'doctor'})
        assert doctor.status_code==201,doctor.text
        doctor_id=doctor.json()['id']
        profile=await http.get(f'/doctors/{doctor_id}/profile',headers=headers)
        assert profile.status_code==200,profile.text
        snapshot=profile.json()
        verified=await http.post(f'/admin/doctors/{doctor_id}/verification',headers=headers,json={'verified':True,'expected_profile_revision':snapshot['profile_revision'],'expected_verification_revision':snapshot['verification_revision'],'expected_status':snapshot['verification_status']})
        assert verified.status_code==200,verified.text
        doctor_login=await http.post('/auth/login',json={'email':'assistant-doctor@example.test','password':'synthetic-password-123'})
        doctor_headers={'Authorization':'Bearer '+doctor_login.json()['access_token']}
        relationship=await http.post('/relationships',headers=patient_headers,json={'doctor_id':doctor_id,'purpose':'Synthetic care'})
        assert relationship.status_code==201,relationship.text
        relationship_id=relationship.json()['id']
        grants={}
        for scope in ('assistant','read_profile'):
            grant=await http.post('/consents',headers=patient_headers,json={'relationship_id':relationship_id,'scope':scope,'purpose':'Synthetic care','expires_at':time.time()+600})
            assert grant.status_code==201,grant.text
            grants[scope]=grant.json()['id']
        doctor_conversation=await http.post('/conversations',headers=doctor_headers,json={'patient_id':patient_id})
        assert doctor_conversation.status_code==201,doctor_conversation.text
        doctor_conversation_id=doctor_conversation.json()['id']
        ticket=await http.post(f'/conversations/{doctor_conversation_id}/ticket',headers=doctor_headers)
        assert ticket.status_code==200,ticket.text
        async with connect(f'ws://api:8000/ws/conversations/{doctor_conversation_id}',origin='http://localhost:3100',proxy=None) as socket:
            await socket.send(json.dumps({'ticket':ticket.json()['ticket']}))
            assert json.loads(await asyncio.wait_for(socket.recv(),10))['type']=='ready'
            await socket.send(json.dumps({'type':'turn','question':'Explain consent race','include_profile':True,'include_knowledge':True}))
            for _ in range(40):
                status=(await http.get('http://mock:11434/test/chat-status')).json()
                if status['started']: break
                await asyncio.sleep(.1)
            assert status['started'], 'Synthetic provider was not reached'
            withdrawn=await http.post(f"/consents/{grants['read_profile']}/withdraw",headers=patient_headers)
            assert withdrawn.status_code==200,withdrawn.text
            assert (await http.post('http://mock:11434/test/release',json={})).status_code==200
            events=[]
            for _ in range(20):
                event=json.loads(await asyncio.wait_for(socket.recv(),20)); events.append(event)
                if event['type'] in ('error','complete'): break
            assert events[-1]['type']=='error' and not any(event['type'] in ('chunk','complete') for event in events),events
        assert (await http.get(f'/conversations/{doctor_conversation_id}/messages',headers=doctor_headers)).status_code==403
        async with Session() as db:
            messages=(await db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id==doctor_conversation_id))).all()
            turns=(await db.scalars(select(AssistantTurn).where(AssistantTurn.conversation_id==doctor_conversation_id))).all()
            assert [message.role for message in messages]==['user'] and len(turns)==1 and turns[0].status=='failed'
        assert (await http.post('http://mock:11434/test/reset',json={})).status_code==200
        retirement_conversation=await http.post('/conversations',headers=patient_headers,json={'patient_id':patient_id})
        assert retirement_conversation.status_code==201,retirement_conversation.text
        retirement_id=retirement_conversation.json()['id']
        ticket=await http.post(f'/conversations/{retirement_id}/ticket',headers=patient_headers)
        assert ticket.status_code==200,ticket.text
        async with connect(f'ws://api:8000/ws/conversations/{retirement_id}',origin='http://localhost:3100',proxy=None) as socket:
            await socket.send(json.dumps({'ticket':ticket.json()['ticket']}))
            assert json.loads(await asyncio.wait_for(socket.recv(),10))['type']=='ready'
            await socket.send(json.dumps({'type':'turn','question':'Explain retirement race','include_profile':True,'include_knowledge':True}))
            for _ in range(40):
                status=(await http.get('http://mock:11434/test/chat-status')).json()
                if status['started']: break
                await asyncio.sleep(.1)
            assert status['started'], 'Synthetic provider was not reached'
            retired=await http.post(f'/admin/knowledge/{source_id}/retire',headers=headers)
            assert retired.status_code==200,retired.text
            assert (await http.post('http://mock:11434/test/release',json={})).status_code==200
            events=[]
            for _ in range(20):
                event=json.loads(await asyncio.wait_for(socket.recv(),20)); events.append(event)
                if event['type'] in ('error','complete'): break
            assert events[-1]['type']=='error' and not any(event['type'] in ('chunk','complete') for event in events),events
        history=await http.get(f'/conversations/{retirement_id}/messages',headers=patient_headers)
        assert history.status_code==200 and [message['role'] for message in history.json()]==['user']
        assert (await http.post('/knowledge/search',headers=patient_headers,json={'query':'synthetic'})).json()['results']==[]
        print('PASS: cited approved knowledge and patient context; denial after cross-patient access, consent withdrawal, or source retirement')
asyncio.run(check())
