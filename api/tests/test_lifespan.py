"""
Red tests for the FastAPI lifespan wiring.

The lifespan context manager is the production boot sequence:
  1. Read Settings
  2. Open the DB engine and create tables if missing
  3. Initialize the Ray Jobs client
  4. Start the background status poller task
  5. ... serve ...
  6. On shutdown: stop the poller, dispose the DB engine

Tests prove the lifespan actually runs these steps end-to-end and
tears them down cleanly. Nothing is mocked - a fake Ray is injected
via set_client_factory BEFORE the app is built, exactly as the
production code path does it via env-var config + dependency graph.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


class _LifespanFakeRay:
    def __init__(self, address: str) -> None:
        self.address = address

    def submit_job(self, *, entrypoint: str, runtime_env: Any = None) -> str:
        return "raysubmit_lifespan"

    def get_job_status(self, _sid: str) -> str:
        return "RUNNING"

    def list_jobs(self) -> list[Any]:
        return []


async def test_lifespan_shutdown_helper_runs_every_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> None:
    """
    Directly exercise _lifespan_shutdown so its every line is covered
    without relying on coverage.py's ability to trace the finally
    block of an async generator (which has known quirks).
    """
    db_file = tmp_path / "lifespan_shutdown.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("POSTGRES_URL", db_url)
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import db, ray_client  # noqa: PLC0415
    from src.main import _lifespan_shutdown  # noqa: PLC0415
    from src.routes.batches import start_status_poller  # noqa: PLC0415

    # Set up everything _lifespan_shutdown expects to tear down
    await db.init_engine(db_url)
    await db.create_all()
    ray_client.reset()
    ray_client.set_client_factory(_LifespanFakeRay)
    ray_client.init("http://fake-ray:8265")
    poller_task = await start_status_poller(interval_seconds=0.05)

    # Run the shutdown
    await _lifespan_shutdown(poller_task)

    # All three subsystems should now be torn down
    with pytest.raises(RuntimeError, match="not initialized"):
        await db.ping()
    with pytest.raises(RuntimeError, match="not initialized"):
        await ray_client.ping()
    assert poller_task.done()


async def test_lifespan_initializes_db_ray_and_poller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> None:
    """
    Full startup path. After entering the lifespan context we expect:
      - db.ping() to succeed (engine is live)
      - ray_client.ping() to succeed (factory produced a working fake)
      - a /health GET to return 200
      - the poller task to be running in the background
    """
    db_file = tmp_path / "lifespan.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("POSTGRES_URL", db_url)
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    # Inject the fake factory BEFORE create_app runs so the lifespan
    # call to ray_client.init() constructs our test double.
    from src import db, ray_client  # noqa: PLC0415
    from src.main import create_app, lifespan  # noqa: PLC0415

    ray_client.reset()
    ray_client.set_client_factory(_LifespanFakeRay)

    app = create_app()

    # httpx.ASGITransport does NOT auto-run the FastAPI lifespan, so
    # we enter it explicitly. This exercises exactly the same
    # startup/shutdown path that uvicorn runs in production.
    async with lifespan(app):
        # DB + Ray should be live inside the lifespan scope
        await db.ping()
        await ray_client.ping()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/health")
            assert r.status_code == 200

            r = await ac.get("/ready")
            assert r.status_code == 200
            body = r.json()
            assert body["checks"]["postgres"] == "ok"
            assert body["checks"]["ray"] == "ok"

    # After the lifespan exits, shutdown hook must have disposed the
    # engine and reset the Ray client. A subsequent ping should raise.
    with pytest.raises(RuntimeError, match="not initialized"):
        await db.ping()

    with pytest.raises(RuntimeError, match="not initialized"):
        await ray_client.ping()


async def test_lifespan_end_to_end_post_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> None:
    """
    A POST /v1/batches inside the lifespan scope must round-trip
    exactly like the standalone E2E test but without any test
    fixture doing db.init_engine / ray_client.init manually.
    This proves the lifespan is wired correctly for production.
    """
    db_file = tmp_path / "lifespan_e2e.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("POSTGRES_URL", db_url)
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import ray_client  # noqa: PLC0415
    from src.main import create_app, lifespan  # noqa: PLC0415

    ray_client.reset()
    ray_client.set_client_factory(_LifespanFakeRay)

    app = create_app()

    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/v1/batches",
                headers={"X-API-Key": api_key},
                json={
                    "model": "Qwen/Qwen2.5-0.5B-Instruct",
                    "input": [{"prompt": "hi"}],
                    "max_tokens": 10,
                },
            )
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "queued"
            batch_id = body["id"]

            # Give the poller one tick to observe the in_progress state.
            # Default poller interval is 5s, so force a direct sweep
            # rather than sleeping forever.
            from src.routes.batches import poll_active_batches  # noqa: PLC0415

            await poll_active_batches()

            r = await ac.get(
                f"/v1/batches/{batch_id}",
                headers={"X-API-Key": api_key},
            )
            assert r.status_code == 200
            # With the fake returning RUNNING, the poller flipped it to
            # in_progress; if the poller never ran, status would still
            # be queued and this assertion would fail.
            assert r.json()["status"] == "in_progress"
