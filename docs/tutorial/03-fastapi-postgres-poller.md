# 03 - FastAPI, Postgres, and the Poller

The compute plane works. Now we build the control plane: an HTTP
API that accepts batches, writes inputs to disk, tells Ray to
process them, and serves back status and results.

This is the largest single file in the final repo, and also the
easiest part to understand because every concern is concrete:

- Parse JSON -> validate shape -> authenticate -> write files ->
  insert a row -> submit a job -> return immediately.

## Where we are

```
   +------------------+            +---------------+
   |   HTTP client    |---(POST)-->|   FastAPI     |
   +------------------+            +-------+-------+
                                            |
                    +-----------+-----------+
                    |                       |
                    v                       v
              Postgres row          input.jsonl + Ray job submit
              (metadata)            (work ordered to the compute plane)
```

## Install dependencies

```bash
pip install "fastapi>=0.115,<1" "uvicorn[standard]>=0.32,<1" \
            "pydantic>=2.8,<3" "pydantic-settings>=2.5,<3" \
            "sqlalchemy[asyncio]>=2.0.30,<3" "aiosqlite>=0.20,<1" \
            "aiofiles>=24.1,<25" "python-ulid>=3.0,<4" "httpx>=0.27,<1"
```

We use SQLite via `aiosqlite` for this tutorial. The production repo
swaps to Postgres by changing one URL; the SQLAlchemy code is the
same.

## The request and response shapes

Before code, define the data contract.

### Request (`POST /v1/batches`)

```json
{
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "input": [
    {"prompt": "What is 2+2?"},
    {"prompt": "Hello world"}
  ],
  "max_tokens": 64
}
```

### Response (immediately, `202 Accepted`)

```json
{
  "id": "batch_01HG...",
  "status": "queued",
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "input_count": 2,
  "created_at": "2026-04-14T...",
  "input_path": "/data/batches/batch_01HG.../input.jsonl",
  "results_path": "/data/batches/batch_01HG.../results.jsonl"
}
```

### Status states

```
queued -------> in_progress -------> completed
                                \
                                 ---> failed
                                \
                                 ---> cancelled
```

Only five states. Client polls with `GET /v1/batches/{id}` and
fetches results with `GET /v1/batches/{id}/results` once status
is `completed`.

## Layer 4: a minimal FastAPI skeleton

Create `app.py`:

```python
# ~/batch-tutorial/app.py
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import aiofiles
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import String, Integer, DateTime, select
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from ulid import ULID


# ─── Config ─────────────────────────────────────────────────────────
class Settings(BaseSettings):
    api_key: str = "demo-api-key-change-me"
    database_url: str = "sqlite+aiosqlite:///./tutorial.db"
    results_dir: Path = Path("./data/batches")
    ray_address: str = "http://localhost:8265"

    class Config:
        env_file = ".env"


settings = Settings()
settings.results_dir.mkdir(parents=True, exist_ok=True)


# ─── Database ───────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, default="queued")
    model: Mapped[str] = mapped_column(String)
    input_count: Mapped[int] = mapped_column(Integer)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    max_tokens: Mapped[int] = mapped_column(Integer, default=64)
    input_path: Mapped[str] = mapped_column(String)
    results_path: Mapped[str] = mapped_column(String)
    ray_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# ─── Schemas ────────────────────────────────────────────────────────
class BatchInputItem(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


class CreateBatchRequest(BaseModel):
    model: str = "Qwen/Qwen2.5-0.5B-Instruct"
    input: list[BatchInputItem] = Field(min_length=1, max_length=10_000)
    max_tokens: int = Field(default=64, ge=1, le=2048)


class BatchObject(BaseModel):
    id: str
    status: str
    model: str
    input_count: int
    completed_count: int = 0
    failed_count: int = 0
    input_path: str
    results_path: str
    ray_job_id: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ─── Auth ───────────────────────────────────────────────────────────
async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    import hmac
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "APIKey"},
        )


# ─── Helpers ────────────────────────────────────────────────────────
def _new_batch_id() -> str:
    return f"batch_{ULID()}"


async def write_inputs_jsonl(batch_id: str, items: list[BatchInputItem]) -> Path:
    batch_dir = settings.results_dir / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    input_path = batch_dir / "input.jsonl"
    async with aiofiles.open(input_path, "w") as f:
        for idx, item in enumerate(items):
            await f.write(json.dumps({"id": str(idx), "prompt": item.prompt}) + "\n")
    return input_path


async def iter_results_ndjson(batch_id: str) -> AsyncIterator[bytes]:
    path = settings.results_dir / batch_id / "results.jsonl"
    async with aiofiles.open(path, "rb") as f:
        async for line in f:
            yield line


# ─── Lifespan ───────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(lifespan=lifespan)


# ─── Routes ─────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/batches", status_code=202, dependencies=[Depends(require_api_key)])
async def create_batch(req: CreateBatchRequest) -> BatchObject:
    now = datetime.now(timezone.utc)
    batch_id = _new_batch_id()

    input_path = await write_inputs_jsonl(batch_id, req.input)
    results_path = settings.results_dir / batch_id / "results.jsonl"

    async with SessionLocal() as session:
        row = Batch(
            id=batch_id,
            status="queued",
            model=req.model,
            input_count=len(req.input),
            max_tokens=req.max_tokens,
            input_path=str(input_path),
            results_path=str(results_path),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return BatchObject.model_validate(row)


@app.get("/v1/batches/{batch_id}", dependencies=[Depends(require_api_key)])
async def get_batch(batch_id: str) -> BatchObject:
    async with SessionLocal() as session:
        row = await session.get(Batch, batch_id)
        if row is None:
            raise HTTPException(404, "unknown batch_id")
        return BatchObject.model_validate(row)


@app.get("/v1/batches/{batch_id}/results", dependencies=[Depends(require_api_key)])
async def get_results(batch_id: str) -> StreamingResponse:
    async with SessionLocal() as session:
        row = await session.get(Batch, batch_id)
        if row is None:
            raise HTTPException(404, "unknown batch_id")
        if row.status != "completed":
            raise HTTPException(409, f"batch not completed (status={row.status})")
    return StreamingResponse(iter_results_ndjson(batch_id),
                             media_type="application/x-ndjson")
```

