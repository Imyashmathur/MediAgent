from types import SimpleNamespace

import pytest

from app.summary_chunks import MAX_CHUNK_BYTES, SummaryCoverageError, batches, build_sources


def test_source_chunks_preserve_text_and_page_layer_at_line_boundaries():
    text = ("😀" * 400 + "\n") * 3
    pages = [SimpleNamespace(page_number=1, method="native", text=text, supplemental_ocr_text="OCR line\n")]
    sources = build_sources("synthetic", "report.pdf", pages)
    primary = [source for source in sources if source["layer"] == "primary"]
    assert "".join(source["text"] for source in primary) == text
    assert len(primary) == 3 and all(len(source["text"].encode("utf-8")) <= MAX_CHUNK_BYTES for source in sources)
    assert len({source["id"] for source in sources}) == 4
    assert sources[-1]["method"] == "ocr" and sources[-1]["page"] == 1
    assert [len(batch) for batch in batches(sources)] == [3, 1]


def test_source_chunks_reject_oversize_lines_without_truncating():
    pages = [SimpleNamespace(page_number=1, method="native", text="x" * 2001, supplemental_ocr_text=None)]
    with pytest.raises(SummaryCoverageError):
        build_sources("synthetic", "report.pdf", pages)
