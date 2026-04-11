"""
Red tests for Pydantic request/response models.

The request model validates what clients send; the response model
defines the OpenAI-shaped contract we return. Both are pure data so
the tests are straightforward Pydantic assertions with no I/O.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


# ─── CreateBatchRequest: happy path ─────────────────────────────────
def test_create_batch_request_minimal_valid() -> None:
    """Minimum viable payload: model name + one prompt."""
    from src.models import CreateBatchRequest

    req = CreateBatchRequest(
        model="Qwen/Qwen2.5-0.5B-Instruct",
        input=[{"prompt": "hello"}],
    )
    assert req.model == "Qwen/Qwen2.5-0.5B-Instruct"
    assert len(req.input) == 1
    assert req.input[0].prompt == "hello"
    # max_tokens has a default so the client doesn't have to supply it
    assert req.max_tokens == 256


def test_create_batch_request_full_payload() -> None:
    """Full payload with explicit max_tokens."""
    from src.models import CreateBatchRequest

    req = CreateBatchRequest(
        model="Qwen/Qwen2.5-0.5B-Instruct",
        input=[{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}],
        max_tokens=128,
    )
    assert req.max_tokens == 128
    assert [i.prompt for i in req.input] == ["a", "b", "c"]


# ─── CreateBatchRequest: input bounds ───────────────────────────────
def test_create_batch_request_rejects_empty_input() -> None:
    """A batch of zero prompts is meaningless."""
    from src.models import CreateBatchRequest

    with pytest.raises(ValidationError):
        CreateBatchRequest(model="foo", input=[])


def test_create_batch_request_rejects_empty_prompt() -> None:
    """A prompt string must be non-empty."""
    from src.models import CreateBatchRequest

    with pytest.raises(ValidationError):
        CreateBatchRequest(model="foo", input=[{"prompt": ""}])


def test_create_batch_request_rejects_prompt_too_long() -> None:
    """Upper bound protects downstream tokenizers from abuse."""
    from src.models import CreateBatchRequest

    huge = "x" * 32_001
    with pytest.raises(ValidationError):
        CreateBatchRequest(model="foo", input=[{"prompt": huge}])


def test_create_batch_request_rejects_too_many_prompts() -> None:
    """Upper bound on batch size prevents DOS."""
    from src.models import CreateBatchRequest

    too_many = [{"prompt": "x"}] * 100_001
    with pytest.raises(ValidationError):
        CreateBatchRequest(model="foo", input=too_many)


# ─── CreateBatchRequest: max_tokens bounds ──────────────────────────
def test_create_batch_request_rejects_zero_max_tokens() -> None:
    """max_tokens=0 generates nothing."""
    from src.models import CreateBatchRequest

    with pytest.raises(ValidationError):
        CreateBatchRequest(
            model="foo", input=[{"prompt": "x"}], max_tokens=0
        )


def test_create_batch_request_rejects_negative_max_tokens() -> None:
    from src.models import CreateBatchRequest

    with pytest.raises(ValidationError):
        CreateBatchRequest(
            model="foo", input=[{"prompt": "x"}], max_tokens=-1
        )


def test_create_batch_request_rejects_max_tokens_over_limit() -> None:
    """8192 is the upper bound — anything higher is rejected."""
    from src.models import CreateBatchRequest

    with pytest.raises(ValidationError):
        CreateBatchRequest(
            model="foo", input=[{"prompt": "x"}], max_tokens=10_000
        )


# ─── CreateBatchRequest: model name ─────────────────────────────────
def test_create_batch_request_rejects_model_with_spaces() -> None:
    """Model name is a repo id — spaces are invalid."""
    from src.models import CreateBatchRequest

    with pytest.raises(ValidationError):
        CreateBatchRequest(
            model="qwen 2.5 0.5b", input=[{"prompt": "x"}]
        )


def test_create_batch_request_accepts_huggingface_style_model() -> None:
    """HuggingFace repo ids use slash + dot + dash."""
    from src.models import CreateBatchRequest

    req = CreateBatchRequest(
        model="Qwen/Qwen2.5-0.5B-Instruct",
        input=[{"prompt": "x"}],
    )
    assert req.model == "Qwen/Qwen2.5-0.5B-Instruct"


# ─── CreateBatchRequest: unknown field handling ─────────────────────
def test_create_batch_request_forbids_unknown_fields() -> None:
    """extra='forbid' so client typos fail fast instead of silently dropping."""
    from src.models import CreateBatchRequest

    with pytest.raises(ValidationError):
        CreateBatchRequest(
            model="foo",
            input=[{"prompt": "x"}],
            temperature=0.7,  # not a recognized field
        )


def test_batch_input_item_forbids_unknown_fields() -> None:
    """Same strictness inside the nested BatchInputItem."""
    from src.models import CreateBatchRequest

    with pytest.raises(ValidationError):
        CreateBatchRequest(
            model="foo",
            input=[{"prompt": "x", "role": "user"}],  # role is not on the model
        )


# ─── BatchObject shape ──────────────────────────────────────────────
def test_batch_object_matches_openai_shape() -> None:
    """Response mirrors OpenAI's Batch object layout exactly."""
    from src.models import BatchObject, RequestCounts

    obj = BatchObject(
        id="batch_01JABCD",
        model="Qwen/Qwen2.5-0.5B-Instruct",
        status="queued",
        created_at=1_744_380_000,
        request_counts=RequestCounts(total=2, completed=0, failed=0),
    )

    dumped = obj.model_dump()
    assert dumped["id"] == "batch_01JABCD"
    assert dumped["object"] == "batch"
    assert dumped["endpoint"] == "/v1/batches"
    assert dumped["model"] == "Qwen/Qwen2.5-0.5B-Instruct"
    assert dumped["status"] == "queued"
    assert dumped["created_at"] == 1_744_380_000
    assert dumped["completed_at"] is None
    assert dumped["error"] is None
    assert dumped["request_counts"] == {"total": 2, "completed": 0, "failed": 0}


