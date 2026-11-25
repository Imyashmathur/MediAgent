from types import SimpleNamespace

import pytest

from app import ocr_provider


def test_tesseract_provider_validates_language_availability_and_commands(monkeypatch):
    calls=[]
    def run(args, **kwargs):
        calls.append(args)
        if args[1:] == ["--version"]:
            return SimpleNamespace(stdout=b"tesseract 5.5.0\n")
        if args[1:] == ["--list-langs"]:
            return SimpleNamespace(stdout=b"List of available languages (2):\neng\nhin\n")
        assert kwargs["input"] == b"synthetic image" and kwargs["timeout"] == 45
        return SimpleNamespace(stdout=b"Synthetic OCR")
    monkeypatch.setattr(ocr_provider.subprocess, "run", run)
    provider=ocr_provider.TesseractOCR(("eng", "hin"))
    assert provider.recognize(b"synthetic image") == "Synthetic OCR"
    assert provider.metadata() == {"provider":"tesseract","version":"5.5.0","languages":["eng","hin"]}
    assert calls[-1] == ["tesseract","stdin","stdout","-l","eng+hin","quiet"]
    with pytest.raises(RuntimeError, match="ocr_language_unavailable"):
        ocr_provider.TesseractOCR(("eng", "missing"))


@pytest.mark.parametrize("setting,value", [("OCR_PROVIDER","unknown"), ("PDF_OCR_MODE","unknown"), ("TESSERACT_LANGS","eng;unsafe"), ("TESSERACT_LANGS","eng+eng"), ("TESSERACT_LANGS","eng+hin+fra+deu+spa")])
def test_ocr_configuration_rejects_invalid_values(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError):
        ocr_provider.config()


def test_disabled_ocr_fails_when_recognition_is_requested(monkeypatch):
    monkeypatch.setenv("OCR_PROVIDER", "disabled")
    provider=ocr_provider.provider_for(ocr_provider.config())
    with pytest.raises(RuntimeError, match="ocr_disabled"):
        provider.recognize(b"synthetic")
