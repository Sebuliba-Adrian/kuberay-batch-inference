"""
Liveness and readiness probe endpoints.

``/health`` is intentionally cheap - no DB, no Ray. Kubernetes uses
it as the livenessProbe: if the process is running and serving HTTP
we stay alive. A crash in Postgres or Ray does NOT cascade into
killing the API pod.

``/ready`` is the dependency check: it pings Postgres and the Ray
dashboard. Kubernetes uses it as the readinessProbe. A failing
/ready keeps the pod running but removes it from the Service, so
traffic only reaches healthy replicas.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Liveness probe",
    response_description="Always 200 if the process is alive",
)
async def health() -> dict[str, str]:
    """Return a static payload proving the process is serving HTTP."""
    return {"status": "ok"}


@router.get(
    "/ready",
    summary="Readiness probe",
    response_description="200 if all dependencies are up, 503 otherwise",
)
async def ready() -> Any:
    """
    Deep dependency check: Postgres reachable + Ray dashboard reachable.

    Imports are deferred to function-call time so this module stays
    side-effect free at import (matters for tests that monkeypatch
    db.ping or ray_client.ping before the first request).
    """
    from src import db, ray_client  # noqa: PLC0415

    checks: dict[str, str] = {}
    overall_ok = True

    # Postgres ping
    try:
        await db.ping()
        checks["postgres"] = "ok"
    except Exception as exc:
        log.warning("ready: postgres check failed: %s", exc)
        checks["postgres"] = f"error: {type(exc).__name__}"
        overall_ok = False

    # Ray dashboard ping
    try:
        await ray_client.ping()
        checks["ray"] = "ok"
    except Exception as exc:
        log.warning("ready: ray check failed: %s", exc)
        checks["ray"] = f"error: {type(exc).__name__}"
        overall_ok = False

    payload = {"status": "ok" if overall_ok else "degraded", "checks": checks}
    return JSONResponse(
        content=payload,
        status_code=(status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE),
    )
