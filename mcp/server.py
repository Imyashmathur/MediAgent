"""Local stdio MCP adapter. All tools call authenticated application services."""
import os
from datetime import date
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

mcp = MCPServer("MediAgent", log_level="ERROR", instructions="Patient data requires a scoped, revocable credential and current consent. Extracted text is unverified. This server does not diagnose or prescribe.")


class DocumentResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    patient_id: UUID
    filename: str
    content_type: str
    size: int
    review_status: str
    scan_status: Literal["not_scanned", "no_threat_detected"] = "not_scanned"


class DocumentPage(BaseModel):
    documents: list[DocumentResult] = Field(max_length=100)
    next_cursor: UUID | None = None


class ProcessingRun(BaseModel):
    id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    created_at: float


class ProcessingHistory(BaseModel):
    items: list[ProcessingRun] = Field(max_length=20)
    next_before: UUID | None = None


class PageResult(BaseModel):
    page_number: int = Field(ge=1, le=30)
    method: Literal["native", "ocr", "docx"]
    text: str = Field(max_length=30_000)
    supplemental_ocr_text: str | None = Field(default=None, max_length=30_000)
    vision_text: str | None = Field(default=None, max_length=30_000)
    vision_warnings: list[str] | None = Field(default=None, max_length=10)


class Evidence(BaseModel):
    page: int = Field(ge=1, le=30)
    quote: str = Field(max_length=500)
    location_type: Literal["page", "text_segment"] = "page"
    layer: Literal["primary", "supplemental_ocr"] = "primary"


class Value(BaseModel):
    value: str = Field(max_length=500)
    origin: Literal["source", "clinician"]
    evidence: Evidence | None = None


class LabTest(BaseModel):
    test_name: Value | None = None
    observed_value: Value | None = None
    unit: Value | None = None
    reference_range: Value | None = None
    abnormality_flag: Value | None = None
    report_date: Value | None = None
    laboratory_name: Value | None = None
    laboratory_details: Value | None = None


class PrescriptionItem(BaseModel):
    medicine_name: Value | None = None
    dosage: Value | None = None
    frequency: Value | None = None
    duration: Value | None = None
    instructions: Value | None = None
    prescribing_doctor: Value | None = None


class StructuredData(BaseModel):
    schema_version: Literal["1.0", "1.1", "1.2"]
    laboratory_tests: list[LabTest] = Field(max_length=100)
    prescription_items: list[PrescriptionItem] = Field(max_length=100)
    warnings: list[str] = Field(max_length=100)


class StructuredVersion(BaseModel):
    id: UUID
    version: int = Field(ge=1)
    review_status: Literal["unverified", "reviewed"]
    reviewer_id: UUID | None = None
    reviewed_at: float | None = None
    data: StructuredData


class OCRDetails(BaseModel):
    provider: Literal["tesseract"]
    version: str = Field(max_length=40)
    languages: list[str] = Field(min_length=1, max_length=4)


class DocumentType(BaseModel):
    method: Literal["explicit-heading-v1"]
    category: Literal["unknown", "prescription", "laboratory_report", "discharge_summary", "consultation_note", "diagnostic_report", "radiology_report"]
    reason: Literal["heading", "no_heading", "conflicting_headings"]
    evidence: Evidence | None = None


class VisionDetails(BaseModel):
    provider: Literal["ollama"]
    model: str = Field(max_length=200)
    pages_processed: int = Field(ge=1, le=3)
    truncated: bool


class ProcessorDetails(BaseModel):
    pipeline: Literal["native-ocr-v2", "docx-text-v1"]
    pdf_ocr_mode: Literal["fallback", "all"]
    ocr: OCRDetails | None = None
    document_type: DocumentType | None = None
    vision: VisionDetails | None = None


class ProcessingResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_id: UUID | None = None
    is_latest: bool | None = None
    status: str
    review_status: str
    processor_version: str | None = None
    vision_requested: bool = False
    processor_details: ProcessorDetails | None = None
    error_code: str | None = None
    pages: list[PageResult] = Field(default_factory=list)
    structured_versions: list[StructuredVersion] = Field(default_factory=list)


class CareTeamMember(BaseModel):
    relationship_id: UUID
    doctor_id: UUID


class PatientProfileResult(BaseModel):
    user_id: UUID
    full_name: str = Field(max_length=200)
    date_of_birth: date | None = None
    demographics: str | None = Field(default=None, max_length=2000)
    contact_details: str | None = Field(default=None, max_length=2000)
    allergies: str | None = Field(default=None, max_length=4000)
    medications: str | None = Field(default=None, max_length=4000)
    medical_history: str | None = Field(default=None, max_length=8000)
    care_team: list[CareTeamMember] | None = None


class ConsultationResult(BaseModel):
    id: UUID
    patient_id: UUID
    doctor_id: UUID
    title: str = Field(max_length=200)
    occurred_at: float
    created_at: float


