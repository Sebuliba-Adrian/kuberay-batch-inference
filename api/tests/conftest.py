"""
Shared pytest fixtures and environment hygiene.

The goal is that every test starts from a clean os.environ so there are
no hidden couplings between tests via leftover env vars (a common
pydantic-settings footgun).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


# ─── Keys that Settings reads ────────────────────────────────────────
# Any env var a Settings subclass looks at goes in this list so the
# autouse fixture can scrub it between tests.
_SETTINGS_ENV_VARS = (
    "API_KEY",
    "LOG_LEVEL",
    "RAY_ADDRESS",
    "MODEL_NAME",
    "MAX_BATCH_SIZE",
    "POSTGRES_URL",
    "RESULTS_DIR",
)


@pytest.fixture(autouse=True)
def _clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Wipe all Settings-related env vars before each test.

    Tests that want a particular env value should monkeypatch.setenv it
    themselves inside the test body. This prevents the test_env_file in
    one test polluting the next.

    Also clears the lru_cache on get_settings() if it's been imported,
    so a cached Settings from an earlier test can't shadow a new env.
    """
    for var in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # Best effort: also clear the Settings singleton cache if src.config
    # has been imported. Using a try/except because very early tests
    # may not have imported src.config yet.
    try:
        from src.config import get_settings  # noqa: PLC0415

        get_settings.cache_clear()
    except ImportError:
        pass

    yield

    # Post-test: clear the cache again so the NEXT test starts clean.
    try:
        from src.config import get_settings  # noqa: PLC0415

        get_settings.cache_clear()
    except ImportError:
        pass


@pytest.fixture
def api_key() -> str:
    """A stable API key string usable across tests."""
    return "test-key-0123456789abcdef"
