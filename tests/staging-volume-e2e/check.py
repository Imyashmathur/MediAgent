"""Synthetic cross-container check of staging locks on a Docker volume."""
import time
from pathlib import Path
from app import object_staging

root = Path('/data/uploads')
path = root / object_staging.STAGING_SUBDIR / '00000000-0000-4000-8000-000000000001'

def wait_for(marker):
    deadline = time.monotonic() + 20
    while not marker.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f'{marker.name} was not observed')
        time.sleep(0.05)

wait_for(root / 'ready')
future = time.time() + object_staging.STALE_AFTER_SECONDS + 60
sweeper = object_staging.StagingSweeper(root)
try:
    assert sweeper.scan(now=future) == 0
    assert path.read_bytes() == b'synthetic staged upload'
    (root / 'release').touch()
    wait_for(root / 'released')
    assert sweeper.scan(now=future) == 1
    assert not path.exists()
finally:
    sweeper.close()
print('PASS: active staging lock is respected across containers; unlocked stale file is removed', flush=True)
(root / 'done').touch()
