# Architecture Walkthrough

A comprehensive onboarding guide to this repository for engineers familiar
with traditional VPS / LAMP-style deployments who are new to Kubernetes,
Ray, and KubeRay.

This document is written against the **`demo/single-file-version`** branch,
where the entire FastAPI control plane lives in one file
(`api/src/app.py`). If you are on `main`, the same functions exist but are
split across modules under `api/src/` and `api/src/routes/` — the line
numbers below will not match, but every function name will.

---

## Table of contents

1. [The stack in one picture](#1-the-stack-in-one-picture)
2. [What each thing is](#2-what-each-thing-is)
3. [Why this shape (vs. a single process)](#3-why-this-shape)
4. [VPS → Kubernetes translation table](#4-vps--kubernetes-translation-table)
5. [Who talks to whom](#5-who-talks-to-whom)
6. [Shared storage: the file-based handshake](#6-shared-storage-the-file-based-handshake)
7. [Request flow: `POST /v1/batches` end to end](#7-request-flow-post-v1batches-end-to-end)
8. [How Ray actually crunches a batch](#8-how-ray-actually-crunches-a-batch)
9. [Ray head vs. Ray worker](#9-ray-head-vs-ray-worker)
10. [The Ray vocabulary (Core / Data / Jobs / Serve / KubeRay)](#10-the-ray-vocabulary)
11. [Cross-cutting concerns: auth, observability, config](#11-cross-cutting-concerns)
12. [Failure modes and recovery](#12-failure-modes-and-recovery)
13. [Mental-model cheat sheet](#13-mental-model-cheat-sheet)
14. [Where to read next, in order](#14-where-to-read-next)

---

## 1. The stack in one picture

```
          +----------------------+
          |        Client        |
          | curl / SDK / browser |
          +----------+-----------+
                     | HTTPS (API key)
                     v
          +----------------------------------+
          |         FastAPI API Pod          |
          |  validation · auth · DB writes   |
          |  Ray Jobs submission · streaming |
          +-----+--------------+-------------+
                |              |
         SQL    |              |  Ray Jobs REST (:8265)
                v              v
      +----------------+   +----------------------+
      |  Postgres Pod  |   |    Ray Head Pod      |
      | batch ledger   |   | scheduler · Jobs API |
      +----------------+   +----------+-----------+
                                      |
                                      | schedules actors
                                      v
                        +------------------------------+
                        |       Ray Worker Pods        |
                        | QwenPredictor actors         |
                        | model loaded once per actor  |
                        +---------------+--------------+
                                        |
                                        | shared file I/O
                                        v
                       +-----------------------------------+
                       |        Shared Storage PVC         |
                       |  /data/batches/<batch_id>/        |
                       |    input.jsonl                    |
                       |    results.jsonl                  |
                       |    _SUCCESS | _FAILED             |
                       +-----------------------------------+
```

One sentence: *FastAPI is the receptionist, Ray is the factory, Postgres
is the ledger, Kubernetes is the building manager, and KubeRay is the team
that sets up the Ray factory inside that building.*

---

## 2. What each thing is

| Layer | Role | Where in the repo |
|---|---|---|
| **FastAPI** | HTTP API: validates input, authenticates, writes the batch row, submits the Ray job, streams results back. | `api/src/app.py` |
| **Postgres** | Durable metadata ledger — batch id, status, counts, timestamps, Ray job id, file paths. Not the result payload. | `k8s/postgres/` |
| **Ray** | Python distributed runtime. Here it runs one job per submitted batch across 2 worker pods. | `inference/jobs/batch_infer.py` |
| **Kubernetes** | Runs all of the above as pods, keeps them alive, mounts volumes, handles service discovery. | `k8s/` |
| **KubeRay** | Kubernetes operator that teaches K8s what a `RayCluster` is. Creates head + worker pods from one YAML declaration. | `k8s/raycluster/raycluster.yaml` |
| **Shared PVC** | A single `/data/batches` directory mounted into *both* the API pod and every Ray pod. The handshake medium. | `k8s/storage/shared-pvc.yaml` |

---

## 3. Why this shape

The problem is *not* "answer one prompt quickly over HTTP." It is:

> accept a batch of prompts, process them offline, and let the client come back later for the result.

That changes the design. A single FastAPI process running the model
directly would:

- block HTTP workers on long inference work;
- tie API scaling to inference scaling;
- make parallelism across replicas or nodes awkward;
- let inference crashes take the API down.

So the codebase splits concerns along a **control plane / compute plane**
boundary:

- **Control plane** — FastAPI. Cheap, stateless, horizontally scalable.
- **Compute plane** — Ray workers. Heavy, stateful (model is loaded once
  per actor), scaled independently.
- **Ledger** — Postgres. Small, queryable, durable job state.
- **Handoff medium** — shared PVC. Large opaque payloads that neither
  belong in the DB nor over HTTP.

---

## 4. VPS → Kubernetes translation table

If you are used to a LAMP box, these equivalences will save you a lot of
time:

| VPS concept | Kubernetes equivalent |
|---|---|
| The machine itself | A Kubernetes **node** |
| `systemd` service | **Deployment** (or StatefulSet) |
| `.env` file / `/etc/default/` | **ConfigMap** (non-secret) + **Secret** (credentials) |
| A folder on disk | **PersistentVolume** exposed via **PVC** |
| `127.0.0.1:5432` / `db.internal` | **Service DNS**, e.g. `postgres.default.svc.cluster.local` |
| `systemctl restart` on crash | Controller restarts the pod automatically |
| Apache/PHP app | **FastAPI pod** |
| Cron job / `supervisord` worker | **Ray job** on the Ray cluster |

You are not learning alien concepts — the same operational questions
(where do config values live, where does data persist, how do services
find each other) have different mechanical answers.

---

## 5. Who talks to whom

- The **client** only talks to **FastAPI**.
- **FastAPI** talks to **Postgres** (SQL), the **Ray head** (Jobs REST
  API on port `8265`), and the **shared PVC** (filesystem).
- **Ray workers** talk only to the **shared PVC**. They do not read
  Postgres. They do not know about FastAPI.
- **Ray does not serve HTTP to the client.** The Jobs API on 8265 is an
  internal control-plane surface for FastAPI only.

The Ray head is reachable via `RAY_ADDRESS`, set in
`k8s/api/configmap.yaml:13`:

```
RAY_ADDRESS: http://qwen-raycluster-head-svc.ray.svc.cluster.local:8265
```

That is Kubernetes DNS for the head service. Inside Python, FastAPI uses
`ray.job_submission.JobSubmissionClient(RAY_ADDRESS)` to dispatch work.

---

## 6. Shared storage: the file-based handshake

On a VPS, your web app and worker script can both read `/var/app/data/`
because they share one filesystem. In Kubernetes each pod has its own
filesystem, so the **API pod** and the **Ray worker pods** need an
explicit shared volume.

That volume is declared in `k8s/storage/shared-pvc.yaml`, and mounted at
`/data/batches` in three places:

- API deployment: `k8s/api/deployment.yaml` (volumeMount)
- Ray head: `k8s/raycluster/raycluster.yaml:140-144`
- Ray workers: `k8s/raycluster/raycluster.yaml:214-218`

### What lives under `/data/batches/<batch_id>/`

| File | Writer | Reader | Purpose |
|---|---|---|---|
| `input.jsonl` | API pod | Ray workers | Prompts, one JSON row each: `{"id":"0","prompt":"..."}` |
| `results.jsonl` | Ray workers | API pod | Completions, one JSON row each, same `id` order |
| `_SUCCESS` | Ray workers | API poller | "Output is complete." Written **last**, after `results.jsonl`. |
| `_FAILED` | Ray workers | API poller | "Output is not safe to read." Contains traceback. |

The `_SUCCESS` / `_FAILED` convention is the same trick old Hadoop/shell
pipelines have always used: one small marker file that atomically signals
"the bigger file next to me is trustworthy." Because the marker is written
last, a reader who sees it can assume `results.jsonl` is fully flushed.

### Why not store everything in Postgres?

Postgres is excellent for **small, queryable metadata** — status, counts,
timestamps, ids — and terrible for **streaming large NDJSON payloads**.
The design uses each tool for what it is good at:

- Postgres = ledger (rows describe batches).
- PVC = warehouse (files contain payloads).

---

## 7. Request flow: `POST /v1/batches` end to end

This is the full path a request travels. Every code reference is on the
`demo/single-file-version` branch.

### 7.1 Request arrives

1. **Validation.** FastAPI parses the JSON body into the Pydantic model
   `CreateBatchRequest` at `api/src/app.py:175`. Nested prompt objects
   are `BatchInputItem` at `api/src/app.py:167`. If the body does not
   type-check, FastAPI returns `422` before your handler ever runs.
2. **Authentication.** The batch router has
   `dependencies=[Depends(require_api_key)]`. FastAPI runs
   `require_api_key` (`api/src/app.py:588`) before the handler, which
   compares the `X-API-Key` header against the configured secret using
   `hmac.compare_digest` (constant-time). A bad key returns `401` with a
   `WWW-Authenticate: APIKey` header per RFC 7235.

### 7.2 Handler body

The handler is `create_batch` at `api/src/app.py:694`. Its steps:

1. **Mint an id.** `_new_batch_id()` (`api/src/app.py:607`) returns
   `batch_<ULID>`. ULIDs are lexicographically sortable by creation time,
   which makes listing and paging cheap.
2. **Write inputs.** `write_inputs_jsonl(...)` (`api/src/app.py:331`)
   creates `/data/batches/<batch_id>/input.jsonl`, one JSON line per
   prompt, assigning row ids `"0"`, `"1"`, `"2"`, ….
3. **Insert Postgres row.** A `Batch` ORM row (`api/src/app.py:214`) is
   persisted with `status="queued"`, counts, and the input/result paths.
4. **Submit to Ray.** The handler builds an entrypoint command string —
   `python /app/jobs/batch_infer.py --batch-id <id> --model <...> --max-tokens <n>` —
   and calls `ray_submit_batch(...)` at `api/src/app.py:442`. That
   function wraps `JobSubmissionClient.submit_job` in
   `starlette.concurrency.run_in_threadpool` because the SDK is sync
   and the handler is `async`.
5. **Attach the Ray job id.** Ray returns a `submission_id`;
   `_attach_ray_job_id(...)` (`api/src/app.py:840`) updates the row so
   the poller and `GET` handlers can later look it up.
6. **Respond.** The handler returns a `BatchObject`
   (`api/src/app.py:192`) with the id, status (`queued`), model, and
   timestamps. No results yet — the client will poll for those.

The handler is `async` from top to bottom. Every blocking call (DB,
filesystem writes, Ray SDK) is either native-async (SQLAlchemy async,
`aiofiles`) or wrapped in the threadpool, so one slow batch submission
cannot stall the event loop.

### 7.3 The background poller

Submission is fire-and-forget from FastAPI's perspective. Something still
has to notice when Ray finishes.

`lifespan()` (`api/src/app.py:993`) starts an async background task that
runs `poll_active_batches()` (`api/src/app.py:856`) on a 5-second tick.
For each active batch, `_poll_one(...)` (`api/src/app.py:877`):

1. Asks Ray for the current `JobStatus` via the Jobs SDK.
2. Maps Ray's states onto the app's five-state machine
   (`queued` → `in_progress` → `completed` | `failed` | `cancelled`).
3. On terminal states, reads `_SUCCESS` (`read_success_marker`,
   `api/src/app.py:364`) or `_FAILED`, pulling counts and/or traceback.
4. Updates the Postgres row atomically.

This reconciliation loop is the reason `GET /v1/batches/<id>` can stay
cheap: the status endpoint only reads Postgres. It never hits Ray or the
filesystem on the request path.

### 7.4 Status and results reads

- `get_batch_status` (`api/src/app.py:763`) — pure DB read, returns the
  current `BatchObject`.
- `get_batch_results` (`api/src/app.py:789`) — confirms `status="completed"`,
  then returns a `StreamingResponse` that wraps
  `iter_results_ndjson(...)` (`api/src/app.py:353`). The file is streamed
  line by line; the API never buffers the whole payload in memory.

---

## 8. How Ray actually crunches a batch

The compute side lives in `inference/jobs/batch_infer.py`. This is the
script FastAPI tells Ray to run.

### 8.1 Entrypoint

`main()` parses `--batch-id`, calls `ray.init(address="auto")` (which
picks up `RAY_ADDRESS` injected by the Ray Jobs runtime), and invokes
`run_batch(...)`. Any unhandled exception writes `_FAILED` with a
traceback and exits non-zero, so KubeRay / the poller can see the
failure.

### 8.2 Pipeline inside `run_batch`

```python
ds = ray.data.read_json(input_path)                      # 1. read JSONL as a Dataset
ds = ds.repartition(max(2, total // 16 or 2))            # 2. split into blocks
out_ds = ds.map_batches(                                 # 3. run the UDF across workers
    QwenPredictor,
    fn_constructor_kwargs={"model_name": ..., "max_tokens": ...},
    concurrency=2,
    batch_size=8,
)
out_ds.repartition(1).write_json(staging_dir)            # 4. one output file
staged_files[0].rename(results_path)                     # 5. atomic rename to results.jsonl
success_marker.write_text(json.dumps({...}))             # 6. _SUCCESS last
```

Why each step:

1. **Read as a Dataset, not a file.** Ray Data treats the JSONL as rows
   with typed columns, enabling parallel processing and streaming.
2. **Repartition.** Without this, one block could land on one worker and
   leave the other idle at the tail. Repartitioning splits work evenly.
3. **`map_batches(QwenPredictor, ...)`** is the heart of the pipeline.
   Ray Data instantiates two actors (one per worker, because
   `concurrency=2`), each running `QwenPredictor.__call__` on chunks of
   `batch_size=8`. The actors are long-lived — the model is loaded once
   in `__init__` and reused for every chunk.
4. **`repartition(1)`** forces a single output block, so the job writes
   exactly one JSON file to the staging directory. (For multi-GB outputs
   you would drop this and stream many files.)
5. **Atomic rename** makes `results.jsonl` appear in its final location
   in one operation — a reader never sees a half-written file at that
   path.
6. **`_SUCCESS` is written last** so its presence proves
   `results.jsonl` is complete.

### 8.3 What `QwenPredictor` does

`QwenPredictor` (`inference/jobs/batch_infer.py:65`) is a class with two
phases:

- **`__init__`** — expensive, runs once per actor: imports `torch`
  and `transformers`, loads the Qwen2.5-0.5B tokenizer and model in
  `bfloat16`, sets eval mode. Imports live *inside* `__init__` so Ray
  does not attempt to pickle torch/transformers when shipping the class
  to the actor.
- **`__call__`** — runs per chunk: for each prompt, formats it with
  `apply_chat_template`, tokenizes, generates with deterministic
  settings (`do_sample=False`), slices off the prompt tokens to keep
  only the completion, decodes, and packs everything into same-length
  arrays that Ray Data can materialize back into rows.

The loop inside `__call__` is intentionally per-prompt rather than a
padded batch. On CPU with variable-length inputs, true batched
generation wastes KV-cache work, and per-prompt isolation means one bad
prompt cannot corrupt its neighbors' outputs. On GPU you would swap in
left-padded batched `generate` for throughput.

### 8.4 Error isolation

Row-level errors (one bad prompt) are caught inside `__call__` and
recorded in the `error` column — the batch keeps going. Job-level errors
(model failed to load, disk full, OOM) propagate up and write `_FAILED`.
Two layers of failure handling, two different outcomes:

| Failure kind | Outcome |
|---|---|
| One prompt crashes generation | That row's `error` is set; `results.jsonl` still contains all other rows; batch `status="completed"`. |
| Model load fails, disk full, etc. | `_FAILED` written; batch `status="failed"`; traceback surfaced via the poller. |

---

## 9. Ray head vs. Ray worker

Two pod roles in the RayCluster, with very different jobs.

| | **Head pod** | **Worker pods** |
|---|---|---|
| Responsibility | Cluster brain — Jobs API, scheduling, dashboard, state | Compute — run actors, hold the model, do generation |
| Count in this repo | 1 | 2 (`k8s/raycluster/raycluster.yaml:173`) |
| Listens on | `:8265` (Jobs REST + dashboard), `:6379` (GCS) | Internal Ray ports |
| CPU for tasks | **`num-cpus: "0"`** — zero compute on purpose (`k8s/raycluster/raycluster.yaml:91`) | Full CPU allocation |
| Who calls it | FastAPI (via `RAY_ADDRESS`) | Only the head, internally |

Setting `num-cpus: "0"` on the head is deliberate. Scheduling and health
checks must stay responsive even when inference is saturating the
cluster, so the head is not allowed to accept tasks. Head = control
plane. Workers = compute plane. Same separation pattern as FastAPI vs.
Ray, one level deeper.

---

## 10. The Ray vocabulary

"Ray" is overloaded. Pulling the pieces apart:

| Term | What it is | Used here? |
|---|---|---|
| **Ray Core** | Low-level tasks, actors, object store. Underpins everything else. | Indirectly — Ray Data sits on top. |
| **Ray Cluster** | One running instance of Ray: head + workers. | Yes — created by KubeRay. |
| **Ray Data** | Distributed dataset library (`read_json`, `repartition`, `map_batches`, `write_json`). | **Yes** — the whole batch pipeline. |
| **Ray Jobs** | A runtime REST API (`:8265`) for submitting entrypoints to an existing cluster. | **Yes** — FastAPI uses `JobSubmissionClient`. |
| **Ray Serve** | Online model serving with HTTP autoscaling. | No — this is offline batch. |
| **KubeRay** | Kubernetes operator defining the `RayCluster`, `RayJob`, `RayService` CRDs. | **Yes** — creates the cluster. |

### Two things both called "Ray Jobs"

A subtle trap worth flagging:

- **Ray Jobs API** — runtime REST submission to a running cluster. This
  repo uses it.
- **`RayJob` CRD** — a Kubernetes object (managed by KubeRay) that spins
  up a fresh RayCluster, runs one entrypoint, and tears down. This repo
  **deliberately does not** use it for per-request batches: a cold
  cluster takes tens of seconds to start, which is worse than keeping a
  warm cluster around. See `docs/ARCHITECTURE.md` for the trade-off.

One sentence to remember the whole vocabulary:

> KubeRay runs Ray on Kubernetes; Ray Jobs submits work to that cluster;
> Ray Data processes the batch data inside that work.

---

## 11. Cross-cutting concerns

### 11.1 Config

Centralised in the `Settings` class (`api/src/app.py:100`), which is a
`pydantic-settings` `BaseSettings`. Env vars come from:

- `k8s/api/configmap.yaml` — non-secret values (`RAY_ADDRESS`,
  `RESULTS_DIR`, `MODEL_NAME`).
- `k8s/api/deployment.yaml:76-96` — `POSTGRES_URL` is assembled from
  the Postgres `Secret` at deploy time.

Secrets are typed as `SecretStr` so they never accidentally get logged
or serialised into responses.

### 11.2 Authentication

Static API key, `X-API-Key` header. `require_api_key`
(`api/src/app.py:588`) compares via `hmac.compare_digest` to defeat
timing side-channels, and returns `401` with
`WWW-Authenticate: APIKey` per RFC 7235 on failure.

### 11.3 Observability

- **Structured JSON logs** with a `ContextVar`-scoped `request_id` and
  `batch_id` on every line.
- **`X-Request-ID`** round-tripped by a middleware — clients can trace
  one request across all log lines.
- **`/metrics`** exposes Prometheus counters in a dedicated
  `CollectorRegistry`: `http_requests_total`, `batch_submitted_total`,
  `batch_terminal_total`, `rayjob_submit_failures_total`. The HTTP
  counter labels requests by the matched **route template** (e.g.
  `/v1/batches/{batch_id}`), not the literal path, to keep cardinality
  bounded.
- **`/health`** and **`/ready`** for liveness and readiness probes.

### 11.4 Tests (main branch only)

The `demo/single-file-version` branch has no tests by design — see
`BRANCH_NOTE.md`. On `main`, the split-module layout has 169 tests at
100% line + branch coverage, enforced by `--cov-fail-under=100` in CI.

---

## 12. Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| API pod crashes after writing `input.jsonl` but before submitting to Ray | On restart, poller sees `queued` rows with no Ray job id → re-submits | Automatic |
| Ray job crashes mid-generation | Top-level handler writes `_FAILED` → poller promotes row to `status="failed"` | Manual re-submit with same inputs |
| One prompt errors inside `__call__` | Error captured in that row's `error` field; other rows succeed | Client-side: inspect `error` column in `results.jsonl` |
| Postgres unavailable at startup | `lifespan` retries with backoff; `/ready` returns 503 until connected | Automatic |
| Ray head unreachable at submission time | `ray_submit_batch` raises; handler returns `503`, `rayjob_submit_failures_total` increments | Retry from the client |
| Disk full on PVC | `write_inputs_jsonl` raises → row rolled back → `500` to client | Ops: expand PVC |
| Pod OOMKilled mid-job | K8s restarts pod; job status surfaces as failed via Ray | Manual re-submit |

The `_SUCCESS` / `_FAILED` marker protocol is what makes most of this
recoverable without a distributed transaction. Files are idempotent:
re-running a batch simply overwrites them.

---

## 13. Mental-model cheat sheet

Pin this somewhere while you're reading the code:

```
FastAPI  = receptionist    (takes request, writes ledger row, dispatches)
Postgres = ledger          (small, queryable, durable)
PVC      = warehouse       (large files, filesystem handshake)
Ray head = dispatcher      (scheduler, Jobs API, NO compute)
Ray workers = factory      (model loaded once, prompts processed)
KubeRay  = factory foreman (creates and maintains the Ray cluster)
K8s      = building mgr    (keeps all pods alive, wires networking)
```

And the one-paragraph description to give at a whiteboard:

> This app exposes a FastAPI API for submitting LLM batches. When a batch
> arrives, the API validates it, authenticates it, writes the inputs to
> a shared PVC, stores a metadata row in Postgres, and submits a
> distributed job to a long-running Ray cluster managed by KubeRay on
> Kubernetes. Ray workers load the Qwen model once per actor, process
> the prompts in parallel using `ray.data.map_batches`, and write NDJSON
> results back to the PVC. A FastAPI background poller keeps Postgres
> status in sync with Ray, and clients later fetch status and stream
> results through the API.

---

## 14. Where to read next

Follow this order — web app first, worker platform second:

1. `api/src/app.py` top to bottom (this branch) — the FastAPI side in
   one file.
2. `k8s/api/deployment.yaml` — how the API pod is configured and
   connected to Postgres, Ray, and the PVC.
3. `k8s/storage/shared-pvc.yaml` + `k8s/postgres/` — the persistence
   layer.
4. `inference/jobs/batch_infer.py` — the Ray side: `QwenPredictor` and
   `run_batch`.
5. `k8s/raycluster/raycluster.yaml` — the declarative Ray cluster:
   head, workers, volumes, resources.
6. `docs/ARCHITECTURE.md` + `docs/TECHNICAL_REPORT.md` — design
   rationale, benchmark numbers, production considerations.

If you internalise steps 1–5, you can explain any request in this
system from the curl to the token, and that is the bar.
