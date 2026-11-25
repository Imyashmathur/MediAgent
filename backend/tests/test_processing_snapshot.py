import hashlib
import os
import pytest
from app.processing import snapshot_file, verify_source


def test_snapshot_rejects_changed_size_hash_and_symlinks(tmp_path):
    source=tmp_path/"source"; source.write_bytes(b"synthetic")
    expected=hashlib.sha256(b"synthetic").hexdigest()
    target=tmp_path/"copy"
    snapshot_file(source,target,expected,len(b"synthetic"))
    assert target.read_bytes()==b"synthetic" and target.stat().st_mode & 0o077 == 0
    verify_source(source,expected,len(b"synthetic"))
    source.write_bytes(b"synthetiX")
    with pytest.raises(ValueError): verify_source(source,expected,len(b"synthetic"))
    source.write_bytes(b"synthetic")
    with pytest.raises(ValueError): snapshot_file(source,tmp_path/"bad-hash","0"*64,len(b"synthetic"))
    with pytest.raises(ValueError): snapshot_file(source,tmp_path/"bad-size",expected,1)
    if hasattr(os,"O_NOFOLLOW"):
        link=tmp_path/"link"; link.symlink_to(source)
        with pytest.raises(OSError): snapshot_file(link,tmp_path/"bad-link",expected,len(b"synthetic"))
