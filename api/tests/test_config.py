"""
Red tests for src.config.Settings.

These tests define the behavior of the Settings class BEFORE it exists.
Run order: write this file → run pytest → see ImportError/AttributeError
(Red) → write src/config.py → re-run → see all green.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError


# ─── API_KEY loading and secret handling ────────────────────────────
def test_settings_raises_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """API_KEY has no default and must be provided via env."""
    from src.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()  # type: ignore[call-arg]

    # Surface which field failed so future refactors stay honest about
    # the contract.
    errors = exc_info.value.errors()
    assert any(err["loc"] == ("API_KEY",) for err in errors)


def test_settings_loads_api_key_from_env(monkeypatch: pytest.MonkeyPatch, api_key: str) -> None:
    """Settings reads API_KEY from the environment as SecretStr."""
    monkeypatch.setenv("API_KEY", api_key)

    from src.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert isinstance(s.API_KEY, SecretStr)
    assert s.API_KEY.get_secret_value() == api_key


def test_api_key_is_redacted_in_repr(monkeypatch: pytest.MonkeyPatch, api_key: str) -> None:
    """The actual secret must NEVER appear in repr(Settings) or str(Settings)."""
    monkeypatch.setenv("API_KEY", api_key)

    from src.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert api_key not in repr(s)
    assert api_key not in str(s)
    # Positive check: the field still shows up, just redacted.
    assert "API_KEY" in repr(s)


# ─── RAY_ADDRESS validator ──────────────────────────────────────────
def test_ray_address_defaults_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default RAY_ADDRESS is usable from a host-forwarded Ray dashboard."""
    monkeypatch.setenv("API_KEY", "k")

    from src.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.RAY_ADDRESS == "http://localhost:8265"


def test_ray_address_rejects_non_http_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ray:// or tcp:// address should be rejected — we speak the Jobs HTTP API."""
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("RAY_ADDRESS", "ray://localhost:10001")

    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_ray_address_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalize URL so downstream string-concatenation doesn't double-slash."""
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("RAY_ADDRESS", "http://ray-head:8265/")

    from src.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.RAY_ADDRESS == "http://ray-head:8265"


# ─── POSTGRES_URL validator ─────────────────────────────────────────
def test_postgres_url_must_use_async_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sync psycopg URL must be rejected — we only use asyncpg or aiosqlite."""
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://user:pass@host/db")

    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_postgres_url_accepts_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """postgresql+asyncpg:// is the production driver."""
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://u:p@h:5432/d")

    from src.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.POSTGRES_URL == "postgresql+asyncpg://u:p@h:5432/d"


def test_postgres_url_accepts_aiosqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """sqlite+aiosqlite:// lets tests use an in-memory database."""
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("POSTGRES_URL", "sqlite+aiosqlite:///:memory:")

    from src.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.POSTGRES_URL == "sqlite+aiosqlite:///:memory:"


# ─── LOG_LEVEL validator ────────────────────────────────────────────
def test_log_level_rejects_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the stdlib logging levels are accepted."""
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")

    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_log_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default log level is INFO."""
    monkeypatch.setenv("API_KEY", "k")

    from src.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.LOG_LEVEL == "INFO"


# ─── MAX_BATCH_SIZE bounds ──────────────────────────────────────────
def test_max_batch_size_has_sensible_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default cap on prompts per batch."""
    monkeypatch.setenv("API_KEY", "k")

    from src.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.MAX_BATCH_SIZE == 1000


def test_max_batch_size_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 prompts per batch makes no sense — reject at config load."""
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("MAX_BATCH_SIZE", "0")

    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


# ─── get_settings() singleton ───────────────────────────────────────
def test_get_settings_returns_cached_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_settings() must return the same instance on repeat calls."""
    monkeypatch.setenv("API_KEY", "k")

    from src.config import get_settings

    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
