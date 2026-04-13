"""
KubeRay Batch Inference - single-file variant.

This module is a deliberate demonstration that the entire FastAPI
control plane fits in one file. The production layout at
``api/src/{config,auth,models,db,storage,ray_client,observability,
logging_config,main,routes/*}.py`` is organizational only: nothing
in the split depends on module boundaries. Every class, function,
and module-level singleton below is the same code, just pasted into
one file under section banners.

Why the split exists in the canonical codebase:
  - Faster pytest import graph (tests for auth don't pull FastAPI).
  - Per-concern coverage output in CI (one file per reviewed layer).
  - Cheaper PR diffs when one layer changes.
  - Easier side-effect-free imports in tests (e.g. monkeypatching
    ``db.ping`` before the first request).

None of those reasons are architectural. Collapsing into one file is
a zero-functionality-change refactor, which is what this branch
demonstrates.

Uvicorn entry point (unchanged from the split version):

    uvicorn src.app:create_app --factory

The factory pattern preserves test ergonomics: ``import src.app``
has no side effects until ``create_app()`` is called.
"""

from __future__ import annotations

# ──────────────────────────── Imports ───────────────────────────────
import asyncio
import contextlib
import datetime as _dt
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import aiofiles
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Response,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import DateTime, Integer, String, Text, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from ulid import ULID

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable

    from starlette.requests import Request

log = logging.getLogger(__name__)


# ═════════════════════════════ CONFIG ════════════════════════════════
# Every env-var-driven setting in one Pydantic Settings class, parsed
# once via @lru_cache. SecretStr hides API_KEY from logs and reprs.


class Settings(BaseSettings):
    """All runtime configuration. Values come from env vars (or .env in dev)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    API_KEY: SecretStr = Field(
        ...,
        description="Static API key required on the X-API-Key header for every request.",
    )

    LOG_LEVEL: str = Field(
        default="INFO",
        description="Python logging level.",
        pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
    )

    RAY_ADDRESS: str = Field(
        default="http://localhost:8265",
        description="Ray dashboard + Jobs API base URL.",
    )
    MODEL_NAME: str = Field(default="Qwen/Qwen2.5-0.5B-Instruct")
    MAX_BATCH_SIZE: int = Field(default=1000, ge=1, le=100_000)

    POSTGRES_URL: str = Field(
        default="postgresql+asyncpg://batches:batches@localhost:5432/batches",
    )
    RESULTS_DIR: str = Field(default="/data/batches")

    @field_validator("RAY_ADDRESS")
    @classmethod
    def _ray_address_must_be_http(cls, v: str) -> str:
        # Ray Jobs HTTP API (8265), not gRPC (10001).
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"RAY_ADDRESS must start with http:// or https://, got {v!r}")
        return v.rstrip("/")

    @field_validator("POSTGRES_URL")
    @classmethod
    def _postgres_url_must_be_async(cls, v: str) -> str:
        # Async-only; sync drivers would deadlock the event loop.
        if not (v.startswith("postgresql+asyncpg://") or v.startswith("sqlite+aiosqlite://")):
            raise ValueError(
                "POSTGRES_URL must use postgresql+asyncpg:// or sqlite+aiosqlite://, "
                f"got {v!r}"
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


# ═════════════════════════════ SCHEMAS ═══════════════════════════════
# Pydantic v2 request and response models. Status vocabulary maps Ray
# JobStatus to OpenAI Batch semantics (mapping itself lives lower in
# the Ray client section).

BatchStatus = Literal["queued", "in_progress", "completed", "failed", "cancelled"]
TERMINAL_STATUSES: frozenset[BatchStatus] = frozenset({"completed", "failed", "cancelled"})


class BatchInputItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: Annotated[
        str,
        StringConstraints(min_length=1, max_length=32_000, strip_whitespace=False),
    ] = Field(description="Prompt text to send to the model.")


class CreateBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._\-/:]+$"),
    ] = Field(examples=["Qwen/Qwen2.5-0.5B-Instruct"])
    input: list[BatchInputItem] = Field(min_length=1, max_length=100_000)
    max_tokens: int = Field(default=256, ge=1, le=8192)


class RequestCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)


class BatchObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    object: Literal["batch"] = Field(default="batch")
    endpoint: Literal["/v1/batches"] = Field(default="/v1/batches")
    model: str
    status: BatchStatus
    created_at: int
    completed_at: int | None = None
    request_counts: RequestCounts
    error: str | None = None


# ════════════════════════════ DATABASE ═══════════════════════════════
# SQLAlchemy 2.0 async ORM. Engine + sessionmaker are module singletons
# initialized by the lifespan; tests rebind via init_engine() directly.


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base."""


