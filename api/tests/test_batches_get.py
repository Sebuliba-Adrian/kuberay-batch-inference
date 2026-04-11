"""
Red tests for GET /v1/batches/{batch_id}.

The status endpoint reads the DB row and returns it in OpenAI-shaped
BatchObject form. It does NOT query Ray on the hot path — the
background status poller (later TDD cycle) is responsible for
keeping the DB row fresh. This separation keeps the GET handler
fast and resilient to Ray outages.
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
        return "raysubmit_fake"

    def get_job_status(self, _sid: str) -> str:
        return "PENDING"

    def list_jobs(self) -> list[Any]:
        return []


@pytest.fixture
async def app_and_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> AsyncIterator[Any]:
    """
    Minimal app + DB with a fake Ray client. Each test gets a fresh
    in-memory SQLite.
    """
    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "sqlite+aiosqlite:///file:batches_get_test?mode=memory&cache=shared&uri=true",
    )
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import db, ray_client  # noqa: PLC0415
    from src.main import create_app  # noqa: PLC0415

    await db.init_engine(
        "sqlite+aiosqlite:///file:batches_get_test?mode=memory&cache=shared&uri=true"
    )
    await db.create_all()

    ray_client.reset()
    ray_client.set_client_factory(_FakeRay)
    ray_client.init("http://fake-ray:8265")

    yield create_app()

    from sqlalchemy import text  # noqa: PLC0415

    async with db.session_scope() as s:
        await s.execute(text("DELETE FROM batches"))
    await db.dispose()
    ray_client.reset()


async def _get(
    app: Any, batch_id: str, *, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(f"/v1/batches/{batch_id}", headers=headers or {})
    return r.status_code, r.json() if r.content else {}


async def _seed_batch(
    *,
    batch_id: str = "batch_seed",
    status: str = "queued",
    input_count: int = 2,
    completed_count: int = 0,
    failed_count: int = 0,
    error: str | None = None,
) -> None:
    """Insert a Batch row for the current test's DB."""
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    async with db.session_scope() as s:
        s.add(
            Batch(
                id=batch_id,
                status=status,
                model="Qwen/Qwen2.5-0.5B-Instruct",
                input_count=input_count,
                completed_count=completed_count,
                failed_count=failed_count,
                error=error,
                max_tokens=50,
            )
        )


# ─── Auth ───────────────────────────────────────────────────────────
async def test_get_batch_rejects_missing_api_key(
    app_and_db: Any,
) -> None:
    status, body = await _get(app_and_db, "batch_anything")
    assert status == 401
    assert body.get("detail") == "Missing API key"


async def test_get_batch_rejects_wrong_api_key(app_and_db: Any) -> None:
    status, body = await _get(app_and_db, "batch_anything", headers={"X-API-Key": "wrong"})
    assert status == 401
    assert body.get("detail") == "Invalid API key"


# ─── Not found ──────────────────────────────────────────────────────
async def test_get_unknown_batch_returns_404(app_and_db: Any, api_key: str) -> None:
    status, body = await _get(app_and_db, "batch_does_not_exist", headers={"X-API-Key": api_key})
    assert status == 404
    assert "not found" in body.get("detail", "").lower()


# ─── Happy paths per state ──────────────────────────────────────────
async def test_get_queued_batch_returns_full_shape(app_and_db: Any, api_key: str) -> None:
    await _seed_batch(batch_id="batch_q", status="queued", input_count=5)

    status, body = await _get(app_and_db, "batch_q", headers={"X-API-Key": api_key})

    assert status == 200
    assert body["id"] == "batch_q"
    assert body["object"] == "batch"
    assert body["endpoint"] == "/v1/batches"
    assert body["status"] == "queued"
    assert body["model"] == "Qwen/Qwen2.5-0.5B-Instruct"
    assert body["request_counts"] == {"total": 5, "completed": 0, "failed": 0}
    assert body["error"] is None
    assert body["completed_at"] is None
    assert isinstance(body["created_at"], int)


async def test_get_in_progress_batch(app_and_db: Any, api_key: str) -> None:
    await _seed_batch(
        batch_id="batch_p",
        status="in_progress",
        input_count=10,
        completed_count=3,
    )

    status, body = await _get(app_and_db, "batch_p", headers={"X-API-Key": api_key})

    assert status == 200
    assert body["status"] == "in_progress"
    assert body["request_counts"]["completed"] == 3
    assert body["request_counts"]["total"] == 10


async def test_get_completed_batch_has_completed_at(app_and_db: Any, api_key: str) -> None:
    """A completed batch must surface completed_at as Unix seconds."""
    import datetime as _dt

    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    now_utc = _dt.datetime.now(_dt.UTC)
    async with db.session_scope() as s:
        s.add(
            Batch(
                id="batch_done",
                status="completed",
                model="m",
                input_count=2,
                completed_count=2,
                failed_count=0,
                completed_at=now_utc,
            )
        )

    status, body = await _get(app_and_db, "batch_done", headers={"X-API-Key": api_key})

    assert status == 200
    assert body["status"] == "completed"
    assert isinstance(body["completed_at"], int)
    assert abs(body["completed_at"] - int(now_utc.timestamp())) < 2


async def test_get_failed_batch_exposes_error(app_and_db: Any, api_key: str) -> None:
    await _seed_batch(
        batch_id="batch_fail",
        status="failed",
        input_count=3,
        failed_count=3,
        error="Ray cluster unreachable",
    )

    status, body = await _get(app_and_db, "batch_fail", headers={"X-API-Key": api_key})

    assert status == 200
    assert body["status"] == "failed"
    assert body["error"] == "Ray cluster unreachable"
    assert body["request_counts"]["failed"] == 3


# ─── Batch id format ────────────────────────────────────────────────
async def test_get_handles_arbitrary_batch_id_characters(app_and_db: Any, api_key: str) -> None:
    """Valid ULID-style ids (letters + digits) must route correctly."""
    await _seed_batch(batch_id="batch_01ABCXYZ0123456789")

    status, _body = await _get(
        app_and_db,
        "batch_01ABCXYZ0123456789",
        headers={"X-API-Key": api_key},
    )
    assert status == 200
