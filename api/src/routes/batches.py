"""
/v1/batches routes.

This module owns the cross-cutting dance between auth, input
validation, database persistence, shared-PVC writes, and Ray job
submission. Each handler is intentionally thin — every piece of
business logic it calls lives in its own well-tested module.

Error translation policy:
    - 401: auth failure (handled by require_api_key dependency)
    - 422: Pydantic validation failure (handled automatically)
    - 404: batch not found
    - 409: results requested before batch reached `completed`
    - 503: Ray cluster unreachable (ConnectionError / RuntimeError)
    - 500: anything else (opaque, logged, do not leak internals)
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from ulid import ULID

from .. import db, ray_client, storage
from ..auth import require_api_key
from ..config import Settings, get_settings
from ..db import Batch
from ..models import TERMINAL_STATUSES, BatchObject, CreateBatchRequest, RequestCounts

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1",
    tags=["batches"],
    dependencies=[Depends(require_api_key)],
)


def _new_batch_id() -> str:
    """Generate a new `batch_<ulid>` identifier."""
    # ULID is 26 chars, lexicographically sortable by creation time,
    # and url-safe — no hyphens to escape, no collision risk.
    return f"batch_{ULID()!s}"


def _to_unix(dt: _dt.datetime) -> int:
    """
    Convert a SQLAlchemy datetime column to Unix seconds.

    SQLite's aiosqlite driver returns DateTime(timezone=True) values
    as NAIVE datetimes — it drops the tzinfo on the way back even
    though we inserted a tz-aware value. Python's default
    ``datetime.timestamp()`` treats a naive datetime as local time,
    which produces a wrong Unix value on any non-UTC host.

    We stored everything in UTC (see src/db.py), so if the value
    comes back naive we know it's UTC and can attach the tzinfo
    before computing the timestamp. Postgres returns tz-aware
    datetimes natively and this branch is a no-op there.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp())


def _batch_row_to_object(row: Batch) -> BatchObject:
    """Convert a DB row to the OpenAI-shaped response model."""
    return BatchObject(
        id=row.id,
        model=row.model,
        status=row.status,  # type: ignore[arg-type] — Literal is a subset
        created_at=_to_unix(row.created_at),
        completed_at=_to_unix(row.completed_at) if row.completed_at else None,
        request_counts=RequestCounts(
            total=row.input_count,
            completed=row.completed_count,
            failed=row.failed_count,
        ),
        error=row.error,
    )


# ─── POST /v1/batches ───────────────────────────────────────────────
@router.post(
    "/batches",
    response_model=BatchObject,
    summary="Submit a new batch inference job",
)
async def create_batch(
    request: CreateBatchRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> BatchObject:
    """
    Create a new batch: assign an id, materialize inputs to the shared
    PVC, persist a row, and submit a job to the Ray cluster.

    On Ray submission failure the row is left in status=failed with
    an error message so ops can trace the attempt even though the
    client received 503.
    """
    batch_id = _new_batch_id()
    log.info("create_batch: id=%s prompts=%d", batch_id, len(request.input))

    # 1. Write inputs to the shared PVC so Ray workers can read them.
    #    Done before the DB insert so if this fails the row is never
    #    created — nothing to clean up.
    input_items = [item.model_dump() for item in request.input]
    input_path = await storage.write_inputs_jsonl(
        Path(settings.RESULTS_DIR), batch_id, input_items
    )

    # 2. Persist the row in queued state. If the DB insert fails the
    #    storage file is orphaned on disk but that's just disk — the
    #    periodic cleanup job (out of scope) handles stragglers.
    async with db.session_scope() as session:
        row = Batch(
            id=batch_id,
            status="queued",
            model=request.model,
            input_count=len(request.input),
            max_tokens=request.max_tokens,
            input_path=str(input_path),
            results_path=str(Path(settings.RESULTS_DIR) / batch_id / "results.jsonl"),
        )
        session.add(row)

    # 3. Submit to Ray. This is the one call that can legitimately fail
    #    after we've committed a row, so we catch and update the row
    #    to status=failed rather than leaving it stuck in queued.
    entrypoint = (
        f"python /app/jobs/batch_infer.py "
        f"--batch-id {batch_id} "
        f"--model {request.model} "
        f"--max-tokens {request.max_tokens}"
    )
    try:
        ray_job_id = await ray_client.submit_batch(
            entrypoint=entrypoint,
            runtime_env={"env_vars": {"RESULTS_DIR": settings.RESULTS_DIR}},
        )
    except (ConnectionError, RuntimeError) as exc:
        log.warning("create_batch: ray submit failed for %s: %s", batch_id, exc)
        await _mark_failed(batch_id, f"Ray submission failed: {type(exc).__name__}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ray cluster unavailable",
        ) from None

    # 4. Update the row with the Ray submission id so the status poller
    #    can find it later.
    await _attach_ray_job_id(batch_id, ray_job_id)

    # 5. Build the response from a fresh read so created_at comes from
    #    the database clock, not the Python process clock.
    refreshed: Batch | None = None
    async with db.session_scope() as session:
        refreshed = await db.get_batch(session, batch_id)

    if refreshed is None:
        # This indicates a logic bug (row was just inserted). Fail loudly
        # with a 500 rather than a confusing KeyError downstream.
        log.error("create_batch: row vanished after insert: %s", batch_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error",
        )
    return _batch_row_to_object(refreshed)


# ─── GET /v1/batches/{batch_id} ─────────────────────────────────────
@router.get(
    "/batches/{batch_id}",
    response_model=BatchObject,
    summary="Get batch status and progress",
)
async def get_batch_status(batch_id: str) -> BatchObject:
    """
    Return the current state of a batch.

    Reads directly from Postgres — does NOT query Ray. The background
    status poller (separate TDD cycle) is responsible for keeping the
    row up to date. This makes the hot-path GET fast and resilient
    even when the Ray cluster is temporarily unreachable.
    """
    async with db.session_scope() as session:
        row = await db.get_batch(session, batch_id)

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch not found: {batch_id}",
        )
    return _batch_row_to_object(row)