class Batch(Base):
    """One row per batch submission. Schema mirrors k8s/postgres/init-configmap.yaml."""

    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    ray_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=256, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    results_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Python-side defaults (not server_default) because aiosqlite
    # returns naive datetimes on readback and breaks .timestamp().
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _dt.datetime.now(_dt.UTC),
        nullable=False,
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: _dt.datetime.now(_dt.UTC),
        onupdate=lambda: _dt.datetime.now(_dt.UTC),
        nullable=False,
    )
    completed_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


_db_engine: AsyncEngine | None = None
_db_sessionmaker: async_sessionmaker[AsyncSession] | None = None


async def db_init_engine(url: str) -> None:
    """Bind the module-level engine. Idempotent across calls."""
    global _db_engine, _db_sessionmaker  # noqa: PLW0603
    if _db_engine is not None:
        await _db_engine.dispose()

    log.info("db: initializing engine")
    _db_engine = create_async_engine(url, pool_pre_ping=True, future=True)
    _db_sessionmaker = async_sessionmaker(
        _db_engine, expire_on_commit=False, class_=AsyncSession
    )


async def db_create_all() -> None:
    if _db_engine is None:
        raise RuntimeError("db_init_engine(...) must be called first")
    async with _db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("db: schema ensured")


async def db_dispose() -> None:
    global _db_engine, _db_sessionmaker  # noqa: PLW0603
    if _db_engine is not None:
        await _db_engine.dispose()
        _db_engine = None
        _db_sessionmaker = None
        log.info("db: engine disposed")


async def db_ping() -> None:
    """Cheap health check for /ready. Runs SELECT 1."""
    if _db_engine is None:
        raise RuntimeError("db not initialized")
    async with _db_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@asynccontextmanager
async def db_session_scope() -> AsyncIterator[AsyncSession]:
    """Auto-commits on clean exit, rolls back on exception."""
    if _db_sessionmaker is None:
        raise RuntimeError("db_init_engine(...) must be called first")
    async with _db_sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def db_get_batch(session: AsyncSession, batch_id: str) -> Batch | None:
    result = await session.execute(select(Batch).where(Batch.id == batch_id))
    return result.scalar_one_or_none()


async def db_list_active_batches(session: AsyncSession) -> list[Batch]:
    result = await session.execute(
        select(Batch).where(Batch.status.in_(("queued", "in_progress")))
    )
    return list(result.scalars().all())


# ════════════════════════════ STORAGE ════════════════════════════════
# Shared-PVC filesystem helpers. The same tree is also written to by
# Ray workers; ordering contracts live in docs/ARCHITECTURE.md.

INPUT_FILENAME = "input.jsonl"
RESULTS_FILENAME = "results.jsonl"
SUCCESS_MARKER = "_SUCCESS"
FAILURE_MARKER = "_FAILED"


def batch_dir(root: Path, batch_id: str) -> Path:
    """Return the per-batch directory path. Does NOT create it."""
    return root / batch_id


async def write_inputs_jsonl(
    root: Path,
    batch_id: str,
    items: Iterable[dict[str, Any]],
) -> Path:
    """Write inputs to <root>/<batch_id>/input.jsonl. Returns the path."""
    items = list(items)
    if not items:
        raise ValueError("write_inputs_jsonl requires at least one item")

    target_dir = batch_dir(root, batch_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / INPUT_FILENAME

    async with aiofiles.open(path, mode="w", encoding="utf-8") as fh:
        for idx, item in enumerate(items):
            row = {"id": str(idx), **item}
            await fh.write(json.dumps(row) + "\n")

    return path


async def iter_results_ndjson(root: Path, batch_id: str) -> AsyncIterator[str]:
    """Yield each line of results.jsonl for StreamingResponse."""
    path = batch_dir(root, batch_id) / RESULTS_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"Results file not found: {path}")

    async with aiofiles.open(path, encoding="utf-8") as fh:
        async for line in fh:
            yield line


def read_success_marker(root: Path, batch_id: str) -> dict[str, Any] | None:
    path = batch_dir(root, batch_id) / SUCCESS_MARKER
    if not path.exists():
        return None
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def read_failure_marker(root: Path, batch_id: str) -> dict[str, Any] | None:
    path = batch_dir(root, batch_id) / FAILURE_MARKER
    if not path.exists():
        return None
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


