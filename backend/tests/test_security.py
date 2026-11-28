import os
import asyncio
import time
import uuid

import pytest
import pytest_asyncio
import pymupdf
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_db
from app.main import app, hash_password
from app.models import AuditEvent, CareMessage, Consent, Document, ExtractionJob, IntegrationCredential, JobOutbox, PasswordReset, User
from app import integration_rate_limit, knowledge, outbox_dispatcher, password_reset, processing
from sqlalchemy import select


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    async def allow_auth(*args):
        return True
    monkeypatch.setattr("app.auth_rate_limit.check", allow_auth)
    monkeypatch.setattr("app.main.UPLOAD_DIR", tmp_path / "uploads")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with session_maker() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, session_maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def register(http, email, role):
    response = await http.post("/auth/register", json={"email": email, "password": "long-enough-password", "full_name": "Synthetic User", "role": role})
    assert response.status_code == 201, response.text
    return response.json()


async def login(http, email):
    response = await http.post("/auth/login", json={"email": email, "password": "long-enough-password"})
    assert response.status_code == 200, response.text
    return response.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_patient_profile_and_denials_are_not_cacheable(client):
    http, _ = client
    patient = await register(http, 'cache-patient@example.test', 'patient')
    other = await register(http, 'cache-other@example.test', 'patient')
    headers = auth((await login(http, patient['email']))['access_token'])
    for patient_id, status in [(patient['id'], 200), (other['id'], 403)]:
        response = await http.get(f'/patients/{patient_id}/profile', headers=headers)
        assert response.status_code == status
        assert response.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['deactivate', 'unverify'])
async def test_patient_policy_rechecks_stale_clinician_account(client, change):
    from fastapi import HTTPException
    from app.main import authorize_patient
    http, sessions = client
    patient = await register(http, f'policy-patient-{change}@example.test', 'patient')
    doctor = await register(http, f'policy-doctor-{change}@example.test', 'doctor')
    ph = auth((await login(http, patient['email']))['access_token'])
    async with sessions() as db:
        (await db.get(User, doctor['id'])).verification_status = 'verified'
        await db.commit()
    relationship = (await http.post('/relationships', headers=ph, json={'doctor_id': doctor['id'], 'purpose': 'care'})).json()
    consent = await http.post('/consents', headers=ph, json={'relationship_id': relationship['id'], 'scope': 'read_documents', 'purpose': 'care', 'expires_at': time.time() + 300})
    assert consent.status_code == 201
    async with sessions() as stale_session:
        stale_doctor = await stale_session.get(User, doctor['id'])
        async with sessions() as changing_session:
            current = await changing_session.get(User, doctor['id'])
            if change == 'deactivate':
                current.active = False
            else:
                current.verification_status = 'pending'
            await changing_session.commit()
        assert stale_doctor.active and stale_doctor.verification_status == 'verified'
        with pytest.raises(HTTPException) as exc:
            await authorize_patient(stale_session, stale_doctor, patient['id'], 'read_documents', 'stale_account_test')
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_patient_access_denial_rolls_back_pending_writes_and_audits(client):
    from fastapi import HTTPException
    from app.main import authorize_patient
    http, sessions = client
    actor = await register(http, 'rollback-actor@example.test', 'patient')
    other = await register(http, 'rollback-other@example.test', 'patient')
    async with sessions() as db:
        user = await db.get(User, actor['id'])
        user.full_name = 'Uncommitted change'
        with pytest.raises(HTTPException) as exc:
            await authorize_patient(db, user, other['id'], 'read_documents', 'rollback_access_test')
        assert exc.value.status_code == 403
    async with sessions() as db:
        assert (await db.get(User, actor['id'])).full_name == 'Synthetic User'
        event = await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == actor['id'], AuditEvent.action == 'rollback_access_test'))
        assert event is not None and event.outcome == 'denied'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['note', 'message', 'conversation'])
async def test_other_access_denials_roll_back_pending_writes(client, kind):
    from fastapi import HTTPException
    from app.main import authorize_care_messages, conversation_access, require_note_doctor
    from app.models import Conversation
    http, sessions = client
    actor = await register(http, f'denial-{kind}@example.test', 'patient')
    other = await register(http, f'denial-other-{kind}@example.test', 'patient')
    async with sessions() as db:
        conversation = Conversation(patient_id=other['id'], owner_id=other['id'])
        db.add(conversation)
        await db.commit()
        conversation_id = conversation.id
    action = {'note': 'note_denial_test', 'message': 'care_message_access', 'conversation': 'conversation_denial_test'}[kind]
    async with sessions() as db:
        user = await db.get(User, actor['id'])
        conversation = await db.get(Conversation, conversation_id)
        user.full_name = 'Uncommitted change'
        with pytest.raises(HTTPException) as exc:
            if kind == 'note':
                await require_note_doctor(db, user, other['id'], action)
            elif kind == 'message':
                await authorize_care_messages(db, user, str(uuid.uuid4()), sending=True)
            else:
                await conversation_access(db, user, conversation, action)
        assert exc.value.status_code == 403
    async with sessions() as db:
        assert (await db.get(User, actor['id'])).full_name == 'Synthetic User'
        event = await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == actor['id'], AuditEvent.action == action))
        assert event is not None and event.outcome == 'denied'


@pytest.mark.asyncio
@pytest.mark.parametrize('patient_id', ['not-a-patient', 'x' * 200])
async def test_invalid_patient_identifier_is_denied_and_not_copied_to_audit(client, patient_id):
    http, sessions = client
    actor = await register(http, f'invalid-patient-{len(patient_id)}@example.test', 'patient')
    headers = auth((await login(http, actor['email']))['access_token'])
    result = await http.get(f'/patients/{patient_id}/profile', headers=headers)
    assert result.status_code == 403
    note_result = await http.post(f'/patients/{patient_id}/notes', headers=headers, json={'content': 'Synthetic note'})
    assert note_result.status_code == 403
    async with sessions() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.actor_id == actor['id'], AuditEvent.action.in_(['profile_read', 'note_create'])))).all()
        assert len(events) == 2
        assert all(event.resource == 'patient:invalid' and event.patient_id is None and event.outcome == 'denied' for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('document_id', ['synthetic-private-input', 'x' * 200])
async def test_invalid_document_identifier_is_not_copied_to_audit(client, document_id):
    http, sessions = client
    actor = await register(http, f'invalid-document-{len(document_id)}@example.test', 'patient')
    headers = auth((await login(http, actor['email']))['access_token'])
    result = await http.post(f'/documents/{document_id}/processing', headers=headers)
    assert result.status_code == 403
    async with sessions() as db:
        event = await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == actor['id'], AuditEvent.action == 'document_processing_request'))
        assert event.resource == 'document:invalid' and event.outcome == 'denied'


@pytest.mark.asyncio
async def test_password_reset_is_single_use_and_revokes_credentials(client, monkeypatch):
    http, sessions = client
    queued = []
    user = await register(http, "reset@example.test", "patient")
    old = await login(http, user["email"])
    async with sessions() as db:
        credential = IntegrationCredential(owner_id=user["id"], patient_id=user["id"], label="synthetic", token_hash="a" * 64, scopes=["documents:read"], expires_at=time.time() + 1000)
        db.add(credential)
        await db.commit()
        credential_id = credential.id
    unknown = await http.post("/auth/password-reset/request", json={"email":"unknown@example.test"})
    known = await http.post("/auth/password-reset/request", json={"email":user["email"]})
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    async with sessions() as db:
        queued = list((await db.scalars(select(PasswordReset.id).where(PasswordReset.user_id == user["id"]).order_by(PasswordReset.created_at))).all())
    assert len(queued) == 1
    token = password_reset.reset_token(queued[0])
    payload = {"token": token, "password":"new-synthetic-password"}
    assert (await http.post("/auth/password-reset/confirm", json=payload)).status_code == 200
    assert (await http.post("/auth/password-reset/confirm", json=payload)).status_code == 400
    assert (await http.get("/me", headers=auth(old["access_token"]))).status_code == 401
    assert (await http.post("/auth/refresh", json={"refresh_token":old["refresh_token"]})).status_code == 401
    assert (await http.post("/auth/login", json={"email":user["email"], "password":"new-synthetic-password"})).status_code == 200
    async with sessions() as db:
        assert (await db.get(IntegrationCredential, credential_id)).revoked_at is not None

    await http.post("/auth/password-reset/request", json={"email":user["email"]})
    async with sessions() as db:
        queued = list((await db.scalars(select(PasswordReset.id).where(PasswordReset.user_id == user["id"]).order_by(PasswordReset.created_at))).all())
        reset = await db.get(PasswordReset, queued[-1])
        reset.expires_at = time.time() - 1
        await db.commit()
    assert (await http.post("/auth/password-reset/confirm", json={"token":password_reset.reset_token(queued[-1]), "password":"another-password-123"})).status_code == 400
    assert (await http.post("/auth/password-reset/confirm", json={"token":"bad-token", "password":"another-password-123"})).status_code == 400


@pytest.mark.asyncio
async def test_reset_delivery_retries_and_discards_expired_or_used_tokens(client):
    from app import reset_mailer
    from app.models import ResetDelivery

    http, sessions = client
    user = await register(http, "delivery@example.test", "patient")
    assert (await http.post("/auth/password-reset/request", json={"email":user["email"]})).status_code == 202
    async with sessions() as db:
        reset = await db.scalar(select(PasswordReset).where(PasswordReset.user_id == user["id"]))
        reset_id = reset.id
        delivery = await db.get(ResetDelivery, reset_id)
        assert delivery.status == "pending" and delivery.attempts == 0
    def unavailable(reset_id, email):
        raise ConnectionError("contains sensitive details")
    assert await reset_mailer.deliver_one(sessions, unavailable)
    async with sessions() as db:
        delivery = await db.get(ResetDelivery, reset_id)
        assert (delivery.status, delivery.attempts, delivery.error_code) == ("pending", 1, "mail_unavailable")
        delivery.next_attempt_at = time.time() - 1
        await db.commit()
    sent = []
    def send(reset_id, email):
        sent.append((reset_id, email))
    assert await reset_mailer.deliver_one(sessions, send)
    assert sent == [(reset_id, user["email"])]
    assert not await reset_mailer.deliver_one(sessions, send)
    async with sessions() as db:
        delivery = await db.get(ResetDelivery, reset_id)
        assert (delivery.status, delivery.attempts, delivery.error_code) == ("accepted", 2, None)
    await http.post("/auth/password-reset/request", json={"email":user["email"]})
    async with sessions() as db:
        expired = await db.scalar(select(PasswordReset).where(PasswordReset.user_id == user["id"], PasswordReset.id != reset_id))
        expired.expires_at = time.time() - 1
        expired_id = expired.id
        await db.commit()
    assert await reset_mailer.deliver_one(sessions, send)
    async with sessions() as db:
        assert (await db.get(ResetDelivery, expired_id)).status == "discarded"
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["used", "inactive", "exhausted"])
async def test_reset_delivery_stops_when_invalid_or_retries_exhausted(client, case):
    from app import reset_mailer
    from app.models import ResetDelivery

    http, sessions = client
    user = await register(http, f"delivery-{case}@example.test", "patient")
    await http.post("/auth/password-reset/request", json={"email":user["email"]})
    async with sessions() as db:
        reset = await db.scalar(select(PasswordReset).where(PasswordReset.user_id == user["id"]))
        delivery = await db.get(ResetDelivery, reset.id)
        reset_id = reset.id
        if case == "used":
            reset.used_at = time.time()
        elif case == "inactive":
            (await db.get(User, user["id"])).active = False
        else:
            delivery.attempts = reset_mailer.MAX_ATTEMPTS - 1
        await db.commit()
    calls = []
    def send(reset_id, email):
        calls.append(reset_id)
        raise ConnectionError("synthetic failure")
    assert await reset_mailer.deliver_one(sessions, send)
    async with sessions() as db:
        delivery = await db.get(ResetDelivery, reset_id)
        assert delivery.status == ("failed" if case == "exhausted" else "discarded")
        assert delivery.completed_at is not None
    assert calls == ([reset_id] if case == "exhausted" else [])
    assert not await reset_mailer.deliver_one(sessions, send)


@pytest.mark.asyncio
async def test_deactivation_revokes_access_and_blocks_patient_records(client, monkeypatch):
    http, sessions = client
    queued = []
    patient = await register(http, "deactivate-patient@example.test", "patient")
    doctor = await register(http, "deactivate-doctor@example.test", "doctor")
    patient_session = await login(http, patient["email"])
    second_session = await login(http, patient["email"])
    patient_auth = auth(patient_session["access_token"])
    doctor_auth = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        doctor_row = await db.get(User, doctor["id"])
        doctor_row.verification_status = "verified"
        credential = IntegrationCredential(owner_id=patient["id"], patient_id=patient["id"], label="synthetic", token_hash="b" * 64, scopes=["documents:read"], expires_at=time.time()+1000)
        db.add(credential)
        await db.commit()
        credential_id = credential.id
    relationship = await http.post("/relationships", json={"doctor_id":doctor["id"], "purpose":"synthetic care"}, headers=patient_auth)
    grant = await http.post("/consents", json={"relationship_id":relationship.json()["id"], "scope":"read_documents", "purpose":"synthetic care", "expires_at":time.time()+300}, headers=patient_auth)
    assert grant.status_code == 201
    assert (await http.get(f"/patients/{patient['id']}/documents", headers=doctor_auth)).status_code == 200
    await http.post("/auth/password-reset/request", json={"email":patient["email"]})
    async with sessions() as db:
        queued = list((await db.scalars(select(PasswordReset.id).where(PasswordReset.user_id == patient["id"]).order_by(PasswordReset.created_at))).all())
    wrong = await http.post("/account/deactivate", json={"password":"incorrect"}, headers=patient_auth)
    assert wrong.status_code == 401
    assert (await http.get("/me", headers=patient_auth)).status_code == 200
    result = await http.post("/account/deactivate", json={"password":"long-enough-password"}, headers=patient_auth)
    assert result.status_code == 204
    assert (await http.get("/me", headers=auth(second_session["access_token"]))).status_code == 401
    assert (await http.post("/auth/refresh", json={"refresh_token":patient_session["refresh_token"]})).status_code == 401
    assert (await http.post("/auth/login", json={"email":patient["email"], "password":"long-enough-password"})).status_code == 401
    assert (await http.post("/auth/password-reset/confirm", json={"token":password_reset.reset_token(queued[0]), "password":"another-password-123"})).status_code == 400
    assert (await http.get(f"/patients/{patient['id']}/documents", headers=doctor_auth)).status_code == 403
    async with sessions() as db:
        assert (await db.get(IntegrationCredential, credential_id)).revoked_at is not None
        assert (await db.get(PasswordReset, queued[0])).used_at is not None
        assert await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == patient["id"], AuditEvent.action == "account_deactivation", AuditEvent.outcome == "success"))


@pytest.mark.asyncio
async def test_profiles_require_distinct_consent_and_doctor_edits_revoke_verification(client):
    http, sessions = client
    patient = await register(http, "profile-patient@example.test", "patient")
    doctor = await register(http, "profile-doctor@example.test", "doctor")
    other = await register(http, "profile-other@example.test", "patient")
    ph = auth((await login(http, patient["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    blank = await http.get(f"/patients/{patient['id']}/profile", headers=ph)
    assert blank.status_code == 200 and blank.json()["allergies"] is None
    assert (await http.post("/profile/patient", headers=ph, json={"date_of_birth":"2999-01-01"})).status_code == 422
    assert (await http.post("/profile/patient", headers=ph, json={"allergies":"Synthetic allergy", "medications":""})).status_code == 200
    assert (await http.get(f"/patients/{patient['id']}/profile", headers=oh)).status_code == 403
    assert (await http.get(f"/patients/{patient['id']}/profile", headers=dh)).status_code == 403
    assert (await http.post("/profile/doctor", headers=dh, json={"specialization":"Synthetic specialty", "registration_identifier":"TEST-123"})).status_code == 200
    async with sessions() as db:
        row = await db.get(User, doctor["id"])
        row.verification_status = "verified"
        await db.commit()
    relationship = await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"], "purpose":"care"})
    assert relationship.status_code == 201
    grant = await http.post("/consents", headers=ph, json={"relationship_id":relationship.json()["id"], "scope":"read_profile", "purpose":"care", "expires_at":time.time()+300})
    assert grant.status_code == 201
    view = await http.get(f"/patients/{patient['id']}/profile", headers=dh)
    assert view.status_code == 200 and view.json()["allergies"] == "Synthetic allergy" and view.json()["medications"] is None
    assert (await http.get(f"/patients/{patient['id']}/documents", headers=dh)).status_code == 403
    assert (await http.post(f"/consents/{grant.json()['id']}/withdraw", headers=ph)).status_code == 200
    assert (await http.get(f"/patients/{patient['id']}/profile", headers=dh)).status_code == 403
    changed = await http.post("/profile/doctor", headers=dh, json={"specialization":"Changed specialty", "registration_identifier":"TEST-123"})
    assert changed.json()["verification_status"] == "pending"
    assert (await http.get(f"/doctors/{doctor['id']}/profile", headers=oh)).status_code == 403


@pytest.mark.asyncio
async def test_verification_rejects_stale_profile_or_status(client):
    from app.models import DoctorVerificationDecision
    http, sessions = client
    doctor = await register(http, "version-doctor@example.test", "doctor")
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        db.add(User(email="version-admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable"))
        await db.commit()
    ah = auth((await login(http, "version-admin@example.test"))["access_token"])
    profile_path = f"/doctors/{doctor['id']}/profile"
    verify_path = f"/admin/doctors/{doctor['id']}/verification"
    snapshot = (await http.get(profile_path, headers=ah)).json()
    assert snapshot["profile_revision"] == 0 and snapshot["verification_status"] == "pending"
    assert (await http.post("/profile/doctor", headers=dh, json={"specialization":"Synthetic specialty"})).json()["profile_revision"] == 1
    stale = {"verified":True, "expected_profile_revision":snapshot["profile_revision"], "expected_verification_revision":snapshot["verification_revision"], "expected_status":snapshot["verification_status"]}
    assert (await http.post(verify_path, headers=ah, json=stale)).status_code == 409
    fresh = (await http.get(profile_path, headers=ah)).json()
    decision = {"verified":True, "expected_profile_revision":fresh["profile_revision"], "expected_verification_revision":fresh["verification_revision"], "expected_status":fresh["verification_status"]}
    assert (await http.post(verify_path, headers=ah, json=decision)).json()["verification_status"] == "verified"
    assert (await http.post(verify_path, headers=ah, json=decision)).status_code == 409
    unchanged = await http.post("/profile/doctor", headers=dh, json={"specialization":"Synthetic specialty"})
    assert unchanged.json()["profile_revision"] == 1 and unchanged.json()["verification_status"] == "verified"
    changed = await http.post("/profile/doctor", headers=dh, json={"specialization":"Different specialty"})
    assert changed.json()["profile_revision"] == 2 and changed.json()["verification_status"] == "pending"
    history_path = f"/admin/doctors/{doctor['id']}/verification-history"
    assert (await http.get(history_path, headers=dh)).status_code == 403
    history = await http.get(history_path, headers=ah)
    assert len(history.json()) == 1 and history.json()[0]["profile_revision"] == 1
    async with sessions() as db:
        decisions = (await db.scalars(select(DoctorVerificationDecision).where(DoctorVerificationDecision.doctor_id == doctor["id"]))).all()
        assert len(decisions) == 1 and decisions[0].profile_revision == 1 and decisions[0].decision == "verified"


@pytest.mark.asyncio
async def test_doctor_worklist_filters_each_resource_by_current_consent(client):
    from app.models import ClinicalNote, ClinicalNoteVersion, StructuredExtractionVersion
    http, sessions = client
    patient = await register(http, "worklist-patient@example.test", "patient")
    other = await register(http, "worklist-other@example.test", "patient")
    doctor = await register(http, "worklist-doctor@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"], "purpose":"care"})).json()
    path = "/doctor/worklist"
    assert (await http.post(path, headers=ph, json={})).status_code == 403
    assert (await http.post(path, headers=dh, json={})).json()["patients"] == []
    grants = {}
    for scope in ("clinical_notes", "read_documents", "review_documents"):
        result = await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":scope, "purpose":"care", "expires_at":time.time()+300})
        grants[scope] = result.json()["id"]
    upload = await http.post("/documents", headers=ph, files={"file":("worklist.pdf",b"%PDF-synthetic","application/pdf")})
    other_upload = await http.post("/documents", headers=oh, files={"file":("private.pdf",b"%PDF-synthetic","application/pdf")})
    assert upload.status_code == other_upload.status_code == 201
    async with sessions() as db:
        note = ClinicalNote(patient_id=patient["id"]); db.add(note); await db.flush()
        db.add(ClinicalNoteVersion(note_id=note.id, version=1, content="Synthetic confidential note", status="pending_review", author_id=doctor["id"]))
        job = ExtractionJob(document_id=upload.json()["id"], requested_by=patient["id"], status="completed")
        db.add(job); await db.flush()
        db.add(StructuredExtractionVersion(job_id=job.id, version=1, review_status="unverified", data={}))
        await db.commit()
    response = await http.post(path, headers=dh, json={})
    assert response.status_code == 200, response.text
    result = response.json()
    assert [p["patient_id"] for p in result["patients"]] == [patient["id"]]
    assert len(result["recent_uploads"]) == len(result["pending_notes"]) == len(result["pending_extractions"]) == 1
    assert "Synthetic confidential note" not in response.text and "private.pdf" not in response.text
    async with sessions() as db:
        db.add(StructuredExtractionVersion(job_id=job.id, version=2, review_status="reviewed", data={}, reviewer_id=doctor["id"], reviewed_at=time.time()))
        await db.commit()
    assert (await http.post(path, headers=dh, json={})).json()["pending_extractions"] == []
    assert len((await http.post(path, headers=dh, json={"query":patient["id"]})).json()["patients"]) == 1
    assert (await http.post(path, headers=dh, json={"query":"%"})).json()["patients"] == []
    await http.post(f"/consents/{grants['review_documents']}/withdraw", headers=ph)
    assert (await http.post(path, headers=dh, json={})).json()["pending_extractions"] == []
    await http.post(f"/consents/{grants['clinical_notes']}/withdraw", headers=ph)
    await http.post(f"/consents/{grants['read_documents']}/withdraw", headers=ph)
    result = (await http.post(path, headers=dh, json={})).json()
    assert all(not result[key] for key in ("patients", "recent_uploads", "pending_notes", "pending_extractions"))
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "pending"
        await db.commit()
    assert (await http.post(path, headers=dh, json={})).status_code == 403


@pytest.mark.asyncio
async def test_notifications_are_transactional_and_obey_current_access(client):
    http, sessions = client
    patient = await register(http, "notify-patient@example.test", "patient")
    doctor = await register(http, "notify-doctor@example.test", "doctor")
    other = await register(http, "notify-other@example.test", "patient")
    ph = auth((await login(http, patient["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"], "purpose":"care"})).json()
    assert [x["kind"] for x in (await http.get("/notifications", headers=dh)).json()] == ["relationship_created"]
    grant = (await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":"clinical_notes", "purpose":"care", "expires_at":time.time()+300})).json()
    note = (await http.post(f"/patients/{patient['id']}/notes", headers=dh, json={"content":"Synthetic private draft"})).json()
    submit = f"/notes/{note['note_id']}/versions/{note['version_id']}/submit"
    assert (await http.post(submit, headers=dh)).status_code == 200
    assert (await http.post(submit, headers=dh)).status_code == 409
    response = await http.get("/notifications", headers=dh)
    assert "Synthetic private draft" not in response.text
    review = [item for item in response.json() if item["kind"] == "note_review_requested"]
    assert len(review) == 1 and review[0]["resource_id"] == note["note_id"]
    assert (await http.post(f"/notifications/{review[0]['id']}/read", headers=oh)).status_code == 404
    read = await http.post(f"/notifications/{review[0]['id']}/read", headers=dh)
    assert read.json()["read_at"] is not None
    assert (await http.post(f"/notifications/{review[0]['id']}/read", headers=dh)).json()["read_at"] == read.json()["read_at"]
    assert not any(item["kind"] == "note_review_requested" for item in (await http.get("/notifications", headers=ph)).json())
    await http.post(f"/consents/{grant['id']}/withdraw", headers=ph)
    visible = (await http.get("/notifications", headers=dh)).json()
    assert not any(item["kind"] == "note_review_requested" for item in visible)
    assert sum(item["kind"] == "consent_changed" for item in visible) == 2
    assert (await http.post(f"/notifications/{review[0]['id']}/read", headers=dh)).status_code == 404
    await http.post(f"/relationships/{rel['id']}/end", headers=ph)
    assert (await http.get("/notifications", headers=dh)).json() == []


@pytest.mark.asyncio
async def test_consultation_history_and_note_link_obey_consent(client):
    http, sessions = client
    patient = await register(http, "consult-patient@example.test", "patient")
    other = await register(http, "consult-other@example.test", "patient")
    doctor = await register(http, "consult-doctor@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        row = await db.get(User, doctor["id"])
        row.verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"], "purpose":"care"})).json()
    consult_path = f"/patients/{patient['id']}/consultations"
    payload = {"title":"Synthetic visit", "occurred_at":time.time()-60}
    assert (await http.post(consult_path, headers=dh, json=payload)).status_code == 403
    grants = {}
    for scope in ("consultations", "clinical_notes"):
        response = await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":scope, "purpose":"care", "expires_at":time.time()+300})
        assert response.status_code == 201
        grants[scope] = response.json()
    assert (await http.post(consult_path, headers=dh, json={"title":"Future", "occurred_at":time.time()+3600})).status_code == 422
    visit = await http.post(consult_path, headers=dh, json=payload)
    assert visit.status_code == 201
    consult_id = visit.json()["id"]
    assert (await http.get(consult_path, headers=oh)).status_code == 403
    assert (await http.get(consult_path, headers=ph)).json()[0]["id"] == consult_id
    note = await http.post(f"/patients/{patient['id']}/notes", headers=dh, json={"content":"Synthetic draft", "consultation_id":consult_id})
    assert note.status_code == 201 and note.json()["consultation_id"] == consult_id
    note_id, version_id = note.json()["note_id"], note.json()["version_id"]
    assert (await http.get(f"/patients/{patient['id']}/notes", headers=ph)).json() == []
    assert (await http.post(f"/notes/{note_id}/versions/{version_id}/submit", headers=dh)).json()["consultation_id"] == consult_id
    assert (await http.post(f"/notes/{note_id}/versions/{version_id}/review", headers=dh, json={"decision":"approve"})).json()["consultation_id"] == consult_id
    assert (await http.get(f"/patients/{patient['id']}/notes", headers=ph)).json()[0]["consultation_id"] == consult_id
    assert (await http.post(f"/notes/{note_id}/versions", headers=dh, json={"content":"Amendment", "consultation_id":"wrong"})).status_code == 409
    amend = await http.post(f"/notes/{note_id}/versions", headers=dh, json={"content":"Amendment"})
    assert amend.status_code == 201 and amend.json()["consultation_id"] == consult_id
    assert (await http.post(f"/consents/{grants['consultations']['id']}/withdraw", headers=ph)).status_code == 200
    assert (await http.get(consult_path, headers=dh)).status_code == 403
    assert (await http.post(f"/patients/{patient['id']}/notes", headers=dh, json={"content":"Draft", "consultation_id":consult_id})).status_code == 403


@pytest.mark.asyncio
async def test_care_messages_are_relationship_scoped_and_revocable(client):
    http, sessions = client
    patient = await register(http, "message-patient@example.test", "patient")
    other = await register(http, "message-other@example.test", "patient")
    doctor = await register(http, "message-doctor@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        row = await db.get(User, doctor["id"])
        row.verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"], "purpose":"care"})).json()
    path = f"/relationships/{rel['id']}/messages"
    assert (await http.post(path, headers=ph, json={"content":"Synthetic message"})).status_code == 403
    assert (await http.get(path, headers=dh)).status_code == 403
    assert (await http.get(path, headers=oh)).status_code == 403
    grant = await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":"care_messages", "purpose":"care", "expires_at":time.time()+300})
    assert grant.status_code == 201
    assert (await http.post(path, headers=ph, json={"content":"   "})).status_code == 422
    first = await http.post(path, headers=ph, json={"content":"Synthetic message"})
    second = await http.post(path, headers=dh, json={"content":"Synthetic reply"})
    assert first.status_code == second.status_code == 201
    doctor_notifications = (await http.get("/notifications", headers=dh)).json()
    patient_notifications = (await http.get("/notifications", headers=ph)).json()
    doctor_messages = [n for n in doctor_notifications if n["kind"] == "care_message_received"]
    patient_messages = [n for n in patient_notifications if n["kind"] == "care_message_received"]
    assert len(doctor_messages) == len(patient_messages) == 1
    assert doctor_messages[0]["resource_id"] == patient_messages[0]["resource_id"] == rel["id"]
    assert "Synthetic message" not in str(doctor_notifications) and "Synthetic reply" not in str(patient_notifications)
    assert not any(n["kind"] == "care_message_received" for n in (await http.get("/notifications", headers=oh)).json())
    assert (await http.post(f"/notifications/{doctor_messages[0]['id']}/read", headers=ph)).status_code == 404
    assert [m["content"] for m in (await http.get(path, headers=dh)).json()] == ["Synthetic message", "Synthetic reply"]
    assert (await http.post(f"/consents/{grant.json()['id']}/withdraw", headers=ph)).status_code == 200
    assert not any(n["kind"] == "care_message_received" for n in (await http.get("/notifications", headers=dh)).json())
    assert (await http.post(f"/notifications/{doctor_messages[0]['id']}/read", headers=dh)).status_code == 404
    assert any(n["id"] == patient_messages[0]["id"] for n in (await http.get("/notifications", headers=ph)).json())
    assert (await http.get(path, headers=dh)).status_code == 403
    assert (await http.post(path, headers=ph, json={"content":"After withdrawal"})).status_code == 403
    assert len((await http.get(path, headers=ph)).json()) == 2
    again = await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":"care_messages", "purpose":"care", "expires_at":time.time()+300})
    assert again.status_code == 201
    assert any(n["id"] == doctor_messages[0]["id"] for n in (await http.get("/notifications", headers=dh)).json())
    assert (await http.post(f"/relationships/{rel['id']}/end", headers=ph)).status_code == 200
    assert (await http.get(path, headers=dh)).status_code == 403
    assert (await http.post(path, headers=ph, json={"content":"After relationship ended"})).status_code == 403
    assert len((await http.get(path, headers=ph)).json()) == 2
    async with sessions() as db:
        assert await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == doctor["id"], AuditEvent.action == "care_messages_read", AuditEvent.outcome == "success"))
        assert await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == doctor["id"], AuditEvent.action == "care_message_access", AuditEvent.outcome == "denied"))


