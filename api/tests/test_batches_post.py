"""
Red tests for POST /v1/batches.

This is the first cross-module integration test - it exercises auth,
models, db, storage, and ray_client all wired through the FastAPI
handler. All external dependencies are faked (in-memory SQLite, tmp
filesystem, FakeRayClient), but the request goes through the full
FastAPI middleware + dependency graph via httpx.ASGITransport.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


# ─── Fake Ray client ────────────────────────────────────────────────
class FakeRayClient:
    """Stand-in so tests never import the real ray package."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.submissions: list[dict[str, Any]] = []
        self.should_fail = False
        self._next_id = 0
        self.status = "PENDING"

    def submit_job(self, *, entrypoint: str, runtime_env: dict[str, Any] | None = None) -> str:
        if self.should_fail:
            raise ConnectionError("ray dashboard unreachable")
        self._next_id += 1
        sub_id = f"raysubmit_{self._next_id:04d}"
        self.submissions.append(
            {"id": sub_id, "entrypoint": entrypoint, "runtime_env": runtime_env}
        )
        return sub_id

    def get_job_status(self, submission_id: str) -> str:
        return self.status

    def list_jobs(self) -> list[dict[str, Any]]:
        return []


# ─── Shared test app fixture ────────────────────────────────────────
@pytest.fixture
async def app_and_deps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_key: str,
) -> AsyncIterator[tuple[Any, FakeRayClient, Path]]:
    """
    Bring up the full FastAPI app wired to fakes:
      - in-memory SQLite DB
      - tmp_path as RESULTS_DIR
      - FakeRayClient injected via set_client_factory
    """
    # 1. Env vars BEFORE create_app() reads Settings
    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "sqlite+aiosqlite:///file:batches_test?mode=memory&cache=shared&uri=true",
    )
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    # 2. Lazy imports AFTER env is set so Settings parses fresh
    from src import db, ray_client  # noqa: PLC0415
    from src.main import create_app  # noqa: PLC0415

    # 3. Initialize the DB
    await db.init_engine("sqlite+aiosqlite:///file:batches_test?mode=memory&cache=shared&uri=true")
    await db.create_all()

    # 4. Inject the fake Ray client
    captured: list[FakeRayClient] = []

    def factory(address: str) -> FakeRayClient:
        c = FakeRayClient(address)
        captured.append(c)
        return c

    ray_client.reset()
    ray_client.set_client_factory(factory)
    ray_client.init("http://fake-ray:8265")
    assert captured
    fake = captured[0]

    # 5. Build the app
    app = create_app()

    yield app, fake, tmp_path

    # 6. Teardown: wipe DB, reset ray client
    from sqlalchemy import text  # noqa: PLC0415

    async with db.session_scope() as s:
        await s.execute(text("DELETE FROM batches"))
    await db.dispose()
    ray_client.reset()


async def _post_batch(
    app: Any,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/v1/batches", headers=headers or {}, json=json_body or {})
    return r.status_code, r.json() if r.content else {}


# ─── Auth ───────────────────────────────────────────────────────────
async def test_post_batch_rejects_missing_api_key(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    app, _fake, _root = app_and_deps
    status, body = await _post_batch(
        app,
        headers={"Content-Type": "application/json"},
        json_body={
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "input": [{"prompt": "hi"}],
            "max_tokens": 10,
        },
    )
    assert status == 401
    assert body.get("detail") == "Missing API key"


async def test_post_batch_rejects_wrong_api_key(
    app_and_deps: tuple[Any, FakeRayClient, Path],
) -> None:
    app, _fake, _root = app_and_deps
    status, body = await _post_batch(
        app,
        headers={"X-API-Key": "wrong"},
        json_body={
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "input": [{"prompt": "hi"}],
            "max_tokens": 10,
        },
    )
    assert status == 401
    assert body.get("detail") == "Invalid API key"


# ─── Validation ─────────────────────────────────────────────────────
async def test_post_batch_rejects_empty_input(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    app, _fake, _root = app_and_deps
    status, _body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={"model": "m", "input": [], "max_tokens": 10},
    )
    assert status == 422


async def test_post_batch_rejects_missing_model(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    app, _fake, _root = app_and_deps
    status, _body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={"input": [{"prompt": "hi"}]},
    )
    assert status == 422


async def test_post_batch_rejects_unknown_field(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    app, _fake, _root = app_and_deps
    status, _body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={
            "model": "m",
            "input": [{"prompt": "hi"}],
            "temperature": 0.5,  # not on the schema
        },
    )
    assert status == 422


# ─── Happy path ─────────────────────────────────────────────────────
async def test_post_batch_happy_path_returns_200(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    """The exact curl from the exercise PDF."""
    app, _fake, _root = app_and_deps
    status, body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "input": [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}],
            "max_tokens": 50,
        },
    )

    assert status == 200
    assert body["object"] == "batch"
    assert body["endpoint"] == "/v1/batches"
    assert body["status"] == "queued"
    assert body["model"] == "Qwen/Qwen2.5-0.5B-Instruct"
    assert body["request_counts"] == {"total": 2, "completed": 0, "failed": 0}


