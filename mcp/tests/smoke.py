"""Opt-in local end-to-end stdio MCP test using synthetic records."""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main():
    api = os.getenv("MEDIAGENT_API_URL", "http://127.0.0.1:8100")
    email = f"mcp-smoke-{uuid.uuid4().hex}@example.test"
    password = "synthetic-password-123"
    async with httpx.AsyncClient(base_url=api, timeout=15) as http:
        patient = await http.post("/auth/register", json={"email": email, "password": password, "full_name": "Synthetic Patient", "role": "patient"})
        patient.raise_for_status()
        patient_id = patient.json()["id"]
        login = await http.post("/auth/login", json={"email": email, "password": password})
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        profile = await http.post("/profile/patient", json={"medical_history": "Synthetic history"}, headers=headers)
        profile.raise_for_status()
        document = await http.post("/documents", files={"file": ("synthetic.pdf", b"%PDF-synthetic", "application/pdf")}, headers=headers)
        document.raise_for_status()
        response = await http.post("/integration-credentials", json={"label": "Synthetic test client", "patient_id": patient_id, "scopes": ["documents:read", "profile:read", "consultations:read"], "expires_at": time.time() + 300}, headers=headers)
        response.raise_for_status()
        credential = response.json()
        params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve().parents[1] / "server.py")], env={"MEDIAGENT_API_URL": api, "MEDIAGENT_INTEGRATION_TOKEN": credential["token"]})
        try:
            async with Client(params) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {"list_patient_documents", "get_document_processing", "list_document_processing_runs", "search_medical_knowledge", "get_patient_profile", "list_patient_consultations"}
                result = await client.call_tool("list_patient_documents", {"patient_id": patient_id})
                assert not result.is_error, result.content
                assert "synthetic.pdf" in str(result.structured_content or result.content)
                denied = await client.call_tool("list_patient_documents", {"patient_id": str(uuid.uuid4())})
                assert denied.is_error
                history = await client.call_tool("list_document_processing_runs", {"document_id": document.json()["id"]})
                assert not history.is_error
                processing = await client.call_tool("get_document_processing", {"document_id": document.json()["id"]})
                assert not processing.is_error and "not_requested" in str(processing.structured_content or processing.content)
                profile_result = await client.call_tool("get_patient_profile", {"patient_id": patient_id})
                assert not profile_result.is_error and "Synthetic history" in str(profile_result.structured_content or profile_result.content)
                consultations = await client.call_tool("list_patient_consultations", {"patient_id": patient_id})
                assert not consultations.is_error
                for _ in range(24):
                    assert not (await client.call_tool("list_patient_documents", {"patient_id": patient_id})).is_error
                limited = await client.call_tool("list_patient_documents", {"patient_id": patient_id})
                assert limited.is_error and "429" in str(limited.content)
                await http.post(f"/integration-credentials/{credential['id']}/revoke", headers=headers)
                revoked = await client.call_tool("list_patient_documents", {"patient_id": patient_id})
                assert revoked.is_error
        finally:
            await http.post(f"/integration-credentials/{credential['id']}/revoke", headers=headers)
            await http.post("/auth/logout", headers=headers)
    print("MCP stdio integration checks passed")


if __name__ == "__main__":
    asyncio.run(main())
