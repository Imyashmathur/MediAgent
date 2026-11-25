"""Conservative, unverified document-type hints from explicit headings."""

HEADINGS = {
    "prescription": "prescription",
    "laboratory report": "laboratory_report",
    "lab report": "laboratory_report",
    "discharge summary": "discharge_summary",
    "consultation note": "consultation_note",
    "consultation notes": "consultation_note",
    "diagnostic report": "diagnostic_report",
    "radiology report": "radiology_report",
}


def classify_document(pages: list[dict]) -> dict:
    """Use exact heading lines only; never resolve conflicting headings by guessing."""
    found: dict[str, dict] = {}
    for page in pages:
        for layer, text in (("primary", page["text"]), ("supplemental_ocr", page.get("supplemental_ocr_text") or "")):
            for line in text.splitlines():
                heading = line.strip()
                category = HEADINGS.get(" ".join(heading.removesuffix(":").split()).casefold())
                if category and category not in found:
                    found[category] = {"page": page["page_number"], "location_type": "text_segment" if page.get("method") == "docx" else "page", "layer": layer, "quote": heading}
    if len(found) == 1:
        category, evidence = next(iter(found.items()))
        return {"method": "explicit-heading-v1", "category": category, "reason": "heading", "evidence": evidence}
    return {"method": "explicit-heading-v1", "category": "unknown", "reason": "conflicting_headings" if found else "no_heading", "evidence": None}