@pytest.mark.asyncio
async def test_care_message_history_pagination_rechecks_consent(client):
    http, sessions = client
    patient = await register(http, "history-patient@example.test", "patient")
    doctor = await register(http, "history-doctor@example.test", "doctor")
    other = await register(http, "history-other@example.test", "patient")
    ph = auth((await login(http, patient["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"], "purpose":"care"})).json()
    grant = (await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":"care_messages", "purpose":"care", "expires_at":time.time()+300})).json()
    async with sessions() as db:
        db.add_all([CareMessage(relationship_id=rel["id"], sender_id=patient["id"], content="Synthetic", created_at=1000.0) for _ in range(102)])
        await db.commit()
    path = f"/relationships/{rel['id']}/messages"
    first = await http.get(path, headers=dh)
    assert len(first.json()) == 100 and first.headers["cache-control"] == "no-store"
    cursor = first.headers["x-next-cursor"]
    second = await http.get(path + f"?cursor={cursor}", headers=dh)
    ids = [m["id"] for m in second.json() + first.json()]
    assert len(ids) == 102 and ids == sorted(set(ids))
    assert "x-next-cursor" not in second.headers
    assert (await http.get(path + "?cursor=invalid", headers=dh)).status_code == 422
    assert (await http.get(path + f"?cursor={cursor}", headers=oh)).status_code == 403
    await http.post(f"/consents/{grant['id']}/withdraw", headers=ph)
    assert (await http.get(path + f"?cursor={cursor}", headers=dh)).status_code == 403
    assert len((await http.get(path + f"?cursor={cursor}", headers=ph)).json()) == 2


@pytest.mark.asyncio
async def test_admin_operational_metadata_excludes_patient_content(client, monkeypatch):
    http, sessions = client
    patient = await register(http, "ops-patient@example.test", "patient")
    ph = auth((await login(http, patient["email"]))["access_token"])
    async with sessions() as db:
        admin = User(email="ops-admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable")
        db.add(admin)
        doc = Document(patient_id=patient["id"], filename="PRIVATE-SYNTHETIC.pdf", content_type="application/pdf", size=20, review_status="unverified")
        db.add(doc)
        await db.flush()
        db.add(ExtractionJob(document_id=doc.id, requested_by=patient["id"], status="failed", error_code="processing_failed"))
        db.add(AuditEvent(actor_id=patient["id"], patient_id=patient["id"], action="synthetic_event", resource="document:PRIVATE-SYNTHETIC", outcome="denied", purpose="PRIVATE-SYNTHETIC medical details"))
        await db.commit()
    ah = auth((await login(http, "ops-admin@example.test"))["access_token"])
    async def redis_ok(): return True
    monkeypatch.setattr("app.auth_rate_limit.redis_available", redis_ok)
    assert (await http.get("/admin/operations", headers=ph)).status_code == 403
    assert (await http.get("/admin/audit", headers=ph)).status_code == 403
    ops = await http.get("/admin/operations", headers=ah)
    assert ops.status_code == 200 and ops.json()["document_jobs"]["failed"] == 1
    assert "PRIVATE-SYNTHETIC" not in ops.text
    events = await http.get("/admin/audit?action=synthetic_event&outcome=denied", headers=ah)
    assert events.status_code == 200 and len(events.json()) == 1
    assert events.json()[0]["patient_id"] == patient["id"] and events.json()[0]["resource_type"] == "document"
    assert "PRIVATE-SYNTHETIC" not in events.text and "purpose" not in events.json()[0]
    assert (await http.get("/admin/audit?limit=101", headers=ah)).status_code == 422
    assert (await http.get("/ready")).status_code == 200
    async def redis_down(): return False
    monkeypatch.setattr("app.auth_rate_limit.redis_available", redis_down)
    assert (await http.get("/ready")).status_code == 503
    assert (await http.get("/admin/operations", headers=ah)).json()["redis"] == "unavailable"


@pytest.mark.asyncio
async def test_admin_audit_keyset_pagination_with_equal_timestamps(client):
    http, sessions = client
    patient = await register(http, "pagination-patient@example.test", "patient")
    ph = auth((await login(http, patient["email"]))["access_token"])
    async with sessions() as db:
        db.add(User(email="pagination-admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable"))
        db.add_all([AuditEvent(actor_id=patient["id"], action="pagination_event", resource="document:secret", outcome="success", created_at=1000.0) for _ in range(5)])
        await db.commit()
    ah = auth((await login(http, "pagination-admin@example.test"))["access_token"])
    path = "/admin/audit?action=pagination_event&outcome=success&limit=2"
    assert (await http.get(path, headers=ph)).status_code == 403
    first = await http.get(path, headers=ah)
    assert first.status_code == 200 and len(first.json()) == 2
    assert first.headers["cache-control"] == "no-store"
    assert "secret" not in first.text
    cursor = first.headers["x-next-cursor"]
    async with sessions() as db:
        db.add(AuditEvent(action="pagination_event", resource="document:new", outcome="success", created_at=1001.0))
        await db.commit()
    second = await http.get(path + f"&cursor={cursor}", headers=ah)
    third = await http.get(path + f"&cursor={second.headers['x-next-cursor']}", headers=ah)
    ids = [event["id"] for page in (first, second, third) for event in page.json()]
    assert len(ids) == 5 and len(set(ids)) == 5 and ids == sorted(ids, reverse=True)
    assert "x-next-cursor" not in third.headers
    assert (await http.get(path + "&cursor=invalid", headers=ah)).status_code == 422
    assert (await http.get(f"/admin/audit?action=other&cursor={cursor}", headers=ah)).status_code == 422


@pytest.mark.asyncio
async def test_admin_user_management_limits_data_and_revokes_access(client, monkeypatch):
    http, sessions = client
    queued=[]
    patient = await register(http, "managed-patient@example.test", "patient")
    doctor = await register(http, "managed-doctor@example.test", "doctor")
    patient_session = await login(http, patient["email"])
    ph = auth(patient_session["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        admin=User(email="managed-admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable")
        db.add(admin)
        credential=IntegrationCredential(owner_id=patient["id"], patient_id=patient["id"], label="synthetic", token_hash="c"*64, scopes=["documents:read"], expires_at=time.time()+300)
        db.add(credential); await db.commit(); admin_id=admin.id; credential_id=credential.id
    ah = auth((await login(http, "managed-admin@example.test"))["access_token"])
    assert (await http.post("/admin/users/search", headers=ph, json={})).status_code == 403
    assert (await http.post(f"/admin/users/{patient['id']}/deactivate", headers=dh, json={"reason":"security"})).status_code == 403
    page=await http.post("/admin/users/search",headers=ah,json={"query":"managed-patient", "role":"patient", "active":True})
    assert page.status_code == 200 and len(page.json()["users"]) == 1
    assert page.json()["users"][0]["id"] == patient["id"] and "password_hash" not in page.text
    assert (await http.post("/admin/users/search",headers=ah,json={"query":"%"})).json()["users"] == []
    first=(await http.post("/admin/users/search",headers=ah,json={"limit":1})).json()
    second=(await http.post("/admin/users/search",headers=ah,json={"limit":1,"after":first["next_cursor"]})).json()
    assert first["users"][0]["id"] != second["users"][0]["id"]
    assert (await http.post(f"/admin/users/{admin_id}/deactivate",headers=ah,json={"reason":"security"})).status_code == 404
    rel = (await http.post("/relationships",headers=ph,json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    assert (await http.post("/consents",headers=ph,json={"relationship_id":rel["id"],"scope":"read_documents","purpose":"care","expires_at":time.time()+300})).status_code == 201
    assert (await http.get(f"/patients/{patient['id']}/documents",headers=dh)).status_code == 200
    await http.post("/auth/password-reset/request", json={"email":patient["email"]})
    async with sessions() as db:
        queued = list((await db.scalars(select(PasswordReset.id).where(PasswordReset.user_id == patient["id"]).order_by(PasswordReset.created_at))).all())
    result = await http.post(f"/admin/users/{patient['id']}/deactivate",headers=ah,json={"reason":"security"})
    assert result.status_code == 200 and result.json()["active"] is False
    assert (await http.post(f"/admin/users/{patient['id']}/deactivate",headers=ah,json={"reason":"security"})).status_code == 200
    assert (await http.get("/me",headers=ph)).status_code == 401
    assert (await http.post("/auth/refresh",json={"refresh_token":patient_session["refresh_token"]})).status_code == 401
    assert (await http.get(f"/patients/{patient['id']}/documents",headers=dh)).status_code == 403
    assert (await http.post("/auth/password-reset/confirm",json={"token":password_reset.reset_token(queued[0]),"password":"replacement-password"})).status_code == 400
    async with sessions() as db:
        assert (await db.get(IntegrationCredential,credential_id)).revoked_at is not None
        assert await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == admin_id, AuditEvent.action == "admin_account_deactivation", AuditEvent.purpose == "security"))


@pytest.mark.asyncio
async def test_request_correlation_is_isolated_and_ignores_client_ids(client):
    import asyncio
    import uuid

    http, sessions = client
    first = await register(http, "correlation-a@example.test", "patient")
    second = await register(http, "correlation-b@example.test", "patient")
    a = auth((await login(http, first["email"]))["access_token"])
    b = auth((await login(http, second["email"]))["access_token"])
    a["X-Request-ID"] = "untrusted-client-value"
    left, right = await asyncio.gather(http.get(f"/patients/{first['id']}/documents", headers=a), http.get(f"/patients/{second['id']}/documents", headers=b))
    assert left.status_code == right.status_code == 200
    left_id, right_id = left.headers["x-request-id"], right.headers["x-request-id"]
    assert str(uuid.UUID(left_id)) == left_id and left_id != right_id and left_id != a["X-Request-ID"]
    async with sessions() as db:
        for request_id, patient_id in ((left_id, first["id"]), (right_id, second["id"])):
            events = (await db.scalars(select(AuditEvent).where(AuditEvent.correlation_id == request_id))).all()
            assert len(events) == 2
            assert {event.action for event in events} == {"document_list", "document_list_release"}
            assert all(event.patient_id == patient_id for event in events)


@pytest.mark.asyncio
async def test_preview_renders_original_and_enforces_document_consent(client, monkeypatch):
    http, sessions = client
    patient = await register(http, "preview-patient@example.test", "patient")
    doctor = await register(http, "preview-doctor@example.test", "doctor")
    other = await register(http, "preview-other@example.test", "patient")
    ph = auth((await login(http, patient["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72,72), "Synthetic first page")
    pdf.new_page().insert_text((72,72), "Synthetic second page")
    content = pdf.tobytes()
    pdf.close()
    upload = await http.post("/documents", headers=ph, files={"file":("synthetic.pdf", content, "application/pdf")})
    document_id = upload.json()["id"]
    path = f"/documents/{document_id}/preview/2"
    assert (await http.get(path, headers=oh)).status_code == 403
    assert (await http.get(path, headers=dh)).status_code == 403
    response = await http.get(path, headers=ph)
    assert response.status_code == 200 and response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert response.headers["content-type"] == "image/png" and response.headers["cache-control"] == "no-store"
    assert (await http.get(f"/documents/{document_id}/preview/3", headers=ph)).status_code == 422
    assert (await http.get(f"/documents/{document_id}/preview/31", headers=ph)).status_code == 422
    async with sessions() as db:
        row = await db.get(User, doctor["id"])
        row.verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"], "purpose":"care"})).json()
    grant = await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":"read_documents", "purpose":"care", "expires_at":time.time()+300})
    assert (await http.get(path, headers=dh)).status_code == 200
    await http.post(f"/consents/{grant.json()['id']}/withdraw", headers=ph)
    assert (await http.get(path, headers=dh)).status_code == 403
    fresh = await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":"read_documents", "purpose":"care", "expires_at":time.time()+300})
    fresh_id = fresh.json()["id"]
    class RenderDuringWithdrawal:
        returncode = 0
        async def communicate(self, input=None):
            assert input == content
            async with sessions() as db:
                consent = await db.get(Consent, fresh_id)
                consent.status = "withdrawn"
                await db.commit()
            return b"\x89PNG\r\n\x1a\nsynthetic", b""
    async def render(*args, **kwargs):
        return RenderDuringWithdrawal()
    monkeypatch.setattr("app.main.asyncio.create_subprocess_exec", render)
    async def communicate(process, content):
        output, _ = await process.communicate(input=content)
        return output
    monkeypatch.setattr("app.main.communicate_preview", communicate)
    assert (await http.get(path, headers=dh)).status_code == 403


@pytest.mark.asyncio
async def test_preview_rejects_modified_file_and_renders_snapshot(client, monkeypatch, tmp_path):
    http, sessions = client
    patient = await register(http, "preview-integrity@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72), "Synthetic preview")
        content = pdf.tobytes()
    doc = (await http.post("/documents", headers=headers, files={"file":("synthetic.pdf",content,"application/pdf")})).json()
    path = tmp_path / "uploads" / doc["id"]
    preview = f"/documents/{doc['id']}/preview/1"
    modified = bytes([content[0] ^ 1]) + content[1:]
    path.write_bytes(modified)
    assert (await http.get(preview, headers=headers)).status_code == 409
    path.write_bytes(content)
    class RenderSnapshot:
        returncode = 0
        async def communicate(self, input=None):
            assert input == content
            path.write_bytes(modified)
            return b"\x89PNG\r\n\x1a\nsynthetic", b""
    async def render(*args, **kwargs):
        assert args[3] == "--stdin" and kwargs["stdin"] == asyncio.subprocess.PIPE
        return RenderSnapshot()
    monkeypatch.setattr("app.main.asyncio.create_subprocess_exec", render)
    async def communicate(process, content):
        output, _ = await process.communicate(input=content)
        return output
    monkeypatch.setattr("app.main.communicate_preview", communicate)
    response = await http.get(preview, headers=headers)
    assert response.status_code == 200 and response.content.startswith(b"\x89PNG")
    assert (await http.get(preview, headers=headers)).status_code == 409
    async with sessions() as db:
        assert await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == patient["id"], AuditEvent.action == "document_preview_integrity", AuditEvent.outcome == "failed"))


@pytest.mark.asyncio
async def test_cancelled_preview_retains_slot_until_snapshot_read_finishes(client, monkeypatch):
    import threading
    from app import file_integrity, main
    http, _ = client
    patient = await register(http, "preview-cancel@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    doc = (await http.post("/documents", headers=headers, files={"file":("synthetic.pdf",b"%PDF-synthetic", "application/pdf")})).json()
    entered, release = threading.Event(), threading.Event()
    original = file_integrity.read_snapshot
    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(file_integrity, "read_snapshot", delayed)
    initial = main.PREVIEW_SLOTS._value
    task = asyncio.create_task(http.get(f"/documents/{doc['id']}/preview/1", headers=headers))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert main.PREVIEW_SLOTS._value == initial - 1
    finally:
        release.set()
    for _ in range(100):
        if main.PREVIEW_SLOTS._value == initial: break
        await asyncio.sleep(0.01)
    assert main.PREVIEW_SLOTS._value == initial


@pytest.mark.asyncio
async def test_docx_upload_processing_and_segment_evidence(client, monkeypatch, tmp_path):
    import io
    import zipfile
    from app.document_parser import DOCX_TYPE, MAIN_TYPE
    http, sessions = client
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    patient = await register(http, "docx-patient@example.test", "patient")
    doctor = await register(http, "docx-doctor@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="{MAIN_TYPE}"/></Types>')
        def table_row(*values):
            return '<w:tr>' + ''.join(f'<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>' for value in values) + '</w:tr>'
        table = '<w:tbl>' + table_row('Medicine', 'Dose', 'Frequency') + table_row('Synthetic medicine', '5 mg', 'daily') + '</w:tbl>'
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Test name: Synthetic glucose</w:t></w:r></w:p><w:p><w:r><w:t>Observed value: 6.2</w:t></w:r></w:p>' + '<w:p><w:r><w:t>Unit: mg</w:t></w:r></w:p>' * 105 + table + '</w:body></w:document>')
    upload = await http.post("/documents", headers=ph, files={"file":("synthetic.docx",output.getvalue(),DOCX_TYPE)})
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]
    assert (await http.get(f"/documents/{doc_id}/preview/1",headers=ph)).status_code == 415
    path = f"/documents/{doc_id}/processing"
    submitted = await http.get(path, headers=ph)
    assert submitted.status_code == 200
    await processing.process_job(submitted.json()["job_id"])
    result = (await http.get(path, headers=ph)).json()
    assert result["status"] == "completed" and result["processor_version"] == "docx-text-v4" and result["pages"][0]["method"] == "docx"
    extraction = result["structured_versions"][0]["data"]
    assert extraction["laboratory_tests"][0]["observed_value"]["evidence"] == {"page":1,"quote":"6.2","location_type":"text_segment","layer":"primary"}
    assert extraction["prescription_items"][0]["dosage"]["value"] == "5 mg"
    assert any("not page numbers" in warning for warning in extraction["warnings"])
    assert len(extraction["warnings"]) == 100 and "warnings were omitted" in extraction["warnings"][-1]
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    for scope in ("read_documents", "review_documents"):
        assert (await http.post("/consents", headers=ph, json={"relationship_id":rel["id"],"scope":scope,"purpose":"care","expires_at":time.time()+300})).status_code == 201
    review_path = path+f"/{submitted.json()['job_id']}/review"
    extraction["laboratory_tests"][0]["observed_value"]["evidence"]["location_type"] = "page"
    assert (await http.post(review_path, headers=dh, json={"expected_version":1,"data":extraction})).status_code == 422
    extraction["laboratory_tests"][0]["observed_value"]["evidence"]["location_type"] = "text_segment"
    assert (await http.post(review_path, headers=dh, json={"expected_version":1,"data":extraction})).status_code == 200


@pytest.mark.asyncio
async def test_upload_stream_limits_and_multipart_cleanup(client, monkeypatch, tmp_path):
    http, _ = client
    monkeypatch.setattr("app.main.MAX_UPLOAD_BYTES", 1024)
    patient = await register(http, "upload-limit@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    too_large = await http.post("/documents", headers=headers, files={"file":("large.pdf", b"%PDF-" + b"x"*1024, "application/pdf")})
    assert too_large.status_code == 413
    assert not list((tmp_path / "uploads").glob("*"))
    two = await http.post("/documents", headers=headers, files=[("file",("a.pdf",b"%PDF-a","application/pdf")),("file",("b.pdf",b"%PDF-b","application/pdf"))])
    assert two.status_code == 400
    extra = await http.post("/documents", headers=headers, data={"extra":"x"}, files={"file":("a.pdf",b"%PDF-a","application/pdf")})
    assert extra.status_code == 400
    boundary = "synthetic-boundary"
    prefix = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="large.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-'.encode()
    async def chunks():
        yield prefix
        for _ in range(80): yield b"x"*1024
        yield f'\r\n--{boundary}--\r\n'.encode()
    chunk_headers = {**headers, "Content-Type":f"multipart/form-data; boundary={boundary}"}
    response = await http.post("/documents", headers=chunk_headers, content=chunks())
    assert response.status_code == 413
    assert not list((tmp_path / "uploads").glob("*"))
    unread = []
    async def unauthorized_stream():
        unread.append(True)
        yield b"sensitive synthetic data"
    assert (await http.post("/documents", headers={"Content-Type":f"multipart/form-data; boundary={boundary}"}, content=unauthorized_stream())).status_code == 401
    assert not unread
    ok = await http.post("/documents", headers=headers, files={"file":("ok.pdf",b"%PDF-synthetic","application/pdf")})
    assert ok.status_code == 201


@pytest.mark.asyncio
async def test_download_rejects_changed_file_and_nonregular_sources(client, monkeypatch, tmp_path):
    import hashlib
    http, sessions = client
    patient = await register(http, "download-integrity@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    content = b"%PDF-synthetic original"
    upload = await http.post("/documents", headers=headers, files={"file":("synthetic.pdf", content, "application/pdf")})
    doc_id = upload.json()["id"]
    path = tmp_path / "uploads" / doc_id
    result = await http.get(f"/documents/{doc_id}/download", headers=headers)
    assert result.status_code == 200 and result.content == content and "attachment" in result.headers["content-disposition"]
    path.write_bytes(b"%PDF-synthetic modified")
    assert len(path.read_bytes()) == len(content)
    assert (await http.get(f"/documents/{doc_id}/download", headers=headers)).status_code == 409
    target = tmp_path / "linked-file"
    target.write_bytes(content)
    path.unlink(); path.symlink_to(target)
    assert (await http.get(f"/documents/{doc_id}/download", headers=headers)).status_code == 409
    path.unlink(); os.mkfifo(path)
    assert (await asyncio.wait_for(http.get(f"/documents/{doc_id}/download", headers=headers), timeout=2)).status_code == 409
    async with sessions() as db:
        doc = await db.get(Document, doc_id)
        assert doc.sha256 == hashlib.sha256(content).hexdigest()
        assert await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == patient["id"], AuditEvent.action == "document_download_integrity", AuditEvent.outcome == "failed"))


@pytest.mark.asyncio
async def test_download_rechecks_consent_after_snapshot(client, monkeypatch, tmp_path):
    import threading
    from app import file_integrity
    http, sessions = client
    patient = await register(http, "download-race-patient@example.test", "patient")
    doctor = await register(http, "download-race-doctor@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    doc = (await http.post("/documents", headers=ph, files={"file":("synthetic.pdf",b"%PDF-synthetic", "application/pdf")})).json()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    grant = (await http.post("/consents", headers=ph, json={"relationship_id":rel["id"],"scope":"read_documents","purpose":"care","expires_at":time.time()+300})).json()
    entered, release = threading.Event(), threading.Event()
    original = file_integrity.read_snapshot
    def delayed(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(file_integrity, "read_snapshot", delayed)
    task = asyncio.create_task(http.get(f"/documents/{doc['id']}/download", headers=dh))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (await http.post(f"/consents/{grant['id']}/withdraw", headers=ph)).status_code == 200
    finally:
        release.set()
    response = await task
    assert response.status_code == 403 and b"%PDF" not in response.content


@pytest.mark.asyncio
async def test_cancelled_download_retains_slot_until_file_read_finishes(client, monkeypatch):
    import threading
    from app import file_integrity, main
    http, _ = client
    patient = await register(http, "download-cancel@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    doc = (await http.post("/documents", headers=headers, files={"file":("synthetic.pdf",b"%PDF-synthetic", "application/pdf")})).json()
    entered, release = threading.Event(), threading.Event()
    original = file_integrity.read_snapshot
    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(file_integrity, "read_snapshot", delayed)
    initial = main.DOWNLOAD_SLOTS._value
    task = asyncio.create_task(http.get(f"/documents/{doc['id']}/download", headers=headers))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert main.DOWNLOAD_SLOTS._value == initial - 1
    finally:
        release.set()
    for _ in range(100):
        if main.DOWNLOAD_SLOTS._value == initial: break
        await asyncio.sleep(0.01)
    assert main.DOWNLOAD_SLOTS._value == initial


@pytest.mark.asyncio
async def test_consent_enforced_for_list_download_and_withdrawal(client):
    http, sessions = client
    patient = await register(http, "patient@example.test", "patient")
    other = await register(http, "other@example.test", "patient")
    doctor = await register(http, "doctor@example.test", "doctor")
    patient_auth = auth((await login(http, patient["email"]))["access_token"])
    doctor_auth = auth((await login(http, doctor["email"]))["access_token"])
    upload = await http.post("/documents", files={"file": ("report.pdf", b"%PDF-synthetic report", "application/pdf")}, headers=patient_auth)
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]
    assert (await http.get(f"/documents/{doc_id}/download", headers=doctor_auth)).status_code == 403
    assert (await http.post("/relationships", json={"doctor_id": doctor["id"], "purpose": "care"}, headers=patient_auth)).status_code == 404
    async with sessions() as db:
        admin = User(email="admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable")
        db.add(admin)
        await db.commit()
    admin_auth = auth((await login(http, "admin@example.test"))["access_token"])
    assert (await http.get(f"/documents/{doc_id}/download", headers=admin_auth)).status_code == 403
    assert (await http.post(f"/admin/doctors/{doctor['id']}/verification", json={"verified": True, "expected_profile_revision":0, "expected_verification_revision":0, "expected_status":"pending"}, headers=admin_auth)).status_code == 200
    rel = await http.post("/relationships", json={"doctor_id": doctor["id"], "purpose": "care"}, headers=patient_auth)
    assert rel.status_code == 201, rel.text
    grant = await http.post("/consents", json={"relationship_id": rel.json()["id"], "scope": "read_documents", "purpose": "care", "expires_at": time.time() + 300}, headers=patient_auth)
    assert grant.status_code == 201, grant.text
    assert (await http.get(f"/patients/{other['id']}/documents", headers=doctor_auth)).status_code == 403
    assert (await http.get(f"/patients/{patient['id']}/documents", headers=doctor_auth)).json()[0]["id"] == doc_id
    download = await http.get(f"/documents/{doc_id}/download", headers=doctor_auth)
    assert download.status_code == 200 and download.content == b"%PDF-synthetic report"
    assert (await http.post(f"/consents/{grant.json()['id']}/withdraw", headers=patient_auth)).json()["status"] == "withdrawn"
    assert (await http.get(f"/documents/{doc_id}/download", headers=doctor_auth)).status_code == 403
    assert (await http.get("/consents", headers=patient_auth)).json()[0]["status"] == "withdrawn"
    history = (await http.get("/consents", headers=patient_auth)).json()[0]
    assert history["doctor_id"] == doctor["id"] and history["relationship_status"] == "active"
    assert history["effective_at"] <= history["withdrawn_at"] and history["withdrawn_at"] <= history["expires_at"]
    other_auth = auth((await login(http, other["email"]))["access_token"])
    assert (await http.get("/consents", headers=other_auth)).json() == []
    assert (await http.get("/consents", headers=doctor_auth)).status_code == 403
    await http.post(f"/relationships/{rel.json()['id']}/end", headers=patient_auth)
    assert (await http.get("/consents", headers=patient_auth)).json()[0]["relationship_status"] == "ended"
    events = (await http.get("/audit/self", headers=doctor_auth)).json()
    assert any(event["action"] == "document_download" and event["outcome"] == "denied" for event in events)


@pytest.mark.asyncio
async def test_refresh_rotation_logout_and_upload_checks(client):
    http, _ = client
    await register(http, "patient@example.test", "patient")
    session = await login(http, "patient@example.test")
    token = session["refresh_token"]
    renewed = await http.post("/auth/refresh", json={"refresh_token": token})
    assert renewed.status_code == 200
    assert (await http.post("/auth/refresh", json={"refresh_token": token})).status_code == 401
    headers = auth(renewed.json()["access_token"])
    bad = await http.post("/documents", files={"file": ("report.pdf", b"not a pdf", "application/pdf")}, headers=headers)
    assert bad.status_code == 415
    assert (await http.post("/auth/logout", headers=headers)).status_code == 204
    assert (await http.get("/me", headers=headers)).status_code == 401
    assert (await http.post("/auth/refresh", json={"refresh_token": renewed.json()["refresh_token"]})).status_code == 401


@pytest.mark.asyncio
async def test_clinical_note_review_versioning_and_consent(client):
    http, sessions = client
    patient = await register(http, "patient-note@example.test", "patient")
    other = await register(http, "other-note@example.test", "patient")
    doctor = await register(http, "doctor-note@example.test", "doctor")
    patient_auth = auth((await login(http, patient["email"]))["access_token"])
    other_auth = auth((await login(http, other["email"]))["access_token"])
    doctor_auth = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        doc = await db.get(User, doctor["id"])
        doc.verification_status = "verified"
        db.add(User(email="admin-note@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable"))
        await db.commit()
    admin_auth = auth((await login(http, "admin-note@example.test"))["access_token"])
    rel = (await http.post("/relationships", json={"doctor_id": doctor["id"], "purpose": "care"}, headers=patient_auth)).json()
    path = f"/patients/{patient['id']}/notes"
    assert (await http.post(path, json={"content": "Synthetic draft"}, headers=doctor_auth)).status_code == 403
    consent = await http.post("/consents", json={"relationship_id": rel["id"], "scope": "clinical_notes", "purpose": "care", "expires_at": time.time() + 300}, headers=patient_auth)
    assert consent.status_code == 201
    async with sessions() as db:
        grant = await db.get(Consent, consent.json()["id"])
        grant.expires_at = time.time() - 1
        await db.commit()
    assert (await http.get(path, headers=doctor_auth)).status_code == 403
    async with sessions() as db:
        grant = await db.get(Consent, consent.json()["id"])
        grant.expires_at = time.time() + 300
        doctor_row = await db.get(User, doctor["id"])
        doctor_row.verification_status = "rejected"
        await db.commit()
    assert (await http.get(path, headers=doctor_auth)).status_code == 403
    async with sessions() as db:
        doctor_row = await db.get(User, doctor["id"])
        doctor_row.verification_status = "verified"
        await db.commit()
    assert (await http.post(path, json={"content": "Patient may not write"}, headers=patient_auth)).status_code == 403
    assert (await http.get(path, headers=admin_auth)).status_code == 403
    assert (await http.get(path, headers=other_auth)).status_code == 403
    draft = await http.post(path, json={"content": "Synthetic draft"}, headers=doctor_auth)
    assert draft.status_code == 201, draft.text
    note = draft.json()
    assert note["status"] == "draft" and (await http.get(path, headers=patient_auth)).json() == []
    transition = f"/notes/{note['note_id']}/versions/{note['version_id']}"
    assert (await http.post(transition + "/review", json={"decision": "approve"}, headers=doctor_auth)).status_code == 409
    assert (await http.post(transition + "/submit", headers=doctor_auth)).json()["status"] == "pending_review"
    assert (await http.get(path, headers=patient_auth)).json() == []
    assert (await http.post(transition + "/review", json={"decision": "approve"}, headers=patient_auth)).status_code == 403
    approved = await http.post(transition + "/review", json={"decision": "approve"}, headers=doctor_auth)
    assert approved.json()["reviewer_id"] == doctor["id"]
    assert (await http.get(path, headers=patient_auth)).json()[0]["content"] == "Synthetic draft"
    revision = await http.post(f"/notes/{note['note_id']}/versions", json={"content": "Synthetic amendment"}, headers=doctor_auth)
    assert revision.status_code == 201 and revision.json()["version"] == 2
    assert (await http.get(path, headers=patient_auth)).json()[0]["content"] == "Synthetic draft"
    compare_path = f"/notes/{note['note_id']}/compare"
    comparison = {"from_version_id":note["version_id"], "to_version_id":revision.json()["version_id"]}
    compared = await http.post(compare_path, json=comparison, headers=doctor_auth)
    assert compared.status_code == 200 and compared.json()["lines"] == [{"kind":"removed", "text":"Synthetic draft"}, {"kind":"added", "text":"Synthetic amendment"}]
    assert (await http.post(compare_path, json=comparison, headers=patient_auth)).status_code == 403
    assert (await http.post(compare_path, json=comparison, headers=other_auth)).status_code == 403
    assert (await http.post(compare_path, json={"from_version_id":revision.json()["version_id"], "to_version_id":note["version_id"]}, headers=doctor_auth)).status_code == 422
    second = f"/notes/{note['note_id']}/versions/{revision.json()['version_id']}"
    assert (await http.post(second + "/submit", headers=doctor_auth)).status_code == 200
    assert (await http.post(second + "/review", json={"decision": "approve"}, headers=doctor_auth)).status_code == 200
    assert (await http.get(path, headers=patient_auth)).json()[0]["content"] == "Synthetic amendment"
    versions = (await http.get(path, headers=doctor_auth)).json()
    assert [(v["version"], v["status"]) for v in versions] == [(2, "approved"), (1, "superseded")]
    assert (await http.post(transition + "/review", json={"decision": "reject"}, headers=doctor_auth)).status_code == 409
    assert (await http.post(f"/consents/{consent.json()['id']}/withdraw", headers=patient_auth)).status_code == 200
    assert (await http.get(path, headers=doctor_auth)).status_code == 403
    assert (await http.post(f"/notes/{note['note_id']}/versions", json={"content": "Unauthorized"}, headers=doctor_auth)).status_code == 403
    assert (await http.post(compare_path, json=comparison, headers=doctor_auth)).status_code == 403


@pytest.mark.asyncio
async def test_independent_note_review_requires_other_authorized_doctor(client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "NOTE_REVIEW_POLICY", "independent")
    http, sessions = client
    patient = await register(http, "independent-patient@example.test", "patient")
    author = await register(http, "independent-author@example.test", "doctor")
    reviewer = await register(http, "independent-reviewer@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    ah = auth((await login(http, author["email"]))["access_token"])
    rh = auth((await login(http, reviewer["email"]))["access_token"])
    async with sessions() as db:
        for doctor in (author, reviewer):
            (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    grants = []
    for doctor in (author, reviewer):
        rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"], "purpose":"care"})).json()
        grant = await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":"clinical_notes", "purpose":"care", "expires_at":time.time()+300})
        assert grant.status_code == 201
        grants.append(grant.json())
    assert (await http.get("/clinical-review-policy", headers=ah)).json() == {"note_review_policy":"independent"}
    note = (await http.post(f"/patients/{patient['id']}/notes", headers=ah, json={"content":"Synthetic draft"})).json()
    base = f"/notes/{note['note_id']}/versions/{note['version_id']}"
    assert (await http.post(base+"/submit", headers=ah)).status_code == 200
    assert not (await http.post("/doctor/worklist", headers=ah, json={"query":""})).json()["pending_notes"]
    assert any(item["note_id"] == note["note_id"] for item in (await http.post("/doctor/worklist", headers=rh, json={"query":""})).json()["pending_notes"])
    assert not any(n["kind"] == "note_review_requested" for n in (await http.get("/notifications", headers=ah)).json())
    assert any(n["kind"] == "note_review_requested" for n in (await http.get("/notifications", headers=rh)).json())
    assert (await http.post(base+"/review", headers=ah, json={"decision":"approve"})).status_code == 403
    assert (await http.post(base+"/review", headers=ah, json={"decision":"reject", "reason":"Needs revision"})).status_code == 403
    assert (await http.get(f"/patients/{patient['id']}/notes", headers=ph)).json() == []
    assert (await http.post(f"/consents/{grants[1]['id']}/withdraw", headers=ph)).status_code == 200
    assert (await http.post(base+"/review", headers=rh, json={"decision":"approve"})).status_code == 403
    rel_id = grants[1]["relationship_id"]
    assert (await http.post("/consents", headers=ph, json={"relationship_id":rel_id,"scope":"clinical_notes","purpose":"care","expires_at":time.time()+300})).status_code == 201
    approved = await http.post(base+"/review", headers=rh, json={"decision":"approve"})
    assert approved.status_code == 200 and approved.json()["reviewer_id"] == reviewer["id"]
    async with sessions() as db:
        assert await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == author["id"], AuditEvent.action == "note_review_independence", AuditEvent.outcome == "denied"))


@pytest.mark.asyncio
async def test_rejected_note_requires_reason_and_remains_hidden_from_patient(client):
    http, sessions = client
    patient = await register(http, "reject-patient@example.test", "patient")
    doctor = await register(http, "reject-doctor@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"], "purpose":"care"})).json()
    await http.post("/consents", headers=ph, json={"relationship_id":rel["id"], "scope":"clinical_notes", "purpose":"care", "expires_at":time.time()+300})
    path = f"/patients/{patient['id']}/notes"
    note = (await http.post(path, headers=dh, json={"content":"Synthetic unreviewed draft"})).json()
    version_path = f"/notes/{note['note_id']}/versions/{note['version_id']}"
    assert (await http.post(version_path+"/submit", headers=dh)).status_code == 200
    assert (await http.post(version_path+"/review", headers=dh, json={"decision":"reject"})).status_code == 422
    assert (await http.post(version_path+"/review", headers=dh, json={"decision":"reject", "reason":"  "})).status_code == 422
    rejected = await http.post(version_path+"/review", headers=dh, json={"decision":"reject", "reason":" Check source evidence "})
    assert rejected.status_code == 200 and rejected.json()["review_comment"] == "Check source evidence"
    assert (await http.get(path, headers=ph)).json() == []
    assert (await http.get(path, headers=dh)).json()[0]["review_comment"] == "Check source evidence"
    amended = await http.post(f"/notes/{note['note_id']}/versions", headers=dh, json={"content":"Synthetic revision"})
    assert amended.status_code == 201 and amended.json()["review_comment"] is None


@pytest.mark.asyncio
async def test_extraction_review_requires_explicit_consent_and_preserves_versions(client, monkeypatch, tmp_path):
    http, sessions = client
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    patient = await register(http, "review-patient@example.test", "patient")
    doctor = await register(http, "review-doctor@example.test", "doctor")
    patient_auth = auth((await login(http, patient["email"]))["access_token"])
    doctor_auth = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        doctor_row = await db.get(User, doctor["id"])
        doctor_row.verification_status = "verified"
        await db.commit()
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Test name: Synthetic glucose\nObserved value: 6.2\nUnit: mmol/L\nReference range: 3.0 - 7.0\nMedicine name: Synthetic medicine\nDosage: 10 mg")
        content = pdf.tobytes()
    document = (await http.post("/documents", files={"file": ("report.pdf", content, "application/pdf")}, headers=patient_auth)).json()
    path = f"/documents/{document['id']}/processing"
    job = (await http.get(path, headers=patient_auth)).json()
    await processing.process_job(job["job_id"])
    result = (await http.get(path, headers=patient_auth)).json()
    extraction = result["structured_versions"][0]
    assert extraction["review_status"] == "unverified"
    assert extraction["range_comparisons"] == ["within"]
    assert extraction["data"]["laboratory_tests"][0]["observed_value"]["evidence"] == {"page": 1, "quote": "6.2", "location_type": "page", "layer": "primary"}
    overview_path = f"/patients/{patient['id']}/extracted-records"
    overview = (await http.get(overview_path, headers=patient_auth)).json()
    assert not overview["truncated"] and len(overview["documents"]) == 1
    entry = overview["documents"][0]
    assert entry["document_id"] == document["id"] and entry["review_status"] == "unverified"
    assert entry["data"]["prescription_items"][0]["medicine_name"]["value"] == "Synthetic medicine"
    assert (await http.get(overview_path, headers=doctor_auth)).status_code == 403
    other = await register(http, "overview-other@example.test", "patient")
    other_auth = auth((await login(http, other["email"]))["access_token"])
    assert (await http.get(overview_path, headers=other_auth)).status_code == 403
    review_path = path + f"/{job['job_id']}/review"
    request = {"expected_version": 1, "data": extraction["data"]}
    assert (await http.post(review_path, json=request, headers=patient_auth)).status_code == 403
    rel = (await http.post("/relationships", json={"doctor_id": doctor["id"], "purpose": "care"}, headers=patient_auth)).json()
    for scope in ("read_documents", "review_documents"):
        grant = await http.post("/consents", json={"relationship_id": rel["id"], "scope": scope, "purpose": "care", "expires_at": time.time() + 300}, headers=patient_auth)
        assert grant.status_code == 201
        if scope == "read_documents":
            assert (await http.post(review_path, json=request, headers=doctor_auth)).status_code == 403
            read_consent = grant.json()
            assert (await http.get(overview_path, headers=doctor_auth)).status_code == 200
        if scope == "review_documents": review_consent = grant.json()
    async with sessions() as db:
        doctor_row = await db.get(User, doctor["id"])
        doctor_row.verification_status = "rejected"
        await db.commit()
    assert (await http.post(review_path, json=request, headers=doctor_auth)).status_code == 403
    async with sessions() as db:
        doctor_row = await db.get(User, doctor["id"])
        doctor_row.verification_status = "verified"
        await db.commit()
    invalid = {"expected_version": 1, "data": {"laboratory_tests": [{"test_name": {"value": "invented", "origin": "source", "evidence": {"page": 1, "quote": "invented"}}}]}}
    assert (await http.post(review_path, json=invalid, headers=doctor_auth)).status_code == 422
    request["data"]["laboratory_tests"][0]["observed_value"] = {"value": "8", "origin": "clinician", "evidence": None}
    request["data"]["laboratory_tests"][0]["unit"] = {"value": "mmol/L corrected", "origin": "clinician", "evidence": None}
    request["data"]["laboratory_tests"][0]["laboratory_details"] = {"value": "Synthetic branch", "origin": "clinician", "evidence": None}
    assert (await http.post(review_path, json=request, headers=doctor_auth)).status_code == 422
    request["data"]["schema_version"] = "1.2"
    reviewed = await http.post(review_path, json=request, headers=doctor_auth)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review_status"] == "reviewed" and reviewed.json()["version"] == 2
    request_id = reviewed.headers["x-request-id"]
    async with sessions() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.correlation_id == request_id))).all()
        assert {event.action for event in events} >= {"document_extraction_review", "document_extraction_source_read", "document_extraction_reviewed"}
        assert all(event.actor_id == doctor["id"] for event in events)
    assert (await http.post(review_path, json=request, headers=doctor_auth)).status_code == 409
    history = (await http.get(path, headers=patient_auth)).json()["structured_versions"]
    assert [item["review_status"] for item in history] == ["reviewed", "unverified"]
    assert [item["range_comparisons"] for item in history] == [["above"], ["within"]]
    assert history[1]["data"]["laboratory_tests"][0]["unit"]["value"] == "mmol/L"
    overview = (await http.get(overview_path, headers=doctor_auth)).json()["documents"][0]
    assert overview["review_status"] == "reviewed" and overview["version"] == 2
    assert overview["data"]["laboratory_tests"][0]["unit"]["value"] == "mmol/L corrected"
    assert overview["data"]["laboratory_tests"][0]["laboratory_details"]["value"] == "Synthetic branch"
    await http.post(f"/consents/{review_consent['id']}/withdraw", headers=patient_auth)
    request["expected_version"] = 2
    assert (await http.post(review_path, json=request, headers=doctor_auth)).status_code == 403
    await http.post(f"/consents/{read_consent['id']}/withdraw", headers=patient_auth)
    assert (await http.get(overview_path, headers=doctor_auth)).status_code == 403
    reprocessed = await http.post(path, headers=patient_auth)
    assert reprocessed.status_code == 202
    assert (await http.get(overview_path, headers=patient_auth)).json()["documents"] == []
    history_page = await http.get(path + "/history", headers=patient_auth)
    assert history_page.status_code == 200 and history_page.headers["cache-control"] == "no-store"
    assert [run["id"] for run in history_page.json()["items"]] == [reprocessed.json()["job_id"], job["job_id"]]
    old_result = await http.get(path, params={"job_id": job["job_id"]}, headers=patient_auth)
    assert old_result.status_code == 200 and old_result.headers["cache-control"] == "no-store" and old_result.json()["is_latest"] is False
    assert old_result.json()["structured_versions"][0]["data"]["laboratory_tests"][0]["unit"]["value"] == "mmol/L corrected"
    assert (await http.get(path + "/history", headers=other_auth)).status_code == 403
    assert (await http.get(path, params={"job_id": job["job_id"]}, headers=doctor_auth)).status_code == 403
    assert (await http.get(path + "/history", headers=doctor_auth)).status_code == 403
    assert (await http.get(path, params={"job_id": str(uuid.uuid4())}, headers=patient_auth)).status_code == 404
    async with sessions() as db:
        for index in range(22):
            db.add(ExtractionJob(document_id=document["id"], requested_by=patient["id"], status="completed", created_at=time.time() - 1000 - index))
        await db.commit()
    first = (await http.get(path + "/history", headers=patient_auth)).json()
    assert len(first["items"]) == 20 and first["next_before"]
    second = (await http.get(path + "/history", params={"before": first["next_before"]}, headers=patient_auth)).json()
    assert len(second["items"]) == 4 and second["next_before"] is None
    assert not ({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
    assert (await http.get(path + "/history", params={"before": str(uuid.uuid4())}, headers=patient_auth)).status_code == 404


@pytest.mark.asyncio
async def test_document_processing_is_authorized_and_idempotent(client, monkeypatch, tmp_path):
    http, sessions = client
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    patient = await register(http, "processing-patient@example.test", "patient")
    other = await register(http, "processing-other@example.test", "patient")
    doctor = await register(http, "processing-doctor@example.test", "doctor")
    patient_auth = auth((await login(http, patient["email"]))["access_token"])
    other_auth = auth((await login(http, other["email"]))["access_token"])
    doctor_auth = auth((await login(http, doctor["email"]))["access_token"])
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Synthetic laboratory report")
        content = pdf.tobytes()
    upload = await http.post("/documents", files={"file": ("report.pdf", content, "application/pdf")}, headers=patient_auth)
    document_id = upload.json()["id"]
    path = f"/documents/{document_id}/processing"
    assert (await http.post(path, headers=other_auth)).status_code == 403
    assert (await http.get(path, headers=other_auth)).status_code == 403
    requested = await http.get(path, headers=patient_auth)
    assert requested.status_code == 200, requested.text
    assert (await http.post(path, headers=patient_auth)).status_code == 409
    await processing.process_job(requested.json()["job_id"])
    await processing.process_job(requested.json()["job_id"])
    async with sessions() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.correlation_id == upload.headers["x-request-id"]))).all()
        assert {event.action for event in events} >= {"document_upload", "document_processing_complete"}
        assert sum(event.action == "document_processing_complete" for event in events) == 1
    notifications = (await http.get("/notifications", headers=patient_auth)).json()
    completed = [item for item in notifications if item["kind"] == "processing_completed" and item["resource_id"] == document_id]
    assert len(completed) == 1 and completed[0]["processing_job_id"] == requested.json()["job_id"]
    result = (await http.get(path, headers=patient_auth)).json()
    assert result["status"] == "completed" and result["review_status"] == "unverified"
    assert result["pages"] == [{"page_number": 1, "method": "native", "text": "Synthetic laboratory report\n"}]
    assert (await http.get(path, headers=doctor_auth)).status_code == 403
    async with sessions() as db:
        doc = await db.get(User, doctor["id"])
        doc.verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", json={"doctor_id": doctor["id"], "purpose": "care"}, headers=patient_auth)).json()
    consent = (await http.post("/consents", json={"relationship_id": rel["id"], "scope": "read_documents", "purpose": "care", "expires_at": time.time() + 300}, headers=patient_auth)).json()
    assert (await http.get(path, headers=doctor_auth)).status_code == 200
    await http.post(f"/consents/{consent['id']}/withdraw", headers=patient_auth)
    assert (await http.get(path, headers=doctor_auth)).status_code == 403
    stalled = await http.post(path, headers=patient_auth)
    async with sessions() as db:
        stale_job = await db.get(ExtractionJob, stalled.json()["job_id"])
        stale_job.status = "running"
        stale_job.started_at = time.time() - 301
        await db.commit()
    assert (await http.post(path, headers=patient_auth)).status_code == 202
    notifications = (await http.get("/notifications", headers=patient_auth)).json()
    failed = [item for item in notifications if item["kind"] == "processing_failed" and item["resource_id"] == document_id]
    assert len(failed) == 1 and failed[0]["processing_job_id"] == stalled.json()["job_id"]
    previous = await http.get(path + "?job_id=" + completed[0]["processing_job_id"], headers=patient_auth)
    assert previous.status_code == 200 and previous.json()["job_id"] == requested.json()["job_id"] and not previous.json()["is_latest"]


@pytest.mark.asyncio
async def test_processing_handles_queue_failure_and_invalid_file(client, monkeypatch, tmp_path):
    http, sessions = client
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    patient = await register(http, "processing-failure@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    upload = await http.post("/documents", files={"file": ("broken.pdf", b"%PDF-not-valid", "application/pdf")}, headers=headers)
    path = f"/documents/{upload.json()['id']}/processing"
    response = await http.get(path, headers=headers)
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    async def unavailable(kind, target_id):
        raise ConnectionError("broker unavailable")
    assert await outbox_dispatcher.dispatch_one(sessions, unavailable)
    async with sessions() as db:
        entry = await db.scalar(select(JobOutbox).where(JobOutbox.target_id == job_id))
        assert (entry.status, entry.attempts, entry.last_error) == ("pending", 1, "publish_failed")
        assert (await db.get(ExtractionJob, job_id)).status == "queued"
        entry.next_attempt_at = time.time() - 1
        await db.commit()
    sent = []
    async def publish(kind, target_id):
        sent.append((kind, target_id))
    assert await outbox_dispatcher.dispatch_one(sessions, publish)
    assert sent == [("document_processing", job_id)]
    async with sessions() as db:
        entry = await db.scalar(select(JobOutbox).where(JobOutbox.target_id == job_id))
        assert (entry.status, entry.attempts, entry.last_error) == ("published", 2, None)
    assert not await outbox_dispatcher.dispatch_one(sessions, publish)
    await processing.process_job(response.json()["job_id"])
    result = (await http.get(path, headers=headers)).json()
    assert result["status"] == "failed" and result["error_code"] == "processing_failed" and result["pages"] == []
    notifications = (await http.get("/notifications", headers=headers)).json()
    assert len([item for item in notifications if item["kind"] == "processing_failed"]) == 1


@pytest.mark.asyncio
async def test_stale_worker_is_fenced_and_job_recovers(client, monkeypatch, tmp_path):
    import asyncio
    import json
    import threading
    from types import SimpleNamespace

    http, sessions = client
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    patient = await register(http, "recovery@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    upload = await http.post("/documents", files={"file": ("test.pdf", b"%PDF-synthetic", "application/pdf")}, headers=headers)
    path = f"/documents/{upload.json()['id']}/processing"
    job_id = (await http.get(path, headers=headers)).json()["job_id"]
    entered, release = threading.Event(), threading.Event()

    def parse(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return SimpleNamespace(stdout=json.dumps({"pages": [{"page_number": 1, "method": "native", "text": "Synthetic report"}]}).encode())

    monkeypatch.setattr(processing, "run_document_parser", parse)
    original = asyncio.create_task(processing.process_job(job_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        async with sessions() as db:
            job = await db.get(ExtractionJob, job_id)
            original_token = job.claim_token
            assert job.status == "running" and job.attempt_count == 1 and original_token
            started_at = job.started_at
        assert await outbox_dispatcher.recover_stale(sessions, started_at + 301) == 1
        release.set()
        await original
        async with sessions() as db:
            job = await db.get(ExtractionJob, job_id)
            assert job.status == "queued" and job.claim_token is None
            entry = await db.scalar(select(JobOutbox).where(JobOutbox.target_id == job_id))
            assert entry.status == "pending"
        await processing.process_job(job_id)
        async with sessions() as db:
            job = await db.get(ExtractionJob, job_id)
            assert job.status == "completed" and job.attempt_count == 2 and job.claim_token != original_token
        assert (await http.get(path, headers=headers)).json()["pages"][0]["text"] == "Synthetic report"
    finally:
        release.set()
        await original


@pytest.mark.asyncio
async def test_expired_claims_stop_after_three_attempts(client):
    from app.models import KnowledgeImport

    http, sessions = client
    patient = await register(http, "recovery-limit@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    upload = await http.post("/documents", files={"file": ("test.pdf", b"%PDF-synthetic", "application/pdf")}, headers=headers)
    requested = await http.get(f"/documents/{upload.json()['id']}/processing", headers=headers)
    job_id = requested.json()["job_id"]
    async with sessions() as db:
        admin = User(email="recovery-admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable")
        db.add(admin)
        await db.flush()
        import_job = KnowledgeImport(created_by=admin.id, filename="test.pdf", media_type="application/pdf", sha256="0" * 64, status="running", started_at=time.time() - 301, attempt_count=3, claim_token="old")
        db.add(import_job)
        job = await db.get(ExtractionJob, job_id)
        job.status, job.started_at, job.attempt_count, job.claim_token = "running", time.time() - 301, 3, "old"
        await db.commit()
        import_id = import_job.id
    assert await outbox_dispatcher.recover_stale(sessions) == 2
    async with sessions() as db:
        for job in (await db.get(ExtractionJob, job_id), await db.get(KnowledgeImport, import_id)):
            assert job.status == "failed" and job.error_code == "worker_lost" and job.claim_token is None
        failure = await db.scalar(select(AuditEvent).where(AuditEvent.correlation_id == upload.headers["x-request-id"], AuditEvent.action == "document_processing_failed"))
        assert failure and failure.outcome == "failed"
    notifications = (await http.get("/notifications", headers=headers)).json()
    assert sum(item["kind"] == "processing_failed" for item in notifications) == 1


@pytest.mark.asyncio
async def test_published_message_that_never_starts_is_redispatched(client):
    http, sessions = client
    patient = await register(http, "lost-message@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    upload = await http.post("/documents", files={"file": ("test.pdf", b"%PDF-synthetic", "application/pdf")}, headers=headers)
    job_id = (await http.get(f"/documents/{upload.json()['id']}/processing", headers=headers)).json()["job_id"]
    async with sessions() as db:
        entry = await db.scalar(select(JobOutbox).where(JobOutbox.target_id == job_id))
        entry.status, entry.published_at = "published", time.time() - 301
        await db.commit()
    assert await outbox_dispatcher.recover_stale(sessions, target_id=job_id) == 1
    sent = []
    async def publish(kind, target_id):
        sent.append((kind, target_id))
    assert await outbox_dispatcher.dispatch_one(sessions, publish)
    assert sent == [("document_processing", job_id)]
    async with sessions() as db:
        assert (await db.get(ExtractionJob, job_id)).status == "queued"


@pytest.mark.asyncio
async def test_knowledge_source_requires_index_and_approval_and_versions_are_retired(client, monkeypatch):
    http, sessions = client
    patient = await register(http, "knowledge-patient@example.test", "patient")
    patient_auth = auth((await login(http, patient["email"]))["access_token"])
    async with sessions() as db:
        db.add(User(email="knowledge-admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable"))
        await db.commit()
    admin_auth = auth((await login(http, "knowledge-admin@example.test"))["access_token"])
    source = {"title": "Synthetic reference", "publisher": "Example publisher", "provenance": "synthetic://example", "publication_date": "2025-01-01", "specialty": "general", "version": "1", "content": "Synthetic reference text", "effective_at": time.time() - 1}
    assert (await http.post("/admin/knowledge", json=source, headers=patient_auth)).status_code == 403
    created = await http.post("/admin/knowledge", json=source, headers=admin_auth)
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    assert (await http.post(f"/admin/knowledge/{source_id}/approve", headers=admin_auth)).status_code == 409
    async def embedding(texts):
        return [[1.0] + [0.0] * 383 for _ in texts]
    async def digest(): return "a" * 64
    monkeypatch.setattr(knowledge, "embedding_digest", digest)
    monkeypatch.setattr(knowledge, "embed_texts", embedding)
    assert (await http.post(f"/admin/knowledge/{source_id}/index", headers=admin_auth)).status_code == 200
    assert (await http.post(f"/admin/knowledge/{source_id}/approve", headers=admin_auth)).json()["status"] == "approved"
    updated = dict(source, version="2", previous_id=source_id, content="Synthetic revised reference")
    new = await http.post("/admin/knowledge", json=updated, headers=admin_auth)
    assert new.status_code == 201
    new_id = new.json()["id"]
    assert (await http.post(f"/admin/knowledge/{new_id}/index", headers=admin_auth)).status_code == 200
    assert (await http.post(f"/admin/knowledge/{new_id}/approve", headers=admin_auth)).status_code == 200
    sources = (await http.get("/admin/knowledge", headers=admin_auth)).json()
    assert {s["version"]: s["status"] for s in sources} == {"1": "retired", "2": "approved"}
    assert (await http.post(f"/admin/knowledge/{source_id}/index", headers=admin_auth)).status_code == 409
    assert (await http.post("/knowledge/search", json={"query": "synthetic"}, headers=patient_auth)).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["logout", "deactivate", "role", "model", "digest"])
async def test_knowledge_index_rechecks_admin_and_configuration_after_embedding(client, monkeypatch, change):
    from app.models import KnowledgeChunk, KnowledgeSource
    http, sessions = client
    async with sessions() as db:
        admin = User(email=f"index-race-{change}@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable")
        db.add(admin); await db.commit(); admin_id = admin.id
    login_data = await login(http, f"index-race-{change}@example.test")
    headers = auth(login_data["access_token"])
    source = {"title":"Synthetic reference", "publisher":"Example", "provenance":"synthetic://reference", "publication_date":"2025-01-01", "specialty":"general", "version":"1", "content":"Synthetic knowledge text", "effective_at":time.time()-1}
    source_id = (await http.post("/admin/knowledge", json=source, headers=headers)).json()["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(texts):
        entered.set()
        await release.wait()
        return [[1.0] + [0.0] * 383 for _ in texts]
    digest_calls = []
    async def digest():
        digest_calls.append(1)
        return ("b" if change == "digest" and len(digest_calls) > 1 else "a") * 64
    monkeypatch.setattr(knowledge, "embedding_digest", digest)
    monkeypatch.setattr(knowledge, "embed_texts", delayed)
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "synthetic-original")
    task = asyncio.create_task(http.post(f"/admin/knowledge/{source_id}/index", headers=headers))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if change == "logout":
            assert (await http.post("/auth/logout", headers=headers)).status_code == 204
        elif change == "model":
            monkeypatch.setenv("OLLAMA_EMBED_MODEL", "synthetic-changed")
        elif change == "digest":
            pass
        else:
            async with sessions() as db:
                user = await db.get(User, admin_id)
                if change == "deactivate": user.active = False
                else: user.role = "patient"
                await db.commit()
    finally:
        release.set()
    response = await asyncio.wait_for(task, timeout=5)
    assert response.status_code == {"logout":401, "deactivate":401, "role":403, "model":409, "digest":409}[change]
    async with sessions() as db:
        assert (await db.get(KnowledgeSource, source_id)).index_status != "ready"
        assert not (await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))).all()


@pytest.mark.asyncio
async def test_knowledge_import_stays_separate_and_requires_approval(client, monkeypatch, tmp_path):
    http, sessions = client
    import_dir = tmp_path / "reference-imports"
    monkeypatch.setattr("app.main.KNOWLEDGE_IMPORT_DIR", import_dir)
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("KNOWLEDGE_IMPORT_DIR", str(import_dir))
    patient = await register(http, "reference-patient@example.test", "patient")
    ph = auth((await login(http, patient["email"]))["access_token"])
    async with sessions() as db:
        db.add(User(email="reference-admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable"))
        await db.commit()
    ah = auth((await login(http, "reference-admin@example.test"))["access_token"])
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72), "Synthetic reference material about blue whales")
        content = pdf.tobytes()
    assert (await http.post("/admin/knowledge/imports", headers=ph, files={"file":("reference.pdf",content,"application/pdf")})).status_code == 403
    uploaded = await http.post("/admin/knowledge/imports", headers=ah, files={"file":("reference.pdf",content,"application/pdf")})
    assert uploaded.status_code == 202, uploaded.text
    job_id = uploaded.json()["id"]
    assert (import_dir / job_id).is_file() and not (tmp_path / "uploads" / job_id).exists()
    assert (await http.get(f"/admin/knowledge/imports/{job_id}",headers=ph)).status_code == 403
    assert (await http.get(f"/admin/knowledge/imports/{job_id}/download",headers=ah)).content == content
    await processing.process_knowledge_import(job_id)
    await processing.process_knowledge_import(job_id)
    async with sessions() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.correlation_id == uploaded.headers["x-request-id"]))).all()
        assert {event.action for event in events} >= {"knowledge_import_requested", "knowledge_import_completed"}
        assert sum(event.action == "knowledge_import_completed" for event in events) == 1
    imported = (await http.get(f"/admin/knowledge/imports/{job_id}",headers=ah)).json()
    assert imported["status"] == "completed" and "Synthetic reference" in imported["extracted_text"]
    metadata = {"title":"Synthetic reference", "publisher":"Test publisher", "provenance":"synthetic://test", "publication_date":"2025-01-01", "specialty":"general", "version":"1", "effective_at":time.time()-1, "content":imported["extracted_text"], "import_id":job_id}
    assert (await http.post("/admin/knowledge",headers=ah,json={**metadata,"content":"Altered"})).status_code == 409
    source = await http.post("/admin/knowledge",headers=ah,json=metadata)
    assert source.status_code == 201 and source.json()["status"] == "draft" and source.json()["import_id"] == job_id
    assert (await http.post(f"/admin/knowledge/{source.json()['id']}/approve",headers=ah)).status_code == 409
    listed = (await http.get("/admin/knowledge",headers=ah)).json()
    assert next(item for item in listed if item["id"] == source.json()["id"])["import_id"] == job_id
    assert (await http.get(f"/admin/knowledge/imports/{job_id}/download",headers=ph)).status_code == 403
    bad = await http.post("/admin/knowledge/imports",headers=ah,files={"file":("broken.pdf",b"%PDF-invalid","application/pdf")})
    assert bad.status_code == 202
    await processing.process_knowledge_import(bad.json()["id"])
    assert (await http.get(f"/admin/knowledge/imports/{bad.json()['id']}",headers=ah)).json()["status"] == "failed"
    async with sessions() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.correlation_id == bad.headers["x-request-id"]))).all()
        assert {event.action for event in events} >= {"knowledge_import_requested", "knowledge_import_failed"}
    tampered = await http.post("/admin/knowledge/imports",headers=ah,files={"file":("tampered.pdf",content,"application/pdf")})
    (import_dir / tampered.json()["id"]).write_bytes(b"%PDF-changed")
    await processing.process_knowledge_import(tampered.json()["id"])
    assert (await http.get(f"/admin/knowledge/imports/{tampered.json()['id']}",headers=ah)).json()["status"] == "failed"
    queued = await http.post("/admin/knowledge/imports",headers=ah,files={"file":("queue.pdf",content,"application/pdf")})
    assert queued.status_code == 202
    async with sessions() as db:
        entry = await db.scalar(select(JobOutbox).where(JobOutbox.target_id == queued.json()["id"]))
        assert entry.kind == "knowledge_import" and entry.status == "pending"
    from app import document_purger
    from app.models import KnowledgeImport
    delete_path = f"/admin/knowledge/imports/{job_id}/delete"
    assert (await http.post(delete_path, headers=ph, json={"confirmation":"DELETE"})).status_code == 403
    assert (await http.post(delete_path, headers=ah, json={"confirmation":"wrong"})).status_code == 422
    deleted = await http.post(delete_path, headers=ah, json={"confirmation":"DELETE"})
    assert deleted.status_code == 202 and deleted.json()["purge_status"] == "pending"
    assert (await http.post(delete_path, headers=ah, json={"confirmation":"DELETE"})).json()["deleted_at"] == deleted.json()["deleted_at"]
    assert (await http.get(f"/admin/knowledge/imports/{job_id}",headers=ah)).status_code == 404
    assert (await http.get(f"/admin/knowledge/imports/{job_id}/download",headers=ah)).status_code == 404
    assert (await http.post("/admin/knowledge", headers=ah, json={**metadata, "version":"2"})).status_code == 409
    sources = (await http.get("/admin/knowledge", headers=ah)).json()
    linked = next(item for item in sources if item["id"] == source.json()["id"])
    assert linked["import_id"] == job_id and linked["content"] == imported["extracted_text"]
    def fail(_path): raise OSError("synthetic failure")
    assert await document_purger.purge_import_one(sessions, fail)
    async with sessions() as db:
        job = await db.get(KnowledgeImport, job_id)
        assert job.purge_status == "pending" and job.purge_attempts == 1 and job.extracted_text is None
        job.purge_after = time.time() - 1
        await db.commit()
    assert await document_purger.purge_import_one(sessions)
    assert not (import_dir / job_id).exists()
    assert not await document_purger.purge_import_one(sessions)
    entry = next(item for item in (await http.get("/admin/knowledge/imports", headers=ah)).json() if item["id"] == job_id)
    assert entry["purge_status"] == "complete" and entry["purged_at"] is not None
    queued_delete = await http.post(f"/admin/knowledge/imports/{queued.json()['id']}/delete", headers=ah, json={"confirmation":"DELETE"})
    assert queued_delete.status_code == 202
    await processing.process_knowledge_import(queued.json()["id"])
    async with sessions() as db:
        job = await db.get(KnowledgeImport, queued.json()["id"])
        assert job.status == "failed" and job.error_code == "import_deleted" and job.extracted_text is None



