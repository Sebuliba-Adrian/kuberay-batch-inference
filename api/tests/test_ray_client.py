"""
Red tests for src.ray_client — async wrapper around Ray's sync
JobSubmissionClient.

Tests never touch the real Ray package. A fake client is injected via
ray_client.set_client_factory() so the module under test is exercised
end-to-end without requiring `pip install ray`.
"""

from __future__ import annotations

from typing import Any

import pytest


# ─── FakeRayClient ──────────────────────────────────────────────────
class FakeRayClient:
    """Stand-in for ray.job_submission.JobSubmissionClient."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.submissions: list[dict[str, Any]] = []
        self.status_overrides: dict[str, str] = {}
        self.list_jobs_called = 0
        self.ping_should_fail = False
        self._next_id = 0

    def submit_job(
        self, *, entrypoint: str, runtime_env: dict[str, Any] | None = None
    ) -> str:
        self._next_id += 1
        sub_id = f"raysubmit_{self._next_id:04d}"
        self.submissions.append(
            {"id": sub_id, "entrypoint": entrypoint, "runtime_env": runtime_env}
        )
        return sub_id

    def get_job_status(self, submission_id: str) -> str:
        # Default to RUNNING; tests override specific ids.
        return self.status_overrides.get(submission_id, "RUNNING")

    def list_jobs(self) -> list[dict[str, Any]]:
        if self.ping_should_fail:
            raise ConnectionError("simulated Ray dashboard down")
        self.list_jobs_called += 1
        return []


# ─── Fixture: inject fake factory before every test ────────────────
@pytest.fixture
def fake_ray() -> FakeRayClient:
    """
    Replace the module's client factory with one that builds and
    captures a FakeRayClient instance. Yields the instance so tests
    can inspect submissions and set status overrides.
    """
    from src import ray_client  # noqa: PLC0415

    captured: list[FakeRayClient] = []

    def factory(address: str) -> FakeRayClient:
        c = FakeRayClient(address)
        captured.append(c)
        return c

    ray_client.reset()
    ray_client.set_client_factory(factory)
    ray_client.init("http://fake-ray:8265")
    assert captured, "factory was never invoked"

    yield captured[0]

    ray_client.reset()


# ─── Initialization / teardown ──────────────────────────────────────
async def test_ping_raises_before_init() -> None:
    """Calling ping() before init() must raise RuntimeError."""
    from src import ray_client  # noqa: PLC0415

    ray_client.reset()
    with pytest.raises(RuntimeError, match="not initialized"):
        await ray_client.ping()


async def test_submit_batch_raises_before_init() -> None:
    from src import ray_client  # noqa: PLC0415

    ray_client.reset()
    with pytest.raises(RuntimeError, match="not initialized"):
        await ray_client.submit_batch(entrypoint="python foo.py")


async def test_get_status_raises_before_init() -> None:
    from src import ray_client  # noqa: PLC0415

    ray_client.reset()
    with pytest.raises(RuntimeError, match="not initialized"):
        await ray_client.get_status("raysubmit_x")


def test_init_passes_address_to_factory() -> None:
    """init(address) must construct the client with that address."""
    from src import ray_client  # noqa: PLC0415

    ray_client.reset()
    captured: list[str] = []

    def factory(addr: str) -> FakeRayClient:
        captured.append(addr)
        return FakeRayClient(addr)

    ray_client.set_client_factory(factory)
    ray_client.init("http://ray-head.ns.svc.cluster.local:8265")

    assert captured == ["http://ray-head.ns.svc.cluster.local:8265"]
    ray_client.reset()


def test_default_factory_builds_real_ray_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When no factory override is installed, init() must fall back to the
    real ``ray.job_submission.JobSubmissionClient``.

    We can't install the real Ray package in the test venv (too heavy),
    so we inject a stub ``ray.job_submission`` module into ``sys.modules``
    that provides a fake JobSubmissionClient. That forces ``_default_factory``
    through its import line without actually talking to Ray.
    """
    import sys
    import types

    from src import ray_client  # noqa: PLC0415

    built: list[str] = []

    class _StubJobSubmissionClient:
        def __init__(self, address: str) -> None:
            built.append(address)
            self.address = address

    # Build a synthetic ``ray.job_submission`` module in sys.modules so
    # the ``from ray.job_submission import JobSubmissionClient`` line
    # inside _default_factory resolves to our stub. Also stub the
    # parent ``ray`` package to satisfy Python's import machinery.
    stub_parent = types.ModuleType("ray")
    stub_child = types.ModuleType("ray.job_submission")
    stub_child.JobSubmissionClient = _StubJobSubmissionClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ray", stub_parent)
    monkeypatch.setitem(sys.modules, "ray.job_submission", stub_child)

    ray_client.reset()  # no factory override — will hit _default_factory
    ray_client.init("http://ray-head:8265")

    assert built == ["http://ray-head:8265"]
    # Clean up the singleton for the next test
    ray_client.reset()