Run:

```bash
uvicorn app:app --reload --port 8000
```

In another terminal:

```bash
curl -s http://localhost:8000/health

curl -s -X POST http://localhost:8000/v1/batches \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: demo-api-key-change-me' \
  -d '{"input":[{"prompt":"Hi"}]}' | jq
```

You get back a batch object with status `queued`. No actual
inference is happening yet - we just wrote `input.jsonl` and a
database row. That is the control plane's job.

### What FastAPI did for you

```
  Incoming JSON
       |
       v
  Pydantic (CreateBatchRequest)  <-- 422 on shape errors
       |
       v
  Depends(require_api_key)        <-- 401 on bad key
       |
       v
  your handler (create_batch)     <-- your code
       |
       v
  Pydantic (BatchObject)          <-- serializes ORM row to JSON
       |
       v
  HTTP 202 JSON response
```

You wrote nothing for validation, authentication, or JSON
serialization. Framework-provided, all of it.

## Layer 5: the poller

Right now batches stay `queued` forever because nothing transitions
them. Once we wire Ray in, a background task will ask Ray "is this
job done?" and update the row.

Add to `app.py`:

```python
# ─── Poller ─────────────────────────────────────────────────────────
import aiofiles.os as aos

async def _poll_one(batch_id: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(Batch, batch_id)
        if row is None:
            return
        results_dir = settings.results_dir / batch_id
        success = results_dir / "_SUCCESS"
        failed = results_dir / "_FAILED"

        if await aos.path.exists(success):
            async with aiofiles.open(success) as f:
                marker = json.loads(await f.read())
            row.status = "completed"
            row.completed_count = marker.get("completed", 0)
            row.failed_count = marker.get("failed", 0)
        elif await aos.path.exists(failed):
            row.status = "failed"

        row.updated_at = datetime.now(timezone.utc)
        await session.commit()


async def poll_active_batches() -> None:
    """Every 5s, reconcile file-marker state into Postgres."""
    while True:
        try:
            async with SessionLocal() as session:
                stmt = select(Batch.id).where(
                    Batch.status.in_(["queued", "in_progress"])
                )
                result = await session.execute(stmt)
                ids = [row[0] for row in result.all()]
            for bid in ids:
                await _poll_one(bid)
        except Exception:
            pass
        await asyncio.sleep(5)
```

