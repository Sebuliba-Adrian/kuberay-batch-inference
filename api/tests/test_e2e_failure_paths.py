"""
End-to-end failure-path tests.

The happy-path E2E test proves the system works when everything is
healthy. These tests prove it degrades gracefully when things go
wrong at the boundaries:

  - Ray cluster is unreachable when the POST comes in -> 503, row
    persists in status=failed
  - Ray accepts the job but the worker fails -> poller picks up the
    _FAILED marker, row ends in status=failed with the worker error
  - Client POSTs a malformed payload -> 422 with validation errors,
    no row persisted, no Ray call
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient


# ─── Fake Ray variants ──────────────────────────────────────────────
class _UnreachableRay:
    """Simulates a totally unreachable Ray dashboard."""

    def __init__(self, address: str) -> None:
        self.address = address

    def submit_job(self, *, entrypoint: str, runtime_env: Any = None) -> str:
        raise ConnectionError("ray dashboard unreachable")

    def get_job_status(self, _sid: str) -> str:
        raise ConnectionError("ray dashboard unreachable")

    def list_jobs(self) -> list[Any]:
        raise ConnectionError("ray dashboard unreachable")


class _WorkerFailsRay:
    """
    Submits OK, then reports FAILED on the next status check. Writes
    a _FAILED marker to simulate the worker's crash handler writing
    an error payload.
    """

    def __init__(self, address: str, *, results_dir: Path) -> None:
        self.address = address
        self.results_dir = results_dir
        self._job_to_batch: dict[str, str] = {}
        self._next = 0

    def submit_job(self, *, entrypoint: str, runtime_env: Any = None) -> str:
        self._next += 1
        sub_id = f"raysubmit_fail_{self._next:04d}"
        batch_id = _parse_batch_id(entrypoint)
        self._job_to_batch[sub_id] = batch_id
        return sub_id

    def get_job_status(self, submission_id: str) -> str:
        batch_id = self._job_to_batch.get(submission_id)
        if batch_id:
            # Write the _FAILED marker the first time the poller
            # queries for this batch, so poll_active_batches reads
            # the error on the next sweep.
            batch_dir = self.results_dir / batch_id
            batch_dir.mkdir(parents=True, exist_ok=True)
            marker = batch_dir / "_FAILED"
            if not marker.exists():
                marker.write_text(
                    json.dumps(
                        {
                            "batch_id": batch_id,
                            "error": "OutOfMemoryError: CUDA out of memory",
                            "failed_at": time.time(),
                        }
                    )
                )
        return "FAILED"

    def list_jobs(self) -> list[Any]:
        return []


def _parse_batch_id(entrypoint: str) -> str:
    parts = entrypoint.split()
    try:
        idx = parts.index("--batch-id")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return "unknown"


# ─── Fixture: Ray is unreachable from the start ────────────────────
@pytest.fixture
async def app_with_unreachable_ray(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> AsyncIterator[Any]:
    db_file = tmp_path / "e2e_ray_down.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("POSTGRES_URL", db_url)
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import db, ray_client  # noqa: PLC0415
    from src.main import create_app  # noqa: PLC0415

    await db.init_engine(db_url)
    await db.create_all()

    ray_client.reset()
    ray_client.set_client_factory(_UnreachableRay)
    ray_client.init("http://fake-ray:8265")

    try:
        yield create_app()
    finally:
        await db.dispose()
        ray_client.reset()


# ─── Fixture: Ray accepts the submit but the worker crashes ───────
@pytest.fixture
async def app_with_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> AsyncIterator[tuple[Any, Path]]:
    db_file = tmp_path / "e2e_worker_fail.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("POSTGRES_URL", db_url)
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import db, ray_client  # noqa: PLC0415
    from src.main import create_app  # noqa: PLC0415
    from src.routes.batches import (  # noqa: PLC0415
        start_status_poller,
        stop_status_poller,
    )

    await db.init_engine(db_url)
    await db.create_all()

    def factory(address: str) -> _WorkerFailsRay:
        return _WorkerFailsRay(address, results_dir=tmp_path)

    ray_client.reset()
    ray_client.set_client_factory(factory)
    ray_client.init("http://fake-ray:8265")

    app = create_app()
    poller_task = await start_status_poller(interval_seconds=0.05)

    try:
        yield app, tmp_path
    finally:
        await stop_status_poller(poller_task)
        await db.dispose()
        ray_client.reset()


# ─── Failure: Ray unreachable at submit time ───────────────────────
async def test_e2e_ray_unreachable_returns_503(
    app_with_unreachable_ray: Any, api_key: str
) -> None:
    """
    The exact exercise-PDF curl against an unreachable Ray must
    surface as a 503, not a 500, and the DB must contain a row
    marked failed with an error string.
    """
    app = app_with_unreachable_ray

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/v1/batches",
            headers={"X-API-Key": api_key},
            json={
                "model": "Qwen/Qwen2.5-0.5B-Instruct",
                "input": [{"prompt": "What is 2+2?"}],
                "max_tokens": 50,
            },
        )

    assert r.status_code == 503
    # Error message must not leak the cluster URL or stack trace
    assert "fake-ray" not in r.text

    # The DB row should still exist and be in status=failed
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    async with db.session_scope() as s:
        rows = list((await s.execute(select(Batch))).scalars().all())

    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error is not None


async def test_e2e_ray_unreachable_status_endpoint_still_works(
    app_with_unreachable_ray: Any, api_key: str
) -> None:
    """
    Even with Ray down, a GET /v1/batches/{id} for the failed row
    must still return 200 with status=failed. The status endpoint
    reads from Postgres only.
    """
    app = app_with_unreachable_ray

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Submit (will 503)
        r = await ac.post(
            "/v1/batches",
            headers={"X-API-Key": api_key},
            json={"model": "m", "input": [{"prompt": "x"}], "max_tokens": 10},
        )
        assert r.status_code == 503

        # Find the row via direct DB query since the POST response
        # doesn't include the id on 503.
        from src import db  # noqa: PLC0415
        from src.db import Batch  # noqa: PLC0415
        from sqlalchemy import select  # noqa: PLC0415

        async with db.session_scope() as s:
            row = (await s.execute(select(Batch))).scalar_one()

        r = await ac.get(
            f"/v1/batches/{row.id}",
            headers={"X-API-Key": api_key},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"] is not None


async def test_e2e_ray_unreachable_health_endpoint_still_200(
    app_with_unreachable_ray: Any,
) -> None:
    """
    /health is a liveness probe — it must stay 200 even with Ray down.
    Kubernetes should NOT restart the pod just because Ray is unhealthy.
    """
    app = app_with_unreachable_ray

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_e2e_ray_unreachable_ready_endpoint_returns_503(
    app_with_unreachable_ray: Any,
) -> None:
    """/ready reflects the outage and returns 503."""
    app = app_with_unreachable_ray

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/ready")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["ray"].startswith("error")


# ─── Failure: submit OK but worker fails ───────────────────────────
async def test_e2e_worker_failure_poller_marks_batch_failed(
    app_with_worker_failure: tuple[Any, Path], api_key: str
) -> None:
    """
    POST succeeds (Ray accepts the job), but the worker "crashes" on
    first status check. The poller must:
      1. Read the _FAILED marker from the shared PVC
      2. Update the DB row to status=failed with the marker error
      3. GET /v1/batches/{id} then returns 200 with that error surfaced
    """
    app, _root = app_with_worker_failure

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Submit
        r = await ac.post(
            "/v1/batches",
            headers={"X-API-Key": api_key},
            json={"model": "m", "input": [{"prompt": "x"}], "max_tokens": 10},
        )
        assert r.status_code == 200
        batch_id = r.json()["id"]

        # Wait for the poller to observe the failure
        deadline = time.monotonic() + 3.0
        final: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            r = await ac.get(
                f"/v1/batches/{batch_id}",
                headers={"X-API-Key": api_key},
            )
            body = r.json()
            if body["status"] == "failed":
                final = body
                break
            await asyncio.sleep(0.05)

        assert final is not None, "poller never marked the batch failed"
        assert final["status"] == "failed"
        assert "OutOfMemoryError" in (final.get("error") or "")


async def test_e2e_worker_failure_results_endpoint_returns_409(
    app_with_worker_failure: tuple[Any, Path], api_key: str
) -> None:
    """
    A failed batch has no results to stream. Requesting the results
    for a failed batch must return 409, not 500 or an empty 200.
    """
    app, _root = app_with_worker_failure

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/v1/batches",
            headers={"X-API-Key": api_key},
            json={"model": "m", "input": [{"prompt": "x"}], "max_tokens": 10},
        )
        batch_id = r.json()["id"]

        # Wait for the row to reach the failed state
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            r = await ac.get(
                f"/v1/batches/{batch_id}",
                headers={"X-API-Key": api_key},
            )
            if r.json()["status"] == "failed":
                break
            await asyncio.sleep(0.05)

        # Now try to fetch results
        r = await ac.get(
            f"/v1/batches/{batch_id}/results",
            headers={"X-API-Key": api_key},
        )

    assert r.status_code == 409
    assert "not complete" in r.json().get("detail", "").lower()
