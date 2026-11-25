import os
import time
import uuid

from app import object_staging


def test_cleanup_skips_locked_staging_and_preserves_local_documents(tmp_path):
    directory = tmp_path / object_staging.STAGING_SUBDIR
    directory.mkdir()
    active, abandoned, local = (str(uuid.uuid4()) for _ in range(3))
    local_path = tmp_path / local
    local_path.write_bytes(b'local document')
    active_path = directory / active
    with active_path.open('xb') as stream:
        held_fd = object_staging.lock_upload(stream.fileno())
        stream.write(b'active upload')
    abandoned_path = directory / abandoned
    abandoned_path.write_bytes(b'abandoned upload')
    (directory / 'not-a-uuid').write_bytes(b'not owned by cleaner')
    (directory / str(uuid.uuid4())).symlink_to(local_path)
    future = time.time() + object_staging.STALE_AFTER_SECONDS + 60
    try:
        assert object_staging.cleanup_stale(tmp_path, now=future) == 1
        assert active_path.read_bytes() == b'active upload'
        assert not abandoned_path.exists()
        assert local_path.read_bytes() == b'local document'
        assert (directory / 'not-a-uuid').exists()
    finally:
        os.close(held_fd)
    assert object_staging.cleanup_stale(tmp_path, now=future) == 1
    assert not active_path.exists() and local_path.exists()


def test_cleanup_keeps_recent_entries_and_refuses_symlinked_staging_directory(tmp_path):
    directory = tmp_path / object_staging.STAGING_SUBDIR
    directory.mkdir()
    recent = directory / str(uuid.uuid4())
    recent.write_bytes(b'recent')
    assert object_staging.cleanup_stale(tmp_path) == 0
    assert recent.exists()
    target = tmp_path / 'other'
    target.mkdir()
    directory.rename(target / 'original')
    directory.symlink_to(target, target_is_directory=True)
    try:
        object_staging.cleanup_stale(tmp_path, now=time.time()+object_staging.STALE_AFTER_SECONDS+60)
    except OSError:
        pass
    else:
        raise AssertionError('Symlinked staging directory must not be scanned')
    assert (target / 'original' / recent.name).exists()


def test_upload_lock_survives_writer_close_until_duplicate_closes(tmp_path):
    path = tmp_path / str(uuid.uuid4())
    with path.open('xb') as writer:
        held_fd = object_staging.lock_upload(writer.fileno())
        writer.write(b'content')
    try:
        with path.open('rb') as reader:
            import fcntl
            try:
                fcntl.flock(reader.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError('The duplicated descriptor must retain the upload lock')
    finally:
        os.close(held_fd)


def test_sweeper_advances_past_bounded_locked_entries(tmp_path):
    directory = tmp_path / object_staging.STAGING_SUBDIR
    directory.mkdir()
    paths = [directory / str(uuid.uuid4()) for _ in range(9)]
    for path in paths:
        path.write_bytes(b'synthetic')
    ordered = [directory / entry.name for entry in os.scandir(directory)]
    locked = []
    for path in ordered[:-1]:
        stream = path.open('rb')
        locked.append((stream, object_staging.lock_upload(stream.fileno())))
    target = ordered[-1]
    future = time.time() + object_staging.STALE_AFTER_SECONDS + 60
    sweeper = object_staging.StagingSweeper(tmp_path)
    try:
        assert sweeper.scan(now=future, max_entries=2) == 0
        for _ in range(5):
            sweeper.scan(now=future, max_entries=2)
        assert not target.exists()
        assert all(path.exists() for path in ordered[:-1])
        for stream, fd in locked:
            os.close(fd)
            stream.close()
        locked.clear()
        # Exhaustion restarts the scan so previously locked entries are revisited.
        for _ in range(10):
            sweeper.scan(now=future, max_entries=2)
        assert not list(directory.iterdir())
    finally:
        sweeper.close()
        for stream, fd in locked:
            os.close(fd)
            stream.close()


def test_cleanup_respects_lock_held_by_another_process(tmp_path):
    import subprocess
    import sys
    directory = tmp_path / object_staging.STAGING_SUBDIR
    directory.mkdir()
    path = directory / str(uuid.uuid4())
    path.write_bytes(b'synthetic')
    code = 'import fcntl,sys; f=open(sys.argv[1],"rb"); fcntl.flock(f,fcntl.LOCK_EX); print("locked",flush=True); sys.stdin.readline()'
    process = subprocess.Popen([sys.executable, '-c', code, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    future = time.time() + object_staging.STALE_AFTER_SECONDS + 60
    try:
        assert process.stdout.readline().strip() == 'locked'
        assert object_staging.cleanup_stale(tmp_path, now=future) == 0
        assert path.exists()
    finally:
        process.stdin.write('\n')
        process.stdin.flush()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
    assert object_staging.cleanup_stale(tmp_path, now=future) == 1
    assert not path.exists()