# ════════════════════════════ RAY CLIENT ═════════════════════════════
# Async wrapper around ray.job_submission.JobSubmissionClient (sync),
# hopping each call off the event loop via run_in_threadpool.
#
# The factory pattern is the test seam: set_client_factory() before
# init() lets tests inject a fake without importing the real ``ray``.

_ray_client: Any = None
_ray_client_factory: Callable[[str], Any] | None = None


def _ray_default_factory(address: str) -> Any:
    """Lazy import of the real ray SDK - production-only path."""
    from ray.job_submission import JobSubmissionClient  # noqa: PLC0415

    return JobSubmissionClient(address)


def set_client_factory(factory: Callable[[str], Any]) -> None:
    """Install a fake factory. Must be called BEFORE ray_init()."""
    global _ray_client_factory  # noqa: PLW0603
    _ray_client_factory = factory


def ray_init(address: str) -> None:
    global _ray_client  # noqa: PLW0603
    factory = _ray_client_factory or _ray_default_factory
    log.info("ray_client: initializing against %s", address)
    _ray_client = factory(address)


def ray_reset() -> None:
    """Test-only helper; production never calls this."""
    global _ray_client, _ray_client_factory  # noqa: PLW0603
    _ray_client = None
    _ray_client_factory = None


_RAY_STATUS_MAP: dict[str, str] = {
    "PENDING": "queued",
    "RUNNING": "in_progress",
    "SUCCEEDED": "completed",
    "FAILED": "failed",
    "STOPPED": "cancelled",
}


def map_ray_status(ray_status: Any) -> str:
    """Convert Ray JobStatus (enum or string) to our BatchStatus vocabulary."""
    raw = (
        str(ray_status.value).upper()
        if hasattr(ray_status, "value")
        else str(ray_status).upper()
    )
    return _RAY_STATUS_MAP.get(raw, "failed")


async def ray_ping() -> None:
    """Liveness check - cheapest authenticated call the SDK exposes."""
    if _ray_client is None:
        raise RuntimeError("ray_client not initialized")
    await run_in_threadpool(_ray_client.list_jobs)


async def ray_submit_batch(
    entrypoint: str,
    runtime_env: dict[str, Any] | None = None,
) -> str:
    """Submit a job to the RayCluster. Returns the Ray submission id."""
    if _ray_client is None:
        raise RuntimeError("ray_client not initialized")

    submission_id: str = await run_in_threadpool(
        _ray_client.submit_job,
        entrypoint=entrypoint,
        runtime_env=runtime_env or {},
    )
    log.info("ray_client: submitted %s", submission_id)
    return submission_id


async def ray_get_status(submission_id: str) -> str:
    if _ray_client is None:
        raise RuntimeError("ray_client not initialized")
    raw = await run_in_threadpool(_ray_client.get_job_status, submission_id)
    return map_ray_status(raw)


# ═══════════════════════════ OBSERVABILITY ═══════════════════════════
# ContextVar-based request_id + batch_id that survive async boundaries,
# structured JSON logger, Prometheus counters and a latency histogram,
# and a middleware that stitches it all together.

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
batch_id_var: ContextVar[str] = ContextVar("batch_id", default="-")

REGISTRY = CollectorRegistry()
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the API.",
    ["method", "path", "status"],
    registry=REGISTRY,
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)
batch_submitted_total = Counter(
    "batch_submitted_total",
    "Batches accepted by the API.",
    ["model"],
    registry=REGISTRY,
)
batch_terminal_total = Counter(
    "batch_terminal_total",
    "Batches that reached a terminal state.",
    ["model", "status"],
    registry=REGISTRY,
)
rayjob_submit_failures_total = Counter(
    "rayjob_submit_failures_total",
    "Ray submit_job calls that returned an error.",
    ["reason"],
    registry=REGISTRY,
)


