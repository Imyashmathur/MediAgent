"""Opt-in integration test of the running Compose stack, using synthetic data."""
import os
import uuid
import io
import time
import json

import httpx
import pytest
import pymupdf
from PIL import Image, ImageDraw, ImageFont
from websockets.sync.client import connect


@pytest.mark.skipif(os.getenv("MEDIAGENT_COMPOSE_SMOKE") != "1", reason="requires running Compose stack")
def test_browser_proxy_registration_login_and_upload():
    base = "http://localhost:3100"
    email = f"synthetic-{uuid.uuid4()}@example.test"
    with httpx.Client(base_url=base, headers={"Origin": base}, timeout=15) as client:
        rejected = client.post("/api/auth/register", headers={"Origin": "https://untrusted.example"}, json={})
        assert rejected.status_code == 403
        response = client.post("/api/auth/register", json={"email": email, "password": "synthetic-password-123", "full_name": "Synthetic Patient", "role": "patient"})
        assert response.status_code == 201, response.text
        patient_id = response.json()["id"]
        response = client.post("/api/auth/login", json={"email": email, "password": "synthetic-password-123"})
        assert response.status_code == 200, response.text
        assert "access_token" not in response.text
        assert "httponly" in response.headers.get("set-cookie", "").lower()
        assert client.get("/api/me").json()["id"] == patient_id
        upload = client.post("/api/documents", files={"file": ("synthetic.pdf", b"%PDF-synthetic", "application/pdf")})
        assert upload.status_code == 201, upload.text
        document_id = upload.json()["id"]
        assert client.get(f"/api/patients/{patient_id}/documents").json()[0]["id"] == document_id
        assert client.get(f"/api/documents/{document_id}/download").content == b"%PDF-synthetic"
        with httpx.Client(base_url=base, headers={"Origin": base}, timeout=15) as other:
            other_email = f"synthetic-other-{uuid.uuid4()}@example.test"
            assert other.post("/api/auth/register", json={"email":other_email, "password":"synthetic-password-123", "full_name":"Synthetic Other", "role":"patient"}).status_code == 201
            assert other.post("/api/auth/login", json={"email":other_email, "password":"synthetic-password-123"}).status_code == 200
            assert other.get(f"/api/documents/{document_id}/download").status_code == 403
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Test name: Synthetic analyte\nObserved value: 5\nUnit: mg/L")
            native_pdf = pdf.tobytes()
        image = Image.new("RGB", (1200, 250), "white")
        ImageDraw.Draw(image).multiline_text((40, 30), "TEST NAME: SYNTHETIC\nOBSERVED VALUE: 5", fill="black", font=ImageFont.load_default(size=48), spacing=15)
        image_file = io.BytesIO()
        image.save(image_file, format="PNG")
        rotated = image.transpose(Image.Transpose.ROTATE_90)
        orientation = rotated.getexif(); orientation[274] = 6
        rotated_file = io.BytesIO()
        rotated.save(rotated_file, format="JPEG", exif=orientation)
        for name, contents, content_type, method, expected in [
            ("native.pdf", native_pdf, "application/pdf", "native", "Synthetic analyte"),
            ("scan.png", image_file.getvalue(), "image/png", "ocr", "SYNTHETIC"),
            ("rotated-scan.jpg", rotated_file.getvalue(), "image/jpeg", "ocr", "SYNTHETIC"),
        ]:
            uploaded = client.post("/api/documents", files={"file": (name, contents, content_type)})
            assert uploaded.status_code == 201, uploaded.text
            process_path = f"/api/documents/{uploaded.json()['id']}/processing"
            submitted = client.post(process_path)
            assert submitted.status_code == 202, submitted.text
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                result = client.get(process_path).json()
                if result["status"] in ("completed", "failed"):
                    break
                time.sleep(0.5)
            assert result["status"] == "completed", result
            assert result["review_status"] == "unverified"
            assert result["pages"][0]["method"] == method and expected in result["pages"][0]["text"]
            transcription = result["structured_versions"][0]
            assert transcription["review_status"] == "unverified"
            test = transcription["data"]["laboratory_tests"][0]
            for field in ("test_name", "observed_value"):
                extracted = test[field]
                assert extracted["origin"] == "source"
                assert extracted["evidence"]["page"] == 1
                assert extracted["value"] == extracted["evidence"]["quote"]
                assert extracted["value"] in result["pages"][0]["text"]
            with httpx.Client(base_url=base, headers={"Origin": base}, timeout=15) as other:
                assert other.post("/api/auth/login", json={"email":other_email, "password":"synthetic-password-123"}).status_code == 200
                assert other.get(process_path).status_code == 403
        conversation = client.post("/api/conversations", json={"patient_id": patient_id})
        assert conversation.status_code == 201, conversation.text
        conversation_id = conversation.json()["id"]
        ticket = client.post(f"/api/conversations/{conversation_id}/ticket")
        assert ticket.status_code == 200, ticket.text
        with connect(f"ws://localhost:8100/ws/conversations/{conversation_id}", origin=base, open_timeout=10) as socket:
            socket.send(json.dumps({"ticket": ticket.json()["ticket"]}))
            assert json.loads(socket.recv(timeout=10))["type"] == "ready"
            socket.send(json.dumps({"type": "turn", "question": "I have chest pain"}))
            assert json.loads(socket.recv(timeout=10))["type"] == "status"
            assert json.loads(socket.recv(timeout=10))["type"] == "chunk"
            assert json.loads(socket.recv(timeout=10))["type"] == "complete"
        history = client.get(f"/api/conversations/{conversation_id}/messages")
        assert [item["role"] for item in history.json()] == ["user", "assistant"]
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/me").status_code == 401
