"""Bounded subprocess execution for untrusted document parsing.

Environment filtering reduces accidental credential inheritance; it is not a
sandbox or protection against a compromised process with access to the host.
"""
import os
import selectors
import signal
import subprocess
import time

MAX_PARSER_OUTPUT = 4_000_000


def parser_environment(*, vision: bool = False, max_upload_bytes: int | None = None) -> dict[str, str]:
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'PYTHONNOUSERSITE': '1', 'OMP_THREAD_LIMIT': '1'}
    names = ['OCR_PROVIDER', 'PDF_OCR_MODE', 'TESSERACT_LANGS', 'TESSDATA_PREFIX']
    if vision:
        names += ['VISION_PROVIDER', 'OLLAMA_BASE_URL', 'OLLAMA_VISION_MODEL']
    for name in names:
        if name in os.environ:
            environment[name] = os.environ[name]
    if max_upload_bytes is not None:
        environment['MAX_UPLOAD_BYTES'] = str(max_upload_bytes)
    return environment


def run_document_parser(args: list[str], *, timeout: float = 180, output_limit: int = MAX_PARSER_OUTPUT) -> subprocess.CompletedProcess:
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=parser_environment(vision=args[-1] == 'vision'), start_new_session=True)
    try:
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(args, timeout)
                data = os.read(process.stdout.fileno(), min(65536, output_limit + 1 - len(output)))
                if not data:
                    break
                output.extend(data)
                if len(output) > output_limit:
                    raise ValueError('parser_output_limit')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, timeout)
        code = process.wait(timeout=remaining)
        if code:
            raise subprocess.CalledProcessError(code, args)
        return subprocess.CompletedProcess(args, code, bytes(output))
    finally:
        # Kill the whole group even if its leader exited while a child kept stdout open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        if process.stdout:
            process.stdout.close()
