"""Bounded local OCR provider and validated worker configuration."""
import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

MAX_PAGE_CHARS = 30_000


class OCRProvider(Protocol):
    def recognize(self, image: bytes) -> str: ...
    def metadata(self) -> dict: ...


@dataclass(frozen=True)
class OCRConfig:
    provider: str
    languages: tuple[str, ...]
    pdf_mode: str


def config() -> OCRConfig:
    provider = os.getenv("OCR_PROVIDER", "tesseract")
    mode = os.getenv("PDF_OCR_MODE", "fallback")
    value = os.getenv("TESSERACT_LANGS", "eng")
    languages = value.split("+")
    if provider not in ("tesseract", "disabled") or mode not in ("fallback", "all"):
        raise ValueError("invalid_ocr_configuration")
    if len(value) > 64 or len(languages) > 4 or len(set(languages)) != len(languages) or any(not re.fullmatch(r"[A-Za-z0-9_]{1,20}", name) for name in languages):
        raise ValueError("invalid_ocr_languages")
    return OCRConfig(provider, tuple(languages), mode)


class DisabledOCR:
    def recognize(self, image: bytes) -> str:
        raise RuntimeError("ocr_disabled")

    def metadata(self) -> dict:
        raise RuntimeError("ocr_disabled")


class TesseractOCR:
    def __init__(self, languages: tuple[str, ...]):
        version = subprocess.run(["tesseract", "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True).stdout.decode("utf-8", errors="replace").splitlines()
        match = re.fullmatch(r"tesseract ([0-9][A-Za-z0-9.+~_-]{0,39})", version[0] if version else "")
        if not match:
            raise RuntimeError("ocr_version_unavailable")
        available = subprocess.run(["tesseract", "--list-langs"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True).stdout.decode("utf-8", errors="replace").splitlines()
        if not set(languages).issubset(set(available[1:])):
            raise RuntimeError("ocr_language_unavailable")
        self.version = match.group(1)
        self.languages = languages

    def recognize(self, image: bytes) -> str:
        result = subprocess.run(["tesseract", "stdin", "stdout", "-l", "+".join(self.languages), "quiet"], input=image, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=True)
        text = result.stdout.decode("utf-8", errors="replace")
        if len(text) > MAX_PAGE_CHARS:
            raise ValueError("page_text_limit")
        return text

    def metadata(self) -> dict:
        return {"provider": "tesseract", "version": self.version, "languages": list(self.languages)}


@lru_cache(maxsize=8)
def provider_for(settings: OCRConfig) -> OCRProvider:
    if settings.provider == "disabled":
        return DisabledOCR()
    return TesseractOCR(settings.languages)
