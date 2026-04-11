"""
FastAPI application factory.

Uvicorn runs this in production via

    uvicorn src.main:create_app --factory

The factory pattern (no module-level ``app`` singleton) keeps
``import src.main`` completely side-effect free, which is essential
for tests that want to build a fresh instance after setting
environment variables.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .config import get_settings
from .logging_config import configure_logging
from .routes.batches import router as batches_router
from .routes.health import router as health_router

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """
    Build and return a fresh FastAPI app.

    Reads Settings on every call so a misconfigured environment fails
    loudly at boot. Configures logging before the app is constructed so
    startup messages use the same format as runtime.
    """
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    log.info("Creating FastAPI app (log level=%s)", settings.LOG_LEVEL)

    app = FastAPI(
        title="KubeRay Batch Inference API",
        description=(
            "OpenAI-shaped batch inference proxy for Qwen2.5-0.5B served "
            "by a KubeRay cluster. All /v1/batches routes require the "
            "X-API-Key header; /health is public for liveness probes."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.include_router(health_router)
    app.include_router(batches_router)

    return app
