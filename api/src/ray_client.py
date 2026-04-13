"""
Async wrapper around Ray's synchronous ``JobSubmissionClient``.

Ray's Python SDK exposes a sync HTTP client against the Jobs REST API
on the Ray head dashboard (port 8265). Calling it directly from an
async FastAPI handler would block the event loop, so every method here
hops off the loop via ``starlette.concurrency.run_in_threadpool``.

The real ``ray`` package is only imported inside ``_default_factory``,
so tests can inject a fake client via ``set_client_factory`` without
installing Ray. The module is deliberately stateful (``_client`` is a
singleton) because routes need a single connection pool for the
lifetime of the process.

Status vocabulary mapping (Ray → OpenAI Batch):

    PENDING   → queued
    RUNNING   → in_progress
    SUCCEEDED → completed
    FAILED    → failed
    STOPPED   → cancelled

Any unknown value maps to ``failed`` - a safe fallback for a future
Ray version that adds a state we don't recognize yet.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

# ─── Module state ───────────────────────────────────────────────────
# Both singletons are typed as Any so this module never has to import
# the real ``ray`` package - tests would fail to collect if it did.
_client: Any = None
_client_factory: Callable[[str], Any] | None = None


def _default_factory(address: str) -> Any:
    """
    Build the real ``ray.job_submission.JobSubmissionClient``.

    Imported lazily so ``ray`` is only loaded if ``init()`` is actually
    called with the default factory. Production code hits this path;
    tests override the factory before calling ``init()``.
    """
    from ray.job_submission import JobSubmissionClient  # noqa: PLC0415

    return JobSubmissionClient(address)


# ─── Lifecycle ──────────────────────────────────────────────────────
def set_client_factory(factory: Callable[[str], Any]) -> None:
    """
    Test seam: install a factory that builds a fake ``JobSubmissionClient``
    instead of importing the real Ray package. Must be called BEFORE
    ``init()``.
    """
    global _client_factory  # noqa: PLW0603
    _client_factory = factory


def init(address: str) -> None:
    """Bind the module-level client. Called once by the FastAPI lifespan."""
    global _client  # noqa: PLW0603
    factory = _client_factory or _default_factory
    log.info("ray_client: initializing against %s", address)
    _client = factory(address)


def reset() -> None:
    """
    Clear the module's client and factory. Used by tests between runs;
    production code never calls this.
    """
    global _client, _client_factory  # noqa: PLW0603
    _client = None
    _client_factory = None


# ─── Status mapping ─────────────────────────────────────────────────
_STATUS_MAP: dict[str, str] = {
    "PENDING": "queued",
    "RUNNING": "in_progress",
    "SUCCEEDED": "completed",
    "FAILED": "failed",
    "STOPPED": "cancelled",
}


def map_status(ray_status: Any) -> str:
    """
    Convert a Ray ``JobStatus`` (enum or string) to our OpenAI-aligned
    ``BatchStatus`` vocabulary. Unknown states map to ``failed`` so an
    observer never gets stuck on a state it does not understand.
    """
    # Real ``ray.job_submission.JobStatus`` is an enum whose ``.value``
    # is the uppercase string. Accept both forms for ergonomics.
    raw = str(ray_status.value).upper() if hasattr(ray_status, "value") else str(ray_status).upper()
    return _STATUS_MAP.get(raw, "failed")


# ─── Operations ─────────────────────────────────────────────────────
async def ping() -> None:
    """
    Liveness check against the Ray dashboard. Issues ``list_jobs()``
    which is the cheapest authenticated call the SDK exposes.

    Raises ``RuntimeError`` if the module was never initialized, or
    whatever the underlying Ray client raises on connection failure.
    """
    if _client is None:
        raise RuntimeError("ray_client not initialized")
    await run_in_threadpool(_client.list_jobs)


async def submit_batch(
    entrypoint: str,
    runtime_env: dict[str, Any] | None = None,
) -> str:
    """
    Submit a job to the RayCluster and return its submission id.

    Parameters
    ----------
    entrypoint:
        Shell command to run on the Ray head, e.g.
        ``"python /app/jobs/batch_infer.py --batch-id b1"``.
    runtime_env:
        Optional Ray runtime_env dict (pip, env_vars, working_dir).
        ``None`` is coerced to an empty dict so the Ray SDK does not
        crash on ``NoneType.get``.
    """
    if _client is None:
        raise RuntimeError("ray_client not initialized")

    submission_id: str = await run_in_threadpool(
        _client.submit_job,
        entrypoint=entrypoint,
        runtime_env=runtime_env or {},
    )
    log.info("ray_client: submitted %s", submission_id)
    return submission_id


async def get_status(submission_id: str) -> str:
    """Return the current status mapped to the BatchStatus vocabulary."""
    if _client is None:
        raise RuntimeError("ray_client not initialized")

    raw = await run_in_threadpool(_client.get_job_status, submission_id)
    return map_status(raw)
