"""
Red tests for the background status poller.

The poller is responsible for keeping the DB row for each in-flight
batch up to date with the Ray job's real state. It runs as a single
asyncio task started by the app lifespan; we test the single-pass
core (`poll_active_batches`) directly without spinning up the loop.

Pipeline for each active batch:
  1. Fetch current Ray status
  2. If still active, do nothing
  3. If SUCCESS, read the _SUCCESS marker for counts and mark completed
  4. If FAILED, read the _FAILED marker (or fall back to generic) and mark failed
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest


# ─── Test fixtures ──────────────────────────────────────────────────
class _PollerFakeRay:
    """Test-controllable Ray client."""

    def __init__(self, address: str) -> None:
        self.statuses: dict[str, str] = {}

    def submit_job(self, *, entrypoint: str, runtime_env: Any = None) -> str:
        return "raysubmit_unused"

    def get_job_status(self, submission_id: str) -> str:
        return self.statuses.get(submission_id, "RUNNING")

    def list_jobs(self) -> list[Any]:
        return []


@pytest.fixture
async def poller_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> AsyncIterator[tuple[_PollerFakeRay, Path]]:
    """
    DB (real file-backed SQLite) + storage dir + controllable fake Ray.

    We use a file path under tmp_path rather than ``:memory:`` because
    SQLite drops in-memory shared-cache databases when the last
    connection closes. The poller task opens and closes connections
    during its sweep; if the background loop gets cancelled mid-flight
    the shared-cache DB can vanish before the fixture teardown runs
    its cleanup DELETE, producing a misleading "no such table" error.
    A real file-backed DB is immune to this race.
    """
    db_file = tmp_path / "poller_test.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("POSTGRES_URL", db_url)
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import db, ray_client  # noqa: PLC0415

    await db.init_engine(db_url)
    await db.create_all()

    captured: list[_PollerFakeRay] = []

    def factory(address: str) -> _PollerFakeRay:
        c = _PollerFakeRay(address)
        captured.append(c)
        return c

    ray_client.reset()
    ray_client.set_client_factory(factory)
    ray_client.init("http://fake-ray:8265")

    yield captured[0], tmp_path

    await db.dispose()
    ray_client.reset()


async def _seed(
    batch_id: str,
    status_val: str,
    ray_job_id: str,
    *,
    input_count: int = 2,
    completed_count: int = 0,
    failed_count: int = 0,
) -> None:
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    async with db.session_scope() as s:
        s.add(
            Batch(
                id=batch_id,
                status=status_val,
                model="m",
                ray_job_id=ray_job_id,
                input_count=input_count,
                completed_count=completed_count,
                failed_count=failed_count,
            )
        )


def _write_success_marker(
    root: Path, batch_id: str, total: int, completed: int, failed: int
) -> None:
    batch_dir = root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "_SUCCESS").write_text(
        json.dumps(
            {
                "batch_id": batch_id,
                "total": total,
                "completed": completed,
                "failed": failed,
                "model": "m",
                "finished_at": 123.0,
            }
        )
    )


def _write_failure_marker(root: Path, batch_id: str, error: str) -> None:
    batch_dir = root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "_FAILED").write_text(
        json.dumps({"batch_id": batch_id, "error": error, "failed_at": 123.0})
    )


# ─── poll_active_batches: still running ────────────────────────────
async def test_poll_leaves_running_batch_unchanged(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    fake, _root = poller_env
    fake.statuses["raysubmit_001"] = "RUNNING"
    await _seed("batch_r", "queued", "raysubmit_001")

    from src import db  # noqa: PLC0415
    from src.routes.batches import poll_active_batches  # noqa: PLC0415

    await poll_active_batches()

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_r")
    assert row is not None
    # Ray RUNNING maps to in_progress - we update the status even when
    # still active, so the row should now reflect that.
    assert row.status == "in_progress"
    assert row.completed_at is None


async def test_poll_keeps_queued_when_ray_still_pending(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    fake, _root = poller_env
    fake.statuses["raysubmit_002"] = "PENDING"
    await _seed("batch_q", "queued", "raysubmit_002")

    from src import db  # noqa: PLC0415
    from src.routes.batches import poll_active_batches  # noqa: PLC0415

    await poll_active_batches()

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_q")
    assert row is not None
    assert row.status == "queued"
    assert row.completed_at is None


# ─── SUCCEEDED → reads counts from _SUCCESS marker ─────────────────
async def test_poll_promotes_succeeded_batch_to_completed(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    fake, root = poller_env
    fake.statuses["raysubmit_003"] = "SUCCEEDED"
    _write_success_marker(root, "batch_ok", total=5, completed=4, failed=1)
    await _seed("batch_ok", "in_progress", "raysubmit_003", input_count=5)

    from src import db  # noqa: PLC0415
    from src.routes.batches import poll_active_batches  # noqa: PLC0415

    await poll_active_batches()

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_ok")

    assert row is not None
    assert row.status == "completed"
    assert row.completed_count == 4
    assert row.failed_count == 1
    assert row.completed_at is not None


async def test_poll_succeeded_without_marker_still_marks_completed(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    """
    If Ray says SUCCEEDED but _SUCCESS is missing (the worker's post-run
    marker write raced), the poller still marks the row completed and
    trusts the input_count as the completed count.
    """
    fake, _root = poller_env
    fake.statuses["raysubmit_004"] = "SUCCEEDED"
    await _seed("batch_no_marker", "in_progress", "raysubmit_004", input_count=3)

    from src import db  # noqa: PLC0415
    from src.routes.batches import poll_active_batches  # noqa: PLC0415

    await poll_active_batches()

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_no_marker")

    assert row is not None
    assert row.status == "completed"
    assert row.completed_count == 3  # fell back to input_count
    assert row.failed_count == 0


# ─── FAILED → reads _FAILED marker error ───────────────────────────
async def test_poll_marks_failed_batch_with_marker_error(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    fake, root = poller_env
    fake.statuses["raysubmit_005"] = "FAILED"
    _write_failure_marker(root, "batch_fail", "vllm oom")
    await _seed("batch_fail", "in_progress", "raysubmit_005")

    from src import db  # noqa: PLC0415
    from src.routes.batches import poll_active_batches  # noqa: PLC0415

    await poll_active_batches()

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_fail")

    assert row is not None
    assert row.status == "failed"
    assert row.error == "vllm oom"
    assert row.completed_at is not None


async def test_poll_failed_without_marker_uses_generic_error(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    fake, _root = poller_env
    fake.statuses["raysubmit_006"] = "FAILED"
    await _seed("batch_nosignal", "in_progress", "raysubmit_006")

    from src import db  # noqa: PLC0415
    from src.routes.batches import poll_active_batches  # noqa: PLC0415

    await poll_active_batches()

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_nosignal")

    assert row is not None
    assert row.status == "failed"
    assert row.error is not None
    assert len(row.error) > 0


# ─── STOPPED → cancelled ───────────────────────────────────────────
async def test_poll_maps_stopped_to_cancelled(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    fake, _root = poller_env
    fake.statuses["raysubmit_007"] = "STOPPED"
    await _seed("batch_stop", "in_progress", "raysubmit_007")

    from src import db  # noqa: PLC0415
    from src.routes.batches import poll_active_batches  # noqa: PLC0415

    await poll_active_batches()

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_stop")
    assert row is not None
    assert row.status == "cancelled"


# ─── Skips batches with no ray_job_id (never submitted) ────────────
async def test_poll_skips_rows_without_ray_job_id(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    """
    A row could be in 'queued' but still have no ray_job_id briefly
    during create_batch. The poller must ignore those rows rather than
    crash on a None submission id.
    """
    _fake, _root = poller_env
    # Intentionally do NOT set a status in fake.statuses
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    async with db.session_scope() as s:
        s.add(
            Batch(
                id="batch_nosub",
                status="queued",
                model="m",
                input_count=1,
                ray_job_id=None,
            )
        )

    from src.routes.batches import poll_active_batches  # noqa: PLC0415

    # Should not raise
    await poll_active_batches()

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_nosub")
    assert row is not None
    # Unchanged
    assert row.status == "queued"


# ─── Ray errors are swallowed, not propagated ───────────────────────
async def test_poll_swallows_ray_errors_on_individual_batches(
    poller_env: tuple[_PollerFakeRay, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    If Ray errors while checking one batch, the poller must log and
    continue to the next - a transient Ray hiccup should not stall
    every in-flight batch.
    """
    fake, _root = poller_env
    fake.statuses["raysubmit_ok"] = "SUCCEEDED"

    # Make get_job_status raise for the FIRST batch only.
    orig = fake.get_job_status
    call_count = {"n": 0}

    def _maybe_raise(sid: str) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionError("flaky")
        return orig(sid)

    fake.get_job_status = _maybe_raise  # type: ignore[method-assign]

    await _seed("batch_a", "in_progress", "raysubmit_a", input_count=1)
    await _seed("batch_b", "in_progress", "raysubmit_ok", input_count=1)

    from src import db  # noqa: PLC0415
    from src.routes.batches import poll_active_batches  # noqa: PLC0415

    # Must not raise - the error on batch_a should be caught
    await poll_active_batches()

    async with db.session_scope() as s:
        a = await db.get_batch(s, "batch_a")
        b = await db.get_batch(s, "batch_b")

    # batch_a stays unchanged, batch_b advances
    assert a is not None
    assert a.status == "in_progress"
    assert b is not None
    assert b.status == "completed"


