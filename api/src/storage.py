"""
Shared-PVC filesystem helpers.

Every filesystem interaction the API layer performs against the
`/data/batches/<id>/...` tree lives here so routes stay free of
pathlib details. The same tree is also written to by the Ray workers
inside the cluster — ordering contracts are documented in
docs/ARCHITECTURE.md.

Layout per batch:
    <root>/<batch_id>/
        input.jsonl     — written by the API before submitting the job
        results.jsonl   — written by the Ray worker when generation ends
        _SUCCESS        — marker JSON written last on clean completion
        _FAILED         — marker JSON written on top-level crash

Functions are written async where they do actual I/O (write_inputs,
iter_results) and sync where they're cheap path ops (is_success,
read_success_marker, batch_dir).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import aiofiles

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from pathlib import Path

INPUT_FILENAME = "input.jsonl"
RESULTS_FILENAME = "results.jsonl"
SUCCESS_MARKER = "_SUCCESS"
FAILURE_MARKER = "_FAILED"


# ─── Path helpers ───────────────────────────────────────────────────
def batch_dir(root: Path, batch_id: str) -> Path:
    """Return the per-batch directory path. Does NOT create it."""
    return root / batch_id


# ─── Input writing ──────────────────────────────────────────────────
async def write_inputs_jsonl(
    root: Path,
    batch_id: str,
    items: Iterable[dict[str, Any]],
) -> Path:
    """
    Write the input prompts to <root>/<batch_id>/input.jsonl.

    Each input item gets a monotonically-increasing integer id (as a
    string) so downstream workers can preserve order even when Ray
    redistributes blocks across actors.

    Returns the absolute path to the written file.
    """
    items = list(items)  # materialize — we need a length check
    if not items:
        raise ValueError("write_inputs_jsonl requires at least one item")

    target_dir = batch_dir(root, batch_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / INPUT_FILENAME

    # Use aiofiles to keep the event loop unblocked when writing
    # thousands of prompts.
    async with aiofiles.open(path, mode="w", encoding="utf-8") as fh:
        for idx, item in enumerate(items):
            row = {"id": str(idx), **item}
            await fh.write(json.dumps(row) + "\n")

    return path


# ─── Results streaming ──────────────────────────────────────────────
async def iter_results_ndjson(
    root: Path,
    batch_id: str,
) -> AsyncIterator[str]:
    """
    Yield each line of <root>/<batch_id>/results.jsonl one at a time.

    Used by the GET /v1/batches/{id}/results StreamingResponse so
    memory stays flat regardless of result set size.
    """
    path = batch_dir(root, batch_id) / RESULTS_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"Results file not found: {path}")

    async with aiofiles.open(path, encoding="utf-8") as fh:
        async for line in fh:
            yield line


# ─── Marker helpers ─────────────────────────────────────────────────
def is_success(root: Path, batch_id: str) -> bool:
    """True if the Ray worker wrote _SUCCESS for this batch."""
    return (batch_dir(root, batch_id) / SUCCESS_MARKER).exists()


def is_failed(root: Path, batch_id: str) -> bool:
    """True if the Ray worker wrote _FAILED for this batch."""
    return (batch_dir(root, batch_id) / FAILURE_MARKER).exists()


def read_success_marker(root: Path, batch_id: str) -> dict[str, Any] | None:
    """
    Parse the _SUCCESS marker and return its JSON payload.
    Returns None if the marker is absent; raises on malformed JSON.
    """
    path = batch_dir(root, batch_id) / SUCCESS_MARKER
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def read_failure_marker(root: Path, batch_id: str) -> dict[str, Any] | None:
    """Parse the _FAILED marker and return its JSON payload (or None)."""
    path = batch_dir(root, batch_id) / FAILURE_MARKER
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
