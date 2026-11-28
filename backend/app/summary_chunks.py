"""Bounded, lossless segmentation of extracted document text for summaries."""

MAX_CHUNK_BYTES = 2000
MAX_TOTAL_BYTES = 180_000
MAX_CHUNKS = 90
BATCH_SIZE = 3


class SummaryCoverageError(ValueError):
    pass


def build_sources(document_id, filename, pages):
    if not pages or [page.page_number for page in pages] != list(range(1, len(pages) + 1)):
        raise SummaryCoverageError("empty or missing extracted pages")
    sources = []
    total = 0
    for page in pages:
        if not page.text.strip():
            raise SummaryCoverageError("empty extracted page")
        location = "text_segment" if page.method == "docx" else "page"
        for layer, text in (("primary", page.text), ("supplemental_ocr", page.supplemental_ocr_text or "")):
            if not text.strip():
                continue
            total += len(text.encode("utf-8"))
            if total > MAX_TOTAL_BYTES:
                raise SummaryCoverageError("document text exceeds the total limit")
            chunks, current, size = [], [], 0
            for line in text.splitlines(keepends=True):
                length = len(line.encode("utf-8"))
                if length > MAX_CHUNK_BYTES:
                    raise SummaryCoverageError("an extracted line exceeds the chunk limit")
                if current and size + length > MAX_CHUNK_BYTES:
                    chunks.append("".join(current))
                    current, size = [], 0
                current.append(line)
                size += length
            if current:
                chunks.append("".join(current))
            for index, chunk in enumerate(chunks, 1):
                base = f"document:{document_id}:{location}:{page.page_number}:{layer}"
                sources.append({"id": f"{base}:part:{index}" if len(chunks) > 1 else base,
                                "kind": "unverified_patient_document", "title": filename,
                                location: page.page_number, "method": "ocr" if layer == "supplemental_ocr" else page.method,
                                "layer": layer, "text": chunk})
                if len(sources) > MAX_CHUNKS:
                    raise SummaryCoverageError("document has too many text chunks")
    if not sources:
        raise SummaryCoverageError("no extracted text")
    return sources


def batches(sources):
    return [sources[index:index + BATCH_SIZE] for index in range(0, len(sources), BATCH_SIZE)]
