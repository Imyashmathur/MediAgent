import json
import subprocess
import sys
import time

import pytest

from app.parser_subprocess import parser_environment, run_document_parser


def test_parser_environment_limits_sensitive_inheritance(monkeypatch):
    monkeypatch.setenv('JWT_SECRET', 'synthetic-secret')
    monkeypatch.setenv('DATABASE_URL', 'synthetic-database')
    monkeypatch.setenv('OCR_PROVIDER', 'disabled')
    monkeypatch.setenv('VISION_PROVIDER', 'ollama')
    monkeypatch.setenv('OLLAMA_BASE_URL', 'http://synthetic.example')
    standard = parser_environment()
    assert standard['OCR_PROVIDER'] == 'disabled'
    assert 'JWT_SECRET' not in standard and 'DATABASE_URL' not in standard
    assert 'VISION_PROVIDER' not in standard and 'OLLAMA_BASE_URL' not in standard
    vision = parser_environment(vision=True)
    assert vision['VISION_PROVIDER'] == 'ollama' and vision['OLLAMA_BASE_URL'] == 'http://synthetic.example'
    result = run_document_parser([sys.executable, '-c', 'import json,os; print(json.dumps(dict(os.environ)))'])
    child = json.loads(result.stdout)
    assert 'JWT_SECRET' not in child and 'DATABASE_URL' not in child and child['OCR_PROVIDER'] == 'disabled'


def test_parser_stops_excess_output_and_times_out():
    with pytest.raises(ValueError, match='parser_output_limit'):
        run_document_parser([sys.executable, '-c', 'import sys; sys.stdout.write("x" * 200000); sys.stdout.flush()'], output_limit=1024)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_document_parser([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=.2)
    assert time.monotonic() - started < 3


def test_parser_rejects_nonzero_exit():
    with pytest.raises(subprocess.CalledProcessError):
        run_document_parser([sys.executable, '-c', 'raise SystemExit(7)'])


def test_real_parser_processes_pdf_and_scanned_image(tmp_path, monkeypatch):
    import pymupdf
    from PIL import Image, ImageDraw
    monkeypatch.setenv('OCR_PROVIDER', 'tesseract')
    monkeypatch.setenv('TESSERACT_LANGS', 'eng')
    monkeypatch.setenv('PDF_OCR_MODE', 'fallback')
    pdf_path = tmp_path / 'synthetic.pdf'
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72,72), 'Synthetic reference')
        pdf.save(pdf_path)
    native = run_document_parser([sys.executable, '-m', 'app.document_parser', str(pdf_path), 'application/pdf'])
    parsed = json.loads(native.stdout)
    assert parsed['pages'][0]['method'] == 'native'
    assert 'Synthetic reference' in parsed['pages'][0]['text']
    image_path = tmp_path / 'synthetic.png'
    image = Image.new('RGB', (600, 160), 'white')
    ImageDraw.Draw(image).text((20, 30), 'Synthetic report', fill='black')
    image.save(image_path)
    scanned = run_document_parser([sys.executable, '-m', 'app.document_parser', str(image_path), 'image/png'])
    parsed = json.loads(scanned.stdout)
    assert parsed['pages'][0]['method'] == 'ocr'
    assert parsed['processor_details']['ocr']['provider'] == 'tesseract'


@pytest.mark.asyncio
async def test_preview_output_is_limited_while_streaming():
    import asyncio
    from app.main import communicate_preview
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"x" * 200000); sys.stdout.flush()', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        with pytest.raises(ValueError, match='preview_output_limit'):
            await asyncio.wait_for(communicate_preview(process, b'', limit=1024), timeout=3)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


@pytest.mark.asyncio
async def test_preview_timeout_terminates_and_reaps_renderer():
    import asyncio
    from app.main import communicate_preview
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(communicate_preview(process, b''), timeout=.2)
    assert process.returncode is not None
