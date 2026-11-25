"""Private, advisory-locked staging for object-backed document uploads."""
import fcntl
import os
import stat
import threading
import time
import uuid
from pathlib import Path

STAGING_SUBDIR = '.object-staging'
STALE_AFTER_SECONDS = 24 * 60 * 60


def lock_upload(fd: int) -> int:
    """Hold an exclusive lock on the same open file description until closed."""
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return os.dup(fd)


def _remove_if_stale(directory_fd: int, name: str, cutoff: float) -> bool:
    try:
        parsed = uuid.UUID(name)
        if parsed.version != 4 or str(parsed) != name:
            return False
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or max(info.st_mtime, info.st_ctime) > cutoff:
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            info = os.fstat(fd)
            if max(info.st_mtime, info.st_ctime) > cutoff:
                return False
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(current.st_mode):
                return False
            os.unlink(name, dir_fd=directory_fd)
            return True
        finally:
            os.close(fd)
    except (FileNotFoundError, ValueError, OSError):
        # Concurrent cleanup and inaccessible or suspicious entries are skipped.
        return False


class StagingSweeper:
    """Scan bounded batches, retaining directory position between passes.

    When a directory has been exhausted, the next scan starts a new pass. API
    and purger processes must share a volume with working cross-process flock.
    """
    def __init__(self, root: Path):
        self.directory = root / STAGING_SUBDIR
        self._fd = None
        self._entries = None
        self._lock = threading.Lock()

    def _close(self) -> None:
        if self._entries is not None:
            self._entries.close()
            self._entries = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def close(self) -> None:
        # A scan may still be running in a thread during async cancellation.
        with self._lock:
            self._close()

    def scan(self, *, now: float | None = None, max_entries: int = 1000) -> int:
        if not 1 <= max_entries <= 10000:
            raise ValueError('Invalid scan bound')
        with self._lock:
            if self._fd is None:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                try:
                    self._fd = os.open(self.directory, flags)
                except FileNotFoundError:
                    return 0
                try:
                    self._entries = os.scandir(self._fd)
                except BaseException:
                    self._close()
                    raise
            cutoff = (time.time() if now is None else now) - STALE_AFTER_SECONDS
            removed = 0
            try:
                for _ in range(max_entries):
                    try:
                        entry = next(self._entries)
                    except StopIteration:
                        self._close()
                        break
                    removed += _remove_if_stale(self._fd, entry.name, cutoff)
            except BaseException:
                self._close()
                raise
            return removed


def cleanup_stale(root: Path, *, now: float | None = None, max_entries: int = 1000) -> int:
    """Run one bounded scan; a long-running purger should use StagingSweeper."""
    sweeper = StagingSweeper(root)
    try:
        return sweeper.scan(now=now, max_entries=max_entries)
    finally:
        sweeper.close()
