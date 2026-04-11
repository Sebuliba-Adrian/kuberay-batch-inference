"""
Async persistence layer for batch job metadata.

SQLAlchemy 2.0 typed ORM with asyncpg (or aiosqlite in tests). Engine
and sessionmaker are module-level singletons initialized by the app
lifespan; tests call init_engine() directly with an in-memory URL.

Design notes
------------
- init_engine / dispose make the module re-entrant: tests can reset
  state between runs without reimporting.
- session_scope() is the canonical way for route code to acquire a
  session; it auto-commits on success and rolls back on exception.
- ping() runs a trivial SELECT 1 so the /ready probe can distinguish
  "not initialized" from "initialized but Postgres is down".
- The schema mirrors k8s/postgres/init-configmap.yaml so tests and
  production operate on the same column contract.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

log = logging.getLogger(__name__)


# ─── ORM base + model ───────────────────────────────────────────────
class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base for all ORM models."""


class Batch(Base):
    """One row per batch submission."""

    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    ray_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=256, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    results_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Python-side default (not server_default) because SQLite's
    # CURRENT_TIMESTAMP returns a naive datetime that aiosqlite
    # surfaces without timezone info, breaking .timestamp() arithmetic
    # in handlers. `datetime.now(UTC)` works for both SQLite and
    # Postgres and round-trips cleanly as a tz-aware value.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ─── Engine + session singletons ────────────────────────────────────
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


async def init_engine(url: str) -> None:
    """Bind the module-level engine. Idempotent across multiple calls."""
    global _engine, _sessionmaker  # noqa: PLW0603
    # Dispose any previous engine first so tests can rebind repeatedly.
    if _engine is not None:
        await _engine.dispose()

    log.info("db: initializing engine")
    _engine = create_async_engine(url, pool_pre_ping=True, future=True)
    _sessionmaker = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


async def create_all() -> None:
    """Create tables if they don't already exist. Idempotent."""
    if _engine is None:
        raise RuntimeError("db.init_engine(...) must be called first")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("db: schema ensured")


async def dispose() -> None:
    """Close the engine on shutdown. Safe to call even if never initialized."""
    global _engine, _sessionmaker  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
        log.info("db: engine disposed")


async def ping() -> None:
    """
    Cheap health check for /ready. Runs SELECT 1 against the engine.
    Raises RuntimeError if the module wasn't initialized.
    """
    if _engine is None:
        raise RuntimeError("db not initialized")
    async with _engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """
    Yield a session that auto-commits on clean exit and rolls back on
    exception. Callers never have to worry about begin/commit/rollback.
    """
    if _sessionmaker is None:
        raise RuntimeError("db.init_engine(...) must be called first")
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ─── Query helpers ──────────────────────────────────────────────────
async def get_batch(session: AsyncSession, batch_id: str) -> Batch | None:
    """Load one batch by id. None when the row does not exist."""
    result = await session.execute(select(Batch).where(Batch.id == batch_id))
    return result.scalar_one_or_none()


async def list_active_batches(session: AsyncSession) -> list[Batch]:
    """Return batches that are not yet in a terminal state."""
    result = await session.execute(
        select(Batch).where(Batch.status.in_(("queued", "in_progress")))
    )
    return list(result.scalars().all())
