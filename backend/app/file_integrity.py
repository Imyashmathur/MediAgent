"""Read bounded regular-file snapshots without following final symlinks."""
import hashlib
import os
import stat
from pathlib import Path


def read_snapshot(path: Path, expected_size: int | None, expected_hash: str | None, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit or (expected_size is not None and info.st_size != expected_size):
            raise ValueError("file size or type changed")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
        if (len(data) != info.st_size or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns or after.st_ctime_ns != info.st_ctime_ns):
            raise ValueError("file changed during read")
        if expected_hash is not None and not hashlib.sha256(data).hexdigest() == expected_hash:
            raise ValueError("file digest changed")
        return data
