"""
Red tests for GET /v1/batches/{id}/results.

The results endpoint streams the JSONL output that the Ray worker
wrote to the shared PVC. Contract:
  - 401 on missing / invalid X-API-Key
  - 404 when the batch row does not exist
  - 409 when the batch exists but is not yet 'completed'
  - 200 with application/x-ndjson on success
  - Body is streamed line-by-line so memory stays flat
"""

from __future__ import annotations

import json
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
async def app_db_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> AsyncIterator[tuple[Any, Path]]:
    """Minimal app + DB + tmp storage + fake Ray, for results tests."""
    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "sqlite+aiosqlite:///file:batches_results_test?mode=memory&cache=shared&uri=true",
    )
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import db, ray_client  # noqa: PLC0415
    from src.main import create_app  # noqa: PLC0415

    await db.init_engine(
        "sqlite+aiosqlite:///file:batches_results_test?mode=memory&cache=shared&uri=true"
    )
    await db.create_all()

    ray_client.reset()
    ray_client.set_client_factory(_FakeRay)
    ray_client.init("http://fake-ray:8265")

    yield create_app(), tmp_path

    from sqlalchemy import text  # noqa: PLC0415

    async with db.session_scope() as s:
        await s.execute(text("DELETE FROM batches"))
    await db.dispose()
    ray_client.reset()


async def _seed_batch(
    batch_id: str,
    status: str,
    *,
    input_count: int = 2,
    completed_count: int = 0,
    results_path: str | None = None,
) -> None:
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    async with db.session_scope() as s:
        s.add(
            Batch(
                id=batch_id,
                status=status,
                model="m",
                input_count=input_count,
                completed_count=completed_count,
                results_path=results_path,
            )
        )


def _write_results_jsonl(root: Path, batch_id: str, rows: list[dict[str, Any]]) -> Path:
    """Write a results.jsonl file to the per-batch subdirectory."""
    batch_dir = root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    path = batch_dir / "results.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


# ─── Auth ───────────────────────────────────────────────────────────
async def test_results_rejects_missing_api_key(
    app_db_storage: tuple[Any, Path],
) -> None:
    app, _root = app_db_storage
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/v1/batches/batch_any/results")
    assert r.status_code == 401


async def test_results_rejects_wrong_api_key(
    app_db_storage: tuple[Any, Path],
) -> None:
    app, _root = app_db_storage
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/v1/batches/batch_any/results",
            headers={"X-API-Key": "wrong"},
        )
    assert r.status_code == 401


# ─── Not found ──────────────────────────────────────────────────────
async def test_results_404_for_unknown_batch(
    app_db_storage: tuple[Any, Path], api_key: str
) -> None:
    app, _root = app_db_storage
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/v1/batches/batch_missing/results",
            headers={"X-API-Key": api_key},
        )
    assert r.status_code == 404
    assert "not found" in r.json().get("detail", "").lower()


# ─── Not yet complete ───────────────────────────────────────────────
async def test_results_409_when_batch_still_queued(
    app_db_storage: tuple[Any, Path], api_key: str
) -> None:
    """Results endpoint must refuse reading until status=completed."""
    app, _root = app_db_storage
    await _seed_batch("batch_queued", "queued")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/v1/batches/batch_queued/results",
            headers={"X-API-Key": api_key},
        )

    assert r.status_code == 409
    assert "not complete" in r.json().get("detail", "").lower()


async def test_results_409_when_batch_in_progress(
    app_db_storage: tuple[Any, Path], api_key: str
) -> None:
    app, _root = app_db_storage
    await _seed_batch("batch_ip", "in_progress")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/v1/batches/batch_ip/results",
            headers={"X-API-Key": api_key},
        )

    assert r.status_code == 409


async def test_results_409_when_batch_failed(
    app_db_storage: tuple[Any, Path], api_key: str
) -> None:
    """A failed batch has no results to stream - 409, not 200."""
    app, _root = app_db_storage
    await _seed_batch("batch_fail", "failed")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/v1/batches/batch_fail/results",
            headers={"X-API-Key": api_key},
        )

    assert r.status_code == 409


# ─── Happy path ─────────────────────────────────────────────────────
async def test_results_200_returns_ndjson_for_completed_batch(
    app_db_storage: tuple[Any, Path], api_key: str
) -> None:
    app, root = app_db_storage
    rows = [
        {
            "id": "0",
            "prompt": "What is 2+2?",
            "response": "4",
            "finish_reason": "stop",
            "prompt_tokens": 6,
            "completion_tokens": 1,
            "error": None,
        },
        {
            "id": "1",
            "prompt": "Hello world",
            "response": "Hi there",
            "finish_reason": "stop",
            "prompt_tokens": 4,
            "completion_tokens": 3,
            "error": None,
        },
    ]
    path = _write_results_jsonl(root, "batch_done", rows)

    await _seed_batch(
        "batch_done",
        "completed",
        input_count=2,
        completed_count=2,
        results_path=str(path),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/v1/batches/batch_done/results",
            headers={"X-API-Key": api_key},
        )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    body_text = r.text
    lines = [line for line in body_text.splitlines() if line]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["id"] == "0"
    assert first["response"] == "4"
    second = json.loads(lines[1])
    assert second["id"] == "1"


async def test_results_streaming_preserves_order(
    app_db_storage: tuple[Any, Path], api_key: str
) -> None:
    """Rows must come out in the same order they were written."""
    app, root = app_db_storage
    rows = [{"id": str(i), "response": f"r{i}"} for i in range(10)]
    _write_results_jsonl(root, "batch_ord", rows)
    await _seed_batch("batch_ord", "completed", input_count=10, completed_count=10)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/v1/batches/batch_ord/results",
            headers={"X-API-Key": api_key},
        )

    assert r.status_code == 200
    lines = [json.loads(line) for line in r.text.splitlines() if line]
    assert [row["id"] for row in lines] == [str(i) for i in range(10)]


# ─── File missing despite status=completed ─────────────────────────
async def test_results_500_when_completed_but_file_missing(
    app_db_storage: tuple[Any, Path], api_key: str
) -> None:
    """
    If the row says 'completed' but results.jsonl is missing, return
    500 - this is a data-plane corruption case.
    """
    app, _root = app_db_storage
    await _seed_batch(
        "batch_corrupt",
        "completed",
        input_count=1,
        completed_count=1,
    )
    # no _write_results_jsonl - file deliberately absent

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/v1/batches/batch_corrupt/results",
            headers={"X-API-Key": api_key},
        )

    assert r.status_code == 500
