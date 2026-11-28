"""Runs in a worker subprocess. Its output contains sensitive document text; never log it."""
import io
import json
import math
import re
import sys
import warnings
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image
from defusedxml import ElementTree
from . import ocr_provider, vision_provider
from .image_normalization import normalize_image

MAX_PAGES = 30
MAX_PIXELS = 20_000_000
MAX_PAGE_CHARS = 30_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CONTENT_TYPES = "{http://schemas.openxmlformats.org/package/2006/content-types}"
MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def parse_docx(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > 500 or len(set(names)) != len(names) or sum(entry.file_size for entry in entries) > 30_000_000:
            raise ValueError("docx_archive_limit")
        for entry in entries:
            parts = entry.filename.split("/")
            if entry.flag_bits & 1 or ".." in parts or "\\" in entry.filename or entry.filename.startswith("/") or entry.file_size > 8_000_000 or (entry.file_size > 1_000_000 and entry.file_size > max(1, entry.compress_size) * 200):
                raise ValueError("unsafe_docx_entry")
        if "word/document.xml" not in names or "[Content_Types].xml" not in names or any(name.lower().endswith("vbaproject.bin") for name in names):
            raise ValueError("invalid_docx_package")
        def read_entry(name: str, limit: int) -> bytes:
            with archive.open(name) as stream:
                content = stream.read(limit + 1)
            if len(content) > limit:
                raise ValueError("docx_part_limit")
            return content
        types = ElementTree.fromstring(read_entry("[Content_Types].xml", 256_000), forbid_dtd=True)
        if types.tag != CONTENT_TYPES + "Types" or not any(node.tag == CONTENT_TYPES + "Override" and node.get("PartName") == "/word/document.xml" and node.get("ContentType") == MAIN_TYPE for node in types):
            raise ValueError("invalid_docx_type")
        root = ElementTree.fromstring(read_entry("word/document.xml", 8_000_000), forbid_dtd=True)
    if root.tag != WORD + "document":
        raise ValueError("invalid_docx_document")
    body = root.find(WORD + "body")
    if body is None:
        raise ValueError("invalid_docx_document")
    stack = [(body, 0)]
    paragraphs = []
    seen = 0
    def paragraph_text(node, depth):
        nonlocal seen
        parts = []
        pending = [(child, depth + 1) for child in reversed(list(node))]
        while pending:
            child, child_depth = pending.pop()
            seen += 1
            if seen > 100_000 or child_depth > 128:
                raise ValueError("docx_structure_limit")
            if child.tag in (WORD + "del", WORD + "moveFrom", WORD + "drawing", WORD + "pict", WORD + "p"):
                continue
            if child.tag == WORD + "r":
                hidden = child.find(f"{WORD}rPr/{WORD}vanish")
                if hidden is not None and hidden.get(WORD + "val", "true").lower() not in ("0", "false", "off"):
                    continue
            if child.tag == WORD + "t" and child.text:
                parts.append(child.text)
            elif child.tag == WORD + "tab":
                parts.append("\t")
            elif child.tag in (WORD + "br", WORD + "cr"):
                parts.append("\n")
            pending.extend((item, child_depth + 1) for item in reversed(list(child)))
        return "".join(parts)

    def simple_table(node, depth):
        nonlocal seen
        pending = [(node, depth)]
        while pending:
            item, item_depth = pending.pop()
            seen += 1
            if seen > 100_000 or item_depth > 128:
                raise ValueError("docx_structure_limit")
            pending.extend((child, item_depth + 1) for child in item)
        rows = node.findall(WORD + "tr")
        if not rows or any(child.tag not in (WORD + "tblPr", WORD + "tblGrid", WORD + "tr") for child in node):
            return None
        output = []
        width = None
        for row in rows:
            row_props = row.find(WORD + "trPr")
            if row_props is not None and row_props.find(WORD + "del") is not None:
                continue
            if row_props is not None and any(row_props.find(WORD + tag) is not None for tag in ("gridBefore", "gridAfter")):
                return None
            cells = row.findall(WORD + "tc")
            if any(child.tag not in (WORD + "trPr", WORD + "tc") for child in row) or not 2 <= len(cells) <= 8 or (width is not None and len(cells) != width):
                return None
            width = len(cells)
            values = []
            for cell in cells:
                paras = cell.findall(WORD + "p")
                if len(paras) != 1 or any(child.tag not in (WORD + "tcPr", WORD + "p") for child in cell):
                    return None
                props = cell.find(WORD + "tcPr")
                if props is not None and any(props.find(WORD + tag) is not None for tag in ("gridSpan", "vMerge", "hMerge")):
                    return None
                value = paragraph_text(paras[0], depth + 3).strip()
                if any(char in value for char in ("|", "\n", "\r", "\t")):
                    return None
                values.append(value)
            output.append(" | ".join(values))
        return output

    while stack:
        node, depth = stack.pop()
        seen += 1
        if seen > 100_000 or depth > 128:
            raise ValueError("docx_structure_limit")
        if node.tag in (WORD + "del", WORD + "moveFrom", WORD + "drawing", WORD + "pict"):
            continue
        if node.tag == WORD + "tr" and node.find(f"{WORD}trPr/{WORD}del") is not None:
            continue
        if node.tag == WORD + "tbl":
            rows = simple_table(node, depth)
            if rows is not None:
                paragraphs.extend(rows)
                continue
        if node.tag == WORD + "p":
            paragraphs.append(paragraph_text(node, depth))
        else:
            stack.extend((child, depth + 1) for child in reversed(list(node)))
    text = "\n".join(paragraphs)
    if len(text) > MAX_PAGES * MAX_PAGE_CHARS:
        raise ValueError("docx_text_limit")
    # Never split a line: a truncated value could otherwise appear complete.
    segments = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        if len(line) > MAX_PAGE_CHARS:
            raise ValueError("docx_line_limit")
        additional = len(line) + (1 if current else 0)
        if current and length + additional > MAX_PAGE_CHARS:
            segments.append("\n".join(current))
            current, length = [], 0
            additional = len(line)
        current.append(line)
        length += additional
    segments.append("\n".join(current))
    if len(segments) > MAX_PAGES:
        raise ValueError("docx_text_limit")
    return [{"page_number": index + 1, "method": "docx", "text": segment} for index, segment in enumerate(segments)]


def ocr(image: bytes) -> str:
    return ocr_provider.provider_for(ocr_provider.config()).recognize(image)


def vision_png(image: Image.Image) -> bytes:
    # Keep transmission bounded and consistent with the preview orientation.
    image = normalize_image(image)
    image.thumbnail((1600, 1600))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def native_lab_table_rows(page, page_text: str, page_number: int) -> list[dict]:
    """Conservatively transcribe four-column native PDF lab tables as unverified rows."""
    if not all(label in page_text.casefold() for label in ("test description", "value(s)", "unit(s)", "reference range")):
        return []
    # PyMuPDF otherwise emits a recommendation on stdout, corrupting parser JSON.
    pymupdf.no_recommend_layout()
    tables = page.find_tables().tables
    header = None
    for table in tables:
        if table.col_count != 4:
            continue
        for index, values in enumerate(table.extract()):
            if [str(value or "").strip().casefold() for value in values] == ["test description", "value(s)", "unit(s)", "reference range"]:
                cells = table.rows[index].cells
                if len(cells) == 4 and all(cells):
                    header = (cells, table.rows[index].bbox[3])
                    break
        if header:
            break
    if not header:
        return []
    header_cells, header_bottom = header
    rows = []
    for table in tables:
        if table.col_count != 4 or table.bbox[3] <= header_bottom:
            continue
        for cells, values in zip(table.rows, table.extract()):
            if cells.bbox[1] < header_bottom - 1 or len(cells.cells) != 4 or not all(cells.cells) or len(values) != 4:
                continue
            if any(abs(cell[0] - expected[0]) > 2 or abs(cell[2] - expected[2]) > 2 for cell, expected in zip(cells.cells, header_cells)):
                continue
            name = (values[0] or "").split("\n")[0].strip()
            value = (values[1] or "").strip()
            unit = (values[2] or "").strip()
            reference = (values[3] or "").strip()
            if (not name or len(name) > 100 or name.endswith(("-", "(")) or name not in page_text
                or not re.fullmatch(r"\s*[<>≤≥]?\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:\s+[HL]\*?)?\s*", value, re.I)
                or value not in page_text):
                continue
            def field(text):
                return {"value": text, "origin": "source", "evidence": {"page": page_number, "quote": text, "location_type": "page", "layer": "primary"}}
            record = {"test_name": field(name), "observed_value": field(value)}
            flagged = re.search(r"\s+([HL]\*?)\s*$", value, re.I)
            if flagged and flagged.group(1) in page_text:
                record["abnormality_flag"] = field(flagged.group(1))
            for key, item in (("unit", unit), ("reference_range", reference)):
                if item and len(item) <= 500 and item in page_text:
                    record[key] = field(item)
            rows.append(record)
            # Retain one extra row so the worker can report truncation.
            if len(rows) >= 101:
                return rows
    return rows


def parse(path: Path, media_type: str, vision: vision_provider.VisionProvider | None = None) -> list[dict]:
    settings = ocr_provider.config()
    pages = []
    if media_type == DOCX_TYPE:
        if vision is not None:
            raise ValueError("vision_docx_unavailable")
        return parse_docx(path)
    if media_type == "application/pdf":
        with pymupdf.open(path) as doc:
            if doc.needs_pass or doc.page_count < 1 or doc.page_count > MAX_PAGES:
                raise ValueError("pdf_page_or_encryption_limit")
            for number, page in enumerate(doc, 1):
                text = page.get_text("text")
                if len(text) > MAX_PAGE_CHARS:
                    raise ValueError("page_text_limit")
                method = "native"
                supplement = None
                if not text.strip() or settings.pdf_mode == "all":
                    # Limit render dimensions before allocating a pixmap.
                    if page.rect.width * page.rect.height * 4 > MAX_PIXELS:
                        raise ValueError("image_pixel_limit")
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                    recognized = ocr(pix.tobytes("png"))
                    if text.strip():
                        supplement = recognized
                    else:
                        text, method = recognized, "ocr"
                result = {"page_number": number, "method": method, "text": text}
                if method == "native":
                    rows = native_lab_table_rows(page, text, number)
                    if rows:
                        result["native_table_labs"] = rows
                if supplement is not None:
                    result["supplemental_ocr_text"] = supplement
                if vision is not None and number <= 3:
                    width, height = page.rect.width, page.rect.height
                    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
                        raise ValueError("invalid_page_size")
                    scale = min(2.0, 1600 / max(width, height))
                    rendered = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                    with Image.open(io.BytesIO(rendered.tobytes("png"))) as image:
                        transcription = vision.transcribe(vision_png(image))
                    result["vision_text"] = transcription.text
                    result["vision_warnings"] = transcription.warnings
                pages.append(result)
    elif media_type in ("image/jpeg", "image/png"):
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("image_pixel_or_frame_limit")
                if image.format != ("PNG" if media_type == "image/png" else "JPEG"):
                    raise ValueError("image_format_mismatch")
                image.load()
                output = io.BytesIO()
                normalized = normalize_image(image)
                normalized.save(output, format="PNG")
                transcription = vision.transcribe(vision_png(normalized)) if vision is not None else None
        result = {"page_number": 1, "method": "ocr", "text": ocr(output.getvalue())}
        if transcription is not None:
            result["vision_text"] = transcription.text
            result["vision_warnings"] = transcription.warnings
        pages.append(result)
    else:
        raise ValueError("unsupported_media_type")
    return pages


if __name__ == "__main__":
    if sys.platform.startswith("linux"):
        import resource
        # Limits apply to this process and are inherited by OCR child processes.
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        use_vision = len(sys.argv) == 4 and sys.argv[3] == "vision"
        if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and not use_vision):
            raise ValueError("invalid_parser_arguments")
        vision_settings = vision_provider.config() if use_vision else None
        if use_vision and vision_settings.provider == "disabled":
            raise ValueError("vision_disabled")
        vision = vision_provider.provider_for(vision_settings) if vision_settings else None
        pages = parse(Path(sys.argv[1]), sys.argv[2], vision)
        settings = ocr_provider.config()
        used_ocr = any(page["method"] == "ocr" or "supplemental_ocr_text" in page for page in pages)
        details = {"pipeline": "docx-text-v4" if sys.argv[2] == DOCX_TYPE else "native-ocr-v2", "pdf_ocr_mode": settings.pdf_mode, "ocr": ocr_provider.provider_for(settings).metadata() if used_ocr else None}
        if vision is not None:
            details["vision"] = {**vision.metadata(), "pages_processed": min(len(pages), 3), "truncated": len(pages) > 3}
        print(json.dumps({"pages": pages, "processor_details": details}, ensure_ascii=False))
    except Exception:
        # No parser exception or document content is written to logs.
        sys.exit(1)