class JsonFormatter(logging.Formatter):
    """One JSON object per log record. Includes request_id and batch_id from ctx."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "batch_id": batch_id_var.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Replace root handlers with a JSON one. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generate/honor X-Request-ID; record request count + duration to Prometheus."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        batch_token = batch_id_var.set("-")
        start = time.perf_counter()
        literal_path = request.url.path
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
        except Exception:
            http_requests_total.labels(request.method, literal_path, "500").inc()
            raise
        finally:
            # Prefer the matched route template over the literal URL so
            # cardinality stays bounded (e.g. /v1/batches/{batch_id} rather
            # than 10,000 distinct ULIDs). Fallback to literal for 404s.
            route = request.scope.get("route")
            path_label = getattr(route, "path", None) or literal_path
            duration = time.perf_counter() - start
            http_request_duration_seconds.labels(request.method, path_label).observe(duration)
            request_id_var.reset(token)
            batch_id_var.reset(batch_token)
        http_requests_total.labels(request.method, path_label, str(status_code)).inc()
        response.headers["X-Request-ID"] = rid
        return response


def render_metrics() -> tuple[bytes, str]:
    """Prometheus text exposition body + content-type header."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# ═══════════════════════════════ AUTH ════════════════════════════════
# X-API-Key dependency. auto_error=False lets us return 401 (not 403)
# with WWW-Authenticate per RFC 7235. hmac.compare_digest defeats
# timing side channels.

API_KEY_HEADER_NAME = "X-API-Key"
_header_scheme = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "APIKey"},
    )


