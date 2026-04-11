"""
Runtime configuration for the batch API.

Single source of truth for every env-var-driven setting the service
reads. Parsed exactly once per process via @lru_cache, validated by
pydantic-settings v2 at startup so misconfiguration fails fast instead
of blowing up at the first request.

SecretStr hides API_KEY from logs, exception reprs, and repr() output —
never downgrade it to a plain str without a deliberate review.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Values come from env vars (or .env in dev)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── Auth ──────────────────────────────────────────────────────
    API_KEY: SecretStr = Field(
        ...,
        description="Static API key required on the X-API-Key header for every request.",
    )

    # ─── Logging ───────────────────────────────────────────────────
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Python logging level.",
        pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
    )

    # ─── Ray cluster ───────────────────────────────────────────────
    RAY_ADDRESS: str = Field(
        default="http://localhost:8265",
        description=(
            "Ray dashboard + Jobs API base URL. Inside the k8s cluster this "
            "is typically http://<raycluster>-head-svc.<ns>.svc.cluster.local:8265"
        ),
    )

    MODEL_NAME: str = Field(
        default="Qwen/Qwen2.5-0.5B-Instruct",
        description="Default model identifier returned in API responses.",
    )

    MAX_BATCH_SIZE: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description="Hard upper bound on prompts per batch request.",
    )

    # ─── PostgreSQL ────────────────────────────────────────────────
    POSTGRES_URL: str = Field(
        default="postgresql+asyncpg://batches:batches@localhost:5432/batches",
        description="SQLAlchemy async URL for the job metadata database.",
    )

    # ─── Shared results storage ────────────────────────────────────
    RESULTS_DIR: str = Field(
        default="/data/batches",
        description=(
            "Root directory on the shared PVC where batch inputs and results "
            "are stored. Each batch lives under RESULTS_DIR/<batch_id>/."
        ),
    )

    # ─── Validators ────────────────────────────────────────────────
    @field_validator("RAY_ADDRESS")
    @classmethod
    def _ray_address_must_be_http(cls, v: str) -> str:
        # We speak the Ray Jobs HTTP API (port 8265), not the gRPC
        # client protocol (port 10001), so ray:// / tcp:// are wrong.
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"RAY_ADDRESS must start with http:// or https://, got {v!r}"
            )
        return v.rstrip("/")

    @field_validator("POSTGRES_URL")
    @classmethod
    def _postgres_url_must_be_async(cls, v: str) -> str:
        # The whole codebase uses SQLAlchemy async — a sync driver would
        # deadlock the event loop the moment it's used.
        if not (
            v.startswith("postgresql+asyncpg://")
            or v.startswith("sqlite+aiosqlite://")
        ):
            raise ValueError(
                "POSTGRES_URL must use an async driver "
                "(postgresql+asyncpg:// or sqlite+aiosqlite://), "
                f"got {v!r}"
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide Settings singleton. Parsed exactly once."""
    return Settings()  # type: ignore[call-arg]