# ─── GET /v1/batches/{batch_id}/results ─────────────────────────────
@router.get(
    "/batches/{batch_id}/results",
    summary="Stream the batch inference results as NDJSON",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"application/x-ndjson": {}},
            "description": "Batch results as newline-delimited JSON.",
        },
        404: {"description": "Batch not found"},
        409: {"description": "Batch exists but has not completed yet"},
    },
)
async def get_batch_results(
    batch_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    """
    Stream the contents of /data/batches/<id>/results.jsonl back to
    the caller one line at a time. Uses application/x-ndjson so
    clients can parse incrementally without loading the whole body
    into memory.

    Returns:
        200 with the file contents streamed as NDJSON.
        404 if the batch row does not exist.
        409 if the batch exists but status != "completed".
        500 if the status says completed but the file is missing.
    """
    # 1. Look up the batch row first — if it doesn't exist we owe the
    # caller a 404 before ever touching the filesystem.
    async with db.session_scope() as session:
        row = await db.get_batch(session, batch_id)

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch not found: {batch_id}",
        )

    # 2. Refuse to stream results for a batch that hasn't finished.
    # "completed" is the only state that has a valid results file;
    # failed / cancelled batches expose their error via the status
    # endpoint, not the results endpoint.
    if row.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Batch not complete (status={row.status})",
        )

    # 3. Double-check the file exists before returning a StreamingResponse;
    # if we let the generator fail lazily the HTTP status would already
    # be 200 and the client would see a mid-stream crash. Surface the
    # inconsistency as a 500 up-front instead.
    root = Path(settings.RESULTS_DIR)
    if not (storage.batch_dir(root, batch_id) / storage.RESULTS_FILENAME).exists():
        log.error(
            "get_batch_results: status=completed but results file missing for %s",
            batch_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Results file missing",
        )

    # 4. Wrap the async file iterator in a StreamingResponse. FastAPI
    # consumes the generator lazily, so memory stays flat regardless
    # of how many rows are in the file.
    return StreamingResponse(
        storage.iter_results_ndjson(root, batch_id),
        media_type="application/x-ndjson",
    )


# ─── Internal helpers ───────────────────────────────────────────────
async def _mark_failed(batch_id: str, error: str) -> None:
    """
    Update a batch row to status=failed with the given error.

    The defensive ``if row is None`` guard is there because this helper
    is called from the Ray-failure path AFTER a successful insert, so
    the row *should* always exist; if it doesn't, there's a logic bug
    we want visible rather than silently dropped.
    """
    async with db.session_scope() as session:
        row = await db.get_batch(session, batch_id)
        if row is None:
            log.error("_mark_failed: row not found: %s", batch_id)
            return
        row.status = "failed"
        row.error = error
        row.completed_at = _dt.datetime.now(_dt.timezone.utc)


async def _attach_ray_job_id(batch_id: str, ray_job_id: str) -> None:
    """Store the Ray submission id on an existing batch row."""
    async with db.session_scope() as session:
        row = await db.get_batch(session, batch_id)
        if row is None:
            log.error("_attach_ray_job_id: row not found: %s", batch_id)
            return
        row.ray_job_id = ray_job_id
