"""
Red tests for src.storage — shared PVC JSONL helpers.

The storage module owns every filesystem interaction the API layer
performs against the shared /data/batches tree. Tests use tmp_path
so each test writes to its own ephemeral directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ─── batch_dir: path helper ─────────────────────────────────────────
def test_batch_dir_returns_subpath(tmp_path: Path) -> None:
    """batch_dir(root, id) == root / id."""
    from src.storage import batch_dir

    d = batch_dir(tmp_path, "batch_01ABC")
    assert d == tmp_path / "batch_01ABC"


def test_batch_dir_does_not_create_directory(tmp_path: Path) -> None:
    """batch_dir() is pure — it never touches the filesystem."""
    from src.storage import batch_dir

    d = batch_dir(tmp_path, "batch_01ABC")
    assert not d.exists()


# ─── write_inputs_jsonl ─────────────────────────────────────────────
async def test_write_inputs_jsonl_creates_file(tmp_path: Path) -> None:
    """Writes one JSON line per input item, creates the batch dir."""
    from src.storage import write_inputs_jsonl

    items = [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}]
    path = await write_inputs_jsonl(tmp_path, "batch_x", items)

    assert path == tmp_path / "batch_x" / "input.jsonl"
    assert path.exists()


async def test_write_inputs_jsonl_content_is_valid_ndjson(tmp_path: Path) -> None:
    """Each line must parse as independent JSON."""
    from src.storage import write_inputs_jsonl

    items = [
        {"prompt": "line one"},
        {"prompt": "line two"},
        {"prompt": "line three"},
    ]
    path = await write_inputs_jsonl(tmp_path, "batch_y", items)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["prompt"] == "line one"
    assert parsed[1]["prompt"] == "line two"
    assert parsed[2]["prompt"] == "line three"


async def test_write_inputs_jsonl_assigns_stable_ids(tmp_path: Path) -> None:
    """
    Each prompt gets a stable integer id (as a string) so the inference
    worker can preserve order when sharding across actors.
    """
    from src.storage import write_inputs_jsonl

    items = [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]
    path = await write_inputs_jsonl(tmp_path, "batch_z", items)

    parsed = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["id"] for row in parsed] == ["0", "1", "2"]


async def test_write_inputs_jsonl_creates_parent_directory(tmp_path: Path) -> None:
    """The batch subdirectory must be created automatically."""
    from src.storage import write_inputs_jsonl

    items = [{"prompt": "x"}]
    path = await write_inputs_jsonl(tmp_path, "batch_new", items)

    assert path.parent.exists()
    assert path.parent.is_dir()


async def test_write_inputs_jsonl_rejects_empty_list(tmp_path: Path) -> None:
    """Empty input is a programmer bug — the API should have caught it."""
    from src.storage import write_inputs_jsonl

    with pytest.raises(ValueError, match="at least one"):
        await write_inputs_jsonl(tmp_path, "batch_empty", [])


# ─── iter_results_ndjson ────────────────────────────────────────────
async def test_iter_results_ndjson_streams_lines(tmp_path: Path) -> None:
    """Async generator yields each line of results.jsonl exactly once."""
    from src.storage import iter_results_ndjson

    batch = tmp_path / "batch_r"
    batch.mkdir()
    results = batch / "results.jsonl"
    results.write_text(
        '{"id":"0","response":"hi"}\n'
        '{"id":"1","response":"there"}\n',
        encoding="utf-8",
    )

    lines: list[str] = []
    async for line in iter_results_ndjson(tmp_path, "batch_r"):
        lines.append(line)

    assert len(lines) == 2
    assert '"id":"0"' in lines[0]
    assert '"id":"1"' in lines[1]


async def test_iter_results_ndjson_trailing_newlines_preserved(
    tmp_path: Path,
) -> None:
    """Each yielded chunk must end with \\n so clients can split cleanly."""
    from src.storage import iter_results_ndjson

    batch = tmp_path / "batch_t"
    batch.mkdir()
    (batch / "results.jsonl").write_text('{"x":1}\n{"x":2}\n', encoding="utf-8")

    chunks = [line async for line in iter_results_ndjson(tmp_path, "batch_t")]
    assert all(chunk.endswith("\n") for chunk in chunks)


async def test_iter_results_ndjson_missing_file_raises(tmp_path: Path) -> None:
    """Asking for results that don't exist must raise FileNotFoundError."""
    from src.storage import iter_results_ndjson

    with pytest.raises(FileNotFoundError):
        async for _ in iter_results_ndjson(tmp_path, "batch_missing"):
            pass


# ─── Success / failure markers ──────────────────────────────────────
def test_is_success_false_when_no_marker(tmp_path: Path) -> None:
    from src.storage import is_success

    (tmp_path / "batch_m").mkdir()
    assert is_success(tmp_path, "batch_m") is False


def test_is_success_true_when_marker_exists(tmp_path: Path) -> None:
    from src.storage import is_success

    batch = tmp_path / "batch_m"
    batch.mkdir()
    (batch / "_SUCCESS").write_text("{}")

    assert is_success(tmp_path, "batch_m") is True


def test_is_failed_false_when_no_marker(tmp_path: Path) -> None:
    from src.storage import is_failed

    (tmp_path / "batch_m").mkdir()
    assert is_failed(tmp_path, "batch_m") is False


def test_is_failed_true_when_marker_exists(tmp_path: Path) -> None:
    from src.storage import is_failed

    batch = tmp_path / "batch_m"
    batch.mkdir()
    (batch / "_FAILED").write_text("{}")

    assert is_failed(tmp_path, "batch_m") is True


# ─── read_success_marker / read_failure_marker ─────────────────────
def test_read_success_marker_returns_counts(tmp_path: Path) -> None:
    """_SUCCESS is a JSON payload with total / completed / failed counts."""
    from src.storage import read_success_marker

    batch = tmp_path / "batch_s"
    batch.mkdir()
    (batch / "_SUCCESS").write_text(
        json.dumps(
            {
                "batch_id": "batch_s",
                "total": 10,
                "completed": 9,
                "failed": 1,
                "model": "m",
                "finished_at": 123.0,
            }
        )
    )

    info = read_success_marker(tmp_path, "batch_s")
    assert info is not None
    assert info["total"] == 10
    assert info["completed"] == 9
    assert info["failed"] == 1


def test_read_success_marker_none_when_absent(tmp_path: Path) -> None:
    """None (not raise) when the marker does not exist yet."""
    from src.storage import read_success_marker

    (tmp_path / "batch_s").mkdir()
    assert read_success_marker(tmp_path, "batch_s") is None


def test_read_failure_marker_returns_error(tmp_path: Path) -> None:
    """_FAILED is a JSON payload with an error string."""
    from src.storage import read_failure_marker

    batch = tmp_path / "batch_f"
    batch.mkdir()
    (batch / "_FAILED").write_text(
        json.dumps(
            {"batch_id": "batch_f", "error": "boom", "failed_at": 123.0}
        )
    )

    info = read_failure_marker(tmp_path, "batch_f")
    assert info is not None
    assert info["error"] == "boom"


def test_read_failure_marker_none_when_absent(tmp_path: Path) -> None:
    from src.storage import read_failure_marker

    (tmp_path / "batch_f").mkdir()
    assert read_failure_marker(tmp_path, "batch_f") is None