# ─── ping() ─────────────────────────────────────────────────────────
async def test_ping_calls_list_jobs_once(fake_ray: FakeRayClient) -> None:
    from src import ray_client  # noqa: PLC0415

    await ray_client.ping()
    assert fake_ray.list_jobs_called == 1


async def test_ping_propagates_connection_error(fake_ray: FakeRayClient) -> None:
    """A broken dashboard must surface the error to the caller."""
    from src import ray_client  # noqa: PLC0415

    fake_ray.ping_should_fail = True
    with pytest.raises(ConnectionError):
        await ray_client.ping()


# ─── submit_batch ───────────────────────────────────────────────────
async def test_submit_batch_returns_submission_id(fake_ray: FakeRayClient) -> None:
    from src import ray_client  # noqa: PLC0415

    sub_id = await ray_client.submit_batch(
        entrypoint="python /app/jobs/batch_infer.py --batch-id b1",
        runtime_env={"pip": []},
    )
    assert sub_id.startswith("raysubmit_")
    assert len(fake_ray.submissions) == 1
    assert fake_ray.submissions[0]["id"] == sub_id


async def test_submit_batch_forwards_entrypoint_and_env(
    fake_ray: FakeRayClient,
) -> None:
    from src import ray_client  # noqa: PLC0415

    await ray_client.submit_batch(
        entrypoint="python run.py",
        runtime_env={"env_vars": {"X": "1"}},
    )
    call = fake_ray.submissions[0]
    assert call["entrypoint"] == "python run.py"
    assert call["runtime_env"] == {"env_vars": {"X": "1"}}


async def test_submit_batch_accepts_none_runtime_env(
    fake_ray: FakeRayClient,
) -> None:
    """runtime_env is optional — None must be coerced to {} not crash."""
    from src import ray_client  # noqa: PLC0415

    await ray_client.submit_batch(entrypoint="python run.py")
    call = fake_ray.submissions[0]
    assert call["runtime_env"] == {}


# ─── Status mapping ─────────────────────────────────────────────────
def test_map_status_pending() -> None:
    from src.ray_client import map_status

    assert map_status("PENDING") == "queued"


def test_map_status_running() -> None:
    from src.ray_client import map_status

    assert map_status("RUNNING") == "in_progress"


def test_map_status_succeeded() -> None:
    from src.ray_client import map_status

    assert map_status("SUCCEEDED") == "completed"


def test_map_status_failed() -> None:
    from src.ray_client import map_status

    assert map_status("FAILED") == "failed"


def test_map_status_stopped() -> None:
    from src.ray_client import map_status

    assert map_status("STOPPED") == "cancelled"


def test_map_status_unknown_falls_back_to_failed() -> None:
    """An unknown Ray state is the safer default for observers — treat as failure."""
    from src.ray_client import map_status

    assert map_status("WEIRD_NEW_STATE") == "failed"


def test_map_status_accepts_enum_like_object() -> None:
    """Real ray JobStatus is an enum with a .value attribute — accept both forms."""
    from src.ray_client import map_status

    class FakeJobStatus:
        def __init__(self, v: str) -> None:
            self.value = v

    assert map_status(FakeJobStatus("SUCCEEDED")) == "completed"


def test_map_status_case_insensitive() -> None:
    """Ray usually returns uppercase, but be tolerant of lowercase strings."""
    from src.ray_client import map_status

    assert map_status("succeeded") == "completed"


# ─── get_status async ───────────────────────────────────────────────
async def test_get_status_returns_mapped_value(fake_ray: FakeRayClient) -> None:
    from src import ray_client  # noqa: PLC0415

    fake_ray.status_overrides["raysubmit_0001"] = "SUCCEEDED"
    result = await ray_client.get_status("raysubmit_0001")
    assert result == "completed"


async def test_get_status_running_maps_to_in_progress(
    fake_ray: FakeRayClient,
) -> None:
    from src import ray_client  # noqa: PLC0415

    fake_ray.status_overrides["raysubmit_0001"] = "RUNNING"
    assert await ray_client.get_status("raysubmit_0001") == "in_progress"
