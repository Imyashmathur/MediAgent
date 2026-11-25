"""Opt-in integration exercise against the migrated Compose PostgreSQL database."""
import os
import asyncio
import time
import uuid

import pytest
import pytest_asyncio
import pymupdf
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import Session, engine
from app.main import app
from app.models import User
from app import knowledge


@pytest_asyncio.fixture(autouse=True)
async def dispose_connections():
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def cleanup_admins(dispose_connections):
    emails = []
    yield emails
    if emails:
        async with Session() as db:
            rows = (await db.scalars(select(User).where(User.email.in_(emails), User.role == "admin"))).all()
            for row in rows:
                row.active = False
            await db.commit()


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
@pytest.mark.parametrize('scope,action,release,endpoint', [
    ('read_profile', 'profile_read', 'profile_read_release', 'profile'),
    ('read_documents', 'document_list', 'document_list_release', 'documents'),
    ('read_documents', 'extracted_records_read', 'extracted_records_read_release', 'extracted'),
    ('consultations', 'consultation_list', 'consultation_list_release', 'consultations'),
    ('clinical_notes', 'note_list', 'note_list_release', 'notes'),
])
async def test_patient_records_are_not_released_after_consent_withdrawal(dispose_connections, monkeypatch, scope, action, release, endpoint):
    from fastapi import HTTPException, Response
    from sqlalchemy import delete, update
    from app import main
    from app.models import AuditEvent, Consent, Relationship
    patient_id, doctor_id, relationship_id, consent_id = (str(uuid.uuid4()) for _ in range(4))
    async with Session() as db:
        db.add_all([
            User(id=patient_id, email=f'profile-patient-{patient_id}@example.test', full_name='Synthetic Patient', role='patient', password_hash='unused', verification_status='not_applicable'),
            User(id=doctor_id, email=f'profile-doctor-{doctor_id}@example.test', full_name='Synthetic Doctor', role='doctor', password_hash='unused', verification_status='verified'),
        ])
        await db.flush()
        db.add(Relationship(id=relationship_id, patient_id=patient_id, doctor_id=doctor_id, purpose='care'))
        await db.flush()
        db.add(Consent(id=consent_id, relationship_id=relationship_id, scope=scope, purpose='care', expires_at=time.time() + 300))
        await db.commit()
    original = main.authorize_patient
    async def withdraw_after_first_check(db, user, target, requested_scope, requested_action):
        await original(db, user, target, requested_scope, requested_action)
        if requested_action == action:
            async with Session() as other:
                await other.execute(update(Consent).where(Consent.id == consent_id).values(status='withdrawn', withdrawn_at=time.time()))
                await other.commit()
    monkeypatch.setattr(main, 'authorize_patient', withdraw_after_first_check)
    try:
        async with Session() as db:
            doctor = await db.get(User, doctor_id)
            with pytest.raises(HTTPException) as exc:
                if endpoint == 'profile':
                    await main.read_patient_profile(patient_id, doctor, db)
                elif endpoint == 'documents':
                    await main.list_documents(patient_id, Response(), doctor, db)
                elif endpoint == 'extracted':
                    await main.extracted_records(patient_id, Response(), doctor, db)
                elif endpoint == 'consultations':
                    await main.list_consultations(patient_id, Response(), doctor, db)
                else:
                    await main.list_notes(patient_id, doctor, db)
            assert exc.value.status_code == 403
        async with Session() as db:
            event = await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == doctor_id, AuditEvent.action == release))
            assert event is not None and event.outcome == 'denied'
    finally:
        async with Session() as db:
            await db.execute(delete(AuditEvent).where(AuditEvent.actor_id == doctor_id))
            await db.execute(delete(Consent).where(Consent.id == consent_id))
            await db.execute(delete(Relationship).where(Relationship.id == relationship_id))
            await db.execute(delete(User).where(User.id.in_([patient_id, doctor_id])))
            await db.commit()


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_invalid_patient_audits_are_bounded_on_postgres(dispose_connections):
    from fastapi import HTTPException
    from sqlalchemy import delete
    from app.main import authorize_patient, require_note_doctor, audit
    from app.models import AuditEvent
    user_id = str(uuid.uuid4())
    async with Session() as db:
        db.add(User(id=user_id, email=f'audit-bound-{user_id}@example.test', full_name='Synthetic Patient', role='patient', password_hash='unused', verification_status='not_applicable'))
        await db.commit()
    try:
        for method in (authorize_patient, require_note_doctor):
            async with Session() as db:
                actor = await db.get(User, user_id)
                with pytest.raises(HTTPException) as exc:
                    if method is authorize_patient:
                        await method(db, actor, 'x' * 200, 'read_documents', 'postgres_invalid_profile')
                    else:
                        await method(db, actor, 'x' * 200, 'postgres_invalid_note')
                assert exc.value.status_code == 403
        async with Session() as db:
            audit(db, user_id, 'postgres_invalid_integration', 'credential:synthetic', 'denied', 'x' * 200)
            await db.commit()
            events = (await db.scalars(select(AuditEvent).where(AuditEvent.actor_id == user_id))).all()
            assert len(events) == 3 and all(event.patient_id is None for event in events)
            assert [event.resource for event in events if event.action != 'postgres_invalid_integration'] == ['patient:invalid', 'patient:invalid']
    finally:
        async with Session() as db:
            await db.execute(delete(AuditEvent).where(AuditEvent.actor_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['deactivate', 'unverify'])
async def test_patient_policy_observes_postgres_account_changes(dispose_connections, change):
    from fastapi import HTTPException
    from sqlalchemy import delete
    from app.main import authorize_patient
    from app.models import AuditEvent, Consent, Relationship
    patient_id, doctor_id, relationship_id = (str(uuid.uuid4()) for _ in range(3))
    async with Session() as db:
        db.add_all([
            User(id=patient_id, email=f'policy-patient-{patient_id}@example.test', full_name='Synthetic Patient', role='patient', password_hash='unused', verification_status='not_applicable'),
            User(id=doctor_id, email=f'policy-doctor-{doctor_id}@example.test', full_name='Synthetic Doctor', role='doctor', password_hash='unused', verification_status='verified'),
        ])
        await db.flush()
        db.add(Relationship(id=relationship_id, patient_id=patient_id, doctor_id=doctor_id, purpose='care'))
        await db.flush()
        db.add(Consent(relationship_id=relationship_id, scope='read_documents', purpose='care', expires_at=time.time() + 300))
        await db.commit()
    try:
        async with Session() as stale_session:
            stale = await stale_session.get(User, doctor_id)
            async with Session() as changing_session:
                current = await changing_session.get(User, doctor_id)
                if change == 'deactivate':
                    current.active = False
                else:
                    current.verification_status = 'pending'
                await changing_session.commit()
            assert stale.active and stale.verification_status == 'verified'
            stale.full_name = 'Uncommitted synthetic change'
            with pytest.raises(HTTPException) as exc:
                await authorize_patient(stale_session, stale, patient_id, 'read_documents', 'postgres_stale_account_test')
            assert exc.value.status_code == 403
        async with Session() as db:
            assert (await db.get(User, doctor_id)).full_name == 'Synthetic Doctor'
            event = await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == doctor_id, AuditEvent.action == 'postgres_stale_account_test'))
            assert event is not None and event.outcome == 'denied'
    finally:
        async with Session() as db:
            await db.execute(delete(AuditEvent).where(AuditEvent.actor_id == doctor_id))
            await db.execute(delete(Consent).where(Consent.relationship_id == relationship_id))
            await db.execute(delete(Relationship).where(Relationship.id == relationship_id))
            await db.execute(delete(User).where(User.id.in_([patient_id, doctor_id])))
            await db.commit()


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_concurrent_expired_claim_recovery(dispose_connections):
    from sqlalchemy import delete
    from app import outbox_dispatcher
    from app.models import Document, ExtractionJob, JobOutbox

    now = time.time()
    user_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    async with Session() as db:
        db.add(User(id=user_id, email=f"outbox-recovery-{user_id}@example.test", full_name="Synthetic Patient", role="patient", password_hash="unused", verification_status="not_applicable"))
        await db.flush()
        db.add(Document(id=document_id, patient_id=user_id, filename="synthetic.pdf", content_type="application/pdf", size=1))
        await db.flush()
        db.add(ExtractionJob(id=job_id, document_id=document_id, requested_by=user_id, status="running", started_at=now, claim_token=str(uuid.uuid4()), attempt_count=1))
        await db.flush()
        db.add(JobOutbox(kind="document_processing", target_id=job_id, status="published"))
        await db.commit()
    try:
        counts = await asyncio.gather(outbox_dispatcher.recover_stale(Session, now + 301, job_id), outbox_dispatcher.recover_stale(Session, now + 301, job_id))
        assert sum(counts) == 1
        async with Session() as db:
            job = await db.get(ExtractionJob, job_id)
            entry = await db.scalar(select(JobOutbox).where(JobOutbox.target_id == job_id))
            assert job.status == "queued" and job.claim_token is None and job.attempt_count == 1
            assert entry.status == "pending" and entry.next_attempt_at == now + 301
    finally:
        async with Session() as db:
            await db.execute(delete(JobOutbox).where(JobOutbox.target_id == job_id))
            await db.execute(delete(ExtractionJob).where(ExtractionJob.id == job_id))
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_concurrent_clinician_update_and_verification(cleanup_admins):
    from app.main import hash_password
    suffix = uuid.uuid4().hex
    password = "synthetic-password-123"
    admin_email = f"verify-admin-{suffix}@example.test"
    doctor_email = f"verify-doctor-{suffix}@example.test"
    cleanup_admins.append(admin_email)
    async with Session() as db:
        db.add(User(email=admin_email, full_name="Synthetic Admin", role="admin", password_hash=hash_password(password), verification_status="not_applicable"))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registration = await client.post("/auth/register", json={"email":doctor_email, "password":password, "full_name":"Synthetic Doctor", "role":"doctor"})
        assert registration.status_code == 201
        doctor_id = registration.json()["id"]
        admin_token = (await client.post("/auth/login", json={"email":admin_email, "password":password})).json()["access_token"]
        doctor_token = (await client.post("/auth/login", json={"email":doctor_email, "password":password})).json()["access_token"]
        ah, dh = {"Authorization":f"Bearer {admin_token}"}, {"Authorization":f"Bearer {doctor_token}"}
        snapshot = (await client.get(f"/doctors/{doctor_id}/profile", headers=ah)).json()
        verification = {"verified":True, "expected_profile_revision":snapshot["profile_revision"], "expected_verification_revision":snapshot["verification_revision"], "expected_status":snapshot["verification_status"]}
        decision, edit = await asyncio.gather(client.post(f"/admin/doctors/{doctor_id}/verification", headers=ah, json=verification), client.post("/profile/doctor", headers=dh, json={"specialization":"Synthetic update"}))
        assert decision.status_code in (200, 409), decision.text
        assert edit.status_code == 200, edit.text
        current = (await client.get(f"/doctors/{doctor_id}/profile", headers=ah)).json()
        assert current["profile_revision"] == 1 and current["verification_status"] == "pending"


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_admin_deactivation_serializes_with_login(cleanup_admins):
    from app.main import hash_password
    suffix=uuid.uuid4().hex
    password="synthetic-password-123"
    admin_email=f"suspend-admin-{suffix}@example.test"
    patient_email=f"suspend-patient-{suffix}@example.test"
    cleanup_admins.append(admin_email)
    async with Session() as db:
        db.add(User(email=admin_email, full_name="Synthetic Admin", role="admin", password_hash=hash_password(password), verification_status="not_applicable"))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        patient=await client.post("/auth/register", json={"email":patient_email,"password":password,"full_name":"Synthetic Patient","role":"patient"})
        assert patient.status_code == 201
        admin_login=await client.post("/auth/login", json={"email":admin_email,"password":password})
        headers={"Authorization":f"Bearer {admin_login.json()['access_token']}"}
        search=await client.post("/admin/users/search", headers=headers, json={"query":patient_email,"role":"patient"})
        assert search.status_code == 200 and [item["id"] for item in search.json()["users"]] == [patient.json()["id"]]
        suspension, login = await asyncio.gather(client.post(f"/admin/users/{patient.json()['id']}/deactivate", headers=headers, json={"reason":"security"}), client.post("/auth/login", json={"email":patient_email,"password":password}))
        assert suspension.status_code == 200 and login.status_code in (200,401)
        if login.status_code == 200:
            assert (await client.get("/me",headers={"Authorization":f"Bearer {login.json()['access_token']}"})).status_code == 401
            assert (await client.post("/auth/refresh",json={"refresh_token":login.json()['refresh_token']})).status_code == 401
        assert (await client.post("/auth/login",json={"email":patient_email,"password":password})).status_code == 401


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_doctor_worklist_queries_postgres():
    from app.models import ExtractionJob, StructuredExtractionVersion
    suffix = uuid.uuid4().hex
    password = "synthetic-password-123"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        users = {}
        for role in ("patient", "doctor"):
            email = f"worklist-{role}-{suffix}@example.test"
            response = await client.post("/auth/register", json={"email":email, "password":password, "full_name":"Synthetic User", "role":role})
            assert response.status_code == 201
            token = (await client.post("/auth/login", json={"email":email, "password":password})).json()["access_token"]
            users[role] = {"id":response.json()["id"], "headers":{"Authorization":f"Bearer {token}"}}
        patient, doctor = users["patient"], users["doctor"]
        async with Session() as db:
            (await db.get(User, doctor["id"])).verification_status = "verified"
            await db.commit()
        rel = (await client.post("/relationships", headers=patient["headers"], json={"doctor_id":doctor["id"], "purpose":"synthetic care"})).json()["id"]
        for scope in ("clinical_notes", "read_documents", "review_documents"):
            response = await client.post("/consents", headers=patient["headers"], json={"relationship_id":rel, "scope":scope, "purpose":"care", "expires_at":time.time()+300})
            assert response.status_code == 201
        upload = await client.post("/documents", headers=patient["headers"], files={"file":("synthetic.pdf",b"%PDF-synthetic","application/pdf")})
        assert upload.status_code == 201
        async with Session() as db:
            job = ExtractionJob(document_id=upload.json()["id"], requested_by=patient["id"], status="completed")
            db.add(job); await db.flush()
            db.add(StructuredExtractionVersion(job_id=job.id, version=1, review_status="unverified", data={}))
            await db.commit()
        note = await client.post(f"/patients/{patient['id']}/notes", headers=doctor["headers"], json={"content":"Synthetic note"})
        version_path = f"/notes/{note.json()['note_id']}/versions/{note.json()['version_id']}"
        assert (await client.post(version_path + "/submit", headers=doctor["headers"])).status_code == 200
        response = await client.post("/doctor/worklist", headers=doctor["headers"], json={})
        assert response.status_code == 200, response.text
        worklist = response.json()
        assert len(worklist["patients"]) == len(worklist["recent_uploads"]) == len(worklist["pending_notes"]) == len(worklist["pending_extractions"]) == 1
        notifications = await client.get("/notifications", headers=doctor["headers"])
        assert notifications.status_code == 200, notifications.text
        assert sum(item["kind"] == "note_review_requested" for item in notifications.json()) == 1
        assert any(item["kind"] == "relationship_created" for item in notifications.json())


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_assistant_reference_note_in_postgres(monkeypatch):
    from app import assistant_provider
    from app.models import AssistantTurn, ConversationMessage
    suffix = uuid.uuid4().hex
    password = "synthetic-password-123"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        users = {}
        for role in ("patient", "doctor"):
            email = f"assistant-note-{role}-{suffix}@example.test"
            response = await client.post("/auth/register", json={"email":email, "password":password, "full_name":"Synthetic User", "role":role})
            assert response.status_code == 201
            token = (await client.post("/auth/login", json={"email":email, "password":password})).json()["access_token"]
            users[role] = {"id":response.json()["id"], "auth":{"Authorization":f"Bearer {token}"}}
        patient, doctor = users["patient"], users["doctor"]
        async with Session() as db:
            (await db.get(User, doctor["id"])).verification_status = "verified"
            await db.commit()
        rel = (await client.post("/relationships", headers=patient["auth"], json={"doctor_id":doctor["id"], "purpose":"care"})).json()["id"]
        for scope in ("assistant", "clinical_notes", "read_documents"):
            assert (await client.post("/consents", headers=patient["auth"], json={"relationship_id":rel, "scope":scope, "purpose":"care", "expires_at":time.time()+300})).status_code == 201
        conversation = (await client.post("/conversations", headers=doctor["auth"], json={"patient_id":patient["id"]})).json()
        async with Session() as db:
            turn = AssistantTurn(conversation_id=conversation["id"], status="completed", include_documents=True, include_notes=False)
            db.add(turn); await db.flush()
            message = ConversationMessage(conversation_id=conversation["id"], turn_id=turn.id, role="assistant", content="Synthetic response", citations=[{"id":"document:synthetic", "title":"Synthetic document", "kind":"unverified_patient_document"}])
            other_message = ConversationMessage(conversation_id=conversation["id"], turn_id=turn.id, role="assistant", content="Another synthetic response", citations=[{"id":"document:synthetic", "title":"Synthetic document", "kind":"unverified_patient_document"}])
            db.add_all([message, other_message]); await db.flush(); message_id=message.id; other_message_id=other_message.id; await db.commit()
        created = await client.post(f"/conversations/{conversation['id']}/messages/{message_id}/note-draft", headers=doctor["auth"], json={"content":"Clinician-written synthetic note"})
        assert created.status_code == 201, created.text
        note = created.json()
        source = await client.get(f"/notes/{note['note_id']}/versions/{note['version_id']}/assistant-source", headers=doctor["auth"])
        assert source.status_code == 200 and source.json()["content"] == "Synthetic response"
        assert (await client.get(f"/patients/{patient['id']}/notes", headers=doctor["auth"])).json()[0]["assistant_referenced"]
        entered, release = asyncio.Event(), asyncio.Event()
        model_calls = []
        async def generate(question, history, sources, audience):
            model_calls.append(1)
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return assistant_provider.Draft(answer="Synthetic AI draft", citations=[sources[0]["id"]])
        monkeypatch.setattr(assistant_provider, "draft_answer", generate)
        generation_path = f"/conversations/{conversation['id']}/messages/{message_id}/generate-note-draft"
        payload = {"request_id":str(uuid.uuid4())}
        pending = asyncio.create_task(client.post(generation_path, headers=doctor["auth"], json=payload))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            in_flight = await client.post(generation_path, headers=doctor["auth"], json=payload)
            assert in_flight.status_code == 409, in_flight.text
        finally:
            release.set()
        generated = await pending
        assert generated.status_code == 201, generated.text
        item = generated.json()
        assert item["status"] == "draft" and item["ai_assisted"]
        replayed = await client.post(generation_path, headers=doctor["auth"], json=payload)
        assert replayed.status_code == 200 and replayed.json()["note_id"] == item["note_id"] and len(model_calls) == 1
        conflict = await client.post(f"/conversations/{conversation['id']}/messages/{other_message_id}/generate-note-draft", headers=doctor["auth"], json=payload)
        assert conflict.status_code == 409
        revised = await client.post(f"/notes/{item['note_id']}/versions/{item['version_id']}/revise", headers=doctor["auth"], json={"content":"Synthetic clinician revision"})
        assert revised.status_code == 201, revised.text
        assert revised.json()["ai_assisted"] and revised.json()["version"] == 2


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_approved_note_and_amendment_in_postgres():
    suffix = uuid.uuid4().hex
    password = "synthetic-password-123"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        users = {}
        for role in ("patient", "doctor"):
            email = f"{role}-{suffix}@example.test"
            response = await client.post("/auth/register", json={"email": email, "password": password, "full_name": "Synthetic User", "role": role})
            assert response.status_code == 201, response.text
            users[role] = response.json()
            token = await client.post("/auth/login", json={"email": email, "password": password})
            users[role]["auth"] = {"Authorization": f"Bearer {token.json()['access_token']}"}
        patient, doctor = users["patient"], users["doctor"]
        async with Session() as db:
            doctor_row = await db.get(User, doctor["id"])
            doctor_row.verification_status = "verified"
            await db.commit()
        relationship = await client.post("/relationships", json={"doctor_id": doctor["id"], "purpose": "synthetic care"}, headers=patient["auth"])
        assert relationship.status_code == 201, relationship.text
        consent = await client.post("/consents", json={"relationship_id": relationship.json()["id"], "scope": "clinical_notes", "purpose": "synthetic care", "expires_at": time.time() + 300}, headers=patient["auth"])
        assert consent.status_code == 201, consent.text
        path = f"/patients/{patient['id']}/notes"
        first = await client.post(path, json={"content": "Synthetic note"}, headers=doctor["auth"])
        assert first.status_code == 201, first.text
        draft = first.json()
        version_path = f"/notes/{draft['note_id']}/versions/{draft['version_id']}"
        assert (await client.post(version_path + "/submit", headers=doctor["auth"])).status_code == 200
        assert (await client.post(version_path + "/review", json={"decision": "approve"}, headers=doctor["auth"])).status_code == 200
        second = await client.post(f"/notes/{draft['note_id']}/versions", json={"content": "Synthetic amendment"}, headers=doctor["auth"])
        assert second.status_code == 201, second.text
        comparison = {"from_version_id":draft["version_id"], "to_version_id":second.json()["version_id"]}
        result = await client.post(f"/notes/{draft['note_id']}/compare", json=comparison, headers=doctor["auth"])
        assert result.status_code == 200 and [line["kind"] for line in result.json()["lines"]] == ["removed", "added"]
        assert (await client.post(f"/notes/{draft['note_id']}/compare", json=comparison, headers=patient["auth"])).status_code == 403
        assert (await client.get(path, headers=patient["auth"])).json()[0]["content"] == "Synthetic note"
        version_path = f"/notes/{draft['note_id']}/versions/{second.json()['version_id']}"
        assert (await client.post(version_path + "/submit", headers=doctor["auth"])).status_code == 200
        result = await client.post(version_path + "/review", json={"decision": "approve"}, headers=doctor["auth"])
        assert result.status_code == 200, result.text
        assert (await client.get(path, headers=patient["auth"])).json()[0]["content"] == "Synthetic amendment"


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL and running worker")
@pytest.mark.asyncio
async def test_extraction_review_in_postgres():
    suffix = uuid.uuid4().hex
    password = "synthetic-password-123"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        users = {}
        for role in ("patient", "doctor"):
            email = f"extract-{role}-{suffix}@example.test"
            result = await client.post("/auth/register", json={"email": email, "password": password, "full_name": "Synthetic User", "role": role})
            assert result.status_code == 201, result.text
            users[role] = result.json()
            token = await client.post("/auth/login", json={"email": email, "password": password})
            users[role]["auth"] = {"Authorization": f"Bearer {token.json()['access_token']}"}
        patient, doctor = users["patient"], users["doctor"]
        async with Session() as db:
            doctor_row = await db.get(User, doctor["id"])
            doctor_row.verification_status = "verified"
            await db.commit()
        rel = await client.post("/relationships", json={"doctor_id": doctor["id"], "purpose": "synthetic care"}, headers=patient["auth"])
        assert rel.status_code == 201, rel.text
        for scope in ("read_documents", "review_documents"):
            consent = await client.post("/consents", json={"relationship_id": rel.json()["id"], "scope": scope, "purpose": "synthetic care", "expires_at": time.time() + 300}, headers=patient["auth"])
            assert consent.status_code == 201, consent.text
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Test name: Synthetic glucose\nObserved value: 6.2\nUnit: mmol/L")
            contents = pdf.tobytes()
        uploaded = await client.post("/documents", files={"file": ("synthetic.pdf", contents, "application/pdf")}, headers=patient["auth"])
        assert uploaded.status_code == 201, uploaded.text
        path = f"/documents/{uploaded.json()['id']}/processing"
        started = await client.post(path, headers=patient["auth"])
        assert started.status_code == 202, started.text
        for _ in range(60):
            state = (await client.get(path, headers=doctor["auth"])).json()
            if state["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.5)
        assert state["status"] == "completed", state
        version = state["structured_versions"][0]
        assert version["data"]["laboratory_tests"][0]["observed_value"]["value"] == "6.2"
        version["data"]["laboratory_tests"][0]["unit"] = {"value": "reviewed unit", "origin": "clinician", "evidence": None}
        reviewed = await client.post(f"{path}/{started.json()['job_id']}/review", json={"expected_version": 1, "data": version["data"]}, headers=doctor["auth"])
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["version"] == 2
        history = (await client.get(path, headers=patient["auth"])).json()["structured_versions"]
        assert history[1]["data"]["laboratory_tests"][0]["unit"]["value"] == "mmol/L"


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL with pgvector")
@pytest.mark.asyncio
async def test_knowledge_semantic_search_filters_approval_and_retirement(monkeypatch, cleanup_admins):
    suffix = uuid.uuid4().hex
    password = "synthetic-password-123"
    cleanup_admins.append(f"knowledge-admin-{suffix}@example.test")
    async def fake_embeddings(texts):
        return [([1.0, 0.0] if "apple" in text.lower() else [0.0, 1.0]) + [0.0] * 382 for text in texts]
    async def fake_digest(): return "a" * 64
    monkeypatch.setattr(knowledge, "embedding_digest", fake_digest)
    monkeypatch.setattr(knowledge, "embed_texts", fake_embeddings)
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", f"synthetic-{suffix}")
    async with Session() as db:
        from app.main import hash_password
        db.add(User(email=f"knowledge-admin-{suffix}@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password(password), verification_status="not_applicable"))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_login = await client.post("/auth/login", json={"email": f"knowledge-admin-{suffix}@example.test", "password": password})
        admin_auth = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        patient_email = f"knowledge-patient-{suffix}@example.test"
        assert (await client.post("/auth/register", json={"email": patient_email, "password": password, "full_name": "Synthetic Patient", "role": "patient"})).status_code == 201
        patient_login = await client.post("/auth/login", json={"email": patient_email, "password": password})
        patient_auth = {"Authorization": f"Bearer {patient_login.json()['access_token']}"}
        template = {"publisher": "Synthetic publisher", "provenance": "synthetic://reference", "publication_date": "2025-01-01", "specialty": f"test-{suffix}", "version": "1", "effective_at": time.time() - 1}
        source_ids = []
        for title, content in (("Apple approved", "apple synthetic reference"), ("Apple draft", "apple unapproved reference"), ("Orange approved", "orange synthetic reference")):
            response = await client.post("/admin/knowledge", json={**template, "title": title, "content": content}, headers=admin_auth)
            assert response.status_code == 201, response.text
            source_id = response.json()["id"]
            source_ids.append(source_id)
            indexed = await client.post(f"/admin/knowledge/{source_id}/index", headers=admin_auth)
            assert indexed.status_code == 200, indexed.text
            if title != "Apple draft":
                assert (await client.post(f"/admin/knowledge/{source_id}/approve", headers=admin_auth)).status_code == 200
        response = await client.post("/knowledge/search", json={"query": "apple", "specialty": template["specialty"]}, headers=patient_auth)
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert [r["source"]["title"] for r in results] == ["Apple approved", "Orange approved"]
        assert results[0]["source"]["provenance"] == "synthetic://reference"
        credential = await client.post('/integration-credentials', json={'label':'Synthetic knowledge client', 'scopes':['knowledge:search'], 'expires_at':time.time()+300}, headers=patient_auth)
        assert credential.status_code == 201, credential.text
        token = {'Authorization': f"Bearer {credential.json()['token']}"}
        integrated = await client.post('/integration/knowledge/search', json={'query':'apple', 'specialty':template['specialty']}, headers=token)
        assert integrated.status_code == 200, integrated.text
        assert integrated.headers['cache-control'] == 'no-store'
        assert [hit['source']['title'] for hit in integrated.json()['results']] == ['Apple approved', 'Orange approved']
        assert (await client.post(f"/integration-credentials/{credential.json()['id']}/revoke", headers=patient_auth)).status_code == 200
        assert (await client.post('/integration/knowledge/search', json={'query':'apple'}, headers=token)).status_code == 401
        async def different_digest(): return "b" * 64
        monkeypatch.setattr(knowledge, "embedding_digest", different_digest)
        assert (await client.post("/knowledge/search", json={"query": "apple", "specialty": template["specialty"]}, headers=patient_auth)).json()["results"] == []
        monkeypatch.setattr(knowledge, "embedding_digest", fake_digest)
        future = await client.post("/admin/knowledge", json={**template, "title": "Future source", "content": "apple future reference", "effective_at": time.time() + 3600}, headers=admin_auth)
        future_id = future.json()["id"]
        assert (await client.post(f"/admin/knowledge/{future_id}/index", headers=admin_auth)).status_code == 200
        assert (await client.post(f"/admin/knowledge/{future_id}/approve", headers=admin_auth)).status_code == 200
        results = (await client.post("/knowledge/search", json={"query": "apple", "specialty": template["specialty"]}, headers=patient_auth)).json()["results"]
        assert "Future source" not in [r["source"]["title"] for r in results]
        assert (await client.post(f"/admin/knowledge/{source_ids[0]}/retire", headers=admin_auth)).status_code == 200
        results = (await client.post("/knowledge/search", json={"query": "apple", "specialty": template["specialty"]}, headers=patient_auth)).json()["results"]
        assert [r["source"]["title"] for r in results] == ["Orange approved"]
        monkeypatch.setenv("OLLAMA_EMBED_MODEL", "different-model")
        results = (await client.post("/knowledge/search", json={"query": "apple", "specialty": template["specialty"]}, headers=patient_auth)).json()["results"]
        assert results == []


@pytest.mark.skipif(os.getenv("MEDIAGENT_OLLAMA_TEST") != "1", reason="requires local Ollama embedding provider")
@pytest.mark.asyncio
async def test_real_embedding_provider_and_pgvector(cleanup_admins):
    suffix = uuid.uuid4().hex
    password = "synthetic-password-123"
    email = f"ollama-admin-{suffix}@example.test"
    cleanup_admins.append(email)
    async with Session() as db:
        from app.main import hash_password
        db.add(User(email=email, full_name="Synthetic Admin", role="admin", password_hash=hash_password(password), verification_status="not_applicable"))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = (await client.post("/auth/login", json={"email": email, "password": password})).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        source = {"title": "Synthetic integration reference", "publisher": "Test fixture", "provenance": "synthetic://test", "publication_date": "2025-01-01", "specialty": f"test-{suffix}", "version": "1", "effective_at": time.time()-1, "content": "Synthetic reference: blue whales live in the ocean."}
        created = await client.post("/admin/knowledge", json=source, headers=headers)
        assert created.status_code == 201, created.text
        source_id = created.json()["id"]
        try:
            indexed = await client.post(f"/admin/knowledge/{source_id}/index", headers=headers)
            assert indexed.status_code == 200, indexed.text
            assert (await client.post(f"/admin/knowledge/{source_id}/approve", headers=headers)).status_code == 200
            search = await client.post("/knowledge/search", json={"query": "ocean mammals", "specialty": source["specialty"]}, headers=headers)
            assert search.status_code == 200, search.text
            assert search.json()["results"][0]["source"]["id"] == source_id
        finally:
            await client.post(f"/admin/knowledge/{source_id}/retire", headers=headers)


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_document_summary_and_deletion_in_postgres(monkeypatch):
    from app import assistant_provider
    from app.models import DocumentSummary, ExtractedPage, ExtractionJob
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "synthetic-model")
    suffix=uuid.uuid4().hex
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        email=f"summary-postgres-{suffix}@example.test"
        credentials={"email":email,"password":"synthetic-password-123"}
        user=await client.post("/auth/register",json={**credentials,"full_name":"Synthetic Patient","role":"patient"})
        assert user.status_code==201, user.text
        login=await client.post("/auth/login",json=credentials)
        headers={"Authorization":f"Bearer {login.json()['access_token']}"}
        upload=await client.post("/documents",headers=headers,files={"file":("synthetic.pdf",b"%PDF-synthetic","application/pdf")})
        assert upload.status_code==201, upload.text
        doc_id=upload.json()["id"]
        async with Session() as db:
            job=ExtractionJob(document_id=doc_id,requested_by=user.json()["id"],status="completed")
            db.add(job); await db.flush(); job_id=job.id
            db.add(ExtractedPage(job_id=job_id,page_number=1,method="native",text="Synthetic source text")); await db.commit()
        async def draft(question, history, sources, audience):
            return assistant_provider.Draft(answer="Synthetic summary",citations=[sources[0]["id"]])
        monkeypatch.setattr(assistant_provider,"draft_answer",draft)
        path=f"/documents/{doc_id}/processing/{job_id}/summaries"
        request={"request_id":str(uuid.uuid4())}
        created=await client.post(path,headers=headers,json=request)
        assert created.status_code==201, created.text
        replay=await client.post(path,headers=headers,json=request)
        assert replay.status_code==200 and replay.json()["id"]==created.json()["id"]
        deleted=await client.post(f"/documents/{doc_id}/delete",headers=headers,json={"confirmation":"DELETE"})
        assert deleted.status_code==202, deleted.text
        async with Session() as db:
            assert not (await db.scalars(select(DocumentSummary).where(DocumentSummary.document_id==doc_id))).all()


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL and Redis")
@pytest.mark.asyncio
async def test_profile_and_consultation_integration_consent_in_postgres():
    suffix=uuid.uuid4().hex
    password="synthetic-password-123"
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        users={}
        for role in ("patient","doctor"):
            credentials={"email":f"history-integration-{role}-{suffix}@example.test","password":password}
            registered=await client.post("/auth/register",json={**credentials,"full_name":"Synthetic User","role":role})
            assert registered.status_code==201, registered.text
            login=await client.post("/auth/login",json=credentials)
            assert login.status_code==200, login.text
            users[role]={"id":registered.json()["id"],"headers":{"Authorization":f"Bearer {login.json()['access_token']}"}}
        patient,doctor=users["patient"],users["doctor"]
        async with Session() as db:
            (await db.get(User,doctor["id"])).verification_status="verified"; await db.commit()
        assert (await client.post("/profile/patient",headers=patient["headers"],json={"medical_history":"Synthetic history"})).status_code==200
        rel=(await client.post("/relationships",headers=patient["headers"],json={"doctor_id":doctor["id"],"purpose":"care"})).json()
        grants={}
        for scope in ("read_profile","profile_integration","consultations","consultations_integration"):
            grant=await client.post("/consents",headers=patient["headers"],json={"relationship_id":rel["id"],"scope":scope,"purpose":"care","expires_at":time.time()+300})
            assert grant.status_code==201, grant.text
            grants[scope]=grant.json()
        assert (await client.post(f"/patients/{patient['id']}/consultations",headers=doctor["headers"],json={"title":"Synthetic consultation","occurred_at":time.time()-1})).status_code==201
        credential=await client.post("/integration-credentials",headers=doctor["headers"],json={"label":"Synthetic client","patient_id":patient["id"],"scopes":["profile:read","consultations:read"],"expires_at":time.time()+300})
        assert credential.status_code==201, credential.text
        token={"Authorization":f"Bearer {credential.json()['token']}"}
        profile_path=f"/integration/patients/{patient['id']}/profile"
        consult_path=f"/integration/patients/{patient['id']}/consultations"
        assert (await client.get(profile_path,headers=token)).json()["medical_history"]=="Synthetic history"
        assert (await client.get(consult_path,headers=token)).json()[0]["title"]=="Synthetic consultation"
        assert (await client.post(f"/consents/{grants['profile_integration']['id']}/withdraw",headers=patient["headers"])).status_code==200
        assert (await client.get(profile_path,headers=token)).status_code==403
        assert (await client.get(consult_path,headers=token)).status_code==200


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_assistant_note_source_lock_serializes_with_amendment():
    from sqlalchemy import delete, update
    from fastapi import HTTPException
    from app.main import ensure_note_sources_current, locked_note
    from app.models import ClinicalNote, ClinicalNoteVersion
    user_id, note_id, version_id = (str(uuid.uuid4()) for _ in range(3))
    async with Session() as db:
        db.add(User(id=user_id, email=f"note-source-lock-{user_id}@example.test", full_name="Synthetic Patient", role="patient", password_hash="unused", verification_status="not_applicable"))
        await db.flush()
        db.add(ClinicalNote(id=note_id, patient_id=user_id)); await db.flush()
        db.add(ClinicalNoteVersion(id=version_id, note_id=note_id, version=1, content="Synthetic approved note", status="approved", author_id=user_id)); await db.commit()
    snapshot = [{"note_id": note_id, "version_id": version_id, "text": "Synthetic approved note"}]
    started, acquired = asyncio.Event(), asyncio.Event()
    async def approve_amendment():
        async with Session() as db:
            started.set()
            await locked_note(db, note_id)
            acquired.set()
            await db.execute(update(ClinicalNoteVersion).where(ClinicalNoteVersion.id == version_id).values(status="superseded"))
            await db.commit()
    task = None
    try:
        async with Session() as db:
            await ensure_note_sources_current(db, user_id, snapshot, lock=True)
            task = asyncio.create_task(approve_amendment())
            await started.wait()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(acquired.wait(), timeout=0.2)
            await db.commit()
        await asyncio.wait_for(task, timeout=5)
        async with Session() as db:
            with pytest.raises(HTTPException):
                await ensure_note_sources_current(db, user_id, snapshot)
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with Session() as db:
            await db.execute(delete(ClinicalNoteVersion).where(ClinicalNoteVersion.id == version_id))
            await db.execute(delete(ClinicalNote).where(ClinicalNote.id == note_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_relationship_restore_never_revives_racing_grants():
    suffix = uuid.uuid4().hex
    password = "synthetic-password-123"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        users = {}
        for role in ("patient", "doctor"):
            email = f"relationship-race-{role}-{suffix}@example.test"
            response = await client.post("/auth/register", json={"email":email,"password":password,"full_name":"Synthetic User","role":role})
            assert response.status_code == 201
            users[role] = response.json()
            token = (await client.post("/auth/login", json={"email":email,"password":password})).json()["access_token"]
            users[role]["headers"] = {"Authorization":f"Bearer {token}"}
        patient, doctor = users["patient"], users["doctor"]
        async with Session() as db:
            (await db.get(User, doctor["id"])).verification_status = "verified"
            await db.commit()
        relationship = await client.post("/relationships", headers=patient["headers"], json={"doctor_id":doctor["id"],"purpose":"synthetic care"})
        assert relationship.status_code == 201
        rel_id = relationship.json()["id"]
        grant_data = {"relationship_id":rel_id,"scope":"read_documents","purpose":"synthetic care","expires_at":time.time()+300}
        assert (await client.post("/consents", headers=patient["headers"], json=grant_data)).status_code == 201
        ending, grant = await asyncio.gather(client.post(f"/relationships/{rel_id}/end", headers=patient["headers"]), client.post("/consents", headers=patient["headers"], json=grant_data))
        assert ending.status_code == 200 and grant.status_code in (201,403), grant.text
        restored = await client.post(f"/relationships/{rel_id}/restore", headers=patient["headers"])
        assert restored.status_code == 200
        history = (await client.get("/consents", headers=patient["headers"])).json()
        assert all(item["status"] == "withdrawn" for item in history)
        assert (await client.get(f"/patients/{patient['id']}/documents", headers=doctor["headers"])).status_code == 403


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_vision_layer_persists_in_postgres(monkeypatch):
    import json
    from types import SimpleNamespace
    from app import processing
    from app.models import ExtractedPage, ExtractionJob
    monkeypatch.setenv("VISION_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_VISION_MODEL", "synthetic-vision")
    email=f"vision-postgres-{uuid.uuid4().hex}@example.test"
    password="synthetic-password-123"
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        registration=await client.post("/auth/register",json={"email":email,"password":password,"full_name":"Synthetic Patient","role":"patient"})
        assert registration.status_code == 201
        patient_id=registration.json()["id"]
        token=(await client.post("/auth/login",json={"email":email,"password":password})).json()["access_token"]
        headers={"Authorization":f"Bearer {token}"}
        capability=(await client.get("/document-processing/capabilities",headers=headers)).json()
        document=(await client.post("/documents",headers=headers,files={"file":("synthetic.pdf",b"%PDF-synthetic","application/pdf")})).json()
        path=f"/documents/{document['id']}/processing"
        async with Session() as db:
            job=ExtractionJob(document_id=document["id"],requested_by=patient_id,status="queued",vision_requested=True,vision_config_fingerprint=capability["configuration"])
            db.add(job); await db.commit(); job_id=job.id
        output={"pages":[{"page_number":1,"method":"native","text":"Test name: Synthetic test","vision_text":"Synthetic vision text","vision_warnings":["uncertain"]}],"processor_details":{"pipeline":"native-ocr-v2","pdf_ocr_mode":"fallback","ocr":None,"vision":{"provider":"ollama","model":"synthetic-vision","pages_processed":1,"truncated":False}}}
        monkeypatch.setattr(processing,"run_document_parser",lambda *args,**kwargs:SimpleNamespace(stdout=json.dumps(output).encode()))
        await processing.process_job(job_id)
        result=(await client.get(path,headers=headers)).json()
        assert result["status"]=="completed" and result["pages"][0]["vision_text"]=="Synthetic vision text"
        async with Session() as db:
            page=await db.scalar(select(ExtractedPage).where(ExtractedPage.job_id==job_id))
            assert page.vision_text=="Synthetic vision text" and page.vision_warnings==["uncertain"]


@pytest.mark.skipif(os.getenv("MEDIAGENT_POSTGRES_TEST") != "1", reason="requires migrated PostgreSQL")
@pytest.mark.asyncio
async def test_refresh_and_logout_race_revokes_session():
    email=f"logout-race-{uuid.uuid4().hex}@example.test"
    password="synthetic-password-123"
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.post("/auth/register",json={"email":email,"password":password,"full_name":"Synthetic Patient","role":"patient"})).status_code == 201
        login=await client.post("/auth/login",json={"email":email,"password":password})
        token=login.json()["refresh_token"]
        refreshed,logout=await asyncio.gather(client.post("/auth/refresh",json={"refresh_token":token}),client.post("/auth/logout",json={"refresh_token":token}))
        assert logout.status_code==204, logout.text
        assert refreshed.status_code in (200,401), refreshed.text
        if refreshed.status_code == 200:
            assert (await client.post("/auth/refresh",json={"refresh_token":refreshed.json()["refresh_token"]})).status_code == 401
            assert (await client.get("/me",headers={"Authorization":f"Bearer {refreshed.json()['access_token']}"})).status_code == 401


@pytest.mark.skipif(os.getenv('MEDIAGENT_POSTGRES_TEST') != '1', reason='requires migrated PostgreSQL')
@pytest.mark.asyncio
async def test_concurrent_clinical_knowledge_review_is_recorded_once(monkeypatch, cleanup_admins):
    from app import main
    from app.main import hash_password
    suffix=uuid.uuid4().hex
    password='synthetic-password-123'
    admin_email=f'knowledge-review-admin-{suffix}@example.test'
    cleanup_admins.append(admin_email)
    monkeypatch.setattr(main,'KNOWLEDGE_CLINICAL_REVIEW_POLICY','required')
    async def embed(texts): return [[1.0]+[0.0]*383 for _ in texts]
    async def digest(): return 'a'*64
    monkeypatch.setattr(knowledge,'embed_texts',embed)
    monkeypatch.setattr(knowledge,'embedding_digest',digest)
    async with Session() as db:
        db.add(User(email=admin_email,full_name='Synthetic Admin',role='admin',password_hash=hash_password(password),verification_status='not_applicable'))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        doctor_email=f'knowledge-review-doctor-{suffix}@example.test'
        doctor=(await client.post('/auth/register',json={'email':doctor_email,'password':password,'full_name':'Synthetic Doctor','role':'doctor'})).json()
        async with Session() as db:
            (await db.get(User,doctor['id'])).verification_status='verified'
            await db.commit()
        async def headers(email):
            response=await client.post('/auth/login',json={'email':email,'password':password})
            assert response.status_code==200, response.text
            return {'Authorization':f"Bearer {response.json()['access_token']}"}
        ah,dh=await headers(admin_email),await headers(doctor_email)
        source={'title':'Synthetic reference','publisher':'Synthetic publisher','provenance':'synthetic://test','publication_date':'2025-01-01','specialty':'general','version':'1','content':'Synthetic reference text','effective_at':time.time()-1}
        created=await client.post('/admin/knowledge',headers=ah,json=source)
        assert created.status_code==201, created.text
        source_id=created.json()['id']
        indexed=await client.post(f'/admin/knowledge/{source_id}/index',headers=ah)
        assert indexed.status_code==200, indexed.text
        assignment=await client.post(f'/admin/knowledge/{source_id}/clinical-reviews',headers=ah,json={'doctor_id':doctor['id']})
        assert assignment.status_code==201, assignment.text
        url=f"/knowledge/clinical-reviews/{assignment.json()['id']}/decision"
        responses=await asyncio.gather(*(client.post(url,headers=dh,json={'decision':'recommended'}) for _ in range(2)))
        assert sorted(response.status_code for response in responses)==[200,409]
        approved=await client.post(f'/admin/knowledge/{source_id}/approve',headers=ah)
        assert approved.status_code==200, approved.text


@pytest.mark.skipif(os.getenv('MEDIAGENT_POSTGRES_TEST') != '1', reason='requires migrated PostgreSQL')
@pytest.mark.asyncio
async def test_concurrent_background_index_claims_write_once(monkeypatch, dispose_connections):
    from datetime import date
    from sqlalchemy import delete
    from app import knowledge_indexing
    from app.models import AuditEvent, KnowledgeChunk, KnowledgeIndexJob, KnowledgeSource, Session as UserSession
    suffix = str(uuid.uuid4())
    user_id, session_id, source_id, job_id = (str(uuid.uuid4()) for _ in range(4))
    content = 'Synthetic concurrent indexing text'
    async with Session() as db:
        db.add(User(id=user_id,email=f'index-concurrent-{suffix}@example.test',full_name='Synthetic Admin',role='admin',password_hash='unused',verification_status='not_applicable'))
        await db.flush()
        db.add(UserSession(id=session_id,user_id=user_id,refresh_hash=uuid.uuid4().hex,expires_at=time.time()+3600))
        db.add(KnowledgeSource(id=source_id,family_id=source_id,title='Synthetic',publisher='Synthetic',provenance='synthetic://test',publication_date=date(2025,1,1),specialty='general',version='1',content=content,status='draft',index_status='not_indexed',effective_at=time.time()-1,created_by=user_id))
        await db.flush()
        db.add(KnowledgeIndexJob(id=job_id,source_id=source_id,requested_by=user_id,session_id=session_id,content_hash=knowledge_indexing.content_hash(content),model=knowledge.embedding_model(),status='queued',next_dispatch_at=time.time()+3600))
        await db.commit()
    calls = []
    async def embed(texts):
        calls.append(1)
        await asyncio.sleep(.05)
        return [[1.0]+[0.0]*383 for _ in texts]
    async def digest(): return 'a'*64
    monkeypatch.setattr(knowledge, 'embed_texts', embed)
    monkeypatch.setattr(knowledge, 'embedding_digest', digest)
    try:
        await asyncio.gather(knowledge_indexing.process_index(job_id), knowledge_indexing.process_index(job_id))
        async with Session() as db:
            job = await db.get(KnowledgeIndexJob, job_id)
            assert job.status == 'completed' and job.attempt_count == 1
            assert len(calls) == 1
            assert len((await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))).all()) == 1
    finally:
        async with Session() as db:
            await db.execute(delete(AuditEvent).where(AuditEvent.actor_id == user_id))
            await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))
            await db.execute(delete(KnowledgeIndexJob).where(KnowledgeIndexJob.id == job_id))
            await db.execute(delete(KnowledgeSource).where(KnowledgeSource.id == source_id))
            await db.execute(delete(UserSession).where(UserSession.id == session_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


@pytest.mark.skipif(os.getenv('MEDIAGENT_POSTGRES_TEST') != '1', reason='requires migrated PostgreSQL')
@pytest.mark.asyncio
async def test_concurrent_object_intent_recording_selects_one_version(dispose_connections):
    import hashlib
    from sqlalchemy import delete
    from app import object_upload_intents as intents
    from app.models import ObjectUploadIntent
    from app.s3_storage import ObjectRef
    actor_id=str(uuid.uuid4()); object_id=str(uuid.uuid4()); digest=hashlib.sha256(b'synthetic').hexdigest()
    async with Session() as db:
        db.add(User(id=actor_id,email=f'object-intent-{actor_id}@example.test',full_name='Synthetic',role='patient',password_hash='unused',verification_status='not_applicable'))
        await db.commit()
    intent_id=await intents.prepare(Session,actor_id=actor_id,store_id='a'*64,bucket='synthetic-private-bucket',namespace='documents',object_id=object_id,size=9,sha256=digest)
    refs=[ObjectRef('synthetic-private-bucket','documents/'+object_id,version,9,digest) for version in ('v1','v2')]
    try:
        results=await asyncio.gather(*(intents.record(Session,intent_id,store_id='a'*64,ref=ref) for ref in refs),return_exceptions=True)
        assert sum(result is None for result in results)==1
        assert sum(isinstance(result,ValueError) for result in results)==1
        async with Session() as db:
            intent=await db.get(ObjectUploadIntent,intent_id)
            assert intent.status=='stored' and intent.version_id in ('v1','v2')
    finally:
        async with Session() as db:
            await db.execute(delete(ObjectUploadIntent).where(ObjectUploadIntent.id==intent_id))
            await db.execute(delete(User).where(User.id==actor_id))
            await db.commit()


@pytest.mark.skipif(os.getenv('MEDIAGENT_POSTGRES_TEST') != '1', reason='requires migrated PostgreSQL')
@pytest.mark.asyncio
async def test_concurrent_document_object_completion_is_idempotent(dispose_connections):
    import hashlib
    from sqlalchemy import delete
    from app import object_document_submissions as submissions, object_upload_intents as intents
    from app.models import AuditEvent, Document, DocumentObjectLink, ObjectDocumentSubmission, ObjectUploadIntent, Session as UserSession
    from app.s3_storage import ObjectRef
    actor_id, session_id, object_id = (str(uuid.uuid4()) for _ in range(3))
    digest=hashlib.sha256(b'synthetic').hexdigest()
    async with Session() as db:
        db.add(User(id=actor_id,email=f'document-object-{actor_id}@example.test',full_name='Synthetic',role='patient',password_hash='unused',verification_status='not_applicable'))
        await db.flush()
        db.add(UserSession(id=session_id,user_id=actor_id,refresh_hash=uuid.uuid4().hex,expires_at=time.time()+3600))
        await db.commit()
    intent_id=await submissions.prepare_document(Session,actor_id=actor_id,session_id=session_id,store_id='a'*64,bucket='synthetic-private-bucket',object_id=object_id,size=9,sha256=digest,filename='synthetic.pdf',content_type='application/pdf',scan_status='not_scanned',correlation_id=str(uuid.uuid4()))
    await intents.record(Session,intent_id,store_id='a'*64,ref=ObjectRef('synthetic-private-bucket','documents/'+object_id,'v1',9,digest))
    try:
        results=await asyncio.gather(*(submissions.complete_document(Session,actor_id=actor_id,intent_id=intent_id,store_id='a'*64) for _ in range(2)))
        assert results==[object_id,object_id]
        async with Session() as db:
            assert (await db.get(DocumentObjectLink,object_id)).intent_id==intent_id
            events=(await db.scalars(select(AuditEvent).where(AuditEvent.actor_id==actor_id,AuditEvent.action=='object_document_upload_completed'))).all()
            assert len(events)==1
    finally:
        async with Session() as db:
            await db.execute(delete(AuditEvent).where(AuditEvent.actor_id==actor_id))
            await db.execute(delete(DocumentObjectLink).where(DocumentObjectLink.document_id==object_id))
            await db.execute(delete(Document).where(Document.id==object_id))
            await db.execute(delete(ObjectDocumentSubmission).where(ObjectDocumentSubmission.intent_id==intent_id))
            await db.execute(delete(ObjectUploadIntent).where(ObjectUploadIntent.id==intent_id))
            await db.execute(delete(UserSession).where(UserSession.id==session_id))
            await db.execute(delete(User).where(User.id==actor_id))
            await db.commit()


@pytest.mark.skipif(os.getenv('MEDIAGENT_POSTGRES_TEST') != '1', reason='requires migrated PostgreSQL')
@pytest.mark.asyncio
async def test_worklist_category_pages_on_postgresql(dispose_connections):
    from fastapi import HTTPException, Response
    from sqlalchemy import delete
    from app.main import WorklistSearch, doctor_worklist
    from app.models import AuditEvent, ClinicalNote, ClinicalNoteVersion, Consent, Document, ExtractionJob, Relationship, StructuredExtractionVersion

    doctor_id, patient_id, rel_id = (str(uuid.uuid4()) for _ in range(3))
    document_ids, note_ids, job_ids = [], [], []
    try:
        async with Session() as db:
            db.add_all([User(id=doctor_id, email=f'worklist-doctor-{doctor_id}@example.test', full_name='Synthetic Doctor', role='doctor', password_hash='unused', verification_status='verified'),
                        User(id=patient_id, email=f'worklist-patient-{patient_id}@example.test', full_name='Synthetic Patient', role='patient', password_hash='unused', verification_status='not_applicable')])
            await db.flush()
            db.add(Relationship(id=rel_id, patient_id=patient_id, doctor_id=doctor_id, purpose='care', status='active'))
            await db.flush()
            for scope in ('read_documents', 'review_documents', 'clinical_notes'):
                db.add(Consent(relationship_id=rel_id, scope=scope, purpose='care', status='active', effective_at=time.time()-1, expires_at=time.time()+3600))
            for _ in range(51):
                document_id, note_id, job_id = (str(uuid.uuid4()) for _ in range(3))
                document_ids.append(document_id); note_ids.append(note_id); job_ids.append(job_id)
                db.add_all([Document(id=document_id, patient_id=patient_id, filename='synthetic.pdf', content_type='application/pdf', size=1, created_at=1000), ClinicalNote(id=note_id, patient_id=patient_id)])
            await db.flush()
            for document_id, note_id, job_id in zip(document_ids, note_ids, job_ids):
                db.add_all([ExtractionJob(id=job_id, document_id=document_id, requested_by=patient_id, status='completed', created_at=1000), ClinicalNoteVersion(id=str(uuid.uuid4()), note_id=note_id, version=1, content='Synthetic', status='pending_review', author_id=doctor_id, created_at=1000)])
            await db.flush()
            db.add_all([StructuredExtractionVersion(job_id=job_id, version=1, data={}, review_status='unverified') for job_id in job_ids])
            await db.commit()
        async with Session() as db:
            doctor = await db.get(User, doctor_id)
            first = await doctor_worklist(WorklistSearch(), Response(), doctor, db)
            assert all(len(first[key]) == 50 for key in ('recent_uploads', 'pending_notes', 'pending_extractions'))
            cursors = {'upload_cursor':first['next_upload_cursor'], 'note_cursor':first['next_note_cursor'], 'extraction_cursor':first['next_extraction_cursor']}
            assert all(cursors.values())
            # Cursors remain usable after their source item leaves the active queue.
            (await db.get(ClinicalNoteVersion, str(cursors['note_cursor']))).status = 'rejected'
            await db.commit()
            second = await doctor_worklist(WorklistSearch(**cursors), Response(), doctor, db)
            for key, identity in [('recent_uploads','document_id'), ('pending_notes','version_id'), ('pending_extractions','job_id')]:
                assert len(second[key]) == 1
                assert len({item[identity] for item in first[key] + second[key]}) == 51
            (await db.get(Document, str(cursors['upload_cursor']))).deleted_at = time.time()
            await db.commit()
            after_deletion = await doctor_worklist(WorklistSearch(upload_cursor=cursors['upload_cursor']), Response(), doctor, db)
            assert len(after_deletion['recent_uploads']) == 1
            grant = await db.scalar(select(Consent).where(Consent.relationship_id == rel_id, Consent.scope == 'read_documents'))
            grant.status = 'withdrawn'
            await db.commit()
            with pytest.raises(HTTPException) as exc:
                await doctor_worklist(WorklistSearch(upload_cursor=cursors['upload_cursor']), Response(), doctor, db)
            assert exc.value.status_code == 422
    finally:
        async with Session() as db:
            await db.execute(delete(AuditEvent).where(AuditEvent.actor_id == doctor_id))
            await db.execute(delete(StructuredExtractionVersion).where(StructuredExtractionVersion.job_id.in_(job_ids)))
            await db.execute(delete(ExtractionJob).where(ExtractionJob.id.in_(job_ids)))
            await db.execute(delete(ClinicalNoteVersion).where(ClinicalNoteVersion.note_id.in_(note_ids)))
            await db.execute(delete(ClinicalNote).where(ClinicalNote.id.in_(note_ids)))
            await db.execute(delete(Document).where(Document.id.in_(document_ids)))
            await db.execute(delete(Consent).where(Consent.relationship_id == rel_id))
            await db.execute(delete(Relationship).where(Relationship.id == rel_id))
            await db.execute(delete(User).where(User.id.in_([doctor_id, patient_id])))
            await db.commit()


@pytest.mark.skipif(os.getenv('MEDIAGENT_POSTGRES_TEST') != '1', reason='requires migrated PostgreSQL')
@pytest.mark.parametrize('deactivate_first', [True, False])
@pytest.mark.asyncio
async def test_processing_completion_serializes_with_deactivation(dispose_connections, monkeypatch, tmp_path, deactivate_first):
    import hashlib
    import json
    from types import SimpleNamespace
    from sqlalchemy import delete, update
    from app import processing
    from app.models import AuditEvent, Document, ExtractedPage, ExtractionJob, Notification, StructuredExtractionVersion

    patient_id, document_id, job_id = (str(uuid.uuid4()) for _ in range(3))
    content = b'%PDF-synthetic'
    (tmp_path / document_id).write_bytes(content)
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path))
    monkeypatch.setattr(processing, 'run_document_parser', lambda args: SimpleNamespace(stdout=json.dumps({'pages':[{'page_number':1, 'method':'native', 'text':'Synthetic content'}]})))
    reached, release = asyncio.Event(), asyncio.Event()
    original_lock = processing.lock_processing_owner
    async def gated_lock(db, user_id):
        if deactivate_first:
            reached.set()
            await release.wait()
            return await original_lock(db, user_id)
        owner = await original_lock(db, user_id)
        reached.set()
        await release.wait()
        return owner
    monkeypatch.setattr(processing, 'lock_processing_owner', gated_lock)
    worker = None
    deactivate = None
    try:
        async with Session() as db:
            db.add(User(id=patient_id, email=f'worker-deactivation-{patient_id}@example.test', full_name='Synthetic Patient', role='patient', password_hash='unused', verification_status='not_applicable'))
            await db.flush()
            db.add(Document(id=document_id, patient_id=patient_id, filename='synthetic.pdf', content_type='application/pdf', size=len(content), sha256=hashlib.sha256(content).hexdigest()))
            await db.flush()
            db.add(ExtractionJob(id=job_id, document_id=document_id, requested_by=patient_id, status='queued'))
            await db.commit()
        async def deactivate_account():
            async with Session() as db:
                await db.execute(update(User).where(User.id == patient_id).values(active=False))
                await db.commit()
        worker = asyncio.create_task(processing.process_job(job_id))
        await asyncio.wait_for(reached.wait(), 10)
        deactivate = asyncio.create_task(deactivate_account())
        if deactivate_first:
            await asyncio.wait_for(deactivate, 5)
        else:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(deactivate), 0.2)
        release.set()
        await asyncio.wait_for(worker, 10)
        await asyncio.wait_for(deactivate, 5)
        async with Session() as db:
            job = await db.get(ExtractionJob, job_id)
            pages = (await db.scalars(select(ExtractedPage).where(ExtractedPage.job_id == job_id))).all()
            assert not (await db.get(User, patient_id)).active
            assert job.status == ('failed' if deactivate_first else 'completed')
            assert len(pages) == (0 if deactivate_first else 1)
    finally:
        release.set()
        for task in (worker, deactivate):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with Session() as db:
            await db.execute(delete(Notification).where(Notification.recipient_id == patient_id))
            await db.execute(delete(AuditEvent).where(AuditEvent.actor_id == patient_id))
            await db.execute(delete(StructuredExtractionVersion).where(StructuredExtractionVersion.job_id == job_id))
            await db.execute(delete(ExtractedPage).where(ExtractedPage.job_id == job_id))
            await db.execute(delete(ExtractionJob).where(ExtractionJob.id == job_id))
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(delete(User).where(User.id == patient_id))
            await db.commit()


