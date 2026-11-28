"""Versioned, conservative transcription contracts; these are not diagnoses."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PARSER_VERSION = "explicit-labels-pipe-native-tables-v7"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(ge=1, le=30)
    quote: str = Field(min_length=1, max_length=500)
    location_type: Literal["page", "text_segment"] = "page"
    layer: Literal["primary", "supplemental_ocr"] = "primary"


class ExtractedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=500)
    origin: Literal["source", "clinician"]
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def source_requires_evidence(self):
        if self.origin == "source" and (not self.evidence or self.value != self.evidence.quote):
            raise ValueError("Source values require an exact evidence quote")
        if self.evidence and self.value not in self.evidence.quote:
            raise ValueError("Evidence must contain the value")
        return self


class LabTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_name: ExtractedValue | None = None
    observed_value: ExtractedValue | None = None
    unit: ExtractedValue | None = None
    reference_range: ExtractedValue | None = None
    abnormality_flag: ExtractedValue | None = None
    report_date: ExtractedValue | None = None
    laboratory_name: ExtractedValue | None = None
    laboratory_details: ExtractedValue | None = None


class PrescriptionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    medicine_name: ExtractedValue | None = None
    dosage: ExtractedValue | None = None
    frequency: ExtractedValue | None = None
    duration: ExtractedValue | None = None
    instructions: ExtractedValue | None = None
    prescribing_doctor: ExtractedValue | None = None


class StructuredExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0", "1.1", "1.2"] = "1.0"
    laboratory_tests: list[LabTest] = Field(default_factory=list, max_length=100)
    prescription_items: list[PrescriptionItem] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def version_supports_layers(self):
        if self.schema_version != "1.2" and any(record.laboratory_details is not None for record in self.laboratory_tests):
            raise ValueError("Laboratory details require schema version 1.2")
        if self.schema_version == "1.0":
            for record in [*self.laboratory_tests, *self.prescription_items]:
                for name in type(record).model_fields:
                    value = getattr(record, name)
                    if value and value.evidence and value.evidence.layer != "primary":
                        raise ValueError("Supplemental OCR evidence requires schema version 1.1")
        return self


LAB_LABELS = {"test name": "test_name", "observed value": "observed_value", "unit": "unit", "reference range": "reference_range", "reported abnormality flag": "abnormality_flag", "report date": "report_date", "laboratory name": "laboratory_name", "laboratory details": "laboratory_details"}
MED_LABELS = {"medicine name": "medicine_name", "dosage": "dosage", "frequency": "frequency", "duration": "duration", "instructions": "instructions", "prescribing doctor": "prescribing_doctor"}
LAB_TABLE_HEADERS = {**LAB_LABELS, "test": "test_name", "result": "observed_value", "flag": "abnormality_flag"}
MED_TABLE_HEADERS = {**MED_LABELS, "medicine": "medicine_name", "drug": "medicine_name", "dose": "dosage"}


def table_cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _extract_layer(pages: list[dict], layer: Literal["primary", "supplemental_ocr"]) -> tuple[list[dict], list[dict], list[str]]:
    """Transcribe one layer independently; never join fields across OCR and native text."""
    labs: list[dict] = []
    meds: list[dict] = []
    warnings: list[str] = []
    current = None
    current_labels = None
    ambiguous: set[str] = set()
    for page in pages:
        # A page or DOCX segment boundary is not evidence that fields belong to
        # an item on the previous page. The original text remains available.
        current, current_labels, ambiguous = None, None, set()
        table_columns: list[str] | None = None
        table_kind: Literal["laboratory", "prescription"] | None = None
        orphan_warning = False
        interruption_warning = False
        for line in page["text"].splitlines():
            if "|" in line:
                cells = table_cells(line)
                normalized = [" ".join(cell.casefold().split()) for cell in cells]
                lab_labels = [LAB_TABLE_HEADERS.get(cell) for cell in normalized]
                med_labels = [MED_TABLE_HEADERS.get(cell) for cell in normalized]
                lab_header = "test_name" in lab_labels and "observed_value" in lab_labels
                med_header = "medicine_name" in med_labels and "dosage" in med_labels
                if lab_header or med_header:
                    current, current_labels, ambiguous = None, None, set()
                    kind = "laboratory" if lab_header else "prescription"
                    labels = lab_labels if lab_header else med_labels
                    if (lab_header and med_header) or len(cells) > 8 or None in labels or len(set(labels)) != len(labels):
                        table_columns = None
                        table_kind = None
                        warnings.append(f"A {kind} table had mixed, unknown or repeated columns and was omitted; inspect the original.")
                    else:
                        table_columns = labels
                        table_kind = kind
                        warnings.append(f"{kind.capitalize()} table values are unverified transcriptions; check every row against the original.")
                    continue
                if table_columns is not None:
                    if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                        continue
                    if len(cells) != len(table_columns):
                        table_columns = None
                        warnings.append(f"A {table_kind} table row had an ambiguous column count; remaining rows need manual review.")
                        table_kind = None
                        continue
                    name_field = "test_name" if table_kind == "laboratory" else "medicine_name"
                    if not cells[table_columns.index(name_field)] or any(len(value) > 500 for value in cells):
                        warnings.append(f"A {table_kind} table row with a missing item name or oversized value was omitted.")
                        continue
                    items = labs if table_kind == "laboratory" else meds
                    if len(items) >= 100:
                        warnings.append(f"Additional {table_kind} table items exceeded the transcription limit and were omitted.")
                        continue
                    record = {}
                    for field, value in zip(table_columns, cells):
                        if value:
                            record[field] = {"value": value, "origin": "source", "evidence": {"page": page["page_number"], "quote": value, "location_type": "text_segment" if page.get("method") == "docx" else "page", "layer": layer}}
                    items.append(record)
                    continue
                # Do not interpret a pipe-delimited line as a single labeled value.
                current, current_labels, ambiguous = None, None, set()
                continue
            table_columns = None
            table_kind = None
            match = re.fullmatch(r"\s*([^:]{1,50}):\s*(\S(?:.*\S)?)\s*", line)
            if not match:
                if line.strip() and current is not None:
                    current, current_labels, ambiguous = None, None, set()
                    if not interruption_warning:
                        warnings.append(f"Unrecognized text interrupted a labeled item on page or text segment {page['page_number']}; inspect the original for omitted fields.")
                        interruption_warning = True
                continue
            label, value = match.group(1).strip().lower(), match.group(2)
            if len(value) > 500:
                warnings.append("A labeled value exceeded the length limit and was omitted.")
                if label in ("test name", "medicine name"):
                    # The following fields belong to an omitted item, not the prior one.
                    current, current_labels, ambiguous = None, None, set()
                elif current is not None and label in current_labels:
                    field = current_labels[label]
                    current.pop(field, None)
                    ambiguous.add(field)
                else:
                    # An oversized unrelated label can mark a different record.
                    current, current_labels, ambiguous = None, None, set()
                continue
            if label == "test name" and len(labs) < 100:
                current, current_labels, ambiguous = {}, LAB_LABELS, set()
                labs.append(current)
            elif label == "medicine name" and len(meds) < 100:
                current, current_labels, ambiguous = {}, MED_LABELS, set()
                meds.append(current)
            elif label in ("test name", "medicine name"):
                current, current_labels, ambiguous = None, None, set()
                warnings.append("Additional labeled items exceeded the transcription limit and were omitted.")
                continue
            if current is None:
                if label in LAB_LABELS or label in MED_LABELS:
                    if not orphan_warning:
                        warnings.append(f"Page or text segment {page['page_number']} contains labeled fields without an item name; inspect the original for omitted or continued items.")
                        orphan_warning = True
                continue
            if label not in current_labels:
                if label in LAB_LABELS or label in MED_LABELS:
                    # A field from a different item type breaks the association.
                    current, current_labels, ambiguous = None, None, set()
                    if not orphan_warning:
                        warnings.append(f"Page or text segment {page['page_number']} contains a labeled field without a matching item name; inspect the original.")
                        orphan_warning = True
                else:
                    current, current_labels, ambiguous = None, None, set()
                    if not interruption_warning:
                        warnings.append(f"Unrecognized text interrupted a labeled item on page or text segment {page['page_number']}; inspect the original for omitted fields.")
                        interruption_warning = True
                continue
            field = current_labels[label]
            if field in current or field in ambiguous:
                current.pop(field, None)
                ambiguous.add(field)
                warnings.append(f"Repeated {label} was omitted for clinician review.")
                continue
            current[field] = {"value": value, "origin": "source", "evidence": {"page": page["page_number"], "quote": value, "location_type": "text_segment" if page.get("method") == "docx" else "page", "layer": layer}}
    return labs, meds, warnings


def bounded_warnings(warnings: list[str]) -> list[str]:
    """Keep schema-valid warnings while making omitted warnings explicit."""
    if len(warnings) <= 100:
        return warnings
    return warnings[:99] + ["Additional transcription warnings were omitted; inspect the original document carefully."]


def extract_labeled_fields(pages: list[dict]) -> StructuredExtraction:
    """Transcribe explicit labels, with separate records and provenance per source layer."""
    labs, meds, warnings = _extract_layer(pages, "primary")
    supplemental = [{"page_number": p["page_number"], "method": "native", "text": p["supplemental_ocr_text"]} for p in pages if p.get("supplemental_ocr_text")]
    if supplemental:
        ocr_labs, ocr_meds, ocr_warnings = _extract_layer(supplemental, "supplemental_ocr")
        warnings.insert(0, "Supplemental OCR and native text are separate unverified transcriptions and may duplicate or conflict; compare both with the original.")
        if len(labs) + len(ocr_labs) > 100 or len(meds) + len(ocr_meds) > 100:
            warnings.append("Additional labeled items exceeded the transcription limit and were omitted.")
        combined_labs = (labs + ocr_labs)[:100]
        return StructuredExtraction(schema_version="1.2" if any(lab.get("laboratory_details") for lab in combined_labs) else "1.1", laboratory_tests=combined_labs, prescription_items=(meds + ocr_meds)[:100], warnings=bounded_warnings(warnings + ocr_warnings))
    return StructuredExtraction(schema_version="1.2" if any(lab.get("laboratory_details") for lab in labs) else "1.0", laboratory_tests=labs, prescription_items=meds, warnings=bounded_warnings(warnings))


def validate_evidence(data: StructuredExtraction, pages: dict[int, str], location_types: dict[int, str] | None = None, supplemental_pages: dict[int, str] | None = None) -> None:
    for record in [*data.laboratory_tests, *data.prescription_items]:
        for name in type(record).model_fields:
            value = getattr(record, name)
            if value and value.evidence:
                layer_pages = pages if value.evidence.layer == "primary" else (supplemental_pages or {})
                if value.evidence.quote not in layer_pages.get(value.evidence.page, ""):
                    raise ValueError("Evidence quote is not present in the specified page layer")
                if value.evidence.layer == "supplemental_ocr" and value.evidence.location_type != "page":
                    raise ValueError("Supplemental OCR requires a page location")
            if value and value.evidence and location_types is not None and location_types.get(value.evidence.page) != value.evidence.location_type:
                raise ValueError("Evidence location type does not match the source")