async def test_post_batch_id_has_batch_prefix(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    app, _fake, _root = app_and_deps
    _status, body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={
            "model": "m",
            "input": [{"prompt": "x"}],
            "max_tokens": 10,
        },
    )
    assert body["id"].startswith("batch_")
    assert len(body["id"]) > len("batch_")


async def test_post_batch_created_at_is_unix_seconds(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    """created_at must be an int within a sane window around 'now'."""
    import time

    app, _fake, _root = app_and_deps
    before = int(time.time()) - 2
    _status, body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={"model": "m", "input": [{"prompt": "x"}], "max_tokens": 10},
    )
    after = int(time.time()) + 2

    assert isinstance(body["created_at"], int)
    assert before <= body["created_at"] <= after


async def test_post_batch_writes_input_jsonl_to_shared_storage(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    """The handler must materialize the inputs onto the shared PVC."""
    app, _fake, root = app_and_deps
    _status, body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={
            "model": "m",
            "input": [{"prompt": "aaa"}, {"prompt": "bbb"}],
            "max_tokens": 10,
        },
    )
    batch_id = body["id"]
    input_path = root / batch_id / "input.jsonl"
    assert input_path.exists()

    lines = input_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["prompt"] == "aaa"
    assert json.loads(lines[1])["prompt"] == "bbb"


async def test_post_batch_submits_to_ray(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    """FakeRayClient.submit_job must be called exactly once with the batch id."""
    app, fake, _root = app_and_deps
    _status, body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "input": [{"prompt": "x"}],
            "max_tokens": 50,
        },
    )

    assert len(fake.submissions) == 1
    submission = fake.submissions[0]
    assert body["id"] in submission["entrypoint"]
    assert "batch_infer.py" in submission["entrypoint"]
    assert "--batch-id" in submission["entrypoint"]
    assert "--max-tokens" in submission["entrypoint"]


async def test_post_batch_persists_row_with_ray_job_id(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    """After a successful submit the DB row stores the ray submission id."""
    app, _fake, _root = app_and_deps
    _status, body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={"model": "m", "input": [{"prompt": "x"}], "max_tokens": 10},
    )
    batch_id = body["id"]

    from src import db  # noqa: PLC0415

    async with db.session_scope() as s:
        row = await db.get_batch(s, batch_id)

    assert row is not None
    assert row.status == "queued"
    assert row.model == "m"
    assert row.input_count == 1
    assert row.max_tokens == 10
    assert row.ray_job_id is not None
    assert row.ray_job_id.startswith("raysubmit_")
    assert row.input_path is not None


# ─── Ray unavailable ────────────────────────────────────────────────
async def test_post_batch_returns_503_when_ray_down(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    """A ConnectionError from Ray must surface as 503, not 500."""
    app, fake, _root = app_and_deps
    fake.should_fail = True

    status, body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={"model": "m", "input": [{"prompt": "x"}], "max_tokens": 10},
    )

    assert status == 503
    # Should not leak the stack trace or the cluster URL
    assert "fake-ray" not in json.dumps(body)


async def test_post_batch_row_is_failed_when_ray_down(
    app_and_deps: tuple[Any, FakeRayClient, Path], api_key: str
) -> None:
    """When Ray errors, the DB row must be left in status=failed with an error."""
    app, fake, _root = app_and_deps
    fake.should_fail = True

    _status, _body = await _post_batch(
        app,
        headers={"X-API-Key": api_key},
        json_body={"model": "m", "input": [{"prompt": "x"}], "max_tokens": 10},
    )

    from sqlalchemy import select  # noqa: PLC0415
    from src import db  # noqa: PLC0415
    from src.db import Batch  # noqa: PLC0415

    async with db.session_scope() as s:
        rows = list((await s.execute(select(Batch))).scalars().all())

    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error is not None