@pytest.mark.skipif(os.getenv('MEDIAGENT_POSTGRES_TEST') != '1', reason='requires migrated PostgreSQL')
@pytest.mark.parametrize('first', ['discard', 'complete'])
@pytest.mark.asyncio
async def test_discard_and_completion_are_mutually_exclusive(dispose_connections, monkeypatch, first):
    import hashlib
    from sqlalchemy import delete
    from app import object_document_submissions as submissions, object_upload_intents as intents
    from app.models import AuditEvent, Document, DocumentObjectLink, ObjectDocumentSubmission, ObjectUploadDiscard, ObjectUploadIntent, Session as UserSession
    from app.s3_storage import ObjectRef

    actor_id, session_id, object_id = (str(uuid.uuid4()) for _ in range(3))
    digest = hashlib.sha256(b'synthetic').hexdigest()
    intent_id = None
    first_task = second_task = None
    held, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = submissions.locked_patient_session
    async def gate(db, owner, session):
        if asyncio.current_task().get_name() == 'first-action':
            await original(db, owner, session)
            held.set()
            await release.wait()
        else:
            second_started.set()
            await original(db, owner, session)
    try:
        async with Session() as db:
            db.add(User(id=actor_id, email=f'discard-race-{actor_id}@example.test', full_name='Synthetic', role='patient', password_hash='unused', verification_status='not_applicable'))
            await db.flush()
            db.add(UserSession(id=session_id, user_id=actor_id, refresh_hash=uuid.uuid4().hex, expires_at=time.time()+3600))
            await db.commit()
        intent_id = await submissions.prepare_document(Session, actor_id=actor_id, session_id=session_id, store_id='a'*64,
            bucket='synthetic-private-bucket', object_id=object_id, size=9, sha256=digest, filename='synthetic.pdf',
            content_type='application/pdf', scan_status='not_scanned', correlation_id=str(uuid.uuid4()))
        await intents.record(Session, intent_id, store_id='a'*64,
            ref=ObjectRef('synthetic-private-bucket', 'documents/'+object_id, 'v1', 9, digest))
        monkeypatch.setattr(submissions, 'locked_patient_session', gate)
        def discard():
            return submissions.discard_document(Session, actor_id=actor_id, session_id=session_id, intent_id=intent_id, correlation_id=str(uuid.uuid4()))
        def complete():
            return submissions.complete_document(Session, actor_id=actor_id, intent_id=intent_id, store_id='a'*64)
        first_task = asyncio.create_task(discard() if first == 'discard' else complete(), name='first-action')
        await asyncio.wait_for(held.wait(), 5)
        second_task = asyncio.create_task(complete() if first == 'discard' else discard(), name='second-action')
        await asyncio.wait_for(second_started.wait(), 5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_task), .2)
        release.set()
        first_result = await asyncio.wait_for(first_task, 5)
        with pytest.raises(ValueError):
            await asyncio.wait_for(second_task, 5)
        assert first_result == (None if first == 'discard' else object_id)
        async with Session() as db:
            marker = await db.get(ObjectUploadDiscard, intent_id)
            document = await db.get(Document, object_id)
            link = await db.get(DocumentObjectLink, object_id)
            if first == 'discard':
                assert marker is not None and document is None and link is None
            else:
                assert marker is None and document is not None and link.intent_id == intent_id
    finally:
        release.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with Session() as db:
            await db.execute(delete(AuditEvent).where(AuditEvent.actor_id == actor_id))
            await db.execute(delete(DocumentObjectLink).where(DocumentObjectLink.document_id == object_id))
            await db.execute(delete(Document).where(Document.id == object_id))
            if intent_id:
                await db.execute(delete(ObjectUploadDiscard).where(ObjectUploadDiscard.intent_id == intent_id))
                await db.execute(delete(ObjectDocumentSubmission).where(ObjectDocumentSubmission.intent_id == intent_id))
                await db.execute(delete(ObjectUploadIntent).where(ObjectUploadIntent.id == intent_id))
            await db.execute(delete(UserSession).where(UserSession.id == session_id))
            await db.execute(delete(User).where(User.id == actor_id))
            await db.commit()