@pytest.mark.asyncio
async def test_integration_credentials_scope_patient_consent_revocation_and_rate_limits(client, monkeypatch):
    http, sessions = client
    async def allow(*args): return True
    monkeypatch.setattr(integration_rate_limit, "check_rate_limit", allow)
    patient = await register(http, "integration-patient@example.test", "patient")
    other = await register(http, "integration-other@example.test", "patient")
    doctor = await register(http, "integration-doctor@example.test", "doctor")
    patient_auth = auth((await login(http, patient["email"]))["access_token"])
    other_auth = auth((await login(http, other["email"]))["access_token"])
    doctor_auth = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        doctor_row = await db.get(User, doctor["id"])
        doctor_row.verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", json={"doctor_id": doctor["id"], "purpose": "care"}, headers=patient_auth)).json()
    consent = (await http.post("/consents", json={"relationship_id": rel["id"], "scope": "read_documents", "purpose": "care", "expires_at": time.time() + 300}, headers=patient_auth)).json()
    document = (await http.post("/documents", files={"file": ("synthetic.pdf", b"%PDF-synthetic", "application/pdf")}, headers=patient_auth)).json()
    other_document = (await http.post("/documents", files={"file": ("other.pdf", b"%PDF-other", "application/pdf")}, headers=other_auth)).json()
    request = {"label": "Synthetic MCP", "scopes": ["documents:read"], "patient_id": patient["id"], "expires_at": time.time() + 300}
    assert (await http.post("/integration-credentials", json=request, headers=doctor_auth)).status_code == 403
    delegation = (await http.post("/consents", json={"relationship_id": rel["id"], "scope": "integration_access", "purpose": "care", "expires_at": time.time() + 300}, headers=patient_auth)).json()
    created = await http.post("/integration-credentials", json=request, headers=doctor_auth)
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    credential = created.json()
    integration_auth = auth(credential["token"])
    assert (await http.get("/me", headers=integration_auth)).status_code == 401
    assert "token" not in (await http.get("/integration-credentials", headers=doctor_auth)).json()[0]
    assert (await http.get(f"/integration/patients/{patient['id']}/documents", headers=integration_auth)).json()[0]["id"] == document["id"]
    assert (await http.get(f"/integration/patients/{other['id']}/documents", headers=integration_auth)).status_code == 403
    assert (await http.get(f"/integration/patients/{'x' * 200}/documents", headers=integration_auth)).status_code == 403
    async with sessions() as db:
        denied = await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == doctor['id'], AuditEvent.action == 'integration_access', AuditEvent.patient_id.is_(None)))
        assert denied is not None and denied.outcome == 'denied'
    assert (await http.get(f"/integration/documents/{other_document['id']}/processing", headers=integration_auth)).status_code == 403
    assert (await http.post("/integration/knowledge/search", json={"query": "synthetic"}, headers=integration_auth)).status_code == 403
    assert (await http.post(f"/integration-credentials/{credential['id']}/revoke", headers=other_auth)).status_code == 404
    async def deny(*args): return False
    monkeypatch.setattr(integration_rate_limit, "check_rate_limit", deny)
    assert (await http.get(f"/integration/patients/{patient['id']}/documents", headers=integration_auth)).status_code == 429
    async def unavailable(*args): raise integration_rate_limit.RateLimitUnavailable()
    monkeypatch.setattr(integration_rate_limit, "check_rate_limit", unavailable)
    assert (await http.get(f"/integration/patients/{patient['id']}/documents", headers=integration_auth)).status_code == 503
    monkeypatch.setattr(integration_rate_limit, "check_rate_limit", allow)
    assert (await http.post(f"/consents/{delegation['id']}/withdraw", headers=patient_auth)).status_code == 200
    assert (await http.get(f"/integration/patients/{patient['id']}/documents", headers=integration_auth)).status_code == 403
    await http.post("/consents", json={"relationship_id": rel["id"], "scope": "integration_access", "purpose": "care", "expires_at": time.time() + 300}, headers=patient_auth)
    assert (await http.post(f"/consents/{consent['id']}/withdraw", headers=patient_auth)).status_code == 200
    assert (await http.get(f"/integration/patients/{patient['id']}/documents", headers=integration_auth)).status_code == 403
    assert (await http.post(f"/integration-credentials/{credential['id']}/revoke", headers=doctor_auth)).status_code == 200
    assert (await http.get(f"/integration/patients/{patient['id']}/documents", headers=integration_auth)).status_code == 401