And start it in `lifespan`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    task = asyncio.create_task(poll_active_batches())
    yield
    task.cancel()
```

Now restart uvicorn. The loop runs invisibly in the background.

### The reconciliation idea

```
Every 5s:
  for each batch where status IN (queued, in_progress):
    look at the filesystem markers
    map: _SUCCESS -> completed
         _FAILED  -> failed
    update the DB row
```

The database is never the source of truth about whether a job
finished. The filesystem is. The database is a cache of the
filesystem that is cheap to read by HTTP handlers.

## Test end-to-end (fake it for now)

Submit a batch:

```bash
BATCH=$(curl -s -X POST http://localhost:8000/v1/batches \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: demo-api-key-change-me' \
  -d '{"input":[{"prompt":"Hi"}]}' | jq -r .id)
```

Simulate Ray producing output (we'll hook up the real Ray submission
in the K8s parts):

```bash
cat > data/batches/$BATCH/results.jsonl <<'EOF'
{"id":"0","prompt":"Hi","response":"Hello!","finish_reason":"stop"}
EOF

cat > data/batches/$BATCH/_SUCCESS <<EOF
{"batch_id":"$BATCH","total":1,"completed":1,"failed":0}
EOF
```

Wait 5 seconds, then:

```bash
curl -s -H 'X-API-Key: demo-api-key-change-me' \
  http://localhost:8000/v1/batches/$BATCH | jq .status
# "completed"

curl -s -H 'X-API-Key: demo-api-key-change-me' \
  http://localhost:8000/v1/batches/$BATCH/results
# {"id":"0","prompt":"Hi","response":"Hello!","finish_reason":"stop"}
```

### What you just proved

```
      POST /v1/batches       POLLER            GET /v1/batches/{id}
            |                   |                        |
            v                   v                        v
      write input.jsonl    read _SUCCESS file        read DB
      insert DB row        update DB row             return JSON
      return id
```

Three independent paths. HTTP handlers never hit Ray directly. The
reconciler is the only piece that talks to the compute plane's
"done" signal. This decoupling is the whole reason the API stays
responsive even when a batch takes 10 minutes.

## The Ray submission step (stub for now)

Replace the `# TODO: submit to Ray` gap by adding, at the end of
`create_batch`, before the `return`:

```python
# For the tutorial, Ray submission is simulated. The finished repo
# calls JobSubmissionClient here and stores the returned
# submission_id on the batch row. We wire the real thing up in the
# Kubernetes part.
```

In the finished repo, the real call looks roughly like:

```python
from ray.job_submission import JobSubmissionClient
from starlette.concurrency import run_in_threadpool

client = JobSubmissionClient(settings.ray_address)
submission_id = await run_in_threadpool(
    client.submit_job,
    entrypoint=f"python /app/jobs/batch_infer.py --batch-id {batch_id} "
               f"--model {req.model} --max-tokens {req.max_tokens}",
    runtime_env={"working_dir": "/app"},
)
row.ray_job_id = submission_id
```

- **`JobSubmissionClient`** is Ray's REST client for submitting
  Python scripts to a running Ray cluster.
- **`run_in_threadpool`** wraps the sync SDK call so it does not
  block FastAPI's async event loop.
- **`ray_job_id`** gets persisted so the poller can ask Ray about
  this specific job.

We'll wire this up once we have a real Ray cluster to talk to.

## What this maps to in the finished repo

| Tutorial concept | Finished file |
|---|---|
| `Settings` class | `api/src/config.py` |
| `Batch` ORM model | `api/src/models.py` |
| `CreateBatchRequest`, `BatchObject` | `api/src/models.py` |
| `require_api_key` | `api/src/auth.py` |
| `create_batch` | `api/src/routes/batches.py` |
| `iter_results_ndjson` | `api/src/storage.py` |
| `poll_active_batches` | `api/src/routes/batches.py` |
| `create_app` factory | `api/src/main.py` |

On the `demo/single-file-version` branch, all of the above live in
one file: `api/src/app.py`.

## Verify

```bash
ls data/batches/     # contains one batch dir per submission
python -c "import sqlite3; print(sqlite3.connect('tutorial.db').execute('SELECT id, status FROM batches').fetchall())"
curl http://localhost:8000/health
```

Next: container and Kubernetes.
Continue to [04-docker-and-kubernetes.md](04-docker-and-kubernetes.md).
