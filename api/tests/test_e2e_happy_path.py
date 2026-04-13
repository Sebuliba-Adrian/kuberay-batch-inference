"""
End-to-end happy-path test: POST → poll → GET status → GET results.

This test exercises the entire request lifecycle the exercise PDF
cares about:

  1. Client POSTs a batch (the exact curl from the brief)
  2. Server persists it, writes inputs to the shared PVC, submits
     to a fake RayCluster
  3. Background status poller picks up the batch and advances it
     from queued → in_progress → completed as the fake Ray cluster
     reports state changes and writes a _SUCCESS marker
  4. Client polls GET /v1/batches/{id} and sees the status transition
  5. Client fetches GET /v1/batches/{id}/results and receives
     NDJSON with one result per input

Nothing is mocked at the handler level - the fake Ray is installed
via the public set_client_factory seam, the DB is a real
file-backed SQLite, the filesystem is a real tmp_path, and the
app is built via the production create_app factory.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


# ─── Controllable fake Ray cluster ─────────────────────────────────
class E2EFakeRay:
    """
    Simulates a Ray job that goes PENDING → RUNNING → SUCCEEDED,
    writing a _SUCCESS marker + a results.jsonl file when it
    reaches SUCCEEDED, mirroring what the real Ray worker would do.
    """

    def __init__(
        self,
        address: str,
        *,
        results_dir: Path | None = None,
    ) -> None:
        self.address = address
        self.results_dir = results_dir
        self._states: dict[str, list[str]] = {}
        self._next_sub = 0
        # Populated by submit_job: ray_job_id -> batch_id
        self._job_to_batch: dict[str, str] = {}

    def submit_job(self, *, entrypoint: str, runtime_env: Any = None) -> str:
        # Parse the batch id out of the entrypoint so we know where
        # to write the _SUCCESS marker + results.jsonl when the
        # "job" reaches SUCCEEDED.
        batch_id = _parse_batch_id(entrypoint)
        self._next_sub += 1
        sub_id = f"raysubmit_{self._next_sub:04d}"
        self._job_to_batch[sub_id] = batch_id
        # Seed a state machine: PENDING (1 call) -> RUNNING (1 call)
        # -> SUCCEEDED (sticky). The poller picks this up over 2-3
        # sweeps.
        self._states[sub_id] = ["PENDING", "RUNNING", "SUCCEEDED"]
        return sub_id

    def get_job_status(self, submission_id: str) -> str:
        states = self._states.get(submission_id, ["FAILED"])
        if len(states) > 1:
            current = states.pop(0)
        else:
            current = states[0]
        # When we hit SUCCEEDED for the first time, materialize the
        # worker's output artifacts so the poller + results endpoint
        # see a realistic shared-PVC layout.
        if current == "SUCCEEDED" and self.results_dir is not None:
            batch_id = self._job_to_batch.get(submission_id)
            if batch_id:
                self._write_success_artifacts(batch_id)
        return current

    def list_jobs(self) -> list[Any]:
        return []

    def _write_success_artifacts(self, batch_id: str) -> None:
        assert self.results_dir is not None
        batch_dir = self.results_dir / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)

        results_path = batch_dir / "results.jsonl"
        # Re-generate results on every SUCCEEDED call is fine - the
        # write is idempotent.
        input_path = batch_dir / "input.jsonl"
        rows: list[dict[str, Any]] = []
        if input_path.exists():
            with input_path.open("r", encoding="utf-8") as fh:
                for idx, line in enumerate(fh):
                    row = json.loads(line)
                    rows.append(
                        {
                            "id": row.get("id", str(idx)),
                            "prompt": row.get("prompt", ""),
                            "response": f"Fake response for: {row.get('prompt', '')[:40]}",
                            "finish_reason": "stop",
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "error": None,
                        }
                    )
        with results_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

        # _SUCCESS marker with counts
        (batch_dir / "_SUCCESS").write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "total": len(rows),
                    "completed": len(rows),
                    "failed": 0,
                    "model": "Qwen/Qwen2.5-0.5B-Instruct",
                    "finished_at": time.time(),
                }
            )
        )


def _parse_batch_id(entrypoint: str) -> str:
    # Entrypoint looks like:
    #   python /app/jobs/batch_infer.py --batch-id batch_01... --model m --max-tokens 50
    parts = entrypoint.split()
    try:
        idx = parts.index("--batch-id")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return "unknown"


# ─── Fixture: full app + lifespan + real poller ────────────────────
@pytest.fixture
async def e2e_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> AsyncIterator[tuple[Any, Path]]:
    """
    Bring up the whole service: DB, fake Ray (with results_dir so it
    materializes the worker's output), and a started background
    status poller.
    """
    db_file = tmp_path / "e2e.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("POSTGRES_URL", db_url)
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    from src import db, ray_client  # noqa: PLC0415
    from src.main import create_app  # noqa: PLC0415
    from src.routes.batches import (  # noqa: PLC0415
        start_status_poller,
        stop_status_poller,
    )

    await db.init_engine(db_url)
    await db.create_all()

    # Hook the results_dir into the Ray factory so the fake Ray can
    # write artifacts to the correct place.
    def factory(address: str) -> E2EFakeRay:
        return E2EFakeRay(address, results_dir=tmp_path)

    ray_client.reset()
    ray_client.set_client_factory(factory)
    ray_client.init("http://fake-ray:8265")

    app = create_app()

    # Real poller running with a tight interval so the E2E test
    # finishes quickly.
    poller_task = await start_status_poller(interval_seconds=0.05)

    try:
        yield app, tmp_path
    finally:
        await stop_status_poller(poller_task)
        await db.dispose()
        ray_client.reset()


# ─── The E2E flow ──────────────────────────────────────────────────
async def test_full_happy_path_post_poll_results(e2e_app: tuple[Any, Path], api_key: str) -> None:
    """
    Reproduces the exact curl from the exercise PDF and asserts the
    whole happy path works: 200 on POST with status=queued, then the
    poller advances the row to in_progress, then completed, then
    GET /results returns two NDJSON rows matching the input prompts.
    """
    app, _root = e2e_app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # ─── 1. Submit the exact exercise-PDF payload ──────────────
        r = await ac.post(
            "/v1/batches",
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "model": "Qwen/Qwen2.5-0.5B-Instruct",
                "input": [
                    {"prompt": "What is 2+2?"},
                    {"prompt": "Hello world"},
                ],
                "max_tokens": 50,
            },
        )
        assert r.status_code == 200, r.text
        submit_body = r.json()
        batch_id = submit_body["id"]
        assert submit_body["status"] == "queued"
        assert submit_body["request_counts"] == {
            "total": 2,
            "completed": 0,
            "failed": 0,
        }

        # ─── 2. Poll until completed (or time out after 3s) ─────────
        deadline = time.monotonic() + 3.0
        final_body: dict[str, Any] | None = None
        saw_non_queued = False
        while time.monotonic() < deadline:
            r = await ac.get(
                f"/v1/batches/{batch_id}",
                headers={"X-API-Key": api_key},
            )
            assert r.status_code == 200
            body = r.json()
            if body["status"] != "queued":
                saw_non_queued = True
            if body["status"] == "completed":
                final_body = body
                break
            if body["status"] in ("failed", "cancelled"):
                raise AssertionError(f"Batch ended in unexpected terminal state: {body}")
            await asyncio.sleep(0.05)

        assert final_body is not None, "batch never reached completed"
        assert saw_non_queued, "poller never advanced the row past queued"
        assert final_body["status"] == "completed"
        assert final_body["request_counts"]["completed"] == 2
        assert final_body["request_counts"]["failed"] == 0
        assert final_body["completed_at"] is not None

        # ─── 3. Fetch results as NDJSON ─────────────────────────────
        r = await ac.get(
            f"/v1/batches/{batch_id}/results",
            headers={"X-API-Key": api_key},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")

        lines = [line for line in r.text.splitlines() if line]
        assert len(lines) == 2

        parsed = [json.loads(line) for line in lines]
        prompts_seen = {row["prompt"] for row in parsed}
        assert prompts_seen == {"What is 2+2?", "Hello world"}
        for row in parsed:
            assert row["response"].startswith("Fake response for:")
            assert row["finish_reason"] == "stop"
            assert row["error"] is None


async def test_full_happy_path_two_concurrent_batches(
    e2e_app: tuple[Any, Path], api_key: str
) -> None:
    """
    Two POSTs in parallel must each get a unique id and independently
    advance to completed without interfering with each other.
    """
    app, _root = e2e_app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Submit both concurrently
        payload = {
            "model": "m",
            "input": [{"prompt": "one"}, {"prompt": "two"}],
            "max_tokens": 10,
        }
        a, b = await asyncio.gather(
            ac.post(
                "/v1/batches",
                headers={"X-API-Key": api_key},
                json=payload,
            ),
            ac.post(
                "/v1/batches",
                headers={"X-API-Key": api_key},
                json=payload,
            ),
        )
        assert a.status_code == 200
        assert b.status_code == 200
        id_a = a.json()["id"]
        id_b = b.json()["id"]
        assert id_a != id_b

        # Wait for both to complete
        deadline = time.monotonic() + 3.0
        done = set()
        while time.monotonic() < deadline and done != {id_a, id_b}:
            for bid in (id_a, id_b):
                if bid in done:
                    continue
                r = await ac.get(
                    f"/v1/batches/{bid}",
                    headers={"X-API-Key": api_key},
                )
                if r.json()["status"] == "completed":
                    done.add(bid)
            await asyncio.sleep(0.05)

        assert done == {id_a, id_b}, "one or both batches did not complete in time"