@pytest.mark.asyncio
async def test_upload_scanner_accepts_rejects_and_fails_closed(client, monkeypatch, tmp_path):
    import asyncio
    import struct

    http, sessions = client
    socket_path = tmp_path / "clamd.sock"
    received = []
    async def fake_clamd(reader, writer):
        try:
            assert await reader.readexactly(10) == b"zINSTREAM\0"
            data = b""
            while size := struct.unpack(">I", await reader.readexactly(4))[0]:
                data += await reader.readexactly(size)
            received.append(data)
            reply = b"stream: Synthetic-Signature FOUND\0" if b"synthetic-threat" in data else b"stream: OK\0"
            writer.write(reply)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_unix_server(fake_clamd, path=str(socket_path))
    monkeypatch.setenv("MALWARE_SCAN_MODE", "clamd")
    monkeypatch.setenv("CLAMD_SOCKET", str(socket_path))
    import_dir = tmp_path / "reference-imports"
    monkeypatch.setattr("app.main.KNOWLEDGE_IMPORT_DIR", import_dir)
    try:
        patient = await register(http, "scanner-patient@example.test", "patient")
        ph = auth((await login(http, patient["email"]))["access_token"])
        async with sessions() as db:
            db.add(User(email="scanner-admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable"))
            await db.commit()
        ah = auth((await login(http, "scanner-admin@example.test"))["access_token"])
        clean = b"%PDF-synthetic-clean"
        infected = b"%PDF-synthetic-threat"
        upload = await http.post("/documents", headers=ph, files={"file": ("clean.pdf", clean, "application/pdf")})
        assert upload.status_code == 201 and upload.json()["scan_status"] == "no_threat_detected"
        assert (await http.get(f"/patients/{patient['id']}/documents", headers=ph)).json()[0]["scan_status"] == "no_threat_detected"
        rejected = await http.post("/documents", headers=ph, files={"file": ("threat.pdf", infected, "application/pdf")})
        assert rejected.status_code == 422 and "Synthetic-Signature" not in rejected.text
        rejected_import = await http.post("/admin/knowledge/imports", headers=ah, files={"file": ("threat.pdf", infected, "application/pdf")})
        assert rejected_import.status_code == 422
        good_import = await http.post("/admin/knowledge/imports", headers=ah, files={"file": ("reference.pdf", clean, "application/pdf")})
        assert good_import.status_code == 202 and good_import.json()["scan_status"] == "no_threat_detected"
        assert received == [clean, infected, infected, clean]
        assert len(list((tmp_path / "uploads").iterdir())) == 1 and len(list(import_dir.iterdir())) == 1
        server.close()
        await server.wait_closed()
        unavailable = await http.post("/documents", headers=ph, files={"file": ("offline.pdf", clean, "application/pdf")})
        assert unavailable.status_code == 503
        assert len(list((tmp_path / "uploads").iterdir())) == 1
        assert len((await http.get(f"/patients/{patient['id']}/documents", headers=ph)).json()) == 1
        async with sessions() as db:
            audits = (await db.scalars(select(AuditEvent).where(AuditEvent.action == "upload_scan"))).all()
            assert sorted(item.outcome for item in audits) == ["denied", "denied", "failed"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_patient_document_deletion_revokes_access_and_retries_file_purge(client, monkeypatch, tmp_path):
    from app import document_purger
    from app.models import ExtractedPage, StructuredExtractionVersion

    http, sessions = client
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    patient = await register(http, "delete-owner@example.test", "patient")
    other = await register(http, "delete-other@example.test", "patient")
    doctor = await register(http, "delete-doctor@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    assert (await http.post("/consents", headers=ph, json={"relationship_id":rel["id"],"scope":"read_documents","purpose":"care","expires_at":time.time()+300})).status_code == 201
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72), "Test name: Synthetic test\nObserved value: 7")
        content=pdf.tobytes()
    uploaded = await http.post("/documents", headers=ph, files={"file":("sensitive.pdf",content,"application/pdf")})
    document_id = uploaded.json()["id"]
    path = f"/documents/{document_id}"
    started = await http.get(path+"/processing", headers=ph)
    await processing.process_job(started.json()["job_id"])
    assert (await http.get(path+"/download", headers=dh)).content == content
    assert (await http.post(path+"/delete", headers=oh, json={"confirmation":"DELETE"})).status_code == 403
    assert (await http.post(path+"/delete", headers=dh, json={"confirmation":"DELETE"})).status_code == 403
    assert (await http.post(path+"/delete", headers=ph, json={"confirmation":"wrong"})).status_code == 422
    deletion = await http.post(path+"/delete", headers=ph, json={"confirmation":"DELETE"})
    assert deletion.status_code == 202 and deletion.json()["purge_status"] == "pending"
    assert (await http.get(path+"/download", headers=ph)).status_code == 404
    assert (await http.get(path+"/processing", headers=dh)).status_code == 404
    assert (await http.get(f"/patients/{patient['id']}/documents", headers=ph)).json() == []
    assert (await http.get(f"/patients/{patient['id']}/extracted-records", headers=ph)).json()["documents"] == []
    assert (tmp_path / "uploads" / document_id).is_file()
    async with sessions() as db:
        assert (await db.get(Document, document_id)).filename == "Deleted document"
        assert not (await db.scalars(select(ExtractedPage))).all()
        assert not (await db.scalars(select(StructuredExtractionVersion))).all()
    def fail(path):
        raise PermissionError("synthetic")
    assert await document_purger.purge_one(sessions, fail)
    async with sessions() as db:
        doc = await db.get(Document, document_id)
        assert doc.purge_status == "pending" and doc.purge_attempts == 1
        doc.purge_after = time.time()-1
        await db.commit()
    assert await document_purger.purge_one(sessions)
    assert not (tmp_path / "uploads" / document_id).exists()
    repeated = await http.post(path+"/delete", headers=ph, json={"confirmation":"DELETE"})
    assert repeated.status_code == 202 and repeated.json()["purge_status"] == "complete"
    statuses = (await http.get("/documents/deletions", headers=ph)).json()
    assert statuses[0]["id"] == document_id and statuses[0]["purge_status"] == "complete"
    assert (await http.get("/documents/deletions", headers=dh)).status_code == 403


