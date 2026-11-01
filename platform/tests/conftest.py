"""Test-wide environment, set before any application module is imported.

`config.settings` is created at import time, so these must be in place before
test modules import `config`. Paths are resolved relative to this file so the
suite behaves the same whether pytest runs from the repo root or `platform/`.
"""

import os
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

os.environ.update(
    {
        "ENV": "test",
        "STIX_HUB_DATABASE_HOST": "localhost",
        "STIX_HUB_DATABASE_PORT": "5432",
        "STIX_HUB_DATABASE_NAME": "app",
        "STIX_HUB_DATABASE_USER": "postgres",
        "STIX_HUB_DATABASE_PASS": "postgres",
        "STIX_HUB_DATABASE_USE_NULLPOOL": "true",
        "STIX_HUB_DATABASE_ENGINE_ECHO": "false",
        "STIX_HUB_ADMIN_API_KEY": "test-admin-key",
        "STIX_HUB_PLATFORM_CONFIG": str(_TESTS_DIR / "platform_config.yaml"),
    }
)
