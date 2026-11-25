"""Render a bounded, non-interactive page image in a separate process."""
import io
import math
import os
import sys
import warnings
from pathlib import Path

if sys.platform.startswith("linux"):
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))

import pymupdf
from PIL import Image
from .image_normalization import normalize_image

MAX_PIXELS = 20_000_000
MAX_PAGES = 30
MAX_DIMENSION = 1600
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def render_bytes(content: bytes, media_type: str, page_number: int) -> bytes:
    if media_type == "application/pdf":
        with pymupdf.open(stream=content, filetype="pdf") as document:
            if document.needs_pass or not 1 <= document.page_count <= MAX_PAGES or not 1 <= page_number <= document.page_count:
                raise ValueError("page unavailable")
            page = document[page_number - 1]
            width, height = page.rect.width, page.rect.height
            if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
                raise ValueError("invalid page")
            scale = min(2.0, MAX_DIMENSION / max(width, height))
            if width * height * scale * scale > MAX_PIXELS:
                raise ValueError("page too large")
            return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
    if media_type not in ("image/jpeg", "image/png") or page_number != 1:
        raise ValueError("page unavailable")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as image:
            if image.format != ("JPEG" if media_type == "image/jpeg" else "PNG") or image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                raise ValueError("invalid image")
            image.load()
            image = normalize_image(image)
            image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()


def render(path: Path, media_type: str, page_number: int) -> bytes:
    return render_bytes(path.read_bytes(), media_type, page_number)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--stdin":
            limit = int(os.getenv("MAX_UPLOAD_BYTES", "10485760"))
            content = sys.stdin.buffer.read(limit + 1)
            if not content or len(content) > limit:
                raise ValueError("input size limit")
            output = render_bytes(content, sys.argv[2], int(sys.argv[3]))
        else:
            output = render(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]))
        sys.stdout.buffer.write(output)
    except Exception:
        # Do not leak file names or document content in subprocess logs.
        sys.exit(1)