@pytest.mark.asyncio
async def test_document_deletion_fences_inflight_extraction(client, monkeypatch, tmp_path):
    import asyncio
    import json
    import threading
    from types import SimpleNamespace
    from app.models import ExtractedPage

    http, sessions = client
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    patient = await register(http, "delete-inflight@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    upload = await http.post("/documents", headers=headers, files={"file":("test.pdf",b"%PDF-synthetic","application/pdf")})
    doc_id = upload.json()["id"]
    job = (await http.get(f"/documents/{doc_id}/processing",headers=headers)).json()["job_id"]
    entered, release = threading.Event(), threading.Event()
    def parse(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return SimpleNamespace(stdout=json.dumps({"pages":[{"page_number":1,"method":"native","text":"Synthetic secret"}]}).encode())
    monkeypatch.setattr(processing, "run_document_parser", parse)
    worker = asyncio.create_task(processing.process_job(job))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (await http.post(f"/documents/{doc_id}/delete",headers=headers,json={"confirmation":"DELETE"})).status_code == 202
    finally:
        release.set()
        await worker
    async with sessions() as db:
        assert (await db.get(ExtractionJob,job)).error_code == "document_deleted"
        assert not (await db.scalars(select(ExtractedPage).where(ExtractedPage.job_id == job))).all()


@pytest.mark.asyncio
async def test_knowledge_import_deletion_fences_running_worker(client, monkeypatch, tmp_path):
    import json
    import threading
    from types import SimpleNamespace
    from app.models import KnowledgeImport

    http, sessions = client
    import_dir = tmp_path / "knowledge"
    monkeypatch.setattr("app.main.KNOWLEDGE_IMPORT_DIR", import_dir)
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("KNOWLEDGE_IMPORT_DIR", str(import_dir))
    async with sessions() as db:
        db.add(User(email="import-delete-admin@example.test", full_name="Synthetic Admin", role="admin", password_hash=hash_password("long-enough-password"), verification_status="not_applicable"))
        await db.commit()
    headers = auth((await login(http, "import-delete-admin@example.test"))["access_token"])
    response = await http.post("/admin/knowledge/imports", headers=headers, files={"file":("synthetic.pdf", b"%PDF-synthetic", "application/pdf")})
    assert response.status_code == 202
    job_id = response.json()["id"]
    entered, release = threading.Event(), threading.Event()
    def parse(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return SimpleNamespace(stdout=json.dumps({"pages":[{"page_number":1,"method":"native","text":"Synthetic reference"}]}).encode())
    monkeypatch.setattr(processing, "run_document_parser", parse)
    worker = asyncio.create_task(processing.process_knowledge_import(job_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (await http.post(f"/admin/knowledge/imports/{job_id}/delete", headers=headers, json={"confirmation":"DELETE"})).status_code == 202
    finally:
        release.set()
        await worker
    async with sessions() as db:
        job = await db.get(KnowledgeImport, job_id)
        assert job.status == "failed" and job.error_code == "import_deleted" and job.extracted_text is None


@pytest.mark.asyncio
async def test_document_summaries_are_cited_scoped_idempotent_and_deleted(client, monkeypatch):
    import uuid
    from app import assistant_provider
    from app.models import DocumentSummary, ExtractedPage

    http, sessions = client
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "synthetic-model")
    patient = await register(http, "summary-patient@example.test", "patient")
    doctor = await register(http, "summary-doctor@example.test", "doctor")
    other = await register(http, "summary-other@example.test", "patient")
    ph = auth((await login(http, patient["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    upload = await http.post("/documents",headers=ph,files={"file":("summary.pdf",b"%PDF-synthetic","application/pdf")})
    doc_id = upload.json()["id"]
    async with sessions() as db:
        job = ExtractionJob(document_id=doc_id,requested_by=patient["id"],status="completed")
        db.add(job); await db.flush(); job_id=job.id
        db.add_all([ExtractedPage(job_id=job_id,page_number=i,method="native",text=f"Synthetic page {i}", supplemental_ocr_text="Synthetic OCR layer" if i == 1 else None) for i in range(1,4)])
        await db.commit()
    calls=[]
    async def draft(question, history, sources, audience):
        calls.append(1)
        assert len(sources) <= 3 and all(source["kind"]=="unverified_patient_document" for source in sources)
        source = sources[1] if len(calls) == 1 else sources[0]
        if len(calls) == 1:
            assert source["layer"] == "supplemental_ocr" and source["text"] == "Synthetic OCR layer"
        return assistant_provider.Draft(answer="Synthetic unverified summary",citations=[source["id"]])
    monkeypatch.setattr(assistant_provider,"draft_answer",draft)
    path=f"/documents/{doc_id}/processing/{job_id}/summaries"
    request={"request_id":str(uuid.uuid4())}
    assert (await http.post(path,headers=oh,json=request)).status_code==403
    assert (await http.post(path,headers=dh,json=request)).status_code==403
    created=await http.post(path,headers=ph,json=request)
    assert created.status_code==201, created.text
    summary=created.json()
    assert summary["review_status"]=="unverified" and not summary["truncated"] and summary["model_name"]=="synthetic-model"
    assert summary["citations"][0]["page"]==1 and summary["citations"][0]["layer"]=="supplemental_ocr" and "text" not in summary["citations"][0]
    replay=await http.post(path,headers=ph,json=request)
    assert replay.status_code==200 and replay.json()["id"]==summary["id"] and len(calls)==2
    assert (await http.get(path,headers=ph)).json()[0]["id"]==summary["id"]
    relationship=(await http.post("/relationships",headers=ph,json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    grants={}
    for scope in ("read_documents","assistant"):
        grants[scope]=(await http.post("/consents",headers=ph,json={"relationship_id":relationship["id"],"scope":scope,"purpose":"care","expires_at":time.time()+300})).json()
    assert (await http.get(path,headers=dh)).status_code==200
    await http.post(f"/consents/{grants['assistant']['id']}/withdraw",headers=ph)
    assert (await http.get(path,headers=dh)).status_code==403
    assert (await http.post(f"/documents/{doc_id}/delete",headers=ph,json={"confirmation":"DELETE"})).status_code==202
    assert (await http.get(path,headers=ph)).status_code==404
    async with sessions() as db:
        assert not (await db.scalars(select(DocumentSummary).where(DocumentSummary.document_id==doc_id))).all()


@pytest.mark.asyncio
async def test_document_summary_not_saved_after_deletion_during_generation(client, monkeypatch):
    import asyncio
    import uuid
    from app import assistant_provider
    from app.models import DocumentSummary, ExtractedPage

    http, sessions=client
    monkeypatch.setenv("OLLAMA_CHAT_MODEL","synthetic-model")
    patient=await register(http,"summary-race@example.test","patient")
    headers=auth((await login(http,patient["email"]))["access_token"])
    upload=await http.post("/documents",headers=headers,files={"file":("test.pdf",b"%PDF-synthetic","application/pdf")})
    doc_id=upload.json()["id"]
    async with sessions() as db:
        job=ExtractionJob(document_id=doc_id,requested_by=patient["id"],status="completed")
        db.add(job); await db.flush(); job_id=job.id
        db.add(ExtractedPage(job_id=job_id,page_number=1,method="native",text="Synthetic text")); await db.commit()
    entered, release=asyncio.Event(), asyncio.Event()
    async def draft(question, history, sources, audience):
        entered.set(); await release.wait()
        return assistant_provider.Draft(answer="Synthetic summary",citations=[sources[0]["id"]])
    monkeypatch.setattr(assistant_provider,"draft_answer",draft)
    path=f"/documents/{doc_id}/processing/{job_id}/summaries"
    request={"request_id":str(uuid.uuid4())}
    task=asyncio.create_task(http.post(path,headers=headers,json=request))
    try:
        await asyncio.wait_for(entered.wait(),timeout=5)
        assert (await http.post(path,headers=headers,json=request)).status_code==409
        assert (await http.post(f"/documents/{doc_id}/delete",headers=headers,json={"confirmation":"DELETE"})).status_code==202
    finally:
        release.set()
    assert (await task).status_code==404
    async with sessions() as db:
        assert not (await db.scalars(select(DocumentSummary).where(DocumentSummary.document_id==doc_id))).all()


@pytest.mark.asyncio
async def test_document_summary_rechecks_doctor_consent_after_model_call(client, monkeypatch):
    import asyncio
    import uuid
    from app import assistant_provider
    from app.models import DocumentSummary, ExtractedPage

    http, sessions = client
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "synthetic-model")
    patient=await register(http,"summary-consent-patient@example.test","patient")
    doctor=await register(http,"summary-consent-doctor@example.test","doctor")
    ph=auth((await login(http,patient["email"]))["access_token"])
    dh=auth((await login(http,doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User,doctor["id"])).verification_status="verified"; await db.commit()
    rel=(await http.post("/relationships",headers=ph,json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    grants={}
    for scope in ("read_documents","assistant"):
        grants[scope]=(await http.post("/consents",headers=ph,json={"relationship_id":rel["id"],"scope":scope,"purpose":"care","expires_at":time.time()+300})).json()
    upload=await http.post("/documents",headers=ph,files={"file":("test.pdf",b"%PDF-synthetic","application/pdf")})
    doc_id=upload.json()["id"]
    async with sessions() as db:
        job=ExtractionJob(document_id=doc_id,requested_by=patient["id"],status="completed")
        db.add(job); await db.flush(); job_id=job.id
        db.add(ExtractedPage(job_id=job_id,page_number=1,method="native",text="Synthetic text")); await db.commit()
    entered,release=asyncio.Event(),asyncio.Event()
    async def draft(question, history, sources, audience):
        entered.set(); await release.wait()
        return assistant_provider.Draft(answer="Synthetic summary",citations=[sources[0]["id"]])
    monkeypatch.setattr(assistant_provider,"draft_answer",draft)
    path=f"/documents/{doc_id}/processing/{job_id}/summaries"
    request={"request_id":str(uuid.uuid4())}
    task=asyncio.create_task(http.post(path,headers=dh,json=request))
    try:
        await asyncio.wait_for(entered.wait(),timeout=5)
        assert (await http.post(f"/consents/{grants['assistant']['id']}/withdraw",headers=ph)).status_code==200
    finally:
        release.set()
    assert (await task).status_code==403
    async with sessions() as db:
        assert not (await db.scalars(select(DocumentSummary).where(DocumentSummary.document_id==doc_id))).all()
    assert (await http.post("/consents",headers=ph,json={"relationship_id":rel["id"],"scope":"assistant","purpose":"care","expires_at":time.time()+300})).status_code==201
    retry=await http.post(path,headers=dh,json=request)
    assert retry.status_code==201, retry.text


@pytest.mark.asyncio
async def test_mixed_ocr_is_separate_from_structured_transcription(client, monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    http, sessions=client
    monkeypatch.setattr(processing,"Session",sessions)
    monkeypatch.setenv("UPLOAD_DIR",str(tmp_path / "uploads"))
    patient=await register(http,"mixed-ocr@example.test","patient")
    headers=auth((await login(http,patient["email"]))["access_token"])
    upload=await http.post("/documents",headers=headers,files={"file":("test.pdf",b"%PDF-synthetic","application/pdf")})
    doc_id=upload.json()["id"]
    job=(await http.get(f"/documents/{doc_id}/processing",headers=headers)).json()["job_id"]
    output={"pages":[{"page_number":1,"method":"native","text":"Laboratory report\nTest name: Native test\nObserved value: 1", "supplemental_ocr_text":"Test name: OCR-only test\nObserved value: 2"}],"processor_details":{"pipeline":"native-ocr-v2","pdf_ocr_mode":"all","ocr":{"provider":"tesseract","version":"5.5.0","languages":["eng"]}}}
    monkeypatch.setattr(processing,"run_document_parser",lambda *args,**kwargs:SimpleNamespace(stdout=json.dumps(output).encode()))
    await processing.process_job(job)
    result=(await http.get(f"/documents/{doc_id}/processing",headers=headers)).json()
    assert result["status"]=="completed" and result["processor_details"]["ocr"]["languages"]==["eng"]
    assert result["processor_details"]["document_type"]["category"] == "laboratory_report"
    assert result["processor_details"]["document_type"]["evidence"]["quote"] == "Laboratory report"
    assert result["pages"][0]["supplemental_ocr_text"].startswith("Test name: OCR-only")
    extraction=result["structured_versions"][0]["data"]
    assert [item["test_name"]["value"] for item in extraction["laboratory_tests"]]==["Native test", "OCR-only test"]
    assert [item["test_name"]["evidence"]["layer"] for item in extraction["laboratory_tests"]] == ["primary", "supplemental_ocr"]
    assert extraction["schema_version"] == "1.1"
    assert any("duplicate or conflict" in warning for warning in extraction["warnings"])
    doctor = await register(http, "mixed-doctor@example.test", "doctor")
    doctor_headers = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=headers, json={"doctor_id": doctor["id"], "purpose": "care"})).json()
    for scope in ("read_documents", "review_documents"):
        grant = await http.post("/consents", headers=headers, json={"relationship_id": rel["id"], "scope": scope, "purpose": "care", "expires_at": time.time() + 300})
        assert grant.status_code == 201
    path = f"/documents/{doc_id}/processing/{job}/review"
    request = {"expected_version": 1, "data": extraction}
    assert (await http.post(path, headers=headers, json=request)).status_code == 403
    forged = json.loads(json.dumps(request))
    forged["data"]["laboratory_tests"][1]["test_name"]["evidence"]["layer"] = "primary"
    assert (await http.post(path, headers=doctor_headers, json=forged)).status_code == 422
    reviewed = await http.post(path, headers=doctor_headers, json=request)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review_status"] == "reviewed"


@pytest.mark.asyncio
async def test_profile_and_consultation_integrations_require_distinct_delegation(client, monkeypatch):
    http, sessions = client
    async def allow(*args): return True
    monkeypatch.setattr(integration_rate_limit, "check_rate_limit", allow)
    patient=await register(http,"history-integration-patient@example.test","patient")
    other=await register(http,"history-integration-other@example.test","patient")
    doctor=await register(http,"history-integration-doctor@example.test","doctor")
    ph=auth((await login(http,patient["email"]))["access_token"])
    oh=auth((await login(http,other["email"]))["access_token"])
    dh=auth((await login(http,doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User,doctor["id"])).verification_status="verified"; await db.commit()
    assert (await http.post("/profile/patient",headers=ph,json={"medical_history":"Synthetic history","allergies":"Synthetic allergy"})).status_code==200
    rel=(await http.post("/relationships",headers=ph,json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    grants={}
    async def grant(scope):
        response=await http.post("/consents",headers=ph,json={"relationship_id":rel["id"],"scope":scope,"purpose":"care","expires_at":time.time()+300})
        assert response.status_code==201, response.text
        grants[scope]=response.json()
    for scope in ("read_profile","consultations","integration_access"):
        await grant(scope)
    consult=await http.post(f"/patients/{patient['id']}/consultations",headers=dh,json={"title":"Synthetic consultation","occurred_at":time.time()-1})
    assert consult.status_code==201
    base={"label":"Synthetic client","patient_id":patient["id"],"expires_at":time.time()+300}
    profile_request={**base,"scopes":["profile:read"]}
    consult_request={**base,"scopes":["consultations:read"]}
    assert (await http.post("/integration-credentials",headers=dh,json=profile_request)).status_code==403
    assert (await http.post("/integration-credentials",headers=dh,json=consult_request)).status_code==403
    assert (await http.post("/integration-credentials",headers=ph,json={**profile_request,"patient_id":None})).status_code==422
    await grant("profile_integration")
    profile_credential=await http.post("/integration-credentials",headers=dh,json=profile_request)
    assert profile_credential.status_code==201, profile_credential.text
    profile_token=auth(profile_credential.json()["token"])
    profile_path=f"/integration/patients/{patient['id']}/profile"
    consult_path=f"/integration/patients/{patient['id']}/consultations"
    response=await http.get(profile_path,headers=profile_token)
    assert response.status_code==200 and response.headers["cache-control"]=="no-store"
    assert response.json()["medical_history"]=="Synthetic history" and "care_team" not in response.json()
    assert (await http.get(f"/integration/patients/{other['id']}/profile",headers=profile_token)).status_code==403
    assert (await http.get(consult_path,headers=profile_token)).status_code==403
    assert (await http.get(f"/integration/patients/{patient['id']}/documents",headers=profile_token)).status_code==403
    await grant("consultations_integration")
    consult_credential=await http.post("/integration-credentials",headers=dh,json=consult_request)
    assert consult_credential.status_code==201, consult_credential.text
    consult_token=auth(consult_credential.json()["token"])
    assert (await http.get(consult_path,headers=consult_token)).json()[0]["title"]=="Synthetic consultation"
    assert (await http.get(profile_path,headers=consult_token)).status_code==403
    await http.post(f"/consents/{grants['profile_integration']['id']}/withdraw",headers=ph)
    assert (await http.get(profile_path,headers=profile_token)).status_code==403
    assert (await http.get(consult_path,headers=consult_token)).status_code==200
    await http.post(f"/consents/{grants['consultations']['id']}/withdraw",headers=ph)
    assert (await http.get(consult_path,headers=consult_token)).status_code==403
    own=await http.post("/integration-credentials",headers=ph,json=profile_request)
    assert own.status_code==201
    assert (await http.get(profile_path,headers=auth(own.json()["token"]))).json()["allergies"]=="Synthetic allergy"
    assert (await http.get(profile_path,headers=oh)).status_code==401
    assert (await http.post(f"/integration-credentials/{own.json()['id']}/revoke",headers=ph)).status_code==200
    assert (await http.get(profile_path,headers=auth(own.json()["token"]))).status_code==401
    async with sessions() as db:
        actions=(await db.scalars(select(AuditEvent.action).where(AuditEvent.actor_id==doctor["id"]))).all()
        assert "integration_patient_profile" in actions and "integration_consultations" in actions


@pytest.mark.asyncio
async def test_note_source_link_requires_document_access_and_survives_amendment(client):
    http, sessions = client
    patient = await register(http, "source-note-patient@example.test", "patient")
    other = await register(http, "source-note-other@example.test", "patient")
    doctor = await register(http, "source-note-doctor@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    oh = auth((await login(http, other["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    grants = {}
    for scope in ("clinical_notes", "read_documents"):
        grants[scope] = (await http.post("/consents", headers=ph, json={"relationship_id":rel["id"],"scope":scope,"purpose":"care","expires_at":time.time()+300})).json()
    doc = (await http.post("/documents", headers=ph, files={"file":("source.pdf",b"%PDF-synthetic","application/pdf")})).json()
    other_doc = (await http.post("/documents", headers=oh, files={"file":("other.pdf",b"%PDF-synthetic","application/pdf")})).json()
    path = f"/patients/{patient['id']}/notes"
    assert (await http.post(path, headers=dh, json={"content":"Synthetic", "source_document_id":other_doc["id"]})).status_code == 404
    created = await http.post(path, headers=dh, json={"content":"Synthetic", "source_document_id":doc["id"]})
    assert created.status_code == 201, created.text
    note = created.json(); assert note["source_document_id"] == doc["id"]
    revision = await http.post(f"/notes/{note['note_id']}/versions/{note['version_id']}/revise", headers=dh, json={"content":"Revised"})
    assert revision.status_code == 201 and revision.json()["source_document_id"] == doc["id"]
    path_version = f"/notes/{note['note_id']}/versions/{revision.json()['version_id']}"
    assert (await http.post(path_version+"/submit",headers=dh)).status_code == 200
    assert (await http.post(path_version+"/review",headers=dh,json={"decision":"approve"})).status_code == 200
    await http.post(f"/consents/{grants['read_documents']['id']}/withdraw", headers=ph)
    assert (await http.get(f"/documents/{doc['id']}/download",headers=dh)).status_code == 403
    assert (await http.post(path,headers=dh,json={"content":"No access", "source_document_id":doc["id"]})).status_code == 403
    denied = await http.post(f"/notes/{note['note_id']}/versions",headers=dh,json={"content":"Amendment", "source_document_id":doc["id"]})
    assert denied.status_code == 403
    amendment = await http.post(f"/notes/{note['note_id']}/versions",headers=dh,json={"content":"Amendment"})
    assert amendment.status_code == 201 and amendment.json()["source_document_id"] == doc["id"]
    patient_notes = (await http.get(path,headers=ph)).json()
    assert patient_notes[0]["source_document_id"] == doc["id"]
    assert (await http.get(f"/documents/{doc['id']}/download",headers=ph)).status_code == 200


@pytest.mark.asyncio
async def test_scheduled_consent_enforces_start_expiry_and_withdrawal(client):
    http, sessions = client
    patient = await register(http, "schedule-patient@example.test", "patient")
    doctor = await register(http, "schedule-doctor@example.test", "doctor")
    ph = auth((await login(http, patient["email"]))["access_token"])
    dh = auth((await login(http, doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User, doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships",headers=ph,json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    now = time.time()
    data = {"relationship_id":rel["id"], "scope":"read_documents", "purpose":"care", "effective_at":now+120, "expires_at":now+300}
    for start, end in [(now-30,now+300), (now+300,now+300), (now+301,now+300)]:
        assert (await http.post("/consents",headers=ph,json={**data,"effective_at":start,"expires_at":end})).status_code == 422
    grant = await http.post("/consents",headers=ph,json=data)
    assert grant.status_code == 201 and grant.json()["effective_at"] == data["effective_at"]
    path = f"/patients/{patient['id']}/documents"
    assert (await http.get(path,headers=dh)).status_code == 403
    history = (await http.get("/consents",headers=ph)).json()
    assert history[0]["effective_at"] == data["effective_at"]
    async with sessions() as db:
        (await db.get(Consent,grant.json()["id"])).effective_at = time.time()-1
        await db.commit()
    assert (await http.get(path,headers=dh)).status_code == 200
    async with sessions() as db:
        (await db.get(Consent,grant.json()["id"])).expires_at = time.time()-1
        await db.commit()
    assert (await http.get(path,headers=dh)).status_code == 403
    async with sessions() as db:
        (await db.get(Consent,grant.json()["id"])).expires_at = time.time()+300
        await db.commit()
    assert (await http.post(f"/consents/{grant.json()['id']}/withdraw",headers=ph)).status_code == 200
    assert (await http.get(path,headers=dh)).status_code == 403
    future = await http.post("/consents",headers=ph,json=data)
    assert future.status_code == 201
    await http.post(f"/consents/{future.json()['id']}/withdraw",headers=ph)
    async with sessions() as db:
        (await db.get(Consent,future.json()["id"])).effective_at = time.time()-1
        await db.commit()
    assert (await http.get(path,headers=dh)).status_code == 403


@pytest.mark.asyncio
async def test_restore_care_relationship_requires_patient_and_fresh_consent(client):
    http, sessions = client
    patient = await register(http, "restore-patient@example.test", "patient")
    other = await register(http, "restore-other@example.test", "patient")
    doctor = await register(http, "restore-doctor@example.test", "doctor")
    ph = auth((await login(http,patient["email"]))["access_token"])
    oh = auth((await login(http,other["email"]))["access_token"])
    dh = auth((await login(http,doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User,doctor["id"])).verification_status = "verified"
        await db.commit()
    rel = (await http.post("/relationships", headers=ph, json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    data={"relationship_id":rel["id"],"scope":"read_documents","purpose":"care","expires_at":time.time()+300}
    grant=(await http.post("/consents",headers=ph,json=data)).json()
    scheduled=(await http.post("/consents",headers=ph,json={**data,"scope":"assistant","effective_at":time.time()+120})).json()
    path=f"/patients/{patient['id']}/documents"
    assert (await http.get(path,headers=dh)).status_code == 200
    assert (await http.post(f"/relationships/{rel['id']}/end",headers=dh)).status_code == 200
    assert (await http.get(path,headers=dh)).status_code == 403
    restore=f"/relationships/{rel['id']}/restore"
    assert (await http.post(restore,headers=dh)).status_code == 403
    assert (await http.post(restore,headers=oh)).status_code == 403
    async with sessions() as db:
        (await db.get(User,doctor["id"])).verification_status="rejected"
        await db.commit()
    assert (await http.post(restore,headers=ph)).status_code == 409
    async with sessions() as db:
        (await db.get(User,doctor["id"])).verification_status="verified"
        await db.commit()
    result=await http.post(restore,headers=ph)
    assert result.status_code == 200 and result.json()["previous_grants_withdrawn"] == 2
    assert (await http.post(restore,headers=ph)).status_code == 409
    assert (await http.get(path,headers=dh)).status_code == 403
    history={item["id"]:item for item in (await http.get("/consents",headers=ph)).json()}
    assert history[grant["id"]]["status"] == history[scheduled["id"]]["status"] == "withdrawn"
    assert history[scheduled["id"]]["withdrawn_at"] < history[scheduled["id"]]["effective_at"]
    assert (await http.post("/consents",headers=ph,json=data)).status_code == 201
    assert (await http.get(path,headers=dh)).status_code == 200
    events=(await http.get("/audit/self",headers=ph)).json()
    assert any(event["action"] == "relationship_restore" for event in events)
    assert sum(event["action"] == "consent_withdraw_on_relationship_restore" for event in events) == 2


@pytest.mark.asyncio
async def test_processing_records_pipe_table_transcription_and_parser_version(client, monkeypatch, tmp_path):
    http, sessions = client
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    patient = await register(http, "table-patient@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    table = "Test | Result | Unit | Reference range\nSynthetic analyte | 5 | mg/L | 2-7\nMedicine | Dose | Frequency\nSynthetic medicine | 5 mg | daily"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72), table)
        content = pdf.tobytes()
    doc = (await http.post("/documents", headers=headers, files={"file":("synthetic.pdf", content, "application/pdf")})).json()
    path = f"/documents/{doc['id']}/processing"
    job = (await http.get(path, headers=headers)).json()
    await processing.process_job(job["job_id"])
    response = (await http.get(path, headers=headers)).json()
    assert response["status"] == "completed" and response["processor_details"]["structured_parser"] == "explicit-labels-pipe-native-tables-v7"
    version = response["structured_versions"][0]
    assert version["review_status"] == "unverified"
    row = version["data"]["laboratory_tests"][0]
    assert row["observed_value"]["value"] == "5" and row["reference_range"]["value"] == "2-7"
    assert row["observed_value"]["evidence"]["page"] == 1 and row["abnormality_flag"] is None
    medicine = version["data"]["prescription_items"][0]
    assert medicine["medicine_name"]["value"] == "Synthetic medicine" and medicine["dosage"]["value"] == "5 mg"
    assert medicine["dosage"]["evidence"]["page"] == 1 and medicine["duration"] is None


@pytest.mark.asyncio
async def test_processing_warns_about_cross_page_orphaned_fields(client, monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    http, sessions = client
    monkeypatch.setattr(processing, "Session", sessions)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    patient = await register(http, "orphan-page@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    doc = (await http.post("/documents", headers=headers, files={"file":("synthetic.pdf",b"%PDF-synthetic","application/pdf")})).json()
    path=f"/documents/{doc['id']}/processing"
    job = (await http.get(path, headers=headers)).json()["job_id"]
    output={"pages":[{"page_number":1,"method":"native","text":"Test name: Synthetic test\nObserved value: 1"},{"page_number":2,"method":"native","text":"Unit: mg/dL"}],"processor_details":{"pipeline":"native-ocr-v2","pdf_ocr_mode":"fallback","ocr":None}}
    monkeypatch.setattr(processing,"run_document_parser",lambda *args,**kwargs:SimpleNamespace(stdout=json.dumps(output).encode()))
    await processing.process_job(job)
    result=(await http.get(path,headers=headers)).json()
    assert result["status"] == "completed"
    data=result["structured_versions"][0]["data"]
    assert data["laboratory_tests"][0]["unit"] is None
    assert any("without an item name" in warning for warning in data["warnings"])
    assert result["pages"][1]["text"] == "Unit: mg/dL"


@pytest.mark.asyncio
async def test_vision_processing_requires_explicit_current_configuration_and_keeps_layer_separate(client, monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    http, sessions=client
    monkeypatch.setattr(processing,"Session",sessions)
    monkeypatch.setenv("UPLOAD_DIR",str(tmp_path / "uploads"))
    monkeypatch.setenv("VISION_PROVIDER","ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL","http://localhost:11434")
    monkeypatch.setenv("OLLAMA_VISION_MODEL","synthetic-vision")
    patient=await register(http,"vision-patient@example.test","patient")
    other=await register(http,"vision-other@example.test","patient")
    ph=auth((await login(http,patient["email"]))["access_token"])
    oh=auth((await login(http,other["email"]))["access_token"])
    capability=(await http.get("/document-processing/capabilities",headers=ph)).json()
    assert capability["vision_available"] and capability["model"] == "synthetic-vision"
    document=(await http.post("/documents",headers=ph,files={"file":("synthetic.pdf",b"%PDF-synthetic","application/pdf")})).json()
    path=f"/documents/{document['id']}/processing"
    requested={"vision_requested":True,"vision_config_fingerprint":capability["configuration"]}
    assert (await http.post(path,headers=oh,json=requested)).status_code == 403
    assert (await http.post(path,headers=ph,json={"vision_requested":True})).status_code == 409
    assert (await http.post(path,headers=ph,json={**requested,"vision_config_fingerprint":"0"*64})).status_code == 409
    async with sessions() as db:
        initial=await db.scalar(select(ExtractionJob).where(ExtractionJob.document_id==document["id"], ExtractionJob.status=="queued"))
        initial.status="failed"
        await db.commit()
    job=(await http.post(path,headers=ph,json=requested)).json()["job_id"]
    output={"pages":[{"page_number":1,"method":"native","text":"Test name: Native\nObserved value: 1","vision_text":"Test name: Vision guess\nObserved value: 2","vision_warnings":["uncertain"]}],"processor_details":{"pipeline":"native-ocr-v2","pdf_ocr_mode":"fallback","ocr":None,"vision":{"provider":"ollama","model":"synthetic-vision","pages_processed":1,"truncated":False}}}
    seen=[]
    def parser(args, **kwargs):
        seen.append(args)
        return SimpleNamespace(stdout=json.dumps(output).encode())
    monkeypatch.setattr(processing,"run_document_parser",parser)
    await processing.process_job(job)
    assert seen[0][-1] == "vision"
    result=(await http.get(path,headers=ph)).json()
    assert result["status"]=="completed" and result["vision_requested"]
    assert result["pages"][0]["vision_text"].startswith("Test name: Vision")
    assert result["pages"][0]["vision_warnings"] == ["uncertain"]
    assert result["processor_details"]["vision"]["model"]=="synthetic-vision"
    data=result["structured_versions"][0]["data"]
    assert [item["test_name"]["value"] for item in data["laboratory_tests"]]==["Native"]
    assert any("not included" in warning for warning in data["warnings"])
    assert (await http.get(path,headers=oh)).status_code==403
    next_job=(await http.post(path,headers=ph,json=requested)).json()["job_id"]
    monkeypatch.setenv("OLLAMA_VISION_MODEL","changed-model")
    await processing.process_job(next_job)
    assert (await http.get(path,headers=ph)).json()["status"]=="failed"
    assert len(seen)==1


@pytest.mark.asyncio
async def test_vision_subprocess_through_api_and_clinician_review(client, monkeypatch, tmp_path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path,request))
            result={"done":True,"done_reason":"stop","message":{"role":"assistant","content":json.dumps({"text":"Synthetic vision reading","warnings":["Verify original"]})}}
            response=json.dumps(result).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(response))); self.end_headers(); self.wfile.write(response)
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    http,sessions=client
    monkeypatch.setattr(processing,"Session",sessions)
    monkeypatch.setenv("UPLOAD_DIR",str(tmp_path / "uploads"))
    monkeypatch.setenv("VISION_PROVIDER","ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL",f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("OLLAMA_VISION_MODEL","synthetic-vision")
    monkeypatch.setenv("OCR_PROVIDER","disabled")
    monkeypatch.setenv("PDF_OCR_MODE","fallback")
    try:
        patient=await register(http,"vision-e2e-patient@example.test","patient")
        doctor=await register(http,"vision-e2e-doctor@example.test","doctor")
        ph=auth((await login(http,patient["email"]))["access_token"])
        dh=auth((await login(http,doctor["email"]))["access_token"])
        async with sessions() as db:
            (await db.get(User,doctor["id"])).verification_status="verified"
            await db.commit()
        rel=(await http.post("/relationships",headers=ph,json={"doctor_id":doctor["id"],"purpose":"care"})).json()
        with pymupdf.open() as pdf:
            for i in range(4): pdf.new_page().insert_text((72,72),f"Test name: Synthetic {i}\nObserved value: 1")
            contents=pdf.tobytes()
        upload=await http.post("/documents",headers=ph,files={"file":("synthetic.pdf",contents,"application/pdf")})
        assert upload.status_code==201, upload.text
        path=f"/documents/{upload.json()['id']}/processing"
        initial=(await http.get(path,headers=ph)).json()["job_id"]
        await processing.process_job(initial)
        config=(await http.get("/document-processing/capabilities",headers=ph)).json()
        queued=await http.post(path,headers=ph,json={"vision_requested":True,"vision_config_fingerprint":config["configuration"]})
        assert queued.status_code==202, queued.text
        await processing.process_job(queued.json()["job_id"])
        result=(await http.get(path,headers=ph)).json()
        assert result["status"]=="completed", result
        assert result["processor_details"]["vision"]["truncated"] and len(calls)==3
        assert [p.get("vision_text") for p in result["pages"]]==["Synthetic vision reading"]*3+[None]
        assert all(p["vision_warnings"]==["Verify original"] for p in result["pages"][:3])
        assert all(item["test_name"]["value"].startswith("Synthetic") for item in result["structured_versions"][0]["data"]["laboratory_tests"])
        assert (await http.get(path,headers=dh)).status_code==403
        grants={}
        for scope in ("read_documents","review_documents"):
            grant=await http.post("/consents",headers=ph,json={"relationship_id":rel["id"],"scope":scope,"purpose":"care","expires_at":time.time()+300})
            assert grant.status_code==201
            grants[scope]=grant.json()
        viewed=await http.get(path,headers=dh)
        assert viewed.status_code==200 and viewed.json()["pages"][0]["vision_text"]=="Synthetic vision reading"
        data=viewed.json()["structured_versions"][0]["data"]
        data["laboratory_tests"][0]["observed_value"]={"value":"Clinician correction","origin":"clinician","evidence":None}
        reviewed=await http.post(f"{path}/{queued.json()['job_id']}/review",headers=dh,json={"expected_version":1,"data":data})
        assert reviewed.status_code==200, reviewed.text
        assert reviewed.json()["data"]["laboratory_tests"][0]["observed_value"]["origin"]=="clinician"
        await http.post(f"/consents/{grants['read_documents']['id']}/withdraw",headers=ph)
        assert (await http.get(path,headers=dh)).status_code==403
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


@pytest.mark.asyncio
async def test_logout_revokes_session_with_current_or_previous_refresh_credential(client):
    import jwt
    from app.main import SECRET
    http, _ = client
    patient = await register(http, "logout-fallback@example.test", "patient")
    session = await login(http, patient["email"])
    previous = session["refresh_token"]
    rotation = await http.post("/auth/refresh",json={"refresh_token":previous})
    assert rotation.status_code == 200
    current = rotation.json()["refresh_token"]
    assert (await http.post("/auth/refresh",json={"refresh_token":previous})).status_code == 401
    payload=jwt.decode(session["access_token"],SECRET,algorithms=["HS256"])
    payload["iat"]=int(time.time())-1000; payload["exp"]=int(time.time())-1
    expired=jwt.encode(payload,SECRET,algorithm="HS256")
    logout=await http.post("/auth/logout",headers=auth(expired),json={"refresh_token":previous})
    assert logout.status_code == 204, logout.text
    assert (await http.post("/auth/refresh",json={"refresh_token":current})).status_code == 401
    assert (await http.get("/me",headers=auth(rotation.json()["access_token"]))).status_code == 401
    assert (await http.post("/auth/logout",json={"refresh_token":previous})).status_code == 401
    other = await login(http, patient["email"])
    assert (await http.post("/auth/logout",json={"refresh_token":other["refresh_token"]})).status_code == 204
    assert (await http.get("/me",headers=auth(other["access_token"]))).status_code == 401


@pytest.mark.asyncio
async def test_synthetic_seed_uses_authorized_workflows(client, monkeypatch, tmp_path):
    from app.seed_synthetic import new_credentials, seed
    http, sessions=client
    monkeypatch.setattr(processing,"Session",sessions)
    monkeypatch.setenv("UPLOAD_DIR",str(tmp_path / "uploads"))
    async with sessions() as db:
        db.add(User(email="seed-admin@example.test",full_name="Synthetic Admin",role="admin",password_hash=hash_password("long-enough-password"),verification_status="not_applicable"))
        await db.commit()
    class ImmediateWorker:
        async def post(self,path,**kwargs):
            response=await http.post(path,**kwargs)
            if path == "/documents" and response.status_code == 201:
                result = await http.get(f"/documents/{response.json()['id']}/processing", headers=kwargs["headers"])
                await processing.process_job(result.json()["job_id"])
            return response
        async def get(self,path,**kwargs): return await http.get(path,**kwargs)
    credentials=new_credentials()
    result=await seed(ImmediateWorker(),"seed-admin@example.test","long-enough-password",credentials,wait_seconds=5)
    patient_login=await http.post("/auth/login",json={"email":credentials.patient_email,"password":credentials.patient_password})
    doctor_login=await http.post("/auth/login",json={"email":credentials.doctor_email,"password":credentials.doctor_password})
    assert patient_login.status_code==doctor_login.status_code==200
    ph=auth(patient_login.json()["access_token"]); dh=auth(doctor_login.json()["access_token"])
    assert doctor_login.json()["user"]["verification_status"] == "verified"
    path=f"/documents/{result['document_id']}/processing"
    document=(await http.get(path,headers=dh)).json()
    assert document["status"]=="completed"
    assert document["structured_versions"][0]["data"]["laboratory_tests"][0]["test_name"]["value"]=="Synthetic demonstration test"
    notes=(await http.get(f"/patients/{result['patient_id']}/notes",headers=ph)).json()
    assert len(notes)==1 and notes[0]["status"]=="approved" and notes[0]["source_document_id"]==result["document_id"]
    assert all(item["purpose"]=="Synthetic demonstration" for item in (await http.get("/consents",headers=ph)).json())


@pytest.mark.asyncio
async def test_new_document_hash_blocks_processing_of_same_size_changed_source(client,monkeypatch,tmp_path):
    import hashlib
    http,sessions=client
    monkeypatch.setattr(processing,"Session",sessions)
    monkeypatch.setenv("UPLOAD_DIR",str(tmp_path / "uploads"))
    patient=await register(http,"integrity-patient@example.test","patient")
    headers=auth((await login(http,patient["email"]))["access_token"])
    original=b"%PDF-synthetic-A"
    doc=(await http.post("/documents",headers=headers,files={"file":("synthetic.pdf",original,"application/pdf")})).json()
    async with sessions() as db:
        assert (await db.get(Document,doc["id"])).sha256 == hashlib.sha256(original).hexdigest()
    path=f"/documents/{doc['id']}/processing"
    job=(await http.get(path,headers=headers)).json()["job_id"]
    (tmp_path / "uploads" / doc["id"]).write_bytes(b"%PDF-synthetic-B")
    calls=[]
    monkeypatch.setattr(processing,"run_document_parser",lambda *args,**kwargs:calls.append(1))
    await processing.process_job(job)
    result=(await http.get(path,headers=headers)).json()
    assert result["status"] == "failed" and result["pages"] == []
    assert calls == []


@pytest.mark.asyncio
async def test_processing_parses_snapshot_and_rejects_changed_original(client,monkeypatch,tmp_path):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    http,sessions=client
    monkeypatch.setattr(processing,"Session",sessions)
    monkeypatch.setenv("UPLOAD_DIR",str(tmp_path / "uploads"))
    patient=await register(http,"snapshot-patient@example.test","patient")
    headers=auth((await login(http,patient["email"]))["access_token"])
    original=b"%PDF-synthetic-A"
    doc=(await http.post("/documents",headers=headers,files={"file":("synthetic.pdf",original,"application/pdf")})).json()
    path=f"/documents/{doc['id']}/processing"
    job=(await http.get(path,headers=headers)).json()["job_id"]
    source=tmp_path / "uploads" / doc["id"]
    seen=[]
    def parse(args,**kwargs):
        snapshot=Path(args[3]); seen.append(snapshot)
        assert snapshot != source and snapshot.read_bytes() == original
        assert snapshot.stat().st_mode & 0o077 == 0
        assert snapshot.parent.stat().st_mode & 0o077 == 0
        source.write_bytes(b"%PDF-synthetic-B")
        assert snapshot.read_bytes() == original
        return SimpleNamespace(stdout=json.dumps({"pages":[{"page_number":1,"method":"native","text":"Synthetic snapshot"}]}).encode())
    monkeypatch.setattr(processing,"run_document_parser",parse)
    await processing.process_job(job)
    result=(await http.get(path,headers=headers)).json()
    assert result["status"]=="failed" and result["pages"]==[]
    assert seen and not seen[0].exists() and not seen[0].parent.exists()


@pytest.mark.asyncio
async def test_sensitive_writes_fail_closed_but_withdrawal_remains_available(client,monkeypatch):
    from app import auth_rate_limit
    http,sessions=client
    patient=await register(http,"write-limit-patient@example.test","patient")
    doctor=await register(http,"write-limit-doctor@example.test","doctor")
    ph=auth((await login(http,patient["email"]))["access_token"])
    dh=auth((await login(http,doctor["email"]))["access_token"])
    async with sessions() as db:
        (await db.get(User,doctor["id"])).verification_status="verified"
        await db.commit()
    rel=(await http.post("/relationships",headers=ph,json={"doctor_id":doctor["id"],"purpose":"care"})).json()
    grant_data={"relationship_id":rel["id"],"scope":"read_documents","purpose":"care","expires_at":time.time()+300}
    grant=(await http.post("/consents",headers=ph,json=grant_data)).json()
    doc=(await http.post("/documents",headers=ph,files={"file":("synthetic.pdf",b"%PDF-synthetic","application/pdf")})).json()
    assert (await http.get(f"/patients/{patient['id']}/documents",headers=dh)).status_code == 200
    calls=[]
    async def limited(operation,address,identity,identity_limit,address_limit):
        calls.append((operation,identity,identity_limit,address_limit))
        return False
    monkeypatch.setattr(auth_rate_limit,"check",limited)
    assert (await http.post("/consents",headers=ph,json=grant_data)).status_code == 429
    assert (await http.post("/relationships",headers=ph,json={"doctor_id":doctor["id"],"purpose":"care"})).status_code == 429
    assert (await http.post("/documents",headers=ph,files={"file":("another.pdf",b"%PDF-synthetic","application/pdf")})).status_code == 429
    assert (await http.post(f"/documents/{doc['id']}/processing",headers=ph)).status_code == 429
    assert [call[0] for call in calls] == ["care-access-write","care-access-write","document-upload","document-processing"]
    assert all(call[1] == patient["id"] for call in calls)
    assert (await http.post(f"/consents/{grant['id']}/withdraw",headers=ph)).status_code == 200
    assert (await http.get(f"/patients/{patient['id']}/documents",headers=dh)).status_code == 403
    assert (await http.post(f"/relationships/{rel['id']}/end",headers=ph)).status_code == 200
    assert (await http.post(f"/relationships/{rel['id']}/restore",headers=ph)).status_code == 429
    async def unavailable(*args): raise auth_rate_limit.RateLimitUnavailable()
    monkeypatch.setattr(auth_rate_limit,"check",unavailable)
    assert (await http.post("/consents",headers=ph,json=grant_data)).status_code == 503
    async with sessions() as db:
        events=(await db.scalars(select(AuditEvent).where(AuditEvent.actor_id==patient["id"],AuditEvent.action.like("%_rate_limit")))).all()
        assert sum(event.outcome=="denied" for event in events)==5
        assert any(event.outcome=="failed" for event in events)
        assert all(event.purpose is None and "synthetic.pdf" not in event.resource for event in events)


@pytest.mark.asyncio
async def test_failed_reference_import_retry_requeues_and_preserves_integrity(client, monkeypatch, tmp_path):
    http, sessions = client
    import_dir = tmp_path / 'reference-retry'
    monkeypatch.setattr('app.main.KNOWLEDGE_IMPORT_DIR', import_dir)
    monkeypatch.setattr(processing, 'Session', sessions)
    monkeypatch.setenv('KNOWLEDGE_IMPORT_DIR', str(import_dir))
    patient = await register(http, 'retry-patient@example.test', 'patient')
    patient_headers = auth((await login(http, patient['email']))['access_token'])
    async with sessions() as db:
        admin = User(email='retry-admin@example.test', full_name='Synthetic Admin', role='admin', password_hash=hash_password('long-enough-password'), verification_status='not_applicable')
        db.add(admin)
        await db.commit()
        admin_id = admin.id
    admin_headers = auth((await login(http, 'retry-admin@example.test'))['access_token'])
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72), 'Synthetic reference retry')
        content = pdf.tobytes()
    uploaded = await http.post('/admin/knowledge/imports', headers=admin_headers, files={'file':('retry.pdf',content,'application/pdf')})
    job_id = uploaded.json()['id']
    retry_path = f'/admin/knowledge/imports/{job_id}/retry'
    assert (await http.post(retry_path, headers=patient_headers)).status_code == 403
    assert (await http.post(retry_path, headers=admin_headers)).status_code == 409
    original_snapshot = processing.snapshot_file
    def fail_snapshot(*args): raise OSError('synthetic transient failure')
    monkeypatch.setattr(processing, 'snapshot_file', fail_snapshot)
    await processing.process_knowledge_import(job_id)
    monkeypatch.setattr(processing, 'snapshot_file', original_snapshot)
    assert (await http.get(f'/admin/knowledge/imports/{job_id}',headers=admin_headers)).json()['status'] == 'failed'
    async with sessions() as db:
        entry = await db.scalar(select(JobOutbox).where(JobOutbox.target_id == job_id))
        entry.status = 'published'; entry.attempts = 4; entry.published_at = time.time()
        await db.commit()
    retried = await http.post(retry_path, headers=admin_headers)
    assert retried.status_code == 202 and retried.json()['status'] == 'queued'
    assert (await http.post(retry_path, headers=admin_headers)).status_code == 409
    async with sessions() as db:
        entry = await db.scalar(select(JobOutbox).where(JobOutbox.target_id == job_id))
        assert entry.status == 'pending' and entry.attempts == 0 and entry.published_at is None
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.correlation_id == retried.headers['x-request-id']))).all()
        assert any(event.action == 'knowledge_import_retry_requested' and event.actor_id == admin_id for event in events)
    await processing.process_knowledge_import(job_id)
    result = (await http.get(f'/admin/knowledge/imports/{job_id}', headers=admin_headers)).json()
    assert result['status'] == 'completed' and 'Synthetic reference retry' in result['extracted_text']
    assert (await http.post(retry_path, headers=admin_headers)).status_code == 409
    await http.post(f'/admin/knowledge/imports/{job_id}/delete', headers=admin_headers, json={'confirmation':'DELETE'})
    assert (await http.post(retry_path, headers=admin_headers)).status_code == 404


@pytest.mark.asyncio
async def test_independent_knowledge_approval_requires_another_administrator(client, monkeypatch):
    from app import main
    http, sessions = client
    monkeypatch.setattr(main, 'KNOWLEDGE_REVIEW_POLICY', 'independent')
    patient = await register(http, 'knowledge-policy-patient@example.test', 'patient')
    patient_auth = auth((await login(http, patient['email']))['access_token'])
    async with sessions() as db:
        for email in ('knowledge-creator@example.test', 'knowledge-reviewer@example.test'):
            db.add(User(email=email, full_name='Synthetic Admin', role='admin', password_hash=hash_password('long-enough-password'), verification_status='not_applicable'))
        await db.commit()
    creator = auth((await login(http, 'knowledge-creator@example.test'))['access_token'])
    reviewer = auth((await login(http, 'knowledge-reviewer@example.test'))['access_token'])
    assert (await http.get('/admin/knowledge/review-policy', headers=patient_auth)).status_code == 403
    policy = (await http.get('/admin/knowledge/review-policy', headers=creator)).json()
    assert policy['knowledge_review_policy'] == 'independent'
    metadata = {'title':'Synthetic reference', 'publisher':'Synthetic', 'provenance':'synthetic://reference', 'publication_date':'2025-01-01', 'specialty':'general', 'version':'1', 'content':'Synthetic reference text', 'effective_at':time.time()-1}
    created = await http.post('/admin/knowledge', headers=creator, json=metadata)
    source_id = created.json()['id']
    async def embedding(texts): return [[1.0]+[0.0]*383 for _ in texts]
    async def digest(): return 'a'*64
    monkeypatch.setattr(knowledge, 'embedding_digest', digest)
    monkeypatch.setattr(knowledge, 'embed_texts', embedding)
    assert (await http.post(f'/admin/knowledge/{source_id}/index', headers=creator)).status_code == 200
    denied = await http.post(f'/admin/knowledge/{source_id}/approve', headers=creator)
    assert denied.status_code == 403
    async with sessions() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.correlation_id == denied.headers['x-request-id']))).all()
        assert any(event.action == 'knowledge_approve' and event.outcome == 'denied' for event in events)
    listed = (await http.get('/admin/knowledge', headers=reviewer)).json()
    assert listed[0]['created_by'] == policy['administrator_id'] and listed[0]['status'] == 'draft'
    approved = await http.post(f'/admin/knowledge/{source_id}/approve', headers=reviewer)
    assert approved.status_code == 200 and approved.json()['status'] == 'approved'
    assert approved.json()['reviewer_id'] != policy['administrator_id']


@pytest.mark.asyncio
async def test_reference_download_checks_integrity_and_deletion_race(client, monkeypatch, tmp_path):
    import threading
    from app import file_integrity
    http, sessions = client
    import_dir = tmp_path / 'reference-download'
    monkeypatch.setattr('app.main.KNOWLEDGE_IMPORT_DIR', import_dir)
    async with sessions() as db:
        db.add(User(email='download-reference-admin@example.test', full_name='Synthetic Admin', role='admin', password_hash=hash_password('long-enough-password'), verification_status='not_applicable'))
        await db.commit()
    headers = auth((await login(http, 'download-reference-admin@example.test'))['access_token'])
    patient = await register(http, 'download-reference-patient@example.test', 'patient')
    patient_headers = auth((await login(http, patient['email']))['access_token'])
    content = b'%PDF-synthetic-reference'
    upload = await http.post('/admin/knowledge/imports', headers=headers, files={'file':('reference.pdf', content, 'application/pdf')})
    job_id = upload.json()['id']
    url = f'/admin/knowledge/imports/{job_id}/download'
    result = await http.get(url, headers=headers)
    assert result.status_code == 200 and result.content == content
    assert result.headers['cache-control'] == 'no-store' and 'attachment' in result.headers['content-disposition']
    assert (await http.get(url, headers=patient_headers)).status_code == 403
    path = import_dir / job_id
    path.write_bytes(b'%PDF-modified-reference!')
    assert (await http.get(url, headers=headers)).status_code == 409
    path.unlink()
    target = tmp_path / 'reference-target'
    target.write_bytes(content)
    path.symlink_to(target)
    assert (await http.get(url, headers=headers)).status_code == 409
    path.unlink(); path.write_bytes(content)
    entered, release = threading.Event(), threading.Event()
    original = file_integrity.read_snapshot
    def delayed(*args):
        snapshot = original(*args)
        entered.set()
        assert release.wait(5)
        return snapshot
    monkeypatch.setattr(file_integrity, 'read_snapshot', delayed)
    pending = asyncio.create_task(http.get(url, headers=headers))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (await http.post(f'/admin/knowledge/imports/{job_id}/delete', headers=headers, json={'confirmation':'DELETE'})).status_code == 202
    finally:
        release.set()
    response = await pending
    assert response.status_code == 409 and content not in response.content
    async with sessions() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.action == 'knowledge_import_download_integrity'))).all()
        assert len(events) == 2 and all(event.outcome == 'failed' for event in events)


@pytest.mark.asyncio
async def test_notification_pagination_enforces_current_consent(client):
    from app.models import Notification
    http, sessions = client
    patient = await register(http, 'notify-page-patient@example.test', 'patient')
    doctor = await register(http, 'notify-page-doctor@example.test', 'doctor')
    other = await register(http, 'notify-page-other@example.test', 'patient')
    ph = auth((await login(http, patient['email']))['access_token'])
    dh = auth((await login(http, doctor['email']))['access_token'])
    oh = auth((await login(http, other['email']))['access_token'])
    async with sessions() as db:
        (await db.get(User, doctor['id'])).verification_status = 'verified'
        await db.commit()
    rel = (await http.post('/relationships', headers=ph, json={'doctor_id':doctor['id'],'purpose':'care'})).json()
    grant = (await http.post('/consents', headers=ph, json={'relationship_id':rel['id'],'scope':'clinical_notes','purpose':'care','expires_at':time.time()+300})).json()
    async with sessions() as db:
        now = time.time()+1
        for index in range(105):
            db.add(Notification(recipient_id=doctor['id'], patient_id=patient['id'], relationship_id=rel['id'], required_scope='clinical_notes', kind='note_review_requested', event_key=f'synthetic-page:{index}', resource_id=str(uuid.uuid4()), created_at=now))
        await db.commit()
    first = await http.get('/notifications', headers=dh)
    assert first.status_code == 200 and len(first.json()) == 100 and first.headers['cache-control'] == 'no-store'
    cursor = first.headers['x-next-cursor']
    second = await http.get('/notifications', headers=dh, params={'cursor':cursor})
    assert second.status_code == 200 and len(second.json()) == 7 and 'x-next-cursor' not in second.headers
    assert not ({item['id'] for item in first.json()} & {item['id'] for item in second.json()})
    assert (await http.get('/notifications', headers=oh, params={'cursor':cursor})).status_code == 422
    assert (await http.get('/notifications', headers=dh, params={'cursor':'bad'})).status_code == 422
    assert (await http.post(f"/consents/{grant['id']}/withdraw", headers=ph)).status_code == 200
    assert (await http.get('/notifications', headers=dh, params={'cursor':cursor})).status_code == 422
    assert not any(item['kind']=='note_review_requested' for item in (await http.get('/notifications',headers=dh)).json())


@pytest.mark.asyncio
async def test_note_processing_source_is_patient_scoped_and_versioned(client, monkeypatch, tmp_path):
    http, sessions = client
    monkeypatch.setattr(processing, 'Session', sessions)
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path / 'uploads'))
    patient = await register(http, 'note-run-patient@example.test', 'patient')
    doctor = await register(http, 'note-run-doctor@example.test', 'doctor')
    other = await register(http, 'note-run-other@example.test', 'patient')
    ph = auth((await login(http, patient['email']))['access_token'])
    dh = auth((await login(http, doctor['email']))['access_token'])
    oh = auth((await login(http, other['email']))['access_token'])
    async with sessions() as db:
        (await db.get(User, doctor['id'])).verification_status = 'verified'
        await db.commit()
    rel = (await http.post('/relationships', headers=ph, json={'doctor_id':doctor['id'],'purpose':'care'})).json()
    for scope in ('clinical_notes', 'read_documents', 'review_documents'):
        assert (await http.post('/consents', headers=ph, json={'relationship_id':rel['id'],'scope':scope,'purpose':'care','expires_at':time.time()+300})).status_code == 201
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72),'Test name: Synthetic test\nObserved value: 5')
        content = pdf.tobytes()
    document = (await http.post('/documents', headers=ph, files={'file':('report.pdf',content,'application/pdf')})).json()
    other_doc = (await http.post('/documents', headers=oh, files={'file':('other.pdf',content,'application/pdf')})).json()
    path=f"/documents/{document['id']}/processing"
    job = (await http.get(path,headers=ph)).json()['job_id']
    notes=f"/patients/{patient['id']}/notes"
    payload={'content':'Synthetic note','source_document_id':document['id'],'source_processing_job_id':job,'source_extraction_version':1,'source_fields':[{'group':'laboratory_tests','index':0,'field':'observed_value'}]}
    assert (await http.post(notes,headers=dh,json=payload)).status_code == 404
    await processing.process_job(job)
    assert (await http.post(notes,headers=dh,json={**payload,'source_document_id':None})).status_code == 422
    assert (await http.post(notes,headers=dh,json={**payload,'source_document_id':other_doc['id']})).status_code == 404
    assert (await http.post(notes,headers=dh,json={**payload,'source_extraction_version':2})).status_code == 404
    assert (await http.post(notes,headers=dh,json={**payload,'source_processing_job_id':None})).status_code == 422
    assert (await http.post(notes,headers=dh,json={**payload,'source_fields':[{'group':'prescription_items','index':0,'field':'observed_value'}]})).status_code == 422
    assert (await http.post(notes,headers=dh,json={**payload,'source_fields':payload['source_fields']*2})).status_code == 422
    assert (await http.post(notes,headers=dh,json={**payload,'source_extraction_version':None})).status_code == 422
    quote_field={**payload['source_fields'][0], 'note_quote':'Synthetic note'}
    assert (await http.post(notes,headers=dh,json={**payload,'source_fields':[dict(quote_field,note_quote='missing')]})).status_code == 422
    assert (await http.post(notes,headers=dh,json={**payload,'content':'Synthetic note Synthetic note','source_fields':[quote_field]})).status_code == 422
    quoted=await http.post(notes,headers=dh,json={**payload,'source_fields':[quote_field]})
    assert quoted.status_code == 201 and quoted.json()['source_fields'][0]['note_quote']=='Synthetic note'
    quoted_path=f"/notes/{quoted.json()['note_id']}/versions/{quoted.json()['version_id']}/revise"
    assert (await http.post(quoted_path,headers=dh,json={'content':'Changed statement'})).status_code == 422
    revised_quote=await http.post(quoted_path,headers=dh,json={'content':'Changed statement','clear_document_source':True})
    assert revised_quote.status_code == 201 and revised_quote.json()['source_fields']==[]
    created = await http.post(notes,headers=dh,json=payload)
    assert created.status_code == 201 and created.json()['source_processing_job_id'] == job
    assert created.json()['source_extraction_version'] == 1 and created.json()['source_fields'] == payload['source_fields']
    extraction = (await http.get(path,headers=dh)).json()['structured_versions'][0]['data']
    assert (await http.post(f'{path}/{job}/review',headers=dh,json={'expected_version':1,'data':extraction})).status_code == 200
    note=created.json()
    revised = await http.post(f"/notes/{note['note_id']}/versions/{note['version_id']}/revise",headers=dh,json={'content':'Synthetic revised note'})
    assert revised.status_code == 201 and revised.json()['source_processing_job_id'] == job and revised.json()['source_extraction_version'] == 1 and revised.json()['source_fields'] == payload['source_fields']
    version=revised.json()['version_id']
    assert (await http.post(f"/notes/{note['note_id']}/versions/{version}/submit",headers=dh)).status_code == 200
    assert (await http.post(f"/notes/{note['note_id']}/versions/{version}/review",headers=dh,json={'decision':'approve'})).status_code == 200
    amended = await http.post(f"/notes/{note['note_id']}/versions",headers=dh,json={'content':'Synthetic amendment'})
    assert amended.status_code == 201 and amended.json()['source_processing_job_id'] == job and amended.json()['source_extraction_version'] == 1 and amended.json()['source_fields'] == payload['source_fields']
    assert (await http.post(path,headers=ph)).status_code == 202
    historical = await http.get(path,headers=dh,params={'job_id':job})
    assert historical.status_code == 200 and historical.json()['is_latest'] is False
    listed = (await http.get(notes,headers=dh)).json()
    assert all(item['source_processing_job_id'] == job and item['source_extraction_version'] == 1 for item in listed if item['note_id'] == note['note_id'])
    assert [v['version'] for v in historical.json()['structured_versions']] == [2,1]
    async def allow_integration(*args): return True
    monkeypatch.setattr(integration_rate_limit, 'check_rate_limit', allow_integration)
    credential = (await http.post('/integration-credentials', headers=ph, json={'label':'Synthetic historical client','patient_id':patient['id'],'scopes':['documents:read'],'expires_at':time.time()+300})).json()
    ih={'Authorization':f"Bearer {credential['token']}"}
    base=f"/integration/documents/{document['id']}/processing"
    history=await http.get(base+'/history',headers=ih)
    assert history.status_code == 200 and any(item['id']==job for item in history.json()['items'])
    old=await http.get(base,headers=ih,params={'job_id':job})
    assert old.status_code == 200 and old.json()['is_latest'] is False
    assert [item['version'] for item in old.json()['structured_versions']] == [2,1]
    assert (await http.get(base,headers=ih,params={'job_id':str(uuid.uuid4())})).status_code == 404
    assert (await http.get(f"/integration/documents/{other_doc['id']}/processing/history",headers=ih)).status_code == 403
    assert (await http.post(f"/integration-credentials/{credential['id']}/revoke",headers=ph)).status_code == 200
    assert (await http.get(base+'/history',headers=ih)).status_code == 401
    cleared = await http.post(f"/notes/{note['note_id']}/versions/{amended.json()['version_id']}/revise", headers=dh, json={'content':'No source used in this revision','clear_document_source':True})
    assert cleared.status_code == 201
    assert cleared.json()['source_document_id'] is None and cleared.json()['source_processing_job_id'] is None
    assert cleared.json()['source_extraction_version'] is None and cleared.json()['source_fields'] == []
    saved = (await http.get(notes,headers=dh)).json()
    old = next(item for item in saved if item['version_id'] == amended.json()['version_id'])
    assert old['source_processing_job_id'] == job and old['source_fields'] == payload['source_fields']


@pytest.mark.asyncio
async def test_extracted_records_pagination_enforces_patient_and_consent(client):
    from app.models import StructuredExtractionVersion
    http, sessions = client
    patient = await register(http, 'extracted-page-patient@example.test', 'patient')
    other = await register(http, 'extracted-page-other@example.test', 'patient')
    doctor = await register(http, 'extracted-page-doctor@example.test', 'doctor')
    ph = auth((await login(http, patient['email']))['access_token'])
    oh = auth((await login(http, other['email']))['access_token'])
    dh = auth((await login(http, doctor['email']))['access_token'])
    async with sessions() as db:
        (await db.get(User, doctor['id'])).verification_status='verified'
        for index in range(27):
            document=Document(id=str(uuid.uuid4()),patient_id=patient['id'],filename=f'synthetic-{index}.pdf',content_type='application/pdf',size=10,created_at=1000)
            db.add(document)
            job=ExtractionJob(id=str(uuid.uuid4()),document_id=document.id,requested_by=patient['id'],status='completed')
            db.add(job)
            db.add(StructuredExtractionVersion(job_id=job.id,version=1,review_status='unverified',data={'schema_version':'1.0','laboratory_tests':[],'prescription_items':[],'warnings':[]}))
        await db.commit()
    rel=(await http.post('/relationships',headers=ph,json={'doctor_id':doctor['id'],'purpose':'care'})).json()
    grant=(await http.post('/consents',headers=ph,json={'relationship_id':rel['id'],'scope':'read_documents','purpose':'care','expires_at':time.time()+300})).json()
    url=f"/patients/{patient['id']}/extracted-records"
    first=await http.get(url,headers=dh)
    assert first.status_code == 200 and len(first.json()['documents']) == 25 and first.json()['truncated']
    cursor=first.json()['next_cursor']
    second=await http.get(url,headers=dh,params={'cursor':cursor})
    assert second.status_code == 200 and len(second.json()['documents']) == 2 and second.json()['next_cursor'] is None
    assert not ({item['document_id'] for item in first.json()['documents']} & {item['document_id'] for item in second.json()['documents']})
    assert (await http.get(url,headers=oh,params={'cursor':cursor})).status_code == 403
    assert (await http.get(f"/patients/{other['id']}/extracted-records",headers=oh,params={'cursor':cursor})).status_code == 422
    assert (await http.post(f"/consents/{grant['id']}/withdraw",headers=ph)).status_code == 200
    assert (await http.get(url,headers=dh,params={'cursor':cursor})).status_code == 403


@pytest.mark.asyncio
async def test_document_list_pages_are_patient_scoped_and_consent_checked(client):
    http, sessions=client
    patient=await register(http,'document-page-patient@example.test','patient')
    other=await register(http,'document-page-other@example.test','patient')
    doctor=await register(http,'document-page-doctor@example.test','doctor')
    ph=auth((await login(http,patient['email']))['access_token'])
    oh=auth((await login(http,other['email']))['access_token'])
    dh=auth((await login(http,doctor['email']))['access_token'])
    async with sessions() as db:
        (await db.get(User,doctor['id'])).verification_status='verified'
        for index in range(103):
            db.add(Document(id=str(uuid.uuid4()),patient_id=patient['id'],filename=f'synthetic-{index}.docx',content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',size=10,created_at=1000))
        await db.commit()
    rel=(await http.post('/relationships',headers=ph,json={'doctor_id':doctor['id'],'purpose':'care'})).json()
    grant=(await http.post('/consents',headers=ph,json={'relationship_id':rel['id'],'scope':'read_documents','purpose':'care','expires_at':time.time()+300})).json()
    url=f"/patients/{patient['id']}/documents"
    first=await http.get(url,headers=dh)
    assert first.status_code==200 and len(first.json())==100 and first.headers['cache-control']=='no-store'
    cursor=first.headers['x-next-cursor']
    second=await http.get(url,headers=dh,params={'cursor':cursor})
    assert len(second.json())==3 and 'x-next-cursor' not in second.headers
    assert not ({d['id'] for d in first.json()} & {d['id'] for d in second.json()})
    async def allow_integration(*args): return True
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(integration_rate_limit, 'check_rate_limit', allow_integration)
    try:
        token = (await http.post('/integration-credentials', headers=ph, json={'label':'Synthetic page client','patient_id':patient['id'],'scopes':['documents:read'],'expires_at':time.time()+300})).json()['token']
        integration_headers = {'Authorization':f'Bearer {token}'}
        first_integration = await http.get(f"/integration/patients/{patient['id']}/documents",headers=integration_headers)
        assert first_integration.status_code == 200 and len(first_integration.json()) == 100
        next_page = await http.get(f"/integration/patients/{patient['id']}/documents",headers=integration_headers,params={'cursor':first_integration.headers['x-next-cursor']})
        assert next_page.status_code == 200 and len(next_page.json()) == 3
        processing = await http.get(f"/integration/documents/{next_page.json()[0]['id']}/processing",headers=integration_headers)
        assert processing.status_code == 200 and processing.json()['status'] == 'not_requested'
    finally:
        monkeypatch.undo()
    old=second.json()[0]
    processing=(await http.get(f"/documents/{old['id']}/processing",headers=dh)).json()
    assert processing['status']=='not_requested' and processing['document']['content_type']==old['content_type']
    assert (await http.get(f"/patients/{other['id']}/documents",headers=oh,params={'cursor':cursor})).status_code==422
    assert (await http.post(f"/consents/{grant['id']}/withdraw",headers=ph)).status_code==200
    assert (await http.get(url,headers=dh,params={'cursor':cursor})).status_code==403


@pytest.mark.asyncio
async def test_patient_access_history_is_scoped_redacted_and_paginated(client):
    http, sessions=client
    patient=await register(http,'audit-patient@example.test','patient')
    other=await register(http,'audit-other@example.test','patient')
    doctor=await register(http,'audit-doctor@example.test','doctor')
    ph=auth((await login(http,patient['email']))['access_token'])
    oh=auth((await login(http,other['email']))['access_token'])
    dh=auth((await login(http,doctor['email']))['access_token'])
    async with sessions() as db:
        for index in range(55):
            db.add(AuditEvent(actor_id=doctor['id'],patient_id=patient['id'],action='document_processing_read',resource='document:synthetic-secret',outcome='success',purpose='synthetic private purpose',created_at=1000))
        db.add(AuditEvent(actor_id=doctor['id'],patient_id=patient['id'],action='document_processing_read',resource='secret',outcome='denied',created_at=1001))
        db.add(AuditEvent(actor_id=other['id'],patient_id=patient['id'],action='document_processing_read',resource='secret',outcome='success',created_at=1001))
        db.add(AuditEvent(actor_id=doctor['id'],patient_id=other['id'],action='document_processing_read',resource='secret',outcome='success',created_at=1001))
        await db.commit()
    first=await http.get('/audit/patient-access',headers=ph)
    assert first.status_code == 200 and len(first.json()) == 50 and first.headers['cache-control']=='no-store'
    assert 'synthetic-secret' not in first.text and 'private purpose' not in first.text
    assert all(set(item)=={'id','actor_id','action','created_at'} and item['actor_id']==doctor['id'] for item in first.json())
    cursor=first.headers['x-next-cursor']
    second=await http.get('/audit/patient-access',headers=ph,params={'cursor':cursor})
    assert len(second.json()) == 5 and 'x-next-cursor' not in second.headers
    assert not ({item['id'] for item in first.json()} & {item['id'] for item in second.json()})
    assert (await http.get('/audit/patient-access',headers=oh,params={'cursor':cursor})).status_code == 422
    assert (await http.get('/audit/patient-access',headers=dh)).status_code == 403


@pytest.mark.asyncio
async def test_assigned_clinical_knowledge_review_gates_approval(client, monkeypatch):
    from app import main
    http, sessions = client
    monkeypatch.setattr(main, 'KNOWLEDGE_CLINICAL_REVIEW_POLICY', 'required')
    patient=await register(http,'knowledge-clinical-patient@example.test','patient')
    doctors=[await register(http,f'knowledge-review-{i}@example.test','doctor') for i in range(5)]
    ph=auth((await login(http,patient['email']))['access_token'])
    auths=[auth((await login(http,doctor['email']))['access_token']) for doctor in doctors]
    async with sessions() as db:
        for doctor in doctors:
            (await db.get(User,doctor['id'])).verification_status='verified'
        db.add(User(email='knowledge-clinical-admin@example.test',full_name='Synthetic Admin',role='admin',password_hash=hash_password('long-enough-password'),verification_status='not_applicable'))
        await db.commit()
    admin=auth((await login(http,'knowledge-clinical-admin@example.test'))['access_token'])
    source={'title':'Synthetic reference','publisher':'Synthetic publisher','provenance':'synthetic://reference','publication_date':'2025-01-01','specialty':'general','version':'1','content':'Synthetic reference text','effective_at':time.time()-1}
    source_id=(await http.post('/admin/knowledge',headers=admin,json=source)).json()['id']
    async def embed(texts): return [[1.0]+[0.0]*383 for _ in texts]
    async def digest(): return 'a'*64
    monkeypatch.setattr(knowledge,'embed_texts',embed)
    monkeypatch.setattr(knowledge,'embedding_digest',digest)
    assert (await http.post(f'/admin/knowledge/{source_id}/index',headers=admin)).status_code == 200
    assert (await http.post(f'/admin/knowledge/{source_id}/approve',headers=admin)).status_code == 409
    path=f'/admin/knowledge/{source_id}/clinical-reviews'
    assert (await http.post(path,headers=ph,json={'doctor_id':doctors[0]['id']})).status_code == 403
    first=await http.post(path,headers=admin,json={'doctor_id':doctors[0]['id']})
    assert first.status_code == 201
    assert (await http.post(path,headers=admin,json={'doctor_id':doctors[0]['id']})).status_code == 409
    assert (await http.get('/knowledge/clinical-reviews',headers=ph)).status_code == 403
    assert (await http.get('/knowledge/clinical-reviews',headers=auths[1])).json() == []
    assigned=(await http.get('/knowledge/clinical-reviews',headers=auths[0])).json()
    assert assigned[0]['source']['content'] == 'Synthetic reference text'
    decision=f"/knowledge/clinical-reviews/{first.json()['id']}/decision"
    assert (await http.post(decision,headers=auths[1],json={'decision':'recommended'})).status_code == 404
    from app.models import KnowledgeSource
    async with sessions() as db:
        row=await db.get(KnowledgeSource,source_id)
        row.content='Altered synthetic reference'
        await db.commit()
    changed=(await http.get('/knowledge/clinical-reviews',headers=auths[0])).json()
    assert changed[0]['source_matches'] is False
    assert (await http.post(decision,headers=auths[0],json={'decision':'recommended'})).status_code == 409
    async with sessions() as db:
        (await db.get(KnowledgeSource,source_id)).content='Synthetic reference text'
        await db.commit()
    assert (await http.post(decision,headers=auths[0],json={'decision':'recommended'})).status_code == 200
    assert (await http.post(decision,headers=auths[0],json={'decision':'recommended'})).status_code == 409
    from app.models import KnowledgeClinicalReview
    async with sessions() as db:
        review=await db.get(KnowledgeClinicalReview,first.json()['id'])
        saved_fingerprint=review.source_fingerprint
        review.source_fingerprint=None
        await db.commit()
    assert (await http.post(f'/admin/knowledge/{source_id}/approve',headers=admin)).status_code == 409
    assert (await http.get(path,headers=admin)).json()[0]['source_matches'] is False
    async with sessions() as db:
        (await db.get(KnowledgeClinicalReview,first.json()['id'])).source_fingerprint=saved_fingerprint
        await db.commit()
    async with sessions() as db:
        (await db.get(KnowledgeSource,source_id)).provenance='synthetic://changed'
        await db.commit()
    assert (await http.post(f'/admin/knowledge/{source_id}/approve',headers=admin)).status_code == 409
    async with sessions() as db:
        (await db.get(KnowledgeSource,source_id)).provenance='synthetic://reference'
        await db.commit()
    second=await http.post(path,headers=admin,json={'doctor_id':doctors[1]['id']})
    assert second.status_code == 201
    assert (await http.post(f'/admin/knowledge/{source_id}/approve',headers=admin)).status_code == 409
    second_decision=f"/knowledge/clinical-reviews/{second.json()['id']}/decision"
    assert (await http.post(second_decision,headers=auths[1],json={'decision':'declined'})).status_code == 422
    assert (await http.post(second_decision,headers=auths[1],json={'decision':'declined','comment':'Synthetic concern'})).status_code == 200
    assert (await http.post(f'/admin/knowledge/{source_id}/approve',headers=admin)).status_code == 409
    third=await http.post(path,headers=admin,json={'doctor_id':doctors[2]['id']})
    assert (await http.post(f"/knowledge/clinical-reviews/{third.json()['id']}/decision",headers=auths[2],json={'decision':'recommended'})).status_code == 200
    verification=f"/admin/doctors/{doctors[2]['id']}/verification"
    assert (await http.post(verification,headers=admin,json={'verified':False,'expected_status':'verified','expected_profile_revision':0,'expected_verification_revision':0})).status_code == 200
    assert (await http.post(f'/admin/knowledge/{source_id}/approve',headers=admin)).status_code == 409
    assert (await http.post(verification,headers=admin,json={'verified':True,'expected_status':'rejected','expected_profile_revision':0,'expected_verification_revision':1})).status_code == 200
    assert (await http.post(f'/admin/knowledge/{source_id}/approve',headers=admin)).status_code == 409
    assert (await http.get(path,headers=admin)).json()[0]['verification_matches'] is False
    fourth=await http.post(path,headers=admin,json={'doctor_id':doctors[3]['id']})
    verification=f"/admin/doctors/{doctors[3]['id']}/verification"
    assert (await http.post(verification,headers=admin,json={'verified':True,'expected_status':'verified','expected_profile_revision':0,'expected_verification_revision':0})).status_code == 200
    assert (await http.get('/knowledge/clinical-reviews',headers=auths[3])).json()[0]['verification_matches'] is False
    assert (await http.post(f"/knowledge/clinical-reviews/{fourth.json()['id']}/decision",headers=auths[3],json={'decision':'recommended'})).status_code == 409
    fifth=await http.post(path,headers=admin,json={'doctor_id':doctors[4]['id']})
    assert (await http.post(f"/knowledge/clinical-reviews/{fifth.json()['id']}/decision",headers=auths[4],json={'decision':'recommended'})).status_code == 200
    approved=await http.post(f'/admin/knowledge/{source_id}/approve',headers=admin)
    assert approved.status_code == 200 and approved.json()['status']=='approved'
    history=(await http.get(path,headers=admin)).json()
    assert [item['status'] for item in history] == ['recommended','pending','recommended','declined','recommended']
    assert (await http.post(path,headers=admin,json={'doctor_id':str(uuid.uuid4())})).status_code == 409


@pytest.mark.asyncio
async def test_background_knowledge_index_dispatch_recovery_and_authorization(client, monkeypatch):
    from app import knowledge_indexing
    from app.models import KnowledgeChunk, KnowledgeIndexJob, KnowledgeSource
    http, sessions = client
    async with sessions() as db:
        db.add(User(email='index-background@example.test', full_name='Synthetic Admin', role='admin', password_hash=hash_password('long-enough-password'), verification_status='not_applicable'))
        await db.commit()
    headers = auth((await login(http, 'index-background@example.test'))['access_token'])
    data = {'title':'Synthetic reference','publisher':'Synthetic','provenance':'synthetic://ref','publication_date':'2025-01-01','specialty':'general','version':'1','content':'Synthetic background indexing text','effective_at':time.time()-1}
    source_id = (await http.post('/admin/knowledge', headers=headers, json=data)).json()['id']
    path = f'/admin/knowledge/{source_id}/index-jobs'
    response = await http.post(path, headers=headers)
    assert response.status_code == 202
    job_id = response.json()['id']
    assert (await http.post(path, headers=headers)).status_code == 409
    published = []
    async def publish(identifier): published.append(identifier)
    assert await outbox_dispatcher.dispatch_index_one(sessions, publish)
    assert published == [job_id]
    assert not await outbox_dispatcher.dispatch_index_one(sessions, publish)
    entered, release = asyncio.Event(), asyncio.Event()
    async def embedding(texts):
        entered.set()
        await release.wait()
        return [[1.0]+[0.0]*383 for _ in texts]
    async def digest(): return 'a'*64
    monkeypatch.setattr(knowledge, 'embed_texts', embedding)
    monkeypatch.setattr(knowledge, 'embedding_digest', digest)
    task = asyncio.create_task(knowledge_indexing.process_index(job_id, sessions))
    await asyncio.wait_for(entered.wait(), 5)
    assert await outbox_dispatcher.recover_index_jobs(sessions, now=time.time()+301) == 1
    release.set()
    await asyncio.wait_for(task, 5)
    async with sessions() as db:
        assert (await db.get(KnowledgeIndexJob, job_id)).status == 'queued'
        assert (await db.get(KnowledgeSource, source_id)).index_status == 'not_indexed'
        assert not (await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))).all()
    await knowledge_indexing.process_index(job_id, sessions)
    await knowledge_indexing.process_index(job_id, sessions)
    assert (await http.get(path, headers=headers)).json()[0]['status'] == 'completed'
    operations=(await http.get('/admin/operations',headers=headers)).json()
    assert operations['knowledge_index_jobs'] == {'completed':1}
    assert operations['knowledge_import_jobs'] == {} and operations['possibly_stale_index_jobs'] == 0
    async with sessions() as db:
        assert (await db.get(KnowledgeSource, source_id)).index_status == 'ready'
        assert len((await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))).all()) == 1
    failed_job = (await http.post(path, headers=headers)).json()['id']
    async def unavailable(): raise knowledge.EmbeddingUnavailable('synthetic failure')
    monkeypatch.setattr(knowledge, 'embedding_digest', unavailable)
    await knowledge_indexing.process_index(failed_job, sessions)
    async with sessions() as db:
        assert (await db.get(KnowledgeIndexJob, failed_job)).error_code == 'embedding_unavailable'
        assert (await db.get(KnowledgeSource, source_id)).index_status == 'ready'
        assert len((await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))).all()) == 1
    next_job = (await http.post(path, headers=headers)).json()['id']
    assert (await http.post('/auth/logout', headers=headers)).status_code == 204
    await knowledge_indexing.process_index(next_job, sessions)
    async with sessions() as db:
        job = await db.get(KnowledgeIndexJob, next_job)
        assert job.status == 'failed' and job.error_code == 'authorization_revoked'
        assert len((await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'digest', 'logout'])
async def test_background_knowledge_index_rechecks_after_provider(client, monkeypatch, change):
    from app import knowledge_indexing
    from app.models import KnowledgeChunk, KnowledgeIndexJob, KnowledgeSource
    http, sessions = client
    async with sessions() as db:
        db.add(User(email=f'background-index-{change}@example.test',full_name='Synthetic Admin',role='admin',password_hash=hash_password('long-enough-password'),verification_status='not_applicable'))
        await db.commit()
    headers=auth((await login(http,f'background-index-{change}@example.test'))['access_token'])
    source={'title':'Synthetic','publisher':'Synthetic','provenance':'synthetic://test','publication_date':'2025-01-01','specialty':'general','version':'1','content':'Synthetic reference','effective_at':time.time()-1}
    source_id=(await http.post('/admin/knowledge',headers=headers,json=source)).json()['id']
    job_id=(await http.post(f'/admin/knowledge/{source_id}/index-jobs',headers=headers)).json()['id']
    entered, release = asyncio.Event(), asyncio.Event()
    calls=[]
    async def digest():
        calls.append(1)
        return ('b' if change=='digest' and len(calls)>1 else 'a')*64
    async def embed(texts):
        entered.set()
        await release.wait()
        return [[1.0]+[0.0]*383 for _ in texts]
    monkeypatch.setattr(knowledge,'embedding_digest',digest)
    monkeypatch.setattr(knowledge,'embed_texts',embed)
    task=asyncio.create_task(knowledge_indexing.process_index(job_id,sessions))
    try:
        await asyncio.wait_for(entered.wait(),5)
        if change=='logout':
            assert (await http.post('/auth/logout',headers=headers)).status_code == 204
        elif change=='source':
            async with sessions() as db:
                (await db.get(KnowledgeSource,source_id)).content='Changed synthetic reference'
                await db.commit()
    finally:
        release.set()
    await asyncio.wait_for(task,5)
    async with sessions() as db:
        job=await db.get(KnowledgeIndexJob,job_id)
        assert job.status=='failed' and job.error_code=={'logout':'authorization_revoked','source':'source_changed','digest':'model_changed'}[change]
        assert (await db.get(KnowledgeSource,source_id)).index_status=='not_indexed'
        assert not (await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id==source_id))).all()


@pytest.mark.asyncio
async def test_email_verification_is_single_use_and_separate_from_clinician_status(client, monkeypatch):
    from app import email_verification, reset_mailer
    from app.models import EmailVerification
    http, sessions = client
    monkeypatch.setenv('EMAIL_VERIFICATION_ENABLED', 'true')
    doctor=await register(http,'synthetic-verify@example.test','doctor')
    headers=auth((await login(http,doctor['email']))['access_token'])
    assert (await http.get('/me',headers=headers)).json()['email_verified_at'] is None
    path='/auth/email-verification/request'
    assert (await http.post(path,headers=headers)).status_code == 202
    async with sessions() as db:
        first=await db.scalar(select(EmailVerification).where(EmailVerification.user_id==doctor['id']))
        first_id=first.id
    delivered=[]
    def sender(identifier,email): delivered.append((identifier,email))
    assert await reset_mailer.deliver_verification_one(sessions,sender)
    assert delivered == [(first_id,doctor['email'])]
    assert (await http.post(path,headers=headers)).status_code == 202
    old=email_verification.verification_token(first_id)
    assert (await http.post('/auth/email-verification/confirm',json={'token':old})).status_code == 400
    async with sessions() as db:
        rows=(await db.scalars(select(EmailVerification).where(EmailVerification.user_id==doctor['id']).order_by(EmailVerification.created_at))).all()
        new_id=rows[-1].id
    token=email_verification.verification_token(new_id)
    assert (await http.post('/auth/email-verification/confirm',json={'token':token+'x'})).status_code == 400
    assert (await http.post('/auth/email-verification/confirm',json={'token':token})).status_code == 200
    assert (await http.post('/auth/email-verification/confirm',json={'token':token})).status_code == 400
    account=(await http.get('/me',headers=headers)).json()
    assert account['email_verified_at'] is not None and account['verification_status']=='pending'
    assert (await http.post(path,headers=headers)).status_code == 409
    assert await reset_mailer.deliver_verification_one(sessions,sender)
    assert len(delivered)==1
    async with sessions() as db:
        assert (await db.get(EmailVerification,new_id)).delivery_status == 'discarded'


@pytest.mark.asyncio
async def test_email_verification_expiry_deactivation_and_delivery_retry(client, monkeypatch):
    from app import email_verification, reset_mailer
    from app.models import EmailVerification
    http, sessions = client
    monkeypatch.setenv('EMAIL_VERIFICATION_ENABLED','true')
    patient=await register(http,'synthetic-mail-retry@example.test','patient')
    headers=auth((await login(http,patient['email']))['access_token'])
    assert (await http.post('/auth/email-verification/request',headers=headers)).status_code == 202
    async with sessions() as db:
        verification=await db.scalar(select(EmailVerification).where(EmailVerification.user_id==patient['id']))
        verification_id=verification.id
    def failing(*args): raise OSError('synthetic mail error')
    assert await reset_mailer.deliver_verification_one(sessions,failing)
    async with sessions() as db:
        verification=await db.get(EmailVerification,verification_id)
        assert verification.delivery_status=='pending' and verification.attempts==1 and verification.error_code=='mail_unavailable'
        verification.expires_at=time.time()-1
        verification.next_attempt_at=time.time()-1
        await db.commit()
    assert (await http.post('/auth/email-verification/confirm',json={'token':email_verification.verification_token(verification_id)})).status_code == 400
    assert await reset_mailer.deliver_verification_one(sessions,failing)
    async with sessions() as db:
        assert (await db.get(EmailVerification,verification_id)).delivery_status=='discarded'
    assert (await http.post('/auth/email-verification/request',headers=headers)).status_code == 202
    async with sessions() as db:
        verification=await db.scalar(select(EmailVerification).where(EmailVerification.user_id==patient['id'],EmailVerification.used_at.is_(None)))
        token=email_verification.verification_token(verification.id)
    assert (await http.post('/account/deactivate',headers=headers,json={'password':'long-enough-password'})).status_code == 204
    assert (await http.post('/auth/email-verification/confirm',json={'token':token})).status_code == 400


@pytest.mark.asyncio
async def test_object_document_download_preview_and_processing_obey_access(client, monkeypatch, tmp_path):
    import hashlib
    import jwt
    from app import document_storage, object_document_submissions, object_upload_intents
    from app.main import SECRET
    from app.s3_storage import ObjectRef
    http, sessions = client
    monkeypatch.setenv('S3_ENDPOINT_URL','https://synthetic-storage.example.test')
    monkeypatch.setenv('S3_REGION','us-east-1')
    monkeypatch.setenv('S3_BUCKET','synthetic-private-bucket')
    monkeypatch.setattr(processing,'Session',sessions)
    monkeypatch.setenv('UPLOAD_DIR',str(tmp_path/'uploads'))
    patient=await register(http,'object-doc-owner@example.test','patient')
    other=await register(http,'object-doc-other@example.test','patient')
    login_data=await login(http,patient['email'])
    headers=auth(login_data['access_token']); other_headers=auth((await login(http,other['email']))['access_token'])
    session_id=jwt.decode(login_data['access_token'],SECRET,algorithms=['HS256'])['sid']
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72),'Test name: Synthetic glucose\nObserved value: 6.2')
        content=pdf.tobytes()
    object_id=str(uuid.uuid4()); digest=hashlib.sha256(content).hexdigest(); settings=document_storage.config()
    submission=await object_document_submissions.prepare_document(sessions,actor_id=patient['id'],session_id=session_id,store_id=settings.store_id,bucket=settings.bucket,object_id=object_id,size=len(content),sha256=digest,filename='synthetic.pdf',content_type='application/pdf',scan_status='not_scanned',correlation_id=str(uuid.uuid4()))
    ref=ObjectRef(settings.bucket,'documents/'+object_id,'v1',len(content),digest)
    await object_upload_intents.record(sessions,submission,store_id=settings.store_id,ref=ref)
    await object_document_submissions.complete_document(sessions,actor_id=patient['id'],intent_id=submission,store_id=settings.store_id)
    seen=[]
    purges=[]
    class Store:
        def read(self, actual):
            assert actual==ref
            seen.append(actual)
            return content
        def purge(self, actual):
            assert actual==ref
            purges.append(actual)
            if len(purges)==1:
                raise OSError('synthetic provider failure')
    monkeypatch.setattr(document_storage,'store',lambda settings:Store())
    path=f'/documents/{object_id}'
    assert (await http.get(path+'/download',headers=other_headers)).status_code==403
    downloaded=await http.get(path+'/download',headers=headers)
    assert downloaded.status_code==200 and downloaded.content==content
    preview=await http.get(path+'/preview/1',headers=headers)
    assert preview.status_code==200 and preview.content.startswith(b'\x89PNG')
    job=(await http.get(path+'/processing',headers=headers)).json()['job_id']
    await processing.process_job(job)
    result=(await http.get(path+'/processing',headers=headers)).json()
    assert result['status']=='completed' and 'Synthetic glucose' in result['pages'][0]['text']
    assert len(seen)>=4
    monkeypatch.setenv('S3_ENDPOINT_URL','https://different-storage.example.test')
    assert (await http.get(path+'/download',headers=headers)).status_code==503
    from app import document_purger
    deleted=await http.post(path+'/delete',headers=headers,json={'confirmation':'DELETE'})
    assert deleted.status_code==202
    assert await document_purger.purge_one(sessions)
    async with sessions() as db:
        row=await db.get(Document,object_id)
        assert row.purge_status=='pending' and row.purged_at is None
        row.purge_after=time.time()-1
        await db.commit()
    assert purges == []  # Changed storage identity must not delete from another store.
    monkeypatch.setenv('S3_ENDPOINT_URL','https://synthetic-storage.example.test')
    assert await document_purger.purge_one(sessions)
    async with sessions() as db:
        row=await db.get(Document,object_id)
        assert row.purge_status=='pending' and row.purged_at is None
        row.purge_after=time.time()-1
        await db.commit()
    assert await document_purger.purge_one(sessions)
    async with sessions() as db:
        row=await db.get(Document,object_id)
        assert row.purge_status=='complete' and row.purged_at is not None
    assert len(purges)==2
    statuses=(await http.get('/documents/deletions',headers=headers)).json()
    assert statuses[0]['storage_kind']=='object' and statuses[0]['purge_status']=='complete'
    assert (await http.get(path+'/download',headers=headers)).status_code==404


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['version', 'logout'])
async def test_object_document_changed_location_during_download_is_not_released(client, monkeypatch, change):
    import hashlib
    import jwt
    import threading
    from app import document_storage, object_document_submissions, object_upload_intents
    from app.main import SECRET
    from app.models import ObjectUploadIntent
    from app.s3_storage import ObjectRef
    http, sessions=client
    monkeypatch.setenv('S3_ENDPOINT_URL','https://synthetic-storage.example.test')
    monkeypatch.setenv('S3_REGION','us-east-1')
    monkeypatch.setenv('S3_BUCKET','synthetic-private-bucket')
    patient=await register(http,'object-race@example.test','patient')
    login_data=await login(http,patient['email']); headers=auth(login_data['access_token'])
    session_id=jwt.decode(login_data['access_token'],SECRET,algorithms=['HS256'])['sid']
    content=b'%PDF-synthetic'; object_id=str(uuid.uuid4()); digest=hashlib.sha256(content).hexdigest(); settings=document_storage.config()
    submission=await object_document_submissions.prepare_document(sessions,actor_id=patient['id'],session_id=session_id,store_id=settings.store_id,bucket=settings.bucket,object_id=object_id,size=len(content),sha256=digest,filename='synthetic.pdf',content_type='application/pdf',scan_status='not_scanned',correlation_id=str(uuid.uuid4()))
    ref=ObjectRef(settings.bucket,'documents/'+object_id,'v1',len(content),digest)
    await object_upload_intents.record(sessions,submission,store_id=settings.store_id,ref=ref)
    await object_document_submissions.complete_document(sessions,actor_id=patient['id'],intent_id=submission,store_id=settings.store_id)
    entered, release=threading.Event(),threading.Event()
    class Store:
        def read(self, actual):
            entered.set()
            assert release.wait(5)
            return content
    monkeypatch.setattr(document_storage,'store',lambda settings:Store())
    task=asyncio.create_task(http.get(f'/documents/{object_id}/download',headers=headers))
    try:
        assert await asyncio.to_thread(entered.wait,5)
        if change == 'version':
            async with sessions() as db:
                (await db.get(ObjectUploadIntent,submission)).version_id='v2'
                await db.commit()
        else:
            assert (await http.post('/auth/logout',headers=headers)).status_code == 204
    finally:
        release.set()
    response=await asyncio.wait_for(task,5)
    assert response.status_code=={'version':409, 'logout':401}[change] and content not in response.content


