"""
Red tests for src.db - the PostgreSQL/SQLAlchemy async persistence layer.

All tests use an in-memory aiosqlite database so they're hermetic and
fast - no docker-compose-postgres fixture required. The module under
test must accept any async SQLAlchemy URL, which it does via
init_engine(url).
"""

from __future__ import annotations

import pytest


# ─── Fixture: fresh in-memory DB per test ──────────────────────────
@pytest.fixture
async def fresh_db() -> None:
    """
    Initialize src.db against a fresh aiosqlite in-memory engine.
    Each test gets its own database; teardown disposes the engine so
    cached state can't leak into the next test.

    We use a URL with cache=shared so that SessionLocal(), an engine,
    and the ping connection all see the same underlying database.
    """
    from src import db  # noqa: PLC0415

    # Each test gets a unique in-memory DB via a named URI.
    # Without `cache=shared` a new :memory: connection gets its own
    # empty database - breaks tests that create_all then query.
    url = "sqlite+aiosqlite:///file:test_db?mode=memory&cache=shared&uri=true"
    await db.init_engine(url)
    await db.create_all()
    try:
        yield
    finally:
        # Wipe all rows and close the engine so the next test is clean.
        async with db.session_scope() as s:
            from sqlalchemy import text  # noqa: PLC0415

            await s.execute(text("DELETE FROM batches"))
        await db.dispose()


# ─── Engine lifecycle ───────────────────────────────────────────────
async def test_init_engine_accepts_aiosqlite() -> None:
    """init_engine() should succeed for the aiosqlite URL form."""
    from src import db  # noqa: PLC0415

    await db.init_engine("sqlite+aiosqlite:///:memory:")
    try:
        await db.create_all()
    finally:
        await db.dispose()


async def test_ping_raises_before_init() -> None:
    """Calling ping() without init_engine() must raise a clear error."""
    from src import db  # noqa: PLC0415

    # Ensure clean slate
    await db.dispose()

    with pytest.raises(RuntimeError, match="not initialized"):
        await db.ping()


async def test_session_scope_raises_before_init() -> None:
    """session_scope() without init_engine() must raise a clear error."""
    from src import db  # noqa: PLC0415

    await db.dispose()

    with pytest.raises(RuntimeError, match="must be called first"):
        async with db.session_scope():
            pass


async def test_create_all_raises_before_init() -> None:
    """create_all() without init_engine() must raise a clear error."""
    from src import db  # noqa: PLC0415

    await db.dispose()

    with pytest.raises(RuntimeError, match="must be called first"):
        await db.create_all()


async def test_init_engine_disposes_previous_engine() -> None:
    """Calling init_engine() twice must dispose the old engine first
    so tests (and hot-reloads) can rebind without leaks."""
    from src import db  # noqa: PLC0415

    await db.init_engine("sqlite+aiosqlite:///:memory:")
    first_engine = db._engine
    assert first_engine is not None

    # Re-init with a different URL exercises the dispose-then-rebind branch
    await db.init_engine("sqlite+aiosqlite:///:memory:")
    second_engine = db._engine
    assert second_engine is not None
    # The new engine instance must NOT be the same object as the first
    assert second_engine is not first_engine

    await db.dispose()


# ─── Ping ───────────────────────────────────────────────────────────
async def test_ping_succeeds_on_live_engine(fresh_db: None) -> None:
    """ping() runs a trivial SELECT against the engine."""
    from src import db  # noqa: PLC0415

    await db.ping()  # should not raise


# ─── CRUD ───────────────────────────────────────────────────────────
async def test_insert_and_fetch_batch(fresh_db: None) -> None:
    """Insert a Batch, read it back by id."""
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    async with db.session_scope() as s:
        s.add(
            Batch(
                id="batch_01ABC",
                status="queued",
                model="Qwen/Qwen2.5-0.5B-Instruct",
                input_count=3,
                max_tokens=128,
            )
        )

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_01ABC")

    assert row is not None
    assert row.id == "batch_01ABC"
    assert row.status == "queued"
    assert row.model == "Qwen/Qwen2.5-0.5B-Instruct"
    assert row.input_count == 3
    assert row.max_tokens == 128
    assert row.completed_count == 0
    assert row.failed_count == 0
    assert row.error is None
    assert row.created_at is not None


async def test_get_batch_returns_none_for_unknown_id(fresh_db: None) -> None:
    """Missing row returns None, does not raise."""
    from src import db  # noqa: PLC0415

    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_does_not_exist")

    assert row is None


async def test_session_scope_commits_on_success(fresh_db: None) -> None:
    """The async context manager must auto-commit on clean exit."""
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    async with db.session_scope() as s:
        s.add(Batch(id="batch_commit", status="queued", model="m", input_count=1))
    # If commit didn't fire, the row would be missing in a fresh session.
    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_commit")
    assert row is not None


async def test_session_scope_rolls_back_on_exception(fresh_db: None) -> None:
    """An exception inside the context manager must roll back the transaction."""
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    class DeliberateError(Exception):
        pass

    with pytest.raises(DeliberateError):
        async with db.session_scope() as s:
            s.add(Batch(id="batch_rollback", status="queued", model="m", input_count=1))
            raise DeliberateError

    # Row must NOT be visible - rollback fired.
    async with db.session_scope() as s:
        row = await db.get_batch(s, "batch_rollback")
    assert row is None


# ─── list_active_batches ────────────────────────────────────────────
async def test_list_active_batches_returns_only_non_terminal(fresh_db: None) -> None:
    """Helper filters to queued + in_progress, excludes terminal states."""
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    async with db.session_scope() as s:
        s.add_all(
            [
                Batch(id="b1", status="queued", model="m", input_count=1),
                Batch(id="b2", status="in_progress", model="m", input_count=1),
                Batch(id="b3", status="completed", model="m", input_count=1),
                Batch(id="b4", status="failed", model="m", input_count=1),
                Batch(id="b5", status="cancelled", model="m", input_count=1),
            ]
        )

    async with db.session_scope() as s:
        active = await db.list_active_batches(s)

    ids = {b.id for b in active}
    assert ids == {"b1", "b2"}


async def test_list_active_batches_empty_when_nothing_active(
    fresh_db: None,
) -> None:
    """Empty list when all batches are terminal or the table is empty."""
    from src import db  # noqa: PLC0415

    async with db.session_scope() as s:
        active = await db.list_active_batches(s)

    assert active == []
