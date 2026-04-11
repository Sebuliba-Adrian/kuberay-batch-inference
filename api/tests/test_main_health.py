"""
Red tests for the main app factory, logging config, and /health route.

This cycle stitches three small modules together:
- src.logging_config.configure_logging()
- src.main.create_app() — FastAPI factory (no module-level app singleton)
- src.routes.health.router — /health endpoint

The app factory pattern (rather than a module-level `app = FastAPI()`)
is deliberate: it lets tests build a fresh instance after setting
environment variables, and it keeps `import src.main` side-effect free.
Uvicorn runs the factory in production via
    uvicorn src.main:create_app --factory
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


# ─── create_app ─────────────────────────────────────────────────────
def test_create_app_returns_fastapi_instance(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """create_app() returns a FastAPI app with title and version set."""
    monkeypatch.setenv("API_KEY", api_key)

    from src.main import create_app

    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.title == "KubeRay Batch Inference API"
    assert app.version == "0.1.0"


def test_create_app_is_side_effect_free_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Importing src.main must NOT construct the app nor read settings.
    This is what lets tests import it with no env vars set.
    """
    # No API_KEY in env; importing src.main must still succeed.
    import src.main  # noqa: F401, PLC0415

    # If importing triggered Settings() it would have already raised.


def test_create_app_raises_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_app() pulls Settings, so missing API_KEY fails loudly."""
    from pydantic import ValidationError

    from src.main import create_app

    with pytest.raises(ValidationError):
        create_app()


# ─── /health route ──────────────────────────────────────────────────
async def test_health_endpoint_returns_ok(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """GET /health returns 200 {'status': 'ok'}."""
    monkeypatch.setenv("API_KEY", api_key)

    from src.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_health_endpoint_requires_no_auth(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """
    Health probes run from kube-probes without credentials — they must
    not be gated behind X-API-Key. This test asserts explicitly that
    omitting the header still works.
    """
    monkeypatch.setenv("API_KEY", api_key)

    from src.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # No X-API-Key header whatsoever
        r = await ac.get("/health")

    assert r.status_code == 200


# ─── configure_logging ──────────────────────────────────────────────
def test_configure_logging_sets_root_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """configure_logging('DEBUG') sets the root logger to DEBUG."""
    from src.logging_config import configure_logging

    original = logging.getLogger().level
    try:
        configure_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG
    finally:
        logging.getLogger().setLevel(original)


def test_configure_logging_attaches_single_handler() -> None:
    """Re-running configure_logging() must not duplicate handlers."""
    from src.logging_config import configure_logging

    configure_logging("INFO")
    first_count = len(logging.getLogger().handlers)

    configure_logging("INFO")
    second_count = len(logging.getLogger().handlers)

    assert first_count == second_count == 1


def test_configure_logging_quiets_uvicorn_access() -> None:
    """
    uvicorn.access is noisy — our setup must raise it to WARNING so
    dev console output stays focused on our own logs.
    """
    from src.logging_config import configure_logging

    # Ensure a clean starting state by clearing any prior level.
    logging.getLogger("uvicorn.access").setLevel(logging.NOTSET)
    configure_logging("INFO")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_configure_logging_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lowercase 'debug' must be accepted just like 'DEBUG'."""
    from src.logging_config import configure_logging

    original = logging.getLogger().level
    try:
        configure_logging("debug")
        assert logging.getLogger().level == logging.DEBUG
    finally:
        logging.getLogger().setLevel(original)
