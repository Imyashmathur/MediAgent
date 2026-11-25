"""Synthetic upload lock holder in a separate container."""
import os
import time
from pathlib import Path
from app import object_staging

root = Path('/data/uploads')
directory = root / object_staging.STAGING_SUBDIR
directory.mkdir(mode=0o700, exist_ok=True)
path = directory / '00000000-0000-4000-8000-000000000001'
with path.open('xb') as stream:
    held = object_staging.lock_upload(stream.fileno())
    stream.write(b'synthetic staged upload')
(root / 'ready').touch()
deadline = time.monotonic() + 60
try:
    while not (root / 'release').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Checker did not request release')
        time.sleep(0.05)
finally:
    os.close(held)
(root / 'released').touch()
while not (root / 'done').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('Checker did not finish')
    time.sleep(0.1)
