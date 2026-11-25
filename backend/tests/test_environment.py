import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("environment", ["production", "Production", "prod", "", "development "])
@pytest.mark.parametrize("module", ["main", "processing", "outbox_dispatcher", "document_purger", "reset_mailer"])
def test_services_reject_non_development_environment(environment, module):
    result = subprocess.run(
        [sys.executable, "-c", f"import app.{module}"],
        env={**os.environ, "APP_ENV": environment},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "Only APP_ENV=development is supported" in result.stderr
