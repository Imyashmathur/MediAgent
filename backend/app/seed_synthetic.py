"""Create an opt-in synthetic care workspace through the local API."""
import asyncio
import getpass
import os
import secrets
import time
import uuid
from dataclasses import dataclass

import httpx
import pymupdf


@dataclass(frozen=True)
class SyntheticCredentials:
    patient_email: str
    patient_password: str
    doctor_email: str
    doctor_password: str


def new_credentials() -> SyntheticCredentials:
    suffix = uuid.uuid4().hex
    return SyntheticCredentials(f"synthetic-patient-{suffix}@example.test", secrets.token_urlsafe(24), f"synthetic-doctor-{suffix}@example.test", secrets.token_urlsafe(24))


def synthetic_report() -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Laboratory report\nTest name: Synthetic demonstration test\nObserved value: 123\nUnit: example units\nReport date: 2025-01-01")
        return document.tobytes()


def checked(response: httpx.Response, status: int) -> dict:
    if response.status_code != status:
        # Do not print server bodies, tokens, or patient data in error messages.
        raise RuntimeError(f"Synthetic setup request failed with HTTP {response.status_code}; expected {status}")
    return response.json() if response.content else {}


async def seed(client: httpx.AsyncClient, admin_email: str, admin_password: str, credentials: SyntheticCredentials, *, wait_seconds: float = 60) -> dict:
    """Create synthetic records via normal API authorization and review workflows."""
    sessions: list[dict] = []
    try:
        return await _seed(client, admin_email, admin_password, credentials, sessions, wait_seconds)
    finally:
        for headers in sessions:
            try:
                await client.post("/auth/logout", headers=headers)
            except httpx.HTTPError:
                pass


async def _seed(client: httpx.AsyncClient, admin_email: str, admin_password: str, credentials: SyntheticCredentials, sessions: list[dict], wait_seconds: float) -> dict:
    admin = checked(await client.post("/auth/login", json={"email":admin_email,"password":admin_password}), 200)
    ah = {"Authorization":f"Bearer {admin['access_token']}"}
    sessions.append(ah)
    if admin["user"]["role"] != "admin":
        raise RuntimeError("The supplied account is not an administrator")
    users = {}
    for role, email, password in (("patient",credentials.patient_email,credentials.patient_password),("doctor",credentials.doctor_email,credentials.doctor_password)):
        users[role] = checked(await client.post("/auth/register",json={"email":email,"password":password,"full_name":f"Synthetic {role.title()}","role":role}),201)
        login = checked(await client.post("/auth/login",json={"email":email,"password":password}),200)
        users[role]["auth"] = {"Authorization":f"Bearer {login['access_token']}"}
        sessions.append(users[role]["auth"])
    patient, doctor = users["patient"], users["doctor"]
    profile = checked(await client.get(f"/doctors/{doctor['id']}/profile",headers=ah),200)
    checked(await client.post(f"/admin/doctors/{doctor['id']}/verification",headers=ah,json={"verified":True,"expected_profile_revision":profile["profile_revision"],"expected_verification_revision":profile["verification_revision"],"expected_status":profile["verification_status"]}),200)
    relationship = checked(await client.post("/relationships",headers=patient["auth"],json={"doctor_id":doctor["id"],"purpose":"Synthetic demonstration"}),201)
    expires_at = time.time() + 86400
    for scope in ("read_documents","review_documents","clinical_notes","assistant"):
        checked(await client.post("/consents",headers=patient["auth"],json={"relationship_id":relationship["id"],"scope":scope,"purpose":"Synthetic demonstration","expires_at":expires_at}),201)
    document = checked(await client.post("/documents",headers=patient["auth"],files={"file":("synthetic-report.pdf",synthetic_report(),"application/pdf")}),201)
    path=f"/documents/{document['id']}/processing"
    checked(await client.post(path,headers=patient["auth"]),202)
    deadline=time.monotonic()+wait_seconds
    while True:
        result=checked(await client.get(path,headers=patient["auth"]),200)
        if result["status"] == "completed": break
        if result["status"] == "failed": raise RuntimeError("Synthetic document processing failed")
        if time.monotonic() >= deadline: raise RuntimeError("Synthetic document processing timed out; check the dispatcher and worker")
        await asyncio.sleep(0.5)
    note=checked(await client.post(f"/patients/{patient['id']}/notes",headers=doctor["auth"],json={"content":"Synthetic demonstration note. This is not real clinical information.","source_document_id":document["id"]}),201)
    version=f"/notes/{note['note_id']}/versions/{note['version_id']}"
    checked(await client.post(version+"/submit",headers=doctor["auth"]),200)
    checked(await client.post(version+"/review",headers=doctor["auth"],json={"decision":"approve"}),200)
    return {"patient_id":patient["id"],"doctor_id":doctor["id"],"document_id":document["id"],"consent_expires_at":expires_at}


def main() -> None:
    if os.getenv("APP_ENV", "development") != "development":
        raise SystemExit("Synthetic seeding is available only in development mode")
    admin_email=input("Existing administrator email: ").strip().lower()
    admin_password=getpass.getpass("Administrator password: ")
    credentials=new_credentials()
    print("Synthetic account credentials (shown once; keep them private):")
    print(f"Patient: {credentials.patient_email} / {credentials.patient_password}")
    print(f"Doctor:  {credentials.doctor_email} / {credentials.doctor_password}")
    async def run():
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000",timeout=20,trust_env=False) as client:
            return await seed(client,admin_email,admin_password,credentials)
    try:
        result=asyncio.run(run())
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        raise SystemExit(f"Synthetic setup did not finish: {type(exc).__name__}. Check local service status; partial synthetic records may remain.") from None
    print(f"Synthetic workspace created. Patient ID: {result['patient_id']}; doctor ID: {result['doctor_id']}.")
    print("Permissions expire after 24 hours. Use the dashboard to withdraw access or deactivate these synthetic accounts.")


if __name__ == "__main__":
    main()
