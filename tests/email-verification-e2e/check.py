import asyncio, re
import httpx
async def check():
    async with httpx.AsyncClient(timeout=5) as client:
        for _ in range(60):
            try:
                if (await client.get('http://api:8000/ready')).status_code==200: break
            except httpx.HTTPError: pass
            await asyncio.sleep(1)
        else: raise AssertionError('API not ready')
        user={'email':'synthetic-email-e2e@example.test','password':'synthetic-password-123','full_name':'Synthetic User','role':'doctor'}
        r=await client.post('http://api:8000/auth/register',json=user)
        assert r.status_code==201,r.text
        r=await client.post('http://api:8000/auth/login',json={'email':user['email'],'password':user['password']})
        assert r.status_code==200,r.text
        headers={'Authorization':'Bearer '+r.json()['access_token']}
        r=await client.post('http://api:8000/auth/email-verification/request',headers=headers)
        assert r.status_code==202,r.text
        for _ in range(40):
            data=(await client.get('http://mailpit:8025/api/v1/messages')).json()
            if data.get('messages'): break
            await asyncio.sleep(.5)
        else: raise AssertionError('No message arrived in Mailpit')
        msg=(await client.get('http://mailpit:8025/api/v1/message/'+data['messages'][0]['ID'])).json()
        assert msg['Subject']=='Verify your MediAgent email address'
        match=re.search(r'/verify-email#token=([0-9a-f.-]+)',msg['Text'])
        assert match, 'Verification link missing from message'
        token=match.group(1)
        r=await client.post('http://api:8000/auth/email-verification/confirm',json={'token':token})
        assert r.status_code==200,r.text
        account=(await client.get('http://api:8000/me',headers=headers)).json()
        assert account['email_verified_at'] is not None and account['verification_status']=='pending'
        assert (await client.post('http://api:8000/auth/email-verification/confirm',json={'token':token})).status_code==400
        print('PASS: API -> durable queue -> mailer -> Mailpit -> single-use link -> verified account')
asyncio.run(check())