# ─── start_status_poller / stop_status_poller lifecycle ────────────
async def test_start_and_stop_status_poller(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    """
    start_status_poller returns an asyncio.Task that can be cancelled
    cleanly by stop_status_poller.
    """
    import asyncio

    from src.routes.batches import start_status_poller, stop_status_poller

    task = await start_status_poller(interval_seconds=0.05)
    assert isinstance(task, asyncio.Task)
    assert not task.done()

    # Let the loop run at least once
    await asyncio.sleep(0.15)

    await stop_status_poller(task)
    assert task.done()
    assert task.cancelled() or task.exception() is None


# ─── Defensive guards for the internal _apply_* helpers ───────────
async def test_apply_success_with_missing_row_returns_silently(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    import datetime as _dt

    from src.routes.batches import _apply_success

    # No seeding - row doesn't exist
    await _apply_success(
        "batch_missing",
        {"completed": 1, "failed": 0},
        _dt.datetime.now(_dt.UTC),
    )  # must not raise


async def test_apply_terminal_with_missing_row_returns_silently(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    import datetime as _dt

    from src.routes.batches import _apply_terminal

    await _apply_terminal(
        "batch_missing",
        "failed",
        "some error",
        _dt.datetime.now(_dt.UTC),
    )  # must not raise


async def test_update_status_only_with_missing_row_returns_silently(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    """
    The compound-condition split in ``_update_status_only`` created two
    explicit early-return branches. This test pins the first one (row
    is None) so coverage.py tracks it deterministically across
    Linux/3.11 and Windows/3.12.
    """
    from src.routes.batches import _update_status_only

    # No seeding - row does not exist. Must not raise.
    await _update_status_only("batch_missing", "in_progress")


async def test_update_status_only_is_idempotent_when_status_unchanged(
    poller_env: tuple[_PollerFakeRay, Path],
) -> None:
    """
    The second early-return branch fires when the stored status already
    matches the requested new status. Covering it explicitly keeps the
    branch count honest.
    """
    from src import db
    from src.db import Batch
    from src.routes.batches import _update_status_only

    async with db.session_scope() as s:
        s.add(
            Batch(
                id="batch_idempotent",
                status="in_progress",
                model="m",
                input_count=1,
            )
        )

    # Calling with the SAME status should hit the early return branch,
    # not actually mutate the row.
    await _update_status_only("batch_idempotent", "in_progress")

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_idempotent")
    assert row is not None
    assert row.status == "in_progress"


# ─── Poller loop re-raises CancelledError from inside the sweep ────
async def test_poller_loop_reraises_cancelled_error_from_sweep(
    poller_env: tuple[_PollerFakeRay, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Force the cancellation to land WHILE ``poll_active_batches()`` is
    mid-execution, not during the idle ``asyncio.sleep`` between
    sweeps. This exercises the ``except asyncio.CancelledError: raise``
    branch deterministically across platforms - otherwise coverage
    only hits it when timing happens to put the task inside the try
    block at cancel time.
    """
    import asyncio

    from src.routes import batches as batches_mod

    inside_sweep = asyncio.Event()

    async def _slow_sweep() -> None:
        inside_sweep.set()
        # Long sleep guaranteed to still be awaiting when the task is
        # cancelled - CancelledError will raise out of this sleep,
        # unwind to the enclosing ``try``, and hit the
        # ``except asyncio.CancelledError: raise`` branch.
        await asyncio.sleep(10)

    monkeypatch.setattr(batches_mod, "poll_active_batches", _slow_sweep)

    task = await batches_mod.start_status_poller(interval_seconds=0.01)
    # Wait until we know the task is INSIDE _slow_sweep (i.e. inside
    # the try block), then cancel it.
    await inside_sweep.wait()
    await batches_mod.stop_status_poller(task)

    assert task.done()
    assert task.cancelled() or isinstance(task.exception(), asyncio.CancelledError)


# ─── Poller loop swallows non-Cancelled exceptions ─────────────────
async def test_poller_loop_logs_and_continues_on_sweep_exception(
    poller_env: tuple[_PollerFakeRay, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    If poll_active_batches raises a non-CancelledError the loop must
    log the error and keep running - a bug in one sweep cannot stall
    all future sweeps.
    """
    import asyncio
    import logging

    from src.routes import batches as batches_mod

    sweep_count = {"n": 0}

    async def _flaky_sweep() -> None:
        sweep_count["n"] += 1
        if sweep_count["n"] == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(batches_mod, "poll_active_batches", _flaky_sweep)

    caplog.set_level(logging.ERROR, logger="src.routes.batches")
    task = await batches_mod.start_status_poller(interval_seconds=0.02)

    # Wait long enough for at least 2 sweeps: the first raises, the
    # second should still run because the loop caught the error.
    await asyncio.sleep(0.15)
    await batches_mod.stop_status_poller(task)

    assert sweep_count["n"] >= 2
    assert any("sweep raised" in record.message for record in caplog.records)
