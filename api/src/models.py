"""
Pydantic v2 request and response models.

Request shape validates client input; response shape mirrors OpenAI's
Batch object so OpenAI SDK clients can talk to this API with only a
base URL swap. Status vocabulary maps Ray's JobStatus to OpenAI's:

  Ray PENDING   -> queued
  Ray RUNNING   -> in_progress
  Ray SUCCEEDED -> completed
  Ray FAILED    -> failed
  Ray STOPPED   -> cancelled

The mapping itself lives in src/ray_client.py; this module only
defines the schema.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# ─── Status literal ─────────────────────────────────────────────────
# Literal gives cheap runtime validation AND a finite enum in the
# generated OpenAPI schema without a separate enum class.
BatchStatus = Literal[
    "queued",
    "in_progress",
    "completed",
    "failed",
    "cancelled",
]

TERMINAL_STATUSES: frozenset[BatchStatus] = frozenset(
    {"completed", "failed", "cancelled"}
)


# ─── Request models ────────────────────────────────────────────────
class BatchInputItem(BaseModel):
    """One prompt inside a batch request."""

    model_config = ConfigDict(extra="forbid")

    prompt: Annotated[
        str,
        StringConstraints(min_length=1, max_length=32_000, strip_whitespace=False),
    ] = Field(description="Prompt text to send to the model.")


class CreateBatchRequest(BaseModel):
    """Body of POST /v1/batches."""

    model_config = ConfigDict(extra="forbid")

    model: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._\-/:]+$",
        ),
    ] = Field(
        description="HuggingFace repo id or local model name.",
        examples=["Qwen/Qwen2.5-0.5B-Instruct"],
    )

    input: list[BatchInputItem] = Field(
        min_length=1,
        max_length=100_000,
        description="List of prompts to process in this batch.",
    )

    max_tokens: int = Field(
        default=256,
        ge=1,
        le=8192,
        description="Max tokens to generate per prompt.",
    )


# ─── Response models ───────────────────────────────────────────────
class RequestCounts(BaseModel):
    """Progress counters, mirrors OpenAI Batch.request_counts."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0, description="Total prompts in the batch.")
    completed: int = Field(ge=0, description="Prompts that finished successfully.")
    failed: int = Field(ge=0, description="Prompts that errored during generation.")


class BatchObject(BaseModel):
    """
    Response shape for every /v1/batches endpoint. Field names match
    OpenAI's Batch object wherever possible.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Unique batch identifier, prefixed 'batch_'.")
    object: Literal["batch"] = Field(default="batch")
    endpoint: Literal["/v1/batches"] = Field(default="/v1/batches")
    model: str = Field(description="Model used for this batch.")
    status: BatchStatus = Field(description="Current lifecycle state.")
    created_at: int = Field(
        description="Unix timestamp (seconds) when the batch was created.",
    )
    completed_at: int | None = Field(
        default=None,
        description="Unix timestamp (seconds) when the batch reached a terminal state.",
    )
    request_counts: RequestCounts = Field(
        description="Progress counters for prompts in the batch.",
    )
    error: str | None = Field(
        default=None,
        description="Human-readable error string if the batch failed.",
    )
