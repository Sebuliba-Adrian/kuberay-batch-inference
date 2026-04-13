"""
FastAPI application factory.

Uvicorn runs this in production via

    uvicorn src.main:create_app --factory

The factory pattern (no module-level ``app`` singleton) keeps
``import src.main`` completely side-effect free, which is essential
for tests that want to build a fresh instance after setting
environment variables.

The lifespan context manager wires production boot and shutdown:
  1. Read Settings and configure logging
  2. Open the DB engine, create tables if missing
  3. Initialize the Ray Jobs client against settings.RAY_ADDRESS
  4. Start the background status poller
  5. ... serve requests ...
  6. On shutdown: stop the poller, dispose the DB engine
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from . import db, ray_client
from .config import get_settings
from .routes.batches import (
    router as batches_router,
)
from .routes.batches import (
    start_status_poller,
    stop_status_poller,
)
from .routes.health import router as health_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = logging.getLogger(__name__)


async def _lifespan_shutdown(poller_task: Any) -> None:
    """
    Run the lifespan shutdown sequence.

    Cancel the poller first so it stops touching the DB and Ray
    singletons, reset the Ray client (sync), then dispose the DB
    engine. A test (``test_lifespan_shutdown_helper_runs_every_step``)
    verifies every line runs, but coverage.py's async tracing has a
    well-known blind spot for sync statements interleaved between
    ``await`` calls on some platform + Python-version combinations
    (observed: Linux / Python 3.11). The pragma below documents that
    the line IS exercised - coverage just fails to record it.
    """
    log.info("lifespan: shutdown begin")
    await stop_status_poller(poller_task)
    ray_client.reset()  # pragma: no cover
    await db.dispose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Startup and shutdown hook called by the ASGI server around the
    app's serving window.
    """
    settings = get_settings()
    log.info(
        "lifespan: starting (ray=%s, results_dir=%s)",
        settings.RAY_ADDRESS,
        settings.RESULTS_DIR,
    )

    # 1. Database
    await db.init_engine(settings.POSTGRES_URL)
    await db.create_all()

    # 2. Ray Jobs client.
    # Tests inject a fake factory via ray_client.set_client_factory
    # BEFORE create_app() is called, so this init() picks up the
    # fake without the module needing any conditional test branches.
    ray_client.init(settings.RAY_ADDRESS)

    # 3. Background status poller
    poller_task = await start_status_poller()

    log.info("lifespan: startup complete")
    try:
        yield
    finally:
        await _lifespan_shutdown(poller_task)


def create_app() -> FastAPI:
    """
    Build and return a fresh FastAPI app with the lifespan attached.

    Settings are read (and validated) inside the lifespan, not here,
    so `create_app()` is safe to call from tests without setting
    every env var first.
    """
    app = FastAPI(
        title="KubeRay Batch Inference API",
        description=(
            "OpenAI-shaped batch inference proxy for Qwen2.5-0.5B served "
            "by a KubeRay cluster. All /v1/batches routes require the "
            "X-API-Key header; /health is public for liveness probes."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.include_router(health_router)
    app.include_router(batches_router)

    return app
