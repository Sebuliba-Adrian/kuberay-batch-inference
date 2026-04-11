"""
Liveness probe endpoint.

`/health` is intentionally cheap: no DB call, no Ray call. It answers
"is this process alive and serving HTTP?" for Kubernetes livenessProbes
so a crashing dependency cannot cascade into killing the API pod.

A deeper /ready check (with DB + Ray dependency verification) lands in
a later TDD cycle.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Liveness probe",
    response_description="Always 200 if the process is alive",
)
async def health() -> dict[str, str]:
    """Return a static payload proving the process is serving HTTP."""
    return {"status": "ok"}
