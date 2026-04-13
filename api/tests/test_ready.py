"""
Red tests for GET /ready - dependency health probe.

/ready is the deeper sibling of /health: it checks that Postgres and
Ray are actually reachable, so Kubernetes only routes traffic to the
pod once its dependencies are up. A failing /ready keeps the pod
running (unlike /health) but removes it from the Service.

Tests fake both dependencies independently so we can exercise:
  - all up -> 200 {"status":"ok", "checks":{"postgres":"ok","ray":"ok"}}
  - DB down -> 503 with postgres:"error: ..."
  - Ray down -> 503 with ray:"error: ..."
  - Both down -> 503 with both error fields populated
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


class _FakeRay:
    def __init__(self, address: str) -> None: ...
    def submit_job(self, *, entrypoint: str, runtime_env: Any = None) -> str:
        return "raysubmit"

    def get_job_status(self, _sid: str) -> str:
        return "PENDING"

    def list_jobs(self) -> list[Any]:
        return []


class _FailingRay(_FakeRay):
    def list_jobs(self) -> list[Any]:
        raise ConnectionError("ray dashboard unreachable")


@pytest.fixture
async def ready_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> AsyncIterator[Any]:
    """Healthy baseline: DB up, Ray up."""
    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "sqlite+aiosqlite:///file:ready_test?mode=memory&cache=shared&uri=true",
    )
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import db, ray_client  # noqa: PLC0415
    from src.main import create_app  # noqa: PLC0415

    await db.init_engine("sqlite+aiosqlite:///file:ready_test?mode=memory&cache=shared&uri=true")
    await db.create_all()

    ray_client.reset()
    ray_client.set_client_factory(_FakeRay)
    ray_client.init("http://fake-ray:8265")

    yield create_app()

    await db.dispose()
    ray_client.reset()


# ─── All up ─────────────────────────────────────────────────────────
async def test_ready_returns_200_when_all_deps_healthy(ready_app: Any) -> None:
    transport = ASGITransport(app=ready_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/ready")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["ray"] == "ok"


async def test_ready_requires_no_auth(ready_app: Any) -> None:
    """Probes run without credentials - /ready must be public."""
    transport = ASGITransport(app=ready_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/ready")  # no X-API-Key
    assert r.status_code == 200


# ─── DB down ────────────────────────────────────────────────────────
async def test_ready_returns_503_when_postgres_down(
    ready_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a Postgres failure by making db.ping() raise."""
    from src import db  # noqa: PLC0415

    async def _broken_ping() -> None:
        raise ConnectionError("pg down")

    monkeypatch.setattr(db, "ping", _broken_ping)

    transport = ASGITransport(app=ready_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/ready")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"].startswith("error")
    assert body["checks"]["ray"] == "ok"


# ─── Ray down ───────────────────────────────────────────────────────
async def test_ready_returns_503_when_ray_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> None:
    """Simulate Ray dashboard failure via a factory that raises in list_jobs."""
    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "sqlite+aiosqlite:///file:ready_test2?mode=memory&cache=shared&uri=true",
    )
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import db, ray_client  # noqa: PLC0415
    from src.main import create_app  # noqa: PLC0415

    await db.init_engine("sqlite+aiosqlite:///file:ready_test2?mode=memory&cache=shared&uri=true")
    await db.create_all()

    ray_client.reset()
    ray_client.set_client_factory(_FailingRay)
    ray_client.init("http://fake-ray:8265")

    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/ready")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["ray"].startswith("error")

    await db.dispose()
    ray_client.reset()
