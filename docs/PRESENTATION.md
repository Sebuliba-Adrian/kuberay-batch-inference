# Presentation — KubeRay Batch Inference

**Target length:** 30-45 minutes
**Format:** Slide deck + live demo + Q&A
**Audience:** Engineering team — DevOps, platform, AI infra

This file is a speaker-notes-style talk track, not the slide deck itself. Each section is one slide. Render as slides in Marp, Deckset, Google Slides, or anything that consumes Markdown.

---

## Slide 1 — Title

> # KubeRay Batch Inference
> ## Distributed offline LLM batch inference on KubeRay
> Adrian Sebuliba — [github.com/Sebuliba-Adrian/kuberay-batch-inference](https://github.com/Sebuliba-Adrian/kuberay-batch-inference)

**Speaker notes:** Set the frame in 30 seconds. I built a production-shaped KubeRay batch inference service for the exercise. The brief was Qwen2.5-0.5B behind a FastAPI `/v1/batches` endpoint — the interesting questions are in the architecture around that, not the model itself.

**Time budget: 1 min**

---

## Slide 2 — What the exercise asked for

- Deploy Qwen2.5-0.5B with KubeRay for **offline batch inference**
- Submit jobs via `POST /v1/batches` with an OpenAI-shaped payload
- Static API key auth via `X-API-Key` header → 401 on bad key
- Answer 5 architectural questions:
  1. How is the output served?
  2. Job management — status and results retrieval
  3. Load balancing — how Ray distributes work, what issues you see
  4. KPIs for production operations
  5. How well does KubeRay integrate with Kubernetes?

**Speaker notes:** I'm going to walk through how I built this, show a live demo, and spend most of the time on questions 3, 4, and 5 — the ones where the interesting trade-offs live.

**Time budget: 1 min**

---

## Slide 3 — The architecture, one picture

```text
  curl :8000         ┌──────────────────────── kind cluster ────────────────────────┐
  X-API-Key          │                                                              │
     │               │  ┌─────────────┐       ┌──────────────────────────────────┐  │
     ▼               │  │ PostgreSQL  │       │       KubeRay operator           │  │
  ┌─────────┐        │  │ (job meta)  │       │  ┌────────────────────────────┐  │  │
  │ FastAPI │────────┼─►│             │       │  │     RayCluster             │  │  │
  │ proxy   │        │  └─────────────┘       │  │  Head + 2 CPU workers      │  │  │
  │         │        │                        │  │  Ray Data map_batches      │  │  │
  │ async   │────────┼───► Ray Jobs API ──────┼─►│  Transformers + Qwen2.5    │  │  │
  │ poller  │        │                        │  └────────────────────────────┘  │  │
  └────┬────┘        │                        └──────────────────────────────────┘  │
       │             │                                        │                     │
       └─────────────┼─────── Shared PVC (RWX hostPath) ──────┘                     │
                     │   /data/batches/<id>/{input,results}.jsonl                   │
                     └──────────────────────────────────────────────────────────────┘
```

**Speaker notes:** Five layers. FastAPI is the API. Postgres is the metadata store — who submitted, when, what state. KubeRay runs the operator which owns a long-running RayCluster. Ray Data does the actual distributed inference. A shared PVC carries inputs and outputs between the API pod and the worker pods. And there's a background status poller inside the FastAPI process — I'll explain why that matters.

**Time budget: 2 min**

---

## Slide 4 — Decision 1: Ray Data, not Ray Core or Ray Serve

**Choice:** Ray Data (`ds.map_batches(Predictor, ...)`)

**Why:**
- Canonical 2025-2026 path for **offline** LLM batch inference (Ray docs + Anyscale)
- Streaming execution, row-level fault tolerance, actor-pool autoscaling, backpressure — all built in
- Ray Serve is for online inference. Ray Core is too low-level. Ray Data is the right level of abstraction.

**Rejected:**
- Ray Core + manual actors → reinvents what Ray Data gives you
- Ray Serve → wrong shape for batch
- Plain Kubernetes Jobs without Ray → loses parallel actor pools, streaming, fault tolerance

**Speaker notes:** I spent some time on this one because there's a lot of legacy code floating around that uses the older `ActorPoolStrategy` pattern. The current-day Ray Data story is very clean.

**Time budget: 2 min**

---

## Slide 5 — Decision 2: Transformers, not vLLM (on CPU)

**Choice:** HuggingFace Transformers + Torch CPU for the laptop-demo path.

**Why NOT vLLM on CPU:**
- vLLM CPU backend (`vllm-cpu`) exists but is fragile
- Default `VLLM_CPU_KVCACHE_SPACE` is too small — [vllm#29233](https://github.com/vllm-project/vllm/issues/29233)
- Qwen2.5-0.5B-specific cache block bug — [vllm#10439](https://github.com/vllm-project/vllm/issues/10439)
- Needs a second wheel, different env vars, breaks on some CPUs

**Why Transformers on CPU is fine:**
- Qwen2.5-0.5B is 0.5 B params — ~1 GB in BF16
- Modern laptop CPU handles it in ~3.5 GiB peak with zero special handling
- One import path, debugger-friendly

**Production upgrade path:** drop-in swap to `ray.data.llm.vLLMEngineProcessorConfig` + `build_llm_processor` on GPU — documented in `inference/jobs/batch_infer.py` as commented code.

**Speaker notes:** This is the "works on the grader's machine" call. vLLM gets you ~2× throughput on GPU, and it's the right production choice. But for the take-home's CPU target, Transformers is the pragmatic path.

**Time budget: 2 min**

---

## Slide 6 — Decision 3: long-running RayCluster + `JobSubmissionClient`

**Choice:** One long-lived `RayCluster`. FastAPI submits via `ray.job_submission.JobSubmissionClient`, wrapped in `starlette.concurrency.run_in_threadpool` so it doesn't block the async event loop.

**Rejected alternative:** `RayJob` CRD creating a fresh cluster per request.

| Option | Cold-start latency | Fits an HTTP API? |
|---|---|---|
| RayJob CRD (cluster per job) | **60-120s** on kind | ❌ |
| Long-running RayCluster + JobSubmissionClient | **sub-second** | ✓ |

**Speaker notes:** This is the single most impactful decision for demo quality. If I'd used RayJob CRDs, the grader would POST the exercise curl and stare at a blank terminal for two minutes while a fresh cluster booted. Long-running cluster is what production batch platforms actually do — Anyscale, OpenAI Batches, AWS Bedrock Batch.

The trade-off is that the cluster always consumes ~6 CPU / 12 GiB RAM even when idle. On a real cloud you'd enable `enableInTreeAutoscaling: true` with a `(min, max)` bound.

**Time budget: 2 min**

---

## Slide 7 — Decision 4: background status poller

**Problem:** Querying Ray on every `GET /v1/batches/{id}` means:
- Every status check is 10-50 ms at best
- API hangs when Ray is down
- `/ready` and `/v1/batches/{id}` can't decouple

**Solution:** Background async task inside the FastAPI process.
- Every 5 s, sweep every batch not in a terminal state
- Fetch Ray status, map to OpenAI vocabulary, update Postgres row
- `GET /{id}` reads Postgres only — fast and resilient to Ray outages

**State machine:**

```
                                      ┌─ completed  (SUCCEEDED + read _SUCCESS marker)
  queued → in_progress → terminal ─►──┼─ failed     (FAILED + read _FAILED marker)
  ▲                                   └─ cancelled  (STOPPED)
  │
  POST /v1/batches
```

**Speaker notes:** This is the pattern I'd recommend for any HTTP-fronted batch system. The moment your GET queries a remote dependency on the hot path, you've coupled your availability to theirs. Decouple with a poller and the API stays fast and honest.

**Time budget: 2 min**

---

## Slide 8 — Live demo

Switch to the terminal. Run through:

```bash
# 1. Show the stack is up
kubectl -n ray get pods

# 2. Show Ray dashboard briefly
# (pre-opened browser tab at http://localhost:8265)

# 3. The exact exercise-PDF curl
curl -X POST http://localhost:8000/v1/batches \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "input": [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}],
    "max_tokens": 50
  }' | jq

# 4. Show the auth check — 401 on missing key
curl -i -X POST http://localhost:8000/v1/batches \
  -H "Content-Type: application/json" \
  -d '{"model":"x","input":[{"prompt":"x"}]}'

# 5. Poll status
curl http://localhost:8000/v1/batches/$BATCH_ID \
  -H "X-API-Key: $API_KEY" | jq

# 6. Fetch results
curl http://localhost:8000/v1/batches/$BATCH_ID/results \
  -H "X-API-Key: $API_KEY"
```

**Speaker notes:** Talk through each step as the output comes back. Highlight:
- 401 on the unauthenticated POST (with the WWW-Authenticate header)
- The ULID-based `batch_01JAB...` id
- The status transition from queued to in_progress to completed during polling
- The NDJSON streaming — one line per prompt, ordered

**Time budget: 5-8 min (longest single slide)**

---

## Slide 9 — What I tested, and how

| Layer | Tests | Coverage strategy |
|---|---|---|
| Unit — config, auth, models, db, storage, ray_client | 91 | Pure logic + fakes |
| Unit — routes (POST, GET, results, /ready) | 42 | httpx.ASGITransport + full dependency graph |
| Unit — background poller | 16 | Every state transition + error paths + cross-platform branch guards |
| Lifespan wiring | 3 | Full boot + shutdown |
| **E2E — happy path** | 2 | POST → poll → GET → results, real lifespan, no mocks at handler |
| **E2E — failure paths** | 6 | Ray down, worker crash, /health still 200, /ready 503 |
| **Total** | **169** | **100% line + 100% branch on 464 statements, 78 branches** |

**Speaker notes:** This is the part I want to highlight most. The entire API layer was built **strict TDD** — failing test first, minimum impl to pass, refactor, then commit. Every line of `src/` exists because a test asked for it. CI runs on `ubuntu-22.04` with a `--cov-fail-under=100` gate — any regression fails the PR. And the full stack has been runtime-verified on a real kind + KubeRay cluster with real Qwen2.5-0.5B inference, not just mocked tests (see docs/TECHNICAL_REPORT.md §3.5).

**Time budget: 3 min**

---

## Slide 10 — Question 1: How is the output served?

**Answer:** `application/x-ndjson` streamed from `GET /v1/batches/{id}/results`.

One JSON object per input prompt, same order as input:

Real output captured from the live kind + KubeRay run:

```json
{"id":"0","prompt":"What is 2+2?","response":"The answer to 2 + 2 is 4. This is a simple addition problem...","finish_reason":"stop",...}
{"id":"1","prompt":"Hello world","response":"Hello! How can I help you today?","finish_reason":"stop",...}
```

**Storage:** `/data/batches/<batch_id>/results.jsonl` on a shared PVC, written by Ray workers, read by the API pod.

**Streaming = flat memory.** `StreamingResponse` + `aiofiles` = 100K rows, single-digit MB resident memory.

**Why NOT Postgres BLOB, `/tmp`, or in-memory:**
- `/tmp` is pod-local — not visible to other pods
- Postgres BLOB bloats the DB and makes pg_dump painful
- In-memory doesn't survive pod restart
- JSONL on PVC mirrors OpenAI Batches API real-world shape

**Time budget: 2 min**

---

## Slide 11 — Question 2: Job management

**Two endpoints:**

| | |
|---|---|
| `GET /v1/batches/{id}` | Status endpoint. Reads **Postgres only**. |
| `GET /v1/batches/{id}/results` | Streams NDJSON. 409 if not yet completed. |

**Why Postgres only for status?**
- Fast (single indexed SELECT)
- Resilient to Ray outages
- Background poller keeps the row fresh

**Who drives which transition:**

- `POST` handler → `queued` (on successful Ray submit) OR `failed` (on Ray error, returned as 503 to client)
- Background poller → `queued→in_progress→completed/failed/cancelled`
- Future: `DELETE /v1/batches/{id}` → `cancelled` (out of exercise scope)

**Time budget: 2 min**

---

## Slide 12 — Question 3: Load balancing (how Ray distributes work)

**How:** `ds.map_batches(QwenPredictor, concurrency=2, batch_size=8)`

1. Ray Data partitions the input into blocks (one per source JSON file, repartitioned to `2× actors`)
2. Actor pool of 2 long-lived actors — one per Ray worker pod
3. Each actor loads Qwen **once** in `__init__`, runs inference on successive blocks in `__call__`
4. Blocks stream through with automatic backpressure
5. Results materialize block-by-block into the output Dataset

**Issues I see (and mitigations):**

| Issue | Mitigation |
|---|---|
| Cold-start dominates small batches | Long-running cluster (done), caller-side batching |
| Straggler tail latency | Smaller `batch_size`, per-prompt timeout |
| Memory skew on variable-length prompts | Length bucketing, cap `max_model_len` to realistic p95 |
| No auto-redistribute on worker OOM | Generous memory limits, actor `max_restarts`, OOMKilled alerts |
| Actor pool not aware of queue depth | Kueue in front for multi-tenant |

**Time budget: 3 min**

---

## Slide 13 — Question 4: KPIs for production operations

**Four layers, alert on the ones in bold:**

**SLI / SLO layer:**
- **Batch acceptance rate** (≥ 99.9%)
- **End-to-end batch success rate** (≥ 99%)
- Time-to-queue (p95 < 300 ms)
- Time-to-complete per prompt-count bucket

**Throughput layer:**
- Prompts/sec, tokens/sec (input + output)
- Actor utilization, GPU utilization (when on GPU)

**Reliability layer:**
- **Ray worker pod restart count**
- **RayCluster `status.state != ready` duration**
- **Postgres up**
- **Status poller sweep failure rate**

**Cost layer:**
- $/1M output tokens
- Cluster idle time

**Privacy-preserving rule:** metrics are **cardinality-limited and content-free**. Count of prompts, bytes in/out, latency histograms, token totals — **never prompt text, never user identifiers**. Logging a prompt in a span attribute or a Prometheus label is a compliance incident.

**Speaker notes:** The privacy note is the one that matters most for any real workload handling user data. Easy to forget, hard to fix retroactively.

**Time budget: 3 min**

---

## Slide 14 — Question 5: KubeRay ↔ Kubernetes integration

**Strengths:**
- CRD-native lifecycle — `kubectl get rayclusters` works
- Auto service discovery via `<name>-head-svc`
- RBAC integration (v1.6 maps K8s RBAC to Ray token auth)
- Autoscaling via K8s primitives + cluster autoscaler
- Pod-shaped workloads — every K8s scheduling primitive available
- Prometheus metrics on `:8080` + standard observability stack

**Limitations:**
- Operator is a single point of failure for reconciliation (default chart replicas=1)
- RayJob CRD cold-start is 60-120 s on kind — forces long-running cluster pattern for HTTP APIs
- kind-specific friction: no native LoadBalancer, need `kind load docker-image`, RWO-only default storage
- Narrow version compatibility: KubeRay v1.6 needs Ray ≥ 2.38, Ray 2.11-2.37 has a dashboard hang bug
- No native batch queueing — Kueue in front for multi-tenant
- Limited tenancy / quota — need an admission webhook or API-layer quota

**Speaker notes:** The honest verdict: KubeRay is the right choice for Ray on Kubernetes, but it's not a complete batch platform. You still need Kueue for multi-tenant queueing, and you still need to own the HTTP API layer yourself.

**Time budget: 3 min**

---

## Slide 15 — Production readiness checklist (what's NOT in this build)

**Intentionally out of scope for the take-home:**

- Per-tenant API keys stored hashed, with rotation
- mTLS between proxy and Ray head
- Ray token auth (`RAY_AUTH_TOKEN`) + K8s RBAC mapping
- Input/output content filtering (prompt injection, PII redaction)
- Per-tenant rate limiting
- Structured audit logging with field-level encryption
- EFS / Azure Files / S3 replacing hostPath PVC
- Horizontal pod autoscaler on the API
- RayCluster autoscaling with `(min, max)` bounds
- Kueue for multi-tenant batch queueing
- OpenTelemetry distributed tracing
- Load tests (k6/locust) with SLO gates
- Chaos tests — kill a worker mid-batch, disconnect Postgres, poison a prompt
- Secret manager (Vault, AWS SM, sealed-secrets, SOPS) replacing the committed k8s Secret

**Speaker notes:** Important to name these so it's clear I know what's missing. The exercise is a demonstration of architecture and discipline, not a finished product.

**Time budget: 1 min**

---

## Slide 16 — Repo layout recap

```
kuberay-batch-inference/
├── README.md                      # entry point
├── Makefile                       # single entry for every op
├── docs/
│   ├── ARCHITECTURE.md            # decision log + trade-offs
│   ├── SETUP.md                   # end-to-end Ubuntu walkthrough
│   ├── API.md                     # REST reference with curl examples
│   ├── TECHNICAL_REPORT.md        # 5-question deep dive + monitoring
│   └── PRESENTATION.md            # ← this file
├── api/                           # FastAPI proxy (169 tests, 100% line + branch cov)
│   ├── src/…
│   ├── tests/…
│   └── Dockerfile
├── inference/                     # Ray worker image + batch job
│   ├── jobs/batch_infer.py
│   └── Dockerfile
├── k8s/                           # manifests
│   ├── kind/kind-config.yaml
│   ├── kuberay/values.yaml
│   ├── raycluster/raycluster.yaml
│   ├── postgres/*.yaml
│   ├── storage/shared-pvc.yaml
│   └── api/*.yaml
├── scripts/                       # setup.sh, up.sh, down.sh, smoke-test.sh
└── .github/workflows/ci.yaml      # lint+typecheck+test+k8s+docker
```

**Time budget: 1 min**

---

## Slide 17 — Questions?

**Time budget: remainder (aim for 8-15 min Q&A)**

### Likely questions I'm ready for:

- **"Why not vLLM?"** → Slide 5 trade-off, documented GPU upgrade path.
- **"How would this handle 1M prompts in one batch?"** → Upper bound is enforced at the Pydantic layer (100K), I'd implement a fan-out layer in front that sharded into N sub-batches.
- **"What happens if Postgres goes down?"** → `/ready` flips to 503, API continues serving `/health` for liveness, no new batches can be accepted (submit would fail), existing status queries return a 503 from the SQL error path. Covered by `test_e2e_ray_unreachable_*` tests (Ray counterpart).
- **"How do you prevent abuse — someone submitting 10K giant prompts?"** → Per-tenant rate limiting with `slowapi` or Envoy. Hard cap at Pydantic layer is the first line.
- **"Why ULID and not UUID v4?"** → ULID is lexicographically sortable by creation time, so `ORDER BY id` = `ORDER BY created_at` for free. Same uniqueness guarantees.
- **"Why Transformers if you know vLLM is 2× faster?"** → CPU-only grader machine. vLLM CPU backend is fragile.
- **"Why the background poller and not real-time status?"** → Decouples API availability from Ray availability. Hot path is Postgres only.
- **"How do you test async code reliably?"** → `pytest-asyncio` with `asyncio_mode=auto`, `httpx.ASGITransport`, in-memory `aiosqlite`, a fake Ray factory seam. Every layer is isolatable.
- **"What about the lifespan coverage gap?"** → Real coverage.py tracing quirk for sync code after the last `await`. I restructured the shutdown helper to interleave sync and async calls so every line is trackable, and dropped a superfluous final log line rather than paper over the gap with `# pragma: no cover`.

---

## Pre-demo checklist

Before the session starts:

- [ ] `make up` is running and stable
- [ ] `make port-forward` is running in terminal 1
- [ ] `make dashboard` is running in terminal 2 with the browser pre-opened at `http://localhost:8265`
- [ ] Terminal 3 is at the repo root with `API_KEY` exported: `export API_KEY=$(grep API_KEY .env | cut -d= -f2)`
- [ ] The exact curl from slide 8 is in the shell history (up-arrow should find it)
- [ ] The GitHub repo is loaded in a browser tab for post-demo exploration
- [ ] `docs/TECHNICAL_REPORT.md` is open in a tab for deep-dive references
- [ ] Screen sharing tested, font size bumped to ~16pt for terminal readability