@pytest.mark.asyncio
async def test_patient_object_upload_and_incomplete_recovery(client,monkeypatch,tmp_path):
    import hashlib
    from app import document_storage, main
    from app.models import DocumentObjectLink, ObjectUploadIntent
    from app.s3_storage import ObjectRef
    http,sessions=client
    monkeypatch.setattr(main,'DOCUMENT_STORAGE_BACKEND','s3')
    monkeypatch.setattr(main,'SessionFactory',sessions)
    monkeypatch.setenv('S3_ENDPOINT_URL','https://synthetic-storage.example.test')
    monkeypatch.setenv('S3_REGION','us-east-1')
    monkeypatch.setenv('S3_BUCKET','synthetic-private-bucket')
    objects={}
    class Store:
        bucket='synthetic-private-bucket'
        fail=False
        def put(self,namespace,identifier,content):
            ref=ObjectRef(self.bucket,f'{namespace}/{identifier}','v1',len(content),hashlib.sha256(content).hexdigest())
            objects[identifier]=(ref,content)
            if self.fail: raise OSError('synthetic response loss')
            return ref
        def read(self,ref): return objects[ref.key.split('/')[1]][1]
        def discover(self,namespace,identifier,size,digest):
            if identifier not in objects: return None
            ref,content=objects[identifier]
            assert len(content)==size and hashlib.sha256(content).hexdigest()==digest
            return ref
    store=Store()
    monkeypatch.setattr(document_storage,'store',lambda settings:store)
    patient=await register(http,'upload-object@example.test','patient')
    other=await register(http,'upload-object-other@example.test','patient')
    old=await login(http,patient['email']); headers=auth(old['access_token'])
    other_headers=auth((await login(http,other['email']))['access_token'])
    data=b'%PDF-synthetic content'
    first=await http.post('/documents',headers=headers,files={'file':('synthetic.pdf',data,'application/pdf')})
    assert first.status_code==201,first.text
    doc_id=first.json()['id']
    assert (await http.get(f'/documents/{doc_id}/download',headers=headers)).content==data
    assert (await http.get(f'/documents/{doc_id}/download',headers=other_headers)).status_code==403
    async with sessions() as db:
        assert await db.get(DocumentObjectLink,doc_id) is not None
    assert not (tmp_path/'uploads'/'.object-staging'/doc_id).exists()
    store.fail=True
    failed=await http.post('/documents',headers=headers,files={'file':('pending.pdf',data,'application/pdf')})
    assert failed.status_code==503 and 'uncertain' in failed.json()['detail'].lower()
    pending=(await http.get('/documents/incomplete-uploads',headers=headers)).json()
    assert len(pending)==1 and pending[0]['filename']=='pending.pdf' and pending[0]['status']=='pending'
    assert (await http.get('/documents/incomplete-uploads',headers=other_headers)).json()==[]
    resume=f"/documents/incomplete-uploads/{pending[0]['id']}/resume"
    assert (await http.post(resume,headers=other_headers)).status_code==404
    assert (await http.post('/auth/logout',headers=headers)).status_code==204
    headers=auth((await login(http,patient['email']))['access_token'])
    resumed=await http.post(resume,headers=headers)
    assert resumed.status_code==200,resumed.text
    recovered_id=resumed.json()['id']
    assert (await http.get(f'/documents/{recovered_id}/download',headers=headers)).content==data
    assert (await http.get('/documents/incomplete-uploads',headers=headers)).json()==[]
    async with sessions() as db:
        assert (await db.get(ObjectUploadIntent,pending[0]['id'])).status=='stored'
        assert (await db.get(DocumentObjectLink,recovered_id)).intent_id==pending[0]['id']
    assert not list((tmp_path/'uploads'/'.object-staging').iterdir())


