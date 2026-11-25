"""Offline validation of MCP output contracts using synthetic data."""
import unittest
import uuid
import asyncio
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from server import DocumentPage, PatientProfileResult, ProcessingResult


class ContractTests(unittest.TestCase):
    def test_processing_preserves_docx_evidence_and_supplemental_ocr(self):
        data = {"status":"completed", "is_latest":False, "review_status":"unverified", "processor_details":{"pipeline":"native-ocr-v2", "pdf_ocr_mode":"all", "ocr":{"provider":"tesseract","version":"5.5.0","languages":["eng"]}}, "pages":[{"page_number":1,"method":"native","text":"Synthetic native","supplemental_ocr_text":"Synthetic OCR"}], "structured_versions":[{"id":str(uuid.uuid4()),"version":1,"review_status":"unverified","data":{"schema_version":"1.0","laboratory_tests":[{"test_name":{"value":"Synthetic","origin":"source","evidence":{"page":1,"quote":"Synthetic","location_type":"text_segment"}}}],"prescription_items":[],"warnings":[]}}]}
        result = ProcessingResult.model_validate(data)
        self.assertFalse(result.is_latest)
        layered = ProcessingResult.model_validate({**data, "structured_versions": [{**data["structured_versions"][0], "data": {**data["structured_versions"][0]["data"], "schema_version": "1.1", "laboratory_tests": [{"test_name": {"value": "Synthetic OCR", "origin": "source", "evidence": {"page": 1, "quote": "Synthetic OCR", "layer": "supplemental_ocr"}}}]}}]})
        self.assertEqual(layered.structured_versions[0].data.laboratory_tests[0].test_name.evidence.layer, "supplemental_ocr")
        extended = ProcessingResult.model_validate({**data, "structured_versions": [{**data["structured_versions"][0], "data": {**data["structured_versions"][0]["data"], "schema_version": "1.2", "laboratory_tests": [{"laboratory_details": {"value":"Synthetic branch", "origin":"clinician"}}]}}]})
        self.assertEqual(extended.structured_versions[0].data.laboratory_tests[0].laboratory_details.value, "Synthetic branch")
        self.assertIsNone(DocumentPage.model_validate({"documents": [], "next_cursor": None}).next_cursor)
        data["processor_details"]["document_type"] = {"method":"explicit-heading-v1", "category":"prescription", "reason":"heading", "evidence":{"page":1,"quote":"Prescription","layer":"supplemental_ocr"}}
        typed = ProcessingResult.model_validate(data)
        self.assertEqual(typed.processor_details.document_type.evidence.layer, "supplemental_ocr")
        data["vision_requested"] = True
        data["pages"][0].update({"vision_text":"Synthetic vision text", "vision_warnings":["uncertain"]})
        data["processor_details"]["vision"] = {"provider":"ollama", "model":"synthetic-vision", "pages_processed":1, "truncated":False}
        vision_result = ProcessingResult.model_validate(data)
        self.assertTrue(vision_result.vision_requested)
        self.assertEqual(vision_result.pages[0].vision_text, "Synthetic vision text")
        self.assertEqual(vision_result.processor_details.vision.model, "synthetic-vision")
        self.assertEqual(result.pages[0].supplemental_ocr_text, "Synthetic OCR")
        self.assertEqual(result.processor_details.ocr.version, "5.5.0")
        self.assertEqual(result.structured_versions[0].data.laboratory_tests[0].test_name.evidence.location_type, "text_segment")

    def test_profile_rejects_invalid_identity(self):
        with self.assertRaises(ValidationError):
            PatientProfileResult.model_validate({"user_id":"not-a-uuid","full_name":"Synthetic"})

    def test_consultation_tool_passes_cursor_and_validates_page(self):
        from server import list_patient_consultations
        patient_id, cursor, next_cursor = (uuid.uuid4() for _ in range(3))
        with patch('server.request', new_callable=AsyncMock, return_value=([], str(next_cursor))) as request:
            page = asyncio.run(list_patient_consultations(patient_id, cursor))
        request.assert_awaited_once_with('GET', f'/integration/patients/{patient_id}/consultations?cursor={cursor}', with_cursor=True)
        self.assertEqual(page.consultations, [])
        self.assertEqual(page.next_cursor, next_cursor)


if __name__ == "__main__":
    unittest.main()