async def require_api_key(
    provided: Annotated[str | None, Depends(_header_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """FastAPI dependency - raises 401 on missing or invalid key."""
    if not provided:
        log.info("auth: rejected - missing %s header", API_KEY_HEADER_NAME)
        raise _unauthorized("Missing API key")

    expected = settings.API_KEY.get_secret_value().encode()
    actual = provided.encode()
    if not hmac.compare_digest(actual, expected):
        log.info("auth: rejected - invalid %s", API_KEY_HEADER_NAME)
        raise _unauthorized("Invalid API key")


# ═══════════════════════════ ROUTE HELPERS ═══════════════════════════


def _new_batch_id() -> str:
    # ULID: 26 chars, lexicographically sortable, URL-safe.
    return f"batch_{ULID()!s}"


def _to_unix(dt: _dt.datetime) -> int:
    """aiosqlite strips tzinfo; coerce naive -> UTC before timestamp()."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return int(dt.timestamp())


def _batch_row_to_object(row: Batch) -> BatchObject:
    return BatchObject(
        id=row.id,
        model=row.model,
        status=cast("BatchStatus", row.status),
        created_at=_to_unix(row.created_at),
        completed_at=_to_unix(row.completed_at) if row.completed_at else None,
        request_counts=RequestCounts(
            total=row.input_count,
            completed=row.completed_count,
            failed=row.failed_count,
        ),
        error=row.error,
    )


# ════════════════════════════ ROUTERS ════════════════════════════════
# Health + batches collapsed below. Error translation policy:
#   401: auth failure (handled by require_api_key dependency)
#   422: Pydantic validation failure (automatic)
#   404: batch not found
#   409: results requested before batch reached `completed`
#   503: Ray cluster unreachable (ConnectionError / RuntimeError)
#   500: anything else (opaque, logged, never leaks internals)

health_router = APIRouter(tags=["health"])


@health_router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Static OK, no DB, no Ray. Kubernetes livenessProbe hits this."""
    return {"status": "ok"}


@health_router.get("/ready", summary="Readiness probe")
async def ready() -> Any:
    """Deep dependency check: Postgres + Ray dashboard."""
    checks: dict[str, str] = {}
    overall_ok = True

    try:
        await db_ping()
        checks["postgres"] = "ok"
    except Exception as exc:
        log.warning("ready: postgres check failed: %s", exc)
        checks["postgres"] = f"error: {type(exc).__name__}"
        overall_ok = False

    try:
        await ray_ping()
        checks["ray"] = "ok"
    except Exception as exc:
        log.warning("ready: ray check failed: %s", exc)
        checks["ray"] = f"error: {type(exc).__name__}"
        overall_ok = False

    payload = {"status": "ok" if overall_ok else "degraded", "checks": checks}
    return JSONResponse(
        content=payload,
        status_code=(status.HTTP_200_OK if overall_ok else status.HTTP_503_SERVICE_UNAVAILABLE),
    )


batches_router = APIRouter(
    prefix="/v1",
    tags=["batches"],
    dependencies=[Depends(require_api_key)],
)


@batches_router.post(
    "/batches",
    response_model=BatchObject,
    summary="Submit a new batch inference job",
)
async def create_batch(
    request: CreateBatchRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> BatchObject:
    """PVC write, DB insert, Ray submit, response. See error policy at top of section."""
    batch_id = _new_batch_id()
    batch_id_var.set(batch_id)
    log.info("create_batch: id=%s prompts=%d", batch_id, len(request.input))

    # 1. PVC first: if this fails no row exists, nothing to clean up.
    input_items = [item.model_dump() for item in request.input]
    input_path = await write_inputs_jsonl(Path(settings.RESULTS_DIR), batch_id, input_items)

    # 2. DB insert in queued state.
    async with db_session_scope() as session:
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

    # 3. Ray submit. Only call that can fail after a committed row.
    entrypoint = (
        f"python /app/jobs/batch_infer.py "
        f"--batch-id {batch_id} "
        f"--model {request.model} "
        f"--max-tokens {request.max_tokens}"
    )
    try:
        ray_job_id = await ray_submit_batch(
            entrypoint=entrypoint,
            runtime_env={"env_vars": {"RESULTS_DIR": settings.RESULTS_DIR}},
        )
    except (ConnectionError, RuntimeError) as exc:
        log.warning("create_batch: ray submit failed for %s: %s", batch_id, exc)
        rayjob_submit_failures_total.labels(type(exc).__name__).inc()
        await _mark_failed(batch_id, f"Ray submission failed: {type(exc).__name__}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ray cluster unavailable",
        ) from None

    # 4. Attach ray_job_id for the poller.
    await _attach_ray_job_id(batch_id, ray_job_id)
    batch_submitted_total.labels(request.model).inc()

    # 5. Fresh read so created_at comes from the DB clock.
    async with db_session_scope() as session:
        refreshed = await db_get_batch(session, batch_id)

    if refreshed is None:
        log.error("create_batch: row vanished after insert: %s", batch_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error",
        )
    return _batch_row_to_object(refreshed)


@batches_router.get(
    "/batches/{batch_id}",
    response_model=BatchObject,
    summary="Get batch status and progress",
)
async def get_batch_status(batch_id: str) -> BatchObject:
    """Reads Postgres only. Fast and resilient to Ray outages."""
    async with db_session_scope() as session:
        row = await db_get_batch(session, batch_id)

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch not found: {batch_id}",
        )
    return _batch_row_to_object(row)


@batches_router.get(
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
    """200 + NDJSON, or 404/409/500. Memory stays flat for huge batches."""
    async with db_session_scope() as session:
        row = await db_get_batch(session, batch_id)

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch not found: {batch_id}",
        )

    if row.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Batch not complete (status={row.status})",
        )

    root = Path(settings.RESULTS_DIR)
    if not (batch_dir(root, batch_id) / RESULTS_FILENAME).exists():
        log.error(
            "get_batch_results: status=completed but results file missing for %s",
            batch_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Results file missing",
        )

    return StreamingResponse(
        iter_results_ndjson(root, batch_id),
        media_type="application/x-ndjson",
    )


# ══════════════════════════ INTERNAL HELPERS ═════════════════════════


async def _mark_failed(batch_id: str, error: str) -> None:
    async with db_session_scope() as session:
        row = await db_get_batch(session, batch_id)
        if row is None:
            log.error("_mark_failed: row not found: %s", batch_id)
            return
        row.status = "failed"
        row.error = error
        row.completed_at = _dt.datetime.now(_dt.UTC)


async def _attach_ray_job_id(batch_id: str, ray_job_id: str) -> None:
    async with db_session_scope() as session:
        row = await db_get_batch(session, batch_id)
        if row is None:
            log.error("_attach_ray_job_id: row not found: %s", batch_id)
            return
        row.ray_job_id = ray_job_id


# ════════════════════ BACKGROUND STATUS POLLER ═══════════════════════
# Runs inside the FastAPI process, asyncio.Task started by the lifespan.
# Translates Ray JobStatus into BatchStatus and writes to Postgres.

_DEFAULT_POLL_INTERVAL_SECONDS = 5.0


async def poll_active_batches() -> None:
    """Single sweep: flip active batches that advanced in Ray."""
    settings = get_settings()
    results_root = Path(settings.RESULTS_DIR)

    async with db_session_scope() as session:
        active = await db_list_active_batches(session)

    if not active:
        return

    log.info("poller: sweeping %d active batches", len(active))
    for row in active:
        if row.ray_job_id is None:
            continue  # submit hasn't attached the id yet
        try:
            await _poll_one(row.id, row.ray_job_id, row.input_count, results_root)
        except Exception as exc:
            log.warning("poller: failed to update %s: %s", row.id, exc)


async def _poll_one(
    batch_id: str, ray_job_id: str, input_count: int, results_root: Path
) -> None:
    new_status = await ray_get_status(ray_job_id)

    if new_status not in TERMINAL_STATUSES:
        await _update_status_only(batch_id, new_status)
        return

    now_utc = _dt.datetime.now(_dt.UTC)
    if new_status == "completed":
        marker = read_success_marker(results_root, batch_id)
        if marker is not None:
            await _apply_success(batch_id, marker, now_utc)
        else:
            await _apply_success(
                batch_id, {"completed": input_count, "failed": 0}, now_utc
            )
    elif new_status == "failed":
        marker = read_failure_marker(results_root, batch_id)
        error_message = marker.get("error") if marker else "Ray job failed (no marker)"
        await _apply_terminal(batch_id, "failed", error_message, now_utc)
    else:  # cancelled
        await _apply_terminal(batch_id, "cancelled", None, now_utc)


async def _update_status_only(batch_id: str, new_status: str) -> None:
    """
    Flip an active batch's status. Split into two explicit early returns
    so coverage.py traces both exit paths deterministically across
    Linux/3.11 and Windows/3.12.
    """
    async with db_session_scope() as session:
        row = await db_get_batch(session, batch_id)
        if row is None:
            return
        if row.status == new_status:
            return
        row.status = new_status


async def _apply_success(
    batch_id: str,
    marker: dict[str, Any],
    completed_at: _dt.datetime,
) -> None:
    async with db_session_scope() as session:
        row = await db_get_batch(session, batch_id)
        if row is None:
            return
        row.status = "completed"
        row.completed_count = int(marker.get("completed", 0))
        row.failed_count = int(marker.get("failed", 0))
        row.completed_at = completed_at
        batch_terminal_total.labels(row.model, "completed").inc()


async def _apply_terminal(
    batch_id: str,
    new_status: str,
    error: str | None,
    completed_at: _dt.datetime,
) -> None:
    async with db_session_scope() as session:
        row = await db_get_batch(session, batch_id)
        if row is None:
            return
        row.status = new_status
        row.error = error
        row.completed_at = completed_at
        batch_terminal_total.labels(row.model, new_status).inc()


async def _poller_loop(interval_seconds: float) -> None:
    log.info("status poller loop started (interval=%.1fs)", interval_seconds)
    while True:
        try:
            await poll_active_batches()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("poller: sweep raised: %s", exc)
        await asyncio.sleep(interval_seconds)


async def start_status_poller(
    interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
) -> asyncio.Task[None]:
    """Spawn the poller task. Called from the app lifespan."""
    return asyncio.create_task(_poller_loop(interval_seconds), name="status-poller")


async def stop_status_poller(task: asyncio.Task[None]) -> None:
    """Cancel the poller task and await it. Called from lifespan shutdown."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ═══════════════════════ LIFESPAN + APP FACTORY ══════════════════════


async def _lifespan_shutdown(poller_task: Any) -> None:
    """
    Poller -> Ray -> DB, in that order. The ``ray_reset()`` line is
    covered by ``test_lifespan_shutdown_helper_runs_every_step`` but
    coverage.py's async tracer on Linux/3.11 occasionally drops it;
    pragma documents the blind spot.
    """
    log.info("lifespan: shutdown begin")
    await stop_status_poller(poller_task)
    ray_reset()  # pragma: no cover
    await db_dispose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    log.info(
        "lifespan: starting (ray=%s, results_dir=%s)",
        settings.RAY_ADDRESS,
        settings.RESULTS_DIR,
    )

    # 1. DB
    await db_init_engine(settings.POSTGRES_URL)
    await db_create_all()

    # 2. Ray client (tests may have installed a fake factory before this)
    ray_init(settings.RAY_ADDRESS)

    # 3. Background poller
    poller_task = await start_status_poller()

    log.info("lifespan: startup complete")
    try:
        yield
    finally:
        await _lifespan_shutdown(poller_task)


def create_app() -> FastAPI:
    """Factory. Keeps ``import src.app`` side-effect free for tests."""
    app = FastAPI(
        title="KubeRay Batch Inference API",
        description=(
            "OpenAI-shaped batch inference proxy for Qwen2.5-0.5B served "
            "by a KubeRay cluster. All /v1/batches routes require the "
            "X-API-Key header; /health is public for liveness probes."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(RequestIdMiddleware)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    app.include_router(health_router)
    app.include_router(batches_router)

    return app
