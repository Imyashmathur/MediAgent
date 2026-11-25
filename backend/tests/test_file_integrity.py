import hashlib
import os

import pytest

from app import file_integrity


def test_snapshot_validates_size_and_digest(tmp_path):
    path = tmp_path / "document"
    path.write_bytes(b"synthetic report")
    digest = hashlib.sha256(b"synthetic report").hexdigest()
    assert file_integrity.read_snapshot(path, 16, digest, 1024) == b"synthetic report"
    with pytest.raises(ValueError, match="file size or type changed"):
        file_integrity.read_snapshot(path, 15, digest, 1024)
    with pytest.raises(ValueError, match="file digest changed"):
        file_integrity.read_snapshot(path, 16, "0" * 64, 1024)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW")
def test_snapshot_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"synthetic report")
    link = tmp_path / "document"
    link.symlink_to(target)
    with pytest.raises(OSError):
        file_integrity.read_snapshot(link, None, None, 1024)


def test_snapshot_rejects_rewrite_with_restored_mtime(tmp_path, monkeypatch):
    path = tmp_path / "document"
    path.write_bytes(b"original")
    original_fstat = os.fstat
    calls = 0

    def change_after_read(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            before = original_fstat(fd)
            path.write_bytes(b"modified")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return original_fstat(fd)

    monkeypatch.setattr(file_integrity.os, "fstat", change_after_read)
    with pytest.raises(ValueError, match="file changed during read"):
        file_integrity.read_snapshot(path, None, None, 1024)