@pytest.mark.asyncio
async def test_object_upload_checks_session_after_remote_write(client,monkeypatch):
    import hashlib
    import threading
    from app import document_storage, main, object_staging
    from app.s3_storage import ObjectRef
    http,sessions=client
    monkeypatch.setattr(main,'DOCUMENT_STORAGE_BACKEND','s3')
    monkeypatch.setattr(main,'SessionFactory',sessions)
    monkeypatch.setenv('S3_ENDPOINT_URL','https://synthetic-storage.example.test')
    monkeypatch.setenv('S3_REGION','us-east-1')
    monkeypatch.setenv('S3_BUCKET','synthetic-private-bucket')
    entered, release=threading.Event(),threading.Event()
    class Store:
        bucket='synthetic-private-bucket'
        def put(self,namespace,identifier,content):
            entered.set()
            assert release.wait(5)
            return ObjectRef(self.bucket,f'{namespace}/{identifier}','v1',len(content),hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(document_storage,'store',lambda settings:Store())
    patient=await register(http,'upload-object-race@example.test','patient')
    headers=auth((await login(http,patient['email']))['access_token'])
    task=asyncio.create_task(http.post('/documents',headers=headers,files={'file':('synthetic.pdf',b'%PDF-synthetic','application/pdf')}))
    try:
        assert await asyncio.to_thread(entered.wait,5)
        staged = list((main.UPLOAD_DIR / object_staging.STAGING_SUBDIR).iterdir())
        assert len(staged) == 1
        assert await asyncio.to_thread(object_staging.cleanup_stale, main.UPLOAD_DIR, now=time.time()+object_staging.STALE_AFTER_SECONDS+60) == 0
        assert staged[0].exists()
        assert (await http.post('/auth/logout',headers=headers)).status_code==204
    finally:
        release.set()
    response=await asyncio.wait_for(task,5)
    assert response.status_code==401
    async with sessions() as db:
        assert not (await db.scalars(select(Document).where(Document.patient_id==patient['id']))).all()


@pytest.mark.asyncio
async def test_incomplete_upload_pagination_and_private_operational_counts(client):
    from app.models import DocumentObjectLink, ObjectDocumentSubmission, ObjectUploadIntent, Session
    http, sessions = client
    patient = await register(http, 'upload-pages@example.test', 'patient')
    other = await register(http, 'upload-pages-other@example.test', 'patient')
    headers = auth((await login(http, patient['email']))['access_token'])
    other_headers = auth((await login(http, other['email']))['access_token'])
    async with sessions() as db:
        admin = User(email='upload-pages-admin@example.test', full_name='Synthetic Admin', role='admin', password_hash=hash_password('long-enough-password'), verification_status='not_applicable')
        db.add(admin)
        session = await db.scalar(select(Session).where(Session.user_id == patient['id']))
        for index in range(53):
            intent_id, object_id = str(uuid.uuid4()), str(uuid.uuid4())
            status = 'stored' if index >= 51 else 'pending'
            intent = ObjectUploadIntent(id=intent_id, actor_id=patient['id'], store_id='a'*64, bucket='synthetic-bucket', namespace='documents', object_id=object_id, size=10, sha256='b'*64, status=status, version_id='v1' if status == 'stored' else None, created_at=time.time()-90000)
            db.add(intent)
            await db.flush()
            db.add(ObjectDocumentSubmission(intent_id=intent_id, session_id=session.id, filename='PRIVATE-SYNTHETIC.pdf', content_type='application/pdf', scan_status='not_scanned', correlation_id=str(uuid.uuid4())))
            if index == 52:
                document = Document(id=object_id, patient_id=patient['id'], filename='PRIVATE-SYNTHETIC.pdf', content_type='application/pdf', size=10)
                db.add(document)
                await db.flush()
                db.add(DocumentObjectLink(document_id=object_id, intent_id=intent_id))
        await db.commit()
    first = await http.get('/documents/incomplete-uploads', headers=headers)
    assert first.status_code == 200 and len(first.json()) == 50
    cursor = first.headers['x-next-cursor']
    assert (await http.get(f'/documents/incomplete-uploads?cursor={cursor}', headers=other_headers)).status_code == 422
    second = await http.get(f'/documents/incomplete-uploads?cursor={cursor}', headers=headers)
    assert second.status_code == 200 and len(second.json()) == 2 and 'x-next-cursor' not in second.headers
    assert len({item['id'] for item in first.json()+second.json()}) == 52
    admin_headers = auth((await login(http, 'upload-pages-admin@example.test'))['access_token'])
    operations = await http.get('/admin/operations', headers=admin_headers)
    assert operations.status_code == 200, operations.text
    assert operations.json()['incomplete_object_uploads'] == {'pending': 51, 'stored': 1}
    assert operations.json()['old_incomplete_object_uploads'] == 52
    assert 'PRIVATE-SYNTHETIC' not in operations.text and patient['id'] not in operations.text
    assert (await http.get('/admin/operations', headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_doctor_worklist_patient_pages_preserve_consent_and_search(client):
    from app.models import Relationship
    http, sessions = client
    doctor = await register(http, 'paged-doctor@example.test', 'doctor')
    other_doctor = await register(http, 'paged-other-doctor@example.test', 'doctor')
    headers = auth((await login(http, doctor['email']))['access_token'])
    other_headers = auth((await login(http, other_doctor['email']))['access_token'])
    async with sessions() as db:
        (await db.get(User, doctor['id'])).verification_status = 'verified'
        (await db.get(User, other_doctor['id'])).verification_status = 'verified'
        for index in range(101):
            patient_id, relationship_id = str(uuid.uuid4()), str(uuid.uuid4())
            db.add(User(id=patient_id, email=f'paged-{index}@example.test', full_name='Same synthetic name', role='patient', password_hash='unused', verification_status='not_applicable'))
            db.add(Relationship(id=relationship_id, patient_id=patient_id, doctor_id=doctor['id'], purpose='care', status='active'))
            db.add(Consent(relationship_id=relationship_id, scope='read_documents', purpose='care', status='active', effective_at=time.time()-10, expires_at=time.time()+3600))
        await db.commit()
    first = await http.post('/doctor/worklist', headers=headers, json={'query':'Same synthetic'})
    assert first.status_code == 200 and len(first.json()['patients']) == 100
    assert first.headers['cache-control'] == 'no-store'
    cursor = first.json()['next_patient_cursor']
    assert cursor == first.json()['patients'][-1]['patient_id']
    second = await http.post('/doctor/worklist', headers=headers, json={'query':'Same synthetic', 'patient_cursor':cursor})
    assert second.status_code == 200 and len(second.json()['patients']) == 1 and second.json()['next_patient_cursor'] is None
    assert len({p['patient_id'] for p in first.json()['patients'] + second.json()['patients']}) == 101
    assert (await http.post('/doctor/worklist', headers=other_headers, json={'patient_cursor':cursor})).status_code == 422
    assert (await http.post('/doctor/worklist', headers=headers, json={'query':'unrelated', 'patient_cursor':cursor})).status_code == 422
    async with sessions() as db:
        relationship = await db.scalar(select(Relationship).where(Relationship.patient_id == cursor, Relationship.doctor_id == doctor['id']))
        consent = await db.scalar(select(Consent).where(Consent.relationship_id == relationship.id))
        consent.status = 'withdrawn'
        await db.commit()
    assert (await http.post('/doctor/worklist', headers=headers, json={'query':'Same synthetic','patient_cursor':cursor})).status_code == 422


@pytest.mark.asyncio
async def test_worklist_item_pages_are_scoped_and_stably_ordered(client):
    from app.models import ClinicalNote, ClinicalNoteVersion, StructuredExtractionVersion
    http, sessions = client
    patient = await register(http, 'worklist-items-patient@example.test', 'patient')
    doctor = await register(http, 'worklist-items-doctor@example.test', 'doctor')
    other = await register(http, 'worklist-items-other@example.test', 'doctor')
    ph = auth((await login(http, patient['email']))['access_token'])
    dh = auth((await login(http, doctor['email']))['access_token'])
    oh = auth((await login(http, other['email']))['access_token'])
    async with sessions() as db:
        (await db.get(User, doctor['id'])).verification_status = 'verified'
        (await db.get(User, other['id'])).verification_status = 'verified'
        await db.commit()
    rel = (await http.post('/relationships', headers=ph, json={'doctor_id':doctor['id'], 'purpose':'care'})).json()
    grants = {}
    for scope in ('clinical_notes', 'read_documents', 'review_documents'):
        response = await http.post('/consents', headers=ph, json={'relationship_id':rel['id'], 'scope':scope, 'purpose':'care', 'expires_at':time.time()+3600})
        assert response.status_code == 201, response.text
        grants[scope] = response.json()['id']
    async with sessions() as db:
        for index in range(51):
            document = Document(id=str(uuid.uuid4()), patient_id=patient['id'], filename=f'synthetic-{index}.pdf', content_type='application/pdf', size=10, created_at=1000)
            note = ClinicalNote(id=str(uuid.uuid4()), patient_id=patient['id'])
            db.add_all([document, note]); await db.flush()
            job = ExtractionJob(id=str(uuid.uuid4()), document_id=document.id, requested_by=patient['id'], status='completed', created_at=1000)
            db.add(job); await db.flush()
            db.add(StructuredExtractionVersion(job_id=job.id, version=1, review_status='unverified', data={}))
            db.add(ClinicalNoteVersion(id=str(uuid.uuid4()), note_id=note.id, version=1, content='Synthetic', status='pending_review', author_id=doctor['id'], created_at=1000))
        await db.commit()
    first = (await http.post('/doctor/worklist', headers=dh, json={})).json()
    cursors = {'upload_cursor':first['next_upload_cursor'], 'note_cursor':first['next_note_cursor'], 'extraction_cursor':first['next_extraction_cursor']}
    assert all(cursors.values())
    assert all(len(first[key]) == 50 for key in ('recent_uploads', 'pending_notes', 'pending_extractions'))
    response = await http.post('/doctor/worklist', headers=dh, json=cursors)
    assert response.status_code == 200, response.text
    second = response.json()
    for key, identity in [('recent_uploads','document_id'), ('pending_notes','version_id'), ('pending_extractions','job_id')]:
        assert len(second[key]) == 1
        assert len({item[identity] for item in first[key]+second[key]}) == 51
    assert not any(second[key] for key in ('next_upload_cursor','next_note_cursor','next_extraction_cursor'))
    assert (await http.post('/doctor/worklist', headers=oh, json=cursors)).status_code == 422
    await http.post(f"/consents/{grants['read_documents']}/withdraw", headers=ph)
    assert (await http.post('/doctor/worklist', headers=dh, json={'upload_cursor':cursors['upload_cursor']})).status_code == 422
    assert (await http.post('/doctor/worklist', headers=dh, json={'extraction_cursor':cursors['extraction_cursor']})).status_code == 422
    assert (await http.post('/doctor/worklist', headers=dh, json={'note_cursor':cursors['note_cursor']})).status_code == 200


@pytest.mark.asyncio
async def test_consultation_pagination_for_dashboard_and_integration(client, monkeypatch):
    from app.models import Consultation
    http, sessions = client
    async def allow(*args): return True
    monkeypatch.setattr(integration_rate_limit, 'check_rate_limit', allow)
    patient = await register(http, 'consult-pages-patient@example.test', 'patient')
    other = await register(http, 'consult-pages-other@example.test', 'patient')
    doctor = await register(http, 'consult-pages-doctor@example.test', 'doctor')
    ph = auth((await login(http, patient['email']))['access_token'])
    oh = auth((await login(http, other['email']))['access_token'])
    dh = auth((await login(http, doctor['email']))['access_token'])
    async with sessions() as db:
        (await db.get(User, doctor['id'])).verification_status = 'verified'
        db.add_all([Consultation(id=str(uuid.uuid4()), patient_id=patient['id'], doctor_id=doctor['id'], title='Synthetic consultation', occurred_at=1000) for _ in range(501)])
        await db.commit()
    rel = (await http.post('/relationships', headers=ph, json={'doctor_id':doctor['id'], 'purpose':'care'})).json()
    grants = {}
    for scope in ('consultations', 'consultations_integration'):
        grant = await http.post('/consents', headers=ph, json={'relationship_id':rel['id'], 'scope':scope, 'purpose':'care', 'expires_at':time.time()+3600})
        assert grant.status_code == 201
        grants[scope] = grant.json()['id']
    credential = await http.post('/integration-credentials', headers=dh, json={'label':'Synthetic client', 'patient_id':patient['id'], 'scopes':['consultations:read'], 'expires_at':time.time()+300})
    assert credential.status_code == 201, credential.text
    ih = auth(credential.json()['token'])
    paths = [(f"/patients/{patient['id']}/consultations", dh), (f"/integration/patients/{patient['id']}/consultations", ih)]
    for path, headers in paths:
        first = await http.get(path, headers=headers)
        assert first.status_code == 200 and len(first.json()) == 500 and first.headers['cache-control'] == 'no-store'
        cursor = first.headers['x-next-cursor']
        second = await http.get(f'{path}?cursor={cursor}', headers=headers)
        assert second.status_code == 200 and len(second.json()) == 1 and 'x-next-cursor' not in second.headers
        assert len({item['id'] for item in first.json()+second.json()}) == 501
    assert (await http.get(f"/patients/{other['id']}/consultations?cursor={cursor}", headers=oh)).status_code == 422
    await http.post(f"/consents/{grants['consultations_integration']}/withdraw", headers=ph)
    assert (await http.get(paths[1][0]+f'?cursor={cursor}', headers=ih)).status_code == 403
    await http.post(f"/consents/{grants['consultations']}/withdraw", headers=ph)
    assert (await http.get(paths[0][0]+f'?cursor={cursor}', headers=dh)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revoked', 'deactivated', 'scope_removed'])
async def test_integration_knowledge_search_rechecks_after_provider_wait(client, monkeypatch, change):
    from app import main
    from app.models import IntegrationCredential
    http, sessions = client
    async def allow(*args): return True
    monkeypatch.setattr(integration_rate_limit, 'check_rate_limit', allow)
    patient = await register(http, f'knowledge-recheck-{change}@example.test', 'patient')
    headers = auth((await login(http, patient['email']))['access_token'])
    created = await http.post('/integration-credentials', headers=headers, json={'label':'Synthetic client', 'scopes':['knowledge:search'], 'expires_at':time.time()+300})
    assert created.status_code == 201, created.text
    credential = created.json()
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_search(data, user, db):
        entered.set()
        await release.wait()
        return {'results':[{'excerpt':'SYNTHETIC-PRIVATE-RESULT'}], 'notice':'synthetic'}
    monkeypatch.setattr(main, 'search_knowledge', delayed_search)
    request = asyncio.create_task(http.post('/integration/knowledge/search', headers=auth(credential['token']), json={'query':'synthetic'}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == 'revoked':
            assert (await http.post(f"/integration-credentials/{credential['id']}/revoke", headers=headers)).status_code == 200
        else:
            async with sessions() as db:
                if change == 'deactivated':
                    (await db.get(User, patient['id'])).active = False
                else:
                    (await db.get(IntegrationCredential, credential['id'])).scopes = []
                await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == (403 if change == 'scope_removed' else 401)
        assert 'SYNTHETIC-PRIVATE-RESULT' not in response.text
        async with sessions() as db:
            event = await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == patient['id'], AuditEvent.action == 'integration_knowledge_search', AuditEvent.outcome == 'denied'))
            assert event is not None
    finally:
        release.set()
        if not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['logout', 'deactivated', 'unchanged'])
async def test_browser_knowledge_search_rechecks_session_after_provider_wait(client, monkeypatch, change):
    from app import main
    http, sessions = client
    patient = await register(http, f'browser-knowledge-recheck-{change}@example.test', 'patient')
    headers = auth((await login(http, patient['email']))['access_token'])
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_search(data, user, db):
        entered.set()
        await release.wait()
        return {'results':[{'excerpt':'SYNTHETIC-RESULT'}], 'notice':'synthetic'}
    monkeypatch.setattr(main, 'search_knowledge', delayed_search)
    pending = asyncio.create_task(http.post('/knowledge/search', headers=headers, json={'query':'synthetic'}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == 'logout':
            assert (await http.post('/auth/logout', headers=headers)).status_code == 204
        elif change == 'deactivated':
            async with sessions() as db:
                (await db.get(User, patient['id'])).active = False
                await db.commit()
        release.set()
        response = await asyncio.wait_for(pending, 5)
        if change == 'unchanged':
            assert response.status_code == 200 and 'SYNTHETIC-RESULT' in response.text
            assert response.headers['cache-control'] == 'no-store'
        else:
            assert response.status_code == 401 and 'SYNTHETIC-RESULT' not in response.text
        async with sessions() as db:
            event = await db.scalar(select(AuditEvent).where(AuditEvent.actor_id == patient['id'], AuditEvent.action == 'knowledge_search_release'))
            assert event and event.outcome == ('success' if change == 'unchanged' else 'denied')
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_upload_id_collision_does_not_remove_existing_file(client, monkeypatch, tmp_path):
    import io
    from starlette.datastructures import Headers, UploadFile
    from app import main
    existing_id = uuid.uuid4()
    uploads = tmp_path / 'uploads'
    uploads.mkdir(exist_ok=True)
    original = uploads / str(existing_id)
    original.write_bytes(b'existing synthetic document')
    monkeypatch.setattr(main.uuid, 'uuid4', lambda: existing_id)
    upload = UploadFile(filename='synthetic.pdf', file=io.BytesIO(b'%PDF-new'), headers=Headers({'content-type':'application/pdf'}))
    with pytest.raises(FileExistsError):
        await main.save_document(upload, None, None)
    assert original.read_bytes() == b'existing synthetic document'


@pytest.mark.asyncio
async def test_discarded_upload_fences_resume_and_reconciles_late_object(client, monkeypatch):
    import hashlib
    from app import document_purger, document_storage, main, object_upload_intents
    from app.models import ObjectUploadDiscard, ObjectUploadIntent
    from app.s3_storage import ObjectRef
    http, sessions = client
    monkeypatch.setattr(main, 'DOCUMENT_STORAGE_BACKEND', 's3')
    monkeypatch.setattr(main, 'SessionFactory', sessions)
    monkeypatch.setenv('S3_ENDPOINT_URL', 'https://synthetic-storage.example.test')
    monkeypatch.setenv('S3_REGION', 'us-east-1')
    monkeypatch.setenv('S3_BUCKET', 'synthetic-private-bucket')
    objects = {}
    class Store:
        bucket = 'synthetic-private-bucket'
        def put(self, namespace, identifier, content):
            raise OSError('synthetic uncertain upload')
        def discover(self, namespace, identifier, size, sha256):
            item = objects.get(identifier)
            if item:
                assert item.size == size and item.sha256 == sha256
            return item
        def purge(self, ref):
            assert objects.pop(ref.key.split('/')[1]) == ref
    monkeypatch.setattr(document_storage, 'store', lambda settings: Store())
    patient = await register(http, 'discard-patient@example.test', 'patient')
    other = await register(http, 'discard-other@example.test', 'patient')
    headers = auth((await login(http, patient['email']))['access_token'])
    other_headers = auth((await login(http, other['email']))['access_token'])
    data = b'%PDF-synthetic content'
    assert (await http.post('/documents', headers=headers, files={'file':('private.pdf',data,'application/pdf')})).status_code == 503
    intent = (await http.get('/documents/incomplete-uploads', headers=headers)).json()[0]
    path = f"/documents/incomplete-uploads/{intent['id']}"
    assert (await http.post(path+'/discard', headers=other_headers)).status_code == 404
    assert (await http.post(path+'/discard', headers=headers)).status_code == 202
    assert (await http.post(path+'/discard', headers=headers)).status_code == 202
    assert (await http.post(path+'/resume', headers=headers)).status_code == 409
    listed = (await http.get('/documents/incomplete-uploads', headers=headers)).json()[0]
    assert listed['status'] == 'discarding' and listed['filename'] == 'Discarded upload' and listed['cleanup_result'] == 'pending'
    assert await document_purger.reconcile_discard_one(sessions)
    async with sessions() as db:
        marker = await db.get(ObjectUploadDiscard, intent['id'])
        record = await db.get(ObjectUploadIntent, intent['id'])
        assert marker.last_result == 'absent' and marker.attempts == 1
        ref = ObjectRef(record.bucket, 'documents/'+record.object_id, 'late-v1', len(data), hashlib.sha256(data).hexdigest())
        store_id, identifier = record.store_id, record.object_id
        marker.next_check_at = time.time()-1
        await db.commit()
    with pytest.raises(ValueError, match='discarded'):
        await object_upload_intents.record(sessions, intent['id'], store_id=store_id, ref=ref)
    objects[identifier] = ref
    assert await document_purger.reconcile_discard_one(sessions)
    assert not objects
    async with sessions() as db:
        marker = await db.get(ObjectUploadDiscard, intent['id'])
        assert marker.last_result == 'removed' and marker.attempts == 2 and marker.next_check_at > time.time()
        assert not (await db.scalars(select(Document).where(Document.patient_id == patient['id']))).all()
    listed = (await http.get('/documents/incomplete-uploads', headers=headers)).json()[0]
    assert listed['cleanup_result'] == 'removed' and listed['cleanup_checked_at'] is not None


@pytest.mark.asyncio
async def test_discard_monitoring_reports_overdue_aggregate_without_patient_data(client, monkeypatch):
    from app import main
    from app.models import ObjectDocumentSubmission, ObjectUploadDiscard, ObjectUploadIntent, Session
    http, sessions = client
    patient = await register(http, 'discard-ops-patient@example.test', 'patient')
    headers = auth((await login(http, patient['email']))['access_token'])
    async with sessions() as db:
        db.add(User(email='discard-ops-admin@example.test', full_name='Synthetic Admin', role='admin', password_hash=hash_password('long-enough-password'), verification_status='not_applicable'))
        session = await db.scalar(select(Session).where(Session.user_id == patient['id']))
        for age in (900, 100):
            intent_id = str(uuid.uuid4())
            db.add(ObjectUploadIntent(id=intent_id, actor_id=patient['id'], store_id='a'*64, bucket='synthetic-bucket', namespace='documents', object_id=str(uuid.uuid4()), size=10, sha256='b'*64, status='pending'))
            await db.flush()
            db.add(ObjectDocumentSubmission(intent_id=intent_id, session_id=session.id, filename='PRIVATE-SYNTHETIC.pdf', content_type='application/pdf', scan_status='not_scanned', correlation_id=str(uuid.uuid4())))
            await db.flush()
            db.add(ObjectUploadDiscard(intent_id=intent_id, requested_at=time.time()-3600, next_check_at=time.time()-age, last_result='error'))
        await db.commit()
    admin_headers = auth((await login(http, 'discard-ops-admin@example.test'))['access_token'])
    response = await http.get('/admin/operations', headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.json()['discarded_object_uploads'] == {'error':2}
    assert response.json()['discard_error_categories'] == {'unclassified':2}
    assert response.json()['overdue_discard_checks'] == 1
    assert response.json()['oldest_discard_check_lag_seconds'] >= 900
    assert 'PRIVATE-SYNTHETIC' not in response.text and patient['id'] not in response.text
    assert (await http.get('/admin/operations', headers=headers)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['configuration', 'integrity', 'provider', 'unexpected'])
async def test_discard_cleanup_sanitizes_and_clears_error_categories(client, kind):
    from botocore.exceptions import EndpointConnectionError
    from app import document_purger, document_storage
    from app.models import ObjectDocumentSubmission, ObjectUploadDiscard, ObjectUploadIntent, Session
    from app.s3_storage import StorageIntegrityError
    http, sessions = client
    patient = await register(http, f'discard-error-{kind}@example.test', 'patient')
    await login(http, patient['email'])
    intent_id = str(uuid.uuid4())
    async with sessions() as db:
        session = await db.scalar(select(Session).where(Session.user_id == patient['id']))
        db.add(ObjectUploadIntent(id=intent_id, actor_id=patient['id'], store_id='a'*64, bucket='synthetic-bucket', namespace='documents', object_id=str(uuid.uuid4()), size=10, sha256='b'*64, status='pending'))
        await db.flush()
        db.add(ObjectDocumentSubmission(intent_id=intent_id, session_id=session.id, filename='Discarded upload', content_type='application/pdf', scan_status='not_scanned', correlation_id=str(uuid.uuid4())))
        await db.flush()
        db.add(ObjectUploadDiscard(intent_id=intent_id, requested_at=time.time(), next_check_at=time.time()-1))
        await db.commit()
    errors = {'configuration':document_storage.StorageUnavailable('PRIVATE-SECRET'), 'integrity':StorageIntegrityError('PRIVATE-SECRET'), 'provider':EndpointConnectionError(endpoint_url='https://private.example.test'), 'unexpected':RuntimeError('PRIVATE-SECRET')}
    expected = {'configuration':'storage_configuration', 'integrity':'integrity_conflict', 'provider':'provider_unavailable', 'unexpected':'unexpected_error'}
    def fail(_): raise errors[kind]
    assert await document_purger.reconcile_discard_one(sessions, fail)
    async with sessions() as db:
        marker = await db.get(ObjectUploadDiscard, intent_id)
        assert marker.last_result == 'error' and marker.error_code == expected[kind]
        if kind == 'integrity':
            db.add(User(email='discard-error-admin@example.test', full_name='Synthetic Admin', role='admin', password_hash=hash_password('long-enough-password'), verification_status='not_applicable'))
        await db.commit()
    if kind == 'integrity':
        admin_headers = auth((await login(http, 'discard-error-admin@example.test'))['access_token'])
        operations = await http.get('/admin/operations', headers=admin_headers)
        assert operations.status_code == 200 and operations.json()['discard_error_categories'] == {'integrity_conflict':1}
        assert 'PRIVATE-SECRET' not in operations.text and patient['id'] not in operations.text
    async with sessions() as db:
        marker = await db.get(ObjectUploadDiscard, intent_id)
        marker.next_check_at = time.time()-1
        await db.commit()
    assert await document_purger.reconcile_discard_one(sessions, lambda _: 'absent')
    async with sessions() as db:
        marker = await db.get(ObjectUploadDiscard, intent_id)
        assert marker.last_result == 'absent' and marker.error_code is None


@pytest.mark.asyncio
async def test_admin_discard_error_listing_and_retry_are_bounded_and_private(client):
    from app.models import ObjectDocumentSubmission, ObjectUploadDiscard, ObjectUploadIntent, Session
    http, sessions = client
    patient = await register(http, 'discard-list-patient@example.test', 'patient')
    ph = auth((await login(http, patient['email']))['access_token'])
    async with sessions() as db:
        db.add(User(email='discard-list-admin@example.test', full_name='Synthetic Admin', role='admin', password_hash=hash_password('long-enough-password'), verification_status='not_applicable'))
        session = await db.scalar(select(Session).where(Session.user_id == patient['id']))
        for _ in range(52):
            intent_id = str(uuid.uuid4())
            db.add(ObjectUploadIntent(id=intent_id, actor_id=patient['id'], store_id='a'*64, bucket='synthetic-bucket', namespace='documents', object_id=str(uuid.uuid4()), size=10, sha256='b'*64, status='pending'))
            await db.flush()
            db.add(ObjectDocumentSubmission(intent_id=intent_id, session_id=session.id, filename='PRIVATE-SYNTHETIC.pdf', content_type='application/pdf', scan_status='not_scanned', correlation_id=str(uuid.uuid4())))
            await db.flush()
            db.add(ObjectUploadDiscard(intent_id=intent_id, requested_at=1000, next_check_at=time.time()+3600, checked_at=1000, attempts=3, last_result='error', error_code='integrity_conflict'))
        await db.commit()
    ah = auth((await login(http, 'discard-list-admin@example.test'))['access_token'])
    path = '/admin/operations/discard-errors'
    assert (await http.get(path, headers=ph)).status_code == 403
    first = await http.get(path, headers=ah)
    assert first.status_code == 200 and len(first.json()) == 50 and first.headers['cache-control'] == 'no-store'
    assert 'PRIVATE-SYNTHETIC' not in first.text and patient['id'] not in first.text
    cursor = first.headers['x-next-cursor']
    second = await http.get(path+f'?cursor={cursor}', headers=ah)
    assert second.status_code == 200 and len(second.json()) == 2 and 'x-next-cursor' not in second.headers
    assert len({item['id'] for item in first.json()+second.json()}) == 52
    chosen = first.json()[0]['id']
    retry = f'{path}/{chosen}/retry'
    assert (await http.post(retry, headers=ph)).status_code == 403
    assert (await http.post(retry, headers=ah)).status_code == 202
    assert (await http.post(retry, headers=ah)).status_code == 202
    assert (await http.post(f'{path}/{uuid.uuid4()}/retry', headers=ah)).status_code == 404
    async with sessions() as db:
        marker = await db.get(ObjectUploadDiscard, chosen)
        assert marker.next_check_at <= time.time() and marker.last_result == 'error'
        event = await db.scalar(select(AuditEvent).where(AuditEvent.action == 'admin_discard_check_retry', AuditEvent.resource == f'discard_check:{chosen}'))
        assert event and event.patient_id is None
        marker.last_result, marker.error_code = 'absent', None
        await db.commit()
    assert (await http.post(retry, headers=ah)).status_code == 409

@pytest.mark.asyncio
@pytest.mark.parametrize("pages", [
    [(1, "x" * 2001)],
    [(number, ("x" * 1900 + "\n") * 4) for number in range(1, 31)],
    [(1, "Synthetic first page"), (2, "  ")],
    [(1, "Synthetic first page"), (3, "Synthetic third page")],
])
async def test_document_summary_refuses_incomplete_source_coverage(client, monkeypatch, pages):
    from app import assistant_provider
    from app.models import ExtractedPage, SummaryGeneration
    http, sessions = client
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "synthetic-model")
    patient = await register(http, "summary-coverage@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    upload = await http.post("/documents", headers=headers, files={"file": ("report.pdf", b"%PDF-synthetic", "application/pdf")})
    doc_id = upload.json()["id"]
    async with sessions() as db:
        job = ExtractionJob(document_id=doc_id, requested_by=patient["id"], status="completed")
        db.add(job)
        await db.flush()
        job_id = job.id
        db.add_all([ExtractedPage(job_id=job_id, page_number=number, method="native", text=text) for number, text in pages])
        await db.commit()
    async def unexpected(*args, **kwargs):
        pytest.fail("The model must not be called with incomplete document coverage")
    monkeypatch.setattr(assistant_provider, "draft_answer", unexpected)
    response = await http.post(f"/documents/{doc_id}/processing/{job_id}/summaries", headers=headers, json={"request_id": str(uuid.uuid4())})
    assert response.status_code == 422
    assert "empty, missing, or exceeds processing limits" in response.json()["detail"]
    async with sessions() as db:
        assert not (await db.scalars(select(SummaryGeneration).where(SummaryGeneration.document_id == doc_id))).all()

@pytest.mark.asyncio
async def test_large_document_summary_processes_every_page_in_bounded_batches(client, monkeypatch):
    from app import assistant_provider
    from app.models import ExtractedPage
    http, sessions = client
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "synthetic-model")
    patient = await register(http, "large-summary@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    upload = await http.post("/documents", headers=headers, files={"file": ("report.pdf", b"%PDF-synthetic", "application/pdf")})
    doc_id = upload.json()["id"]
    async with sessions() as db:
        job = ExtractionJob(document_id=doc_id, requested_by=patient["id"], status="completed")
        db.add(job)
        await db.flush()
        job_id = job.id
        db.add_all([ExtractedPage(job_id=job_id, page_number=i, method="native", text=f"Synthetic page {i}\n") for i in range(1, 20)])
        await db.commit()
    seen = []
    async def draft(question, history, sources, audience):
        assert len(sources) <= 3
        seen.extend(source["page"] for source in sources)
        return assistant_provider.Draft(answer="Unverified synthetic page summary.", citations=[sources[0]["id"]])
    monkeypatch.setattr(assistant_provider, "draft_answer", draft)
    response = await http.post(f"/documents/{doc_id}/processing/{job_id}/summaries", headers=headers, json={"request_id":str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    assert seen == list(range(1, 20))
    assert "Part 7:" in response.json()["content"]
    assert response.json()["truncated"] is False
    assert {item["page"] for item in response.json()["citations"]} == {1, 4, 7, 10, 13, 16, 19}

@pytest.mark.asyncio
async def test_large_summary_stops_before_next_batch_after_account_deactivation(client, monkeypatch):
    from app import assistant_provider
    from app.models import DocumentSummary, ExtractedPage
    http, sessions = client
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "synthetic-model")
    patient = await register(http, "large-summary-revoke@example.test", "patient")
    headers = auth((await login(http, patient["email"]))["access_token"])
    uploaded = await http.post("/documents", headers=headers, files={"file": ("report.pdf", b"%PDF-synthetic", "application/pdf")})
    doc_id = uploaded.json()["id"]
    async with sessions() as db:
        job = ExtractionJob(document_id=doc_id, requested_by=patient["id"], status="completed")
        db.add(job)
        await db.flush()
        job_id = job.id
        db.add_all([ExtractedPage(job_id=job_id, page_number=i, method="native", text=f"Synthetic {i}") for i in range(1, 5)])
        await db.commit()
    calls = []
    async def draft(question, history, sources, audience):
        calls.append(1)
        async with sessions() as db:
            (await db.get(User, patient["id"])).active = False
            await db.commit()
        return assistant_provider.Draft(answer="Synthetic unverified output", citations=[sources[0]["id"]])
    monkeypatch.setattr(assistant_provider, "draft_answer", draft)
    response = await http.post(f"/documents/{doc_id}/processing/{job_id}/summaries", headers=headers, json={"request_id": str(uuid.uuid4())})
    assert response.status_code in (401, 403)
    assert len(calls) == 1
    async with sessions() as db:
        assert not (await db.scalars(select(DocumentSummary).where(DocumentSummary.document_id == doc_id))).all()

@pytest.mark.asyncio
async def test_native_pdf_table_comparisons_remain_unverified_and_source_linked(client, monkeypatch, tmp_path):
    http, sessions = client
    monkeypatch.setattr(processing, 'Session', sessions)
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path / 'uploads'))
    patient = await register(http, 'native-table-ranges@example.test', 'patient')
    headers = auth((await login(http, patient['email']))['access_token'])
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        xs, ys = [40, 250, 340, 410, 550], [70, 95, 120, 145, 170]
        for x in xs: page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys: page.draw_line((xs[0], y), (xs[-1], y))
        rows = [['Test Description', 'Value(s)', 'Unit(s)', 'Reference Range'],
                ['Synthetic A', '5.4', '%', '< 5.7'],
                ['Synthetic B', '1.54 H*', 'ratio', '<1.00'],
                ['Synthetic C', '< 148 L*', 'pg/mL', '187 - 883']]
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                page.insert_text((xs[column] + 3, ys[row] + 16), value, fontsize=8)
        # A separate four-column reference chart uses different column boundaries;
        # its example values must not become patient results.
        example_xs, example_ys = [55, 145, 240, 345, 500], [200, 225, 250]
        for x in example_xs: page.draw_line((x, example_ys[0]), (x, example_ys[-1]))
        for y in example_ys: page.draw_line((example_xs[0], y), (example_xs[-1], y))
        for row, values in enumerate([['Chart', 'Value', 'Unit', 'Note'], ['Example', '6', '%', '126']]):
            for column, value in enumerate(values):
                page.insert_text((example_xs[column] + 3, example_ys[row] + 16), value, fontsize=8)
        content = pdf.tobytes()
    uploaded = await http.post('/documents', headers=headers, files={'file': ('table.pdf', content, 'application/pdf')})
    assert uploaded.status_code == 201, uploaded.text
    path = f"/documents/{uploaded.json()['id']}/processing"
    job_id = (await http.get(path, headers=headers)).json()['job_id']
    await processing.process_job(job_id)
    result = (await http.get(path, headers=headers)).json()
    assert result['status'] == 'completed', result
    version = result['structured_versions'][0]
    assert version['review_status'] == 'unverified'
    assert version['range_comparisons'] == ['within', 'above', 'below']
    labs = version['data']['laboratory_tests']
    assert [row['test_name']['value'] for row in labs] == ['Synthetic A', 'Synthetic B', 'Synthetic C']
    assert labs[1]['observed_value']['evidence']['quote'] == '1.54 H*'
    assert labs[1]['abnormality_flag']['value'] == 'H*'
    assert labs[2]['reference_range']['evidence']['page'] == 1
    assert any('unverified' in warning and 'table' in warning for warning in version['data']['warnings'])