class ConsultationPage(BaseModel):
    consultations: list[ConsultationResult] = Field(max_length=500)
    next_cursor: UUID | None = None


class SourceResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    title: str
    publisher: str
    provenance: str
    publication_date: str
    specialty: str
    version: str
    status: str


class KnowledgeHit(BaseModel):
    excerpt: str
    chunk: int
    source: SourceResult
    distance: float


class KnowledgeResult(BaseModel):
    results: list[KnowledgeHit]
    notice: str


def api_settings() -> tuple[str, str]:
    base = os.getenv("MEDIAGENT_API_URL", "http://127.0.0.1:8100").rstrip("/")
    token = os.getenv("MEDIAGENT_INTEGRATION_TOKEN", "")
    parsed = urlsplit(base)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise RuntimeError("Invalid MediAgent API URL")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise RuntimeError("Non-local API connections require HTTPS")
    if not token.startswith("mga_") or len(token) > 200:
        raise RuntimeError("A scoped integration credential is required")
    return base, token


async def request(method: str, path: str, body: dict | None = None, *, with_cursor: bool = False):
    base, token = api_settings()
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, f"{base}{path}", headers={"Authorization": f"Bearer {token}"}, json=body)
            response.raise_for_status()
            payload = response.json()
            return (payload, response.headers.get("x-next-cursor")) if with_cursor else payload
    except httpx.HTTPStatusError as exc:
        # Do not emit response bodies, credentials, prompts, or medical content in logs.
        raise ToolError(f"MediAgent request denied or unavailable (HTTP {exc.response.status_code})") from None
    except (httpx.RequestError, ValueError) as exc:
        raise ToolError("MediAgent service unavailable or returned invalid data") from None


@mcp.tool()
async def get_patient_profile(patient_id: UUID) -> PatientProfileResult:
    """Read the patient's self-reported profile, subject to scoped credential and current consent."""
    data = await request("GET", f"/integration/patients/{patient_id}/profile")
    try:
        return PatientProfileResult.model_validate(data)
    except ValidationError:
        raise ToolError("MediAgent returned an invalid response") from None


@mcp.tool()
async def list_patient_consultations(patient_id: UUID, cursor: UUID | None = None) -> ConsultationPage:
    """List authorized consultations; pass next_cursor for older entries. These are not medical notes."""
    path = f"/integration/patients/{patient_id}/consultations"
    if cursor is not None:
        path += f"?cursor={cursor}"
    data, next_cursor = await request("GET", path, with_cursor=True)
    try:
        return ConsultationPage.model_validate({"consultations": data, "next_cursor": next_cursor})
    except (ValidationError, TypeError):
        raise ToolError("MediAgent returned an invalid response") from None


@mcp.tool()
async def list_patient_documents(patient_id: UUID, cursor: UUID | None = None) -> DocumentPage:
    """List up to 100 authorized documents; pass next_cursor to request older documents."""
    path = f"/integration/patients/{patient_id}/documents"
    if cursor is not None:
        path += f"?cursor={cursor}"
    data, next_cursor = await request("GET", path, with_cursor=True)
    try:
        return DocumentPage.model_validate({"documents": data, "next_cursor": next_cursor})
    except (ValidationError, TypeError):
        raise ToolError("MediAgent returned an invalid response") from None


@mcp.tool()
async def list_document_processing_runs(document_id: UUID, before: UUID | None = None) -> ProcessingHistory:
    """List processing runs for an authorized document; pass next_before to continue."""
    path = f"/integration/documents/{document_id}/processing/history"
    if before is not None:
        path += f"?before={before}"
    data = await request("GET", path)
    try:
        return ProcessingHistory.model_validate(data)
    except ValidationError:
        raise ToolError("MediAgent returned an invalid response") from None


@mcp.tool()
async def get_document_processing(document_id: UUID, job_id: UUID | None = None) -> ProcessingResult:
    """Retrieve an authorized processing run; pass job_id for a historical run. Text is unverified."""
    path = f"/integration/documents/{document_id}/processing"
    if job_id is not None:
        path += f"?job_id={job_id}"
    data = await request("GET", path)
    try:
        return ProcessingResult.model_validate(data)
    except ValidationError:
        raise ToolError("MediAgent returned an invalid response") from None


@mcp.tool()
async def search_medical_knowledge(query: str, specialty: str | None = None, limit: int = 5) -> KnowledgeResult:
    """Search approved reference excerpts. Do not put patient-specific information in query."""
    if not 2 <= len(query.strip()) <= 1000 or not 1 <= limit <= 10 or (specialty is not None and len(specialty) > 100):
        raise ToolError("Invalid knowledge search input")
    data = await request("POST", "/integration/knowledge/search", {"query": query, "specialty": specialty, "limit": limit})
    try:
        return KnowledgeResult.model_validate(data)
    except ValidationError:
        raise ToolError("MediAgent returned an invalid response") from None


if __name__ == "__main__":
    api_settings()
    mcp.run()