def test_batch_object_status_enum_is_openai_vocabulary() -> None:
    """Only the OpenAI-aligned status strings are allowed."""
    from src.models import BatchObject, RequestCounts

    counts = RequestCounts(total=0, completed=0, failed=0)
    for good in ("queued", "in_progress", "completed", "failed", "cancelled"):
        BatchObject(
            id="batch_x",
            model="m",
            status=good,  # type: ignore[arg-type]
            created_at=0,
            request_counts=counts,
        )

    with pytest.raises(ValidationError):
        BatchObject(
            id="batch_x",
            model="m",
            status="running",  # not in the allowed set  # type: ignore[arg-type]
            created_at=0,
            request_counts=counts,
        )


def test_batch_object_completed_at_is_optional() -> None:
    """completed_at is None until the batch reaches a terminal state."""
    from src.models import BatchObject, RequestCounts

    obj = BatchObject(
        id="batch_x",
        model="m",
        status="in_progress",
        created_at=0,
        request_counts=RequestCounts(total=1, completed=0, failed=0),
    )
    assert obj.completed_at is None


def test_batch_object_with_completed_at_set() -> None:
    """completed_at fills in when the batch finishes."""
    from src.models import BatchObject, RequestCounts

    obj = BatchObject(
        id="batch_x",
        model="m",
        status="completed",
        created_at=100,
        completed_at=200,
        request_counts=RequestCounts(total=1, completed=1, failed=0),
    )
    assert obj.completed_at == 200


def test_request_counts_rejects_negative_values() -> None:
    """Counts can't be negative — that's a bug waiting to happen."""
    from src.models import RequestCounts

    with pytest.raises(ValidationError):
        RequestCounts(total=-1, completed=0, failed=0)


# ─── TERMINAL_STATUSES export ───────────────────────────────────────
def test_terminal_statuses_is_frozen_set() -> None:
    """State machine helper for 'is this batch done?' checks."""
    from src.models import TERMINAL_STATUSES

    assert "completed" in TERMINAL_STATUSES
    assert "failed" in TERMINAL_STATUSES
    assert "cancelled" in TERMINAL_STATUSES
    assert "queued" not in TERMINAL_STATUSES
    assert "in_progress" not in TERMINAL_STATUSES
    # Frozen so nobody mutates it at runtime
    assert isinstance(TERMINAL_STATUSES, frozenset)
