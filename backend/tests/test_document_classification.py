from app.document_classification import classify_document


def test_explicit_heading_has_source_and_does_not_infer_from_body():
    result = classify_document([{"page_number": 1, "method": "native", "text": "Synthetic report\n Prescription: \nTest name: Synthetic"}])
    assert result == {"method": "explicit-heading-v1", "category": "prescription", "reason": "heading", "evidence": {"page": 1, "location_type": "page", "layer": "primary", "quote": "Prescription:"}}
    assert classify_document([{"page_number": 1, "text": "A prescription was mentioned."}])["category"] == "unknown"


def test_conflicting_layers_are_unknown_and_docx_uses_segment():
    page = {"page_number": 2, "method": "native", "text": "Laboratory report", "supplemental_ocr_text": "Discharge summary"}
    result = classify_document([page])
    assert result["category"] == "unknown" and result["reason"] == "conflicting_headings" and result["evidence"] is None
    page["text"] = "No heading"
    result = classify_document([page])
    assert result["evidence"]["layer"] == "supplemental_ocr"
    result = classify_document([{"page_number": 3, "method": "docx", "text": "Radiology report"}])
    assert result["evidence"]["location_type"] == "text_segment"
