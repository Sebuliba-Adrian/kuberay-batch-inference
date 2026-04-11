"""
Coverage-closing unit tests for src.routes.batches internals.

The happy-path tests in test_batches_post.py exercise the route via
HTTP but don't hit the defensive 'row is None' guards or the
Postgres-tz-aware branch of _to_unix. These tests target those paths
directly at the function level.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, AsyncIterator

import pytest


# ─── _to_unix: both branches ────────────────────────────────────────
def test_to_unix_with_naive_datetime_assumes_utc() -> None:
    """A naive datetime must be interpreted as UTC (SQLite path)."""
    from src.routes.batches import _to_unix

    naive = _dt.datetime(2026, 1, 1, 0, 0, 0)  # no tzinfo
    # 2026-01-01 00:00:00 UTC = 1767225600
    assert _to_unix(naive) == 1_767_225_600


def test_to_unix_with_tz_aware_datetime_passes_through() -> None:
    """A tz-aware datetime (Postgres path) must not be mutated."""
    from src.routes.batches import _to_unix

    aware = _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
    assert _to_unix(aware) == 1_767_225_600


def test_to_unix_tz_aware_non_utc_converts_correctly() -> None:
    """A tz-aware datetime in another zone must convert to correct UTC seconds."""
    from src.routes.batches import _to_unix

    # 2026-01-01 03:00:00 UTC+3 == 2026-01-01 00:00:00 UTC
    plus_three = _dt.timezone(_dt.timedelta(hours=3))
    aware = _dt.datetime(2026, 1, 1, 3, 0, 0, tzinfo=plus_three)
    assert _to_unix(aware) == 1_767_225_600


# ─── Fixture: minimal DB + settings for internal-function tests ────
@pytest.fixture
async def _db_only(
    monkeypatch: pytest.MonkeyPatch, api_key: str, tmp_path: Path
) -> AsyncIterator[None]:
    """
    Init just the DB and settings — no Ray, no app build. Enough to
    call the module helpers directly without going through HTTP.
    """
    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "sqlite+aiosqlite:///file:batches_internals?mode=memory&cache=shared&uri=true",
    )
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))

    from src import db  # noqa: PLC0415

    await db.init_engine(
        "sqlite+aiosqlite:///file:batches_internals?mode=memory&cache=shared&uri=true"
    )
    await db.create_all()

    yield

    from sqlalchemy import text  # noqa: PLC0415

    async with db.session_scope() as s:
        await s.execute(text("DELETE FROM batches"))
    await db.dispose()


# ─── Defensive guards: _mark_failed with missing row ───────────────
async def test_mark_failed_logs_and_returns_when_row_missing(
    _db_only: None, caplog: pytest.LogCaptureFixture
) -> None:
    """_mark_failed for an id that doesn't exist logs an error and returns."""
    import logging

    from src.routes.batches import _mark_failed

    caplog.set_level(logging.ERROR, logger="src.routes.batches")
    await _mark_failed("batch_does_not_exist", "boom")

    assert any(
        "row not found" in record.message for record in caplog.records
    )


async def test_attach_ray_job_id_logs_and_returns_when_row_missing(
    _db_only: None, caplog: pytest.LogCaptureFixture
) -> None:
    """_attach_ray_job_id for an id that doesn't exist logs and returns."""
    import logging

    from src.routes.batches import _attach_ray_job_id

    caplog.set_level(logging.ERROR, logger="src.routes.batches")
    await _attach_ray_job_id("batch_does_not_exist", "raysubmit_x")

    assert any(
        "row not found" in record.message for record in caplog.records
    )


# ─── Happy paths of the internal helpers ────────────────────────────
async def test_mark_failed_updates_existing_row(_db_only: None) -> None:
    """_mark_failed hits the happy path when the row exists."""
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415
    from src.routes.batches import _mark_failed

    async with db.session_scope() as s:
        s.add(
            Batch(
                id="batch_to_fail",
                status="queued",
                model="m",
                input_count=1,
            )
        )

    await _mark_failed("batch_to_fail", "something broke")

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_to_fail")

    assert row is not None
    assert row.status == "failed"
    assert row.error == "something broke"
    assert row.completed_at is not None


async def test_attach_ray_job_id_updates_existing_row(_db_only: None) -> None:
    """_attach_ray_job_id hits the happy path when the row exists."""
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415
    from src.routes.batches import _attach_ray_job_id

    async with db.session_scope() as s:
        s.add(
            Batch(
                id="batch_to_attach",
                status="queued",
                model="m",
                input_count=1,
            )
        )

    await _attach_ray_job_id("batch_to_attach", "raysubmit_7777")

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_to_attach")

    assert row is not None
    assert row.ray_job_id == "raysubmit_7777"


# ─── Defensive guard: refreshed is None after insert ────────────────
async def test_create_batch_returns_500_when_refreshed_row_vanishes(
    _db_only: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> None:
    """
    Monkeypatch db.get_batch so the post-insert refresh returns None.
    The handler must raise a 500 HTTPException rather than crashing
    with AttributeError.
    """
    from fastapi import HTTPException

    from src import db, ray_client  # noqa: PLC0415
    from src.models import BatchInputItem, CreateBatchRequest  # noqa: PLC0415
    from src.routes import batches as batches_mod  # noqa: PLC0415

    # Fake Ray so submit_batch succeeds
    class _Fake:
        def __init__(self, address: str) -> None: ...
        def submit_job(self, *, entrypoint: str, runtime_env: Any = None) -> str:
            return "raysubmit_test"
        def get_job_status(self, _sid: str) -> str:
            return "PENDING"
        def list_jobs(self) -> list[Any]:
            return []

    ray_client.reset()
    ray_client.set_client_factory(_Fake)
    ray_client.init("http://fake:8265")

    # Patch db.get_batch IN THE MODULE NAMESPACE where batches.py imported it
    async def _always_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(batches_mod.db, "get_batch", _always_none)

    # Call the route handler directly with a valid request
    req = CreateBatchRequest(
        model="m",
        input=[BatchInputItem(prompt="x")],
        max_tokens=10,
    )

    from src.config import get_settings  # noqa: PLC0415

    with pytest.raises(HTTPException) as exc_info:
        await batches_mod.create_batch(request=req, settings=get_settings())

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Internal error"

    ray_client.reset()
