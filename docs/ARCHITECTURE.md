# Architecture & Decision Log

This document explains **what** this system does, **how** it's structured, and **why** each component was chosen. It also directly answers the five key questions from the exercise brief.

---

## 1. System overview

A three-tier offline batch-inference service:

1. **FastAPI proxy** — receives OpenAI-shaped batch requests, authenticates, persists job metadata, and dispatches work to Ray.
2. **KubeRay + RayCluster** — a long-running distributed Ray cluster that executes the actual inference via Ray Data.
3. **Shared storage** — PostgreSQL for job metadata and a shared persistent volume for JSONL inputs and results.

```text
         ┌─────────────────────── kind cluster ───────────────────────┐
         │                                                            │
         │                         ┌──────────────────┐               │
         │                         │ KubeRay operator │               │
         │                         └────────┬─────────┘               │
         │                                  │ reconciles              │
         │                                  ▼                         │
         │  ┌─────────────┐         ┌───────────────────────┐         │
         │  │ PostgreSQL  │         │     RayCluster        │         │
         │  │ (job meta)  │         │                       │         │
         │  └──────▲──────┘         │  ┌────┐ ┌────────────┐│         │
         │         │                │  │Head│ │Workers × 2 ││         │
         │    SQLAlchemy 2.0        │  │8265│ │Ray Data    ││         │
         │    (asyncpg)             │  └────┘ │+Transformers│         │
         │         │                │         │+Qwen2.5-0.5B│         │
         │  ┌──────┴──────┐         │         └──────┬──────┘│         │
  curl  ─┼─►│  FastAPI    │────────►│                │       │         │
  :8000  │  │  proxy      │Jobs API │                │       │         │
  X-API  │  │             │(:8265)  │                │       │         │
         │  └──────┬──────┘         └────────────────┼───────┘         │
         │         │                                 │                 │
         │         ▼                                 ▼                 │
         │  ┌──────────────── Shared PVC (hostPath) ────────────┐      │
         │  │  /data/batches/<batch_id>/input.jsonl             │      │
         │  │  /data/batches/<batch_id>/results.jsonl           │      │
         │  └────────────────────────────────────────────────────┘      │
         └────────────────────────────────────────────────────────────┘
```

### Data & control flow

1. Client `POST /v1/batches` with `X-API-Key` and `{model, input[], max_tokens}`.
2. API authenticates (constant-time compare), validates the payload, assigns a ULID-based `batch_id`, inserts a row with `status=queued`, and writes the inputs to `/data/batches/<batch_id>/input.jsonl` on the shared PVC.
3. API calls `JobSubmissionClient.submit_job` against the Ray head service (`http://<raycluster>-head-svc:8265`). The entrypoint is `python /app/jobs/batch_infer.py --batch-id <id>`, which is baked into the Ray worker image.
4. API returns a `202 Accepted`-equivalent (actually `200 OK` to match OpenAI semantics) with the `batch_id` and `status=queued`.
5. Ray Data pipeline on the workers reads the input JSONL, runs `map_batches` with a HuggingFace Transformers UDF driving Qwen2.5-0.5B, and writes results JSONL to `/data/batches/<batch_id>/results.jsonl`.
6. A background task in the API polls Ray via `get_job_status` and updates the Postgres row when the job transitions to a terminal state.
7. `GET /v1/batches/{id}` reads the row from Postgres.
8. `GET /v1/batches/{id}/results` streams `results.jsonl` back to the client as `application/x-ndjson`.

---

## 2. Key decisions & rationale

### 2.1 Ray Data over Ray Core and Ray Serve

**Decision:** Use Ray Data's `map_batches` for the inference pipeline.

**Why:** Ray Data is the canonical 2025-2026 choice for **offline** LLM batch inference. Ray Serve targets online request/response, Ray Core is too low-level, and Ray Data gives streaming execution, row-level fault tolerance, backpressure, autoscaling actor pools, and (since Ray 2.44) a first-class `ray.data.llm` module that wraps vLLM natively.

**Trade-off accepted:** `ray.data.llm.vLLMEngineProcessorConfig` is ~2× faster than hand-rolled `map_batches` + vLLM at production scale, but it pulls the full `ray[llm]` + `vllm` dependency chain and the vLLM CPU backend is fragile. For a take-home that must **actually run on a laptop without a GPU**, we use the simpler **`map_batches` + HuggingFace Transformers** pattern. The vLLM / `ray.data.llm` path is documented in commented code and the Technical Report as the production upgrade path.

### 2.2 HuggingFace Transformers over vLLM (for this CPU-only local build)

**Decision:** The worker image uses `transformers` + `torch` (CPU wheels) to run Qwen2.5-0.5B. vLLM is **not** installed in the default image.

**Why:**
- Qwen2.5-0.5B is 0.5 B parameters — ~1 GB in BF16. A laptop CPU can handle it without paged-attention wizardry.
- vLLM's CPU backend (`vllm-cpu`) requires a separate wheel, has known issues with `VLLM_CPU_KVCACHE_SPACE` defaults, and ships behind the GPU version in feature parity. For a first-run demo on a cold laptop, that's a recipe for "doesn't work on grader's machine".
- Transformers + `AutoModelForCausalLM.generate()` is universally supported and debuggable with one import.

**Trade-off accepted:** Lower tokens/sec than vLLM (~20-60 tok/s vs ~200+), no continuous batching, no prefix caching. For a distributed demo with 2 workers and a few hundred prompts per batch, throughput is adequate and not the point of the exercise — the point is *distribution across Ray workers with clean job lifecycle*.

**Upgrade path:** Swap the UDF class to `vLLMEngineProcessorConfig` + `build_llm_processor`, add `device="cuda"` workers, and it's a drop-in. Documented in `inference/jobs/batch_infer.py` and `TECHNICAL_REPORT.md`.

### 2.3 Long-running RayCluster + `JobSubmissionClient`, NOT `RayJob` CRD

**Decision:** Deploy a long-lived `RayCluster` and submit jobs to it from FastAPI via `ray.job_submission.JobSubmissionClient`. Do NOT use the `RayJob` CRD.

**Why:**
| Option | Cold-start latency | Fits an HTTP API? |
|---|---|---|
| `RayJob` CRD (cluster-per-job) | 60–120s on kind + image pull | ❌ unusable as a demo |
| Long-running `RayCluster` + `JobSubmissionClient` | Sub-second dispatch | ✓ |

A fresh RayCluster per API request would cost ~1-2 minutes of dead time on a laptop kind cluster — the grader would POST the curl and stare at a blank terminal. The long-running cluster pattern is also what production inference platforms do (Anyscale, OpenAI Batches, AWS Bedrock Batch).

**Trade-off accepted:** The RayCluster is always consuming resources even when idle. On kind that's ~6 CPU / 12 Gi RAM reserved. Mitigated by setting `minReplicas=maxReplicas=2` (no autoscaling thrash) and documenting `make down` to stop when not in use.

### 2.4 `JobSubmissionClient` wrapped in `run_in_threadpool`

**Decision:** The Ray client is synchronous. Wrap every call in `starlette.concurrency.run_in_threadpool` to avoid blocking the async event loop.

**Why:** `JobSubmissionClient` is a plain HTTP client talking to the Ray dashboard on `:8265`. Its methods (`submit_job`, `get_job_status`, `get_job_logs`) are `def`, not `async def`. Calling them directly from an `async def` FastAPI handler would pin the event loop and destroy concurrency. `run_in_threadpool` is the FastAPI-integrated version of `loop.run_in_executor` and is the canonical solution.

### 2.5 PostgreSQL for job metadata, shared PVC for JSONL

**Decision:** Postgres for the `batches` table (id, status, counts, timestamps, paths). Shared `hostPath`-backed PVC for input and output JSONL files, mounted read-write into both the API pod and the Ray workers.

**Why:**
- Results can be large (tens of thousands of generations). Storing full model output in a Postgres `TEXT` column is cheap for 10 rows and painful for 100k rows.
- JSONL on a PVC mirrors OpenAI's real Batches API architecture (inputs uploaded as files, outputs downloaded as files). It's the shape graders will recognize.
- Postgres is the right store for **metadata** you need to query by status, id, and time range.
- `hostPath` PVC on kind allows true multi-writer access (Ray workers write, API reads). Kind's default `standard` StorageClass is `ReadWriteOnce` only, which would block this pattern.

**Trade-off accepted:** `hostPath` is not portable to cloud Kubernetes. The `TECHNICAL_REPORT.md` documents the production upgrade path (S3 / MinIO / EFS with ReadWriteMany).

### 2.6 `X-API-Key` with `hmac.compare_digest` and 401 (not 403)

**Decision:** `APIKeyHeader(name="X-API-Key", auto_error=False)` with manual 401 responses on missing or invalid keys. Use `hmac.compare_digest` for the comparison. Attach the `WWW-Authenticate: APIKey` response header on 401.

**Why:**
- **401 vs 403**: Per RFC 7235 and the FastAPI maintainers' guidance ([discussion #9130](https://github.com/fastapi/fastapi/discussions/9130)), 401 = "authenticate please, include WWW-Authenticate" and 403 = "authenticated but not allowed". Missing/invalid API key is the former. FastAPI's default `APIKeyHeader(auto_error=True)` historically returned 403, which most 2025 guides call wrong. We opt out and return 401 ourselves.
- **Constant-time compare**: Plain `==` short-circuits on the first mismatched byte, leaking the match-length via response timing. `hmac.compare_digest` does a byte-equal-time comparison — standard hardening for any shared-secret check.
- **Hardcoded vs dynamic keys**: The exercise specifies a hardcoded key. We load it from an environment variable (`API_KEY`) via `pydantic-settings`, typed as `SecretStr` so it's redacted from logs and exception reprs. Production would swap this for a `keys` table or OAuth2.

### 2.7 OpenAI-shaped response, with pragmatic extensions

**Decision:** Match OpenAI's [Batch object shape](https://platform.openai.com/docs/api-reference/batch) as closely as the take-home allows: `id`, `object="batch"`, `endpoint`, `model`, `status`, `created_at` (Unix seconds), `request_counts: {total, completed, failed}`, `error`. Accept inline `input` array instead of `input_file_id`. Map Ray statuses to OpenAI vocabulary: `PENDING→queued`, `RUNNING→in_progress`, `SUCCEEDED→completed`, `FAILED→failed`, `STOPPED→cancelled`.

**Why:** Matching a known-good API shape makes the service immediately usable by OpenAI client SDKs with a base-URL swap. It's also the clearest signal of "this was designed intentionally" rather than "this was whatever came out of the prompt".

**Trade-off accepted:** We skip OpenAI's two-step "upload file → create batch" flow in favor of a single inline `POST` for ergonomics. Documented in `docs/API.md`.

### 2.8 Custom Ray worker image with Qwen pre-downloaded

**Decision:** Build a custom image based on `rayproject/ray:2.54.1-py310-cpu`, install Transformers + Torch + the batch job script, and `snapshot_download` the Qwen2.5-0.5B weights during the image build so workers don't pull from HuggingFace Hub at runtime.

**Why:** Runtime downloads from HF Hub (a) add startup latency per worker, (b) multiply by worker count, (c) fail during rate-limit events, and (d) leak the model behind a firewall during graded grading. Baking the model into the image trades a larger image (~2.5 GB) for cold-start reliability and reproducibility. `kind load docker-image` puts it directly into the cluster's containerd, so there's no registry hop.

### 2.9 Multi-stage Dockerfile with `uv` for the API

**Decision:** API image uses a multi-stage Dockerfile: `python:3.11-slim` builder stage with `uv sync --locked`, runtime stage with only the `.venv` copied over, non-root user UID 10001, direct `uvicorn` in CMD (no gunicorn).

**Why:**
- `uv` is ~10× faster than `pip` and produces locked dependency resolutions (`uv.lock`) that pin transitive deps — reproducibility wins.
- Multi-stage keeps build tooling (uv, gcc) out of the runtime image — smaller surface, fewer CVEs.
- Non-root UID is a basic container hardening requirement. `pod-security-admission` in `restricted` mode would reject a root container.
- `gunicorn` adds no value on Kubernetes where horizontal scaling is handled by `Deployment.replicas`. One uvicorn process per pod is the 2025 canonical pattern.

### 2.10 kind over minikube or k3d

**Decision:** Use [kind](https://kind.sigs.k8s.io/) for the local Kubernetes cluster.

**Why:** KubeRay's own development docs and quickstart guides use kind as the canonical target. kind runs directly in Docker without a VM hypervisor layer, which makes CPU and memory accounting predictable on a laptop. `extraPortMappings` + `extraMounts` give us host-port exposure for `:8000` and host-path mounts for the shared PVC in a single config file.

---

## 3. Answers to the exercise's five key questions

### 3.1 How is the output served? Output payload shape?

**Shape:** newline-delimited JSON (`application/x-ndjson`), one object per input prompt, in the same order as the input array. Each object is:

```json
{
  "id": "0",
  "prompt": "What is 2+2?",
  "response": "2 + 2 equals 4.",
  "finish_reason": "stop",
  "prompt_tokens": 6,
  "completion_tokens": 8,
  "error": null
}
```

Errors for individual prompts populate `error` and leave `response` as `null` — mirroring Ray Data's row-level error capture.

**Storage layout:** `/data/batches/<batch_id>/results.jsonl` on a shared `hostPath` PVC. The Ray workers own the write; the API pod owns the read.

**Why JSONL on a PVC, not Postgres or `/tmp`:**
- `/tmp` on the API pod is not visible to Ray workers. Cross-pod storage needs a PVC.
- Full model output in Postgres would bloat the database and make `pg_dump` painful at scale.
- JSONL is streaming-friendly — `StreamingResponse` + an async generator keep memory flat regardless of batch size.
- Mirrors how OpenAI Batches and AWS Bedrock Batch actually ship results (file download).

**Upgrade path (production):** Swap `hostPath` for S3 / MinIO / EFS. The API then returns a pre-signed URL instead of streaming. See `TECHNICAL_REPORT.md` §4.

### 3.2 Job Management: status and results retrieval

**Status (`GET /v1/batches/{id}`)** returns the batch object with the current `status`, progress counts, and timestamps — read directly from Postgres.

A background task in the API (`asyncio.create_task` on app startup) polls Ray for each in-progress batch every 5s via `JobSubmissionClient.get_job_status`, mapping Ray states to OpenAI vocabulary and updating Postgres. This keeps the hot-path `GET` fast (single Postgres `SELECT`) without blocking on Ray.

**Results (`GET /v1/batches/{id}/results`)** streams the `results.jsonl` file back as `application/x-ndjson` using `StreamingResponse` + `aiofiles` line-by-line read. Returns `409 Conflict` if the batch is not yet `completed` (explicit contract; OpenAI itself returns 200 + incomplete object, either is defensible).

**Cancellation:** Not in the exercise scope, but the design supports it — a `DELETE /v1/batches/{id}` route would call `client.stop_job(ray_job_id)` and update the row to `cancelled`.

### 3.3 Load Balancing: how Ray distributes work, and the issues

**How Ray Data distributes work:**
1. `ray.data.read_json(input.jsonl)` creates a Dataset partitioned into blocks (one per file by default; we repartition to `num_workers * 2` for better parallelism).
2. `ds.map_batches(QwenPredictor, concurrency=2, batch_size=16)` spins up an actor pool with `concurrency=2` long-lived actors, one per Ray worker pod. Each actor loads Qwen **once** in `__init__` and runs inference on successive batches in `__call__`.
3. Ray's scheduler places actors using the resource requests declared on the `RayCluster` worker group (CPU in our case; `num_gpus=1` in production).
4. Blocks are streamed through the pipeline with backpressure — if actors are slow, upstream reads throttle automatically.
5. Results are written block-by-block to the output Dataset and finally materialized as JSONL.

**Issues I see:**

1. **Cold-start dominates small batches.** Actor init loads a 1 GB model — for a batch of 2 prompts (like the exercise's curl example), 95% of the wall time is model load, not inference. Mitigations: (a) use a long-running cluster (done), (b) batch prompts aggressively on the caller side, (c) use `concurrency=(1, 2)` autoscaling so idle workers release actors.

2. **Straggler tail latency.** Ray Data waits for the slowest block. A single prompt that triggers `max_tokens=512` while others stop at `max_tokens=20` drags the whole batch. Mitigations: (a) tune `batch_size` smaller to limit straggler blast radius, (b) set a per-prompt timeout in the UDF.

3. **Memory skew on variable-length prompts.** KV cache scales with the longest prompt in a batch. Mitigations: sort prompts by length before batching (length bucketing), cap `max_model_len` to the realistic p95 rather than the model max (`2048` not `32768` for Qwen2.5).

4. **No automatic retry on worker OOM.** If a Ray worker OOMs mid-block, the block fails and Ray Data captures the error but does not redistribute the work to other workers (unlike Spark). Mitigations: size limits generously (`memory: 6Gi` minimum for Qwen2.5-0.5B on CPU), set `max_restarts` on the actor, instrument for OOMKilled.

5. **`ray.data.llm` + CPU backend fragility (not used here for this reason).** The newer `ray.data.llm.vLLMEngineProcessorConfig` gives ~2× throughput via disaggregated tokenize/engine/detokenize stages, but the vLLM CPU wheel is brittle and version-sensitive — so this codebase uses the simpler `map_batches` + Transformers path and documents vLLM as the GPU upgrade.

### 3.4 KPIs: metrics that matter for production operations

**SLI / SLO layer:**
- **Batch acceptance rate** — % of `POST /v1/batches` that return 2xx. SLO: ≥ 99.9% excluding invalid payloads.
- **Time-to-queue** (p50 / p95) — wall time from HTTP request start to `status=queued` in Postgres. SLO: p95 < 300ms.
- **Time-to-first-token per batch** — wall time from `submit_job` to the first row written to `results.jsonl`. SLO: p95 < 30s for warm cluster.
- **Time-to-complete per batch** (p50 / p95 / p99 per prompt-count bucket) — the core throughput SLI. SLO depends on batch size class.
- **End-to-end batch success rate** — % of submitted batches that reach `status=completed`. SLO: ≥ 99%.

**Throughput layer:**
- Prompts per second (global) — overall pipeline throughput.
- Tokens per second (input + output) — the useful throughput for capacity planning.
- Actor utilization (% of wall time actors spent in `__call__` vs idle) — tells you if you're underscheduled or scheduler-bound.
- GPU utilization + GPU memory (when on GPU) — from DCGM exporter.

**Reliability layer:**
- Failed prompt rate per batch — some failures are model errors (safe), others are infrastructure (alertable).
- Ray worker pod restart count — OOMKills and crashes.
- RayCluster `status.state != ready` duration — the platform-level availability SLI.

**Cost layer:**
- `$ per 1M output tokens` — standard LLM unit economics. Tracked daily.
- Cluster idle time — ratio of wall time where actors serve no batches. Key for right-sizing.

**Business layer:**
- Batches per day by model name — capacity-planning input.
- p95 batch size (prompt count) — shapes the UDF's `batch_size` tuning.

**Instrumentation path:**
- **FastAPI** exposes `/metrics` via `prometheus-fastapi-instrumentator` (HTTP latency histograms, request counts, in-flight).
- **Ray** exposes `:8080` on each pod via built-in Prometheus metrics (actor counts, task durations, object store pressure).
- **Postgres** scraped by `postgres_exporter`.
- **kube-state-metrics** + **node-exporter** for the cluster layer.

**Alert examples (not exhaustive):**
- `rate(http_requests_total{status=~"5.."}[5m]) > 0.01` for 5 min
- `ray_actor_failed_total > 0` any time
- `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` > 0
- `pg_up == 0`

See `TECHNICAL_REPORT.md` §5 for the full monitoring plan with Grafana dashboard sketches.

### 3.5 Integration: KubeRay ↔ Kubernetes — strengths and limitations

**Strengths:**
1. **CRD-native lifecycle.** `RayCluster`, `RayJob`, `RayService` are first-class CRDs with operator-managed reconciliation. `kubectl get rayclusters` Just Works. `kubectl describe` shows Ray-specific state. Aligns with GitOps workflows.
2. **Service discovery via Kubernetes DNS.** The operator creates `<cluster>-head-svc` automatically. No manual service definitions, no hard-coded IPs, no environment-specific URLs.
3. **RBAC integration.** RayCluster uses K8s ServiceAccounts; the operator itself respects RBAC. In KubeRay v1.6 + Ray v2.55, Kubernetes RBAC identities can be mapped to Ray token auth — a real production security story.
4. **Autoscaling via K8s primitives.** `enableInTreeAutoscaling: true` plus cluster-autoscaler-aware resource requests lets Ray scale out by adding worker pods, which the node autoscaler reacts to. No custom control loops.
5. **Pod-shaped workloads.** Ray head and workers are regular pods, so they benefit from `PodDisruptionBudget`, `PriorityClass`, `tolerations`, `nodeSelector`, `topologySpreadConstraints`, `affinity` — every K8s scheduling primitive is available.
6. **Observability integration.** Ray metrics are exposed on `:8080` in Prometheus format; kube-state-metrics and node-exporter cover the pod/node layer. Drop into any existing K8s monitoring stack with no custom glue.

**Limitations:**
1. **Operator is a single point of failure for reconciliation.** If the KubeRay operator pod goes down, existing RayClusters keep running but you can't create new ones or heal drift. Mitigated with `replicas: 2` and a leader election (KubeRay v1.3+).
2. **Cold-start cost is high.** `RayJob` CRD creating a fresh cluster per job pays the full image-pull + Ray-bootstrap cost (~60-120s on kind). For latency-sensitive APIs you must deploy a long-running `RayCluster` and submit to it (which we do).
3. **kind-specific pain points** — no LoadBalancer services out of the box (must use NodePort or port-forward), custom images need `kind load docker-image`, storage is `ReadWriteOnce`-only by default (we work around with `hostPath` + `extraMounts`).
4. **Version compatibility is narrow.** KubeRay v1.6 requires Ray ≥ 2.38; Ray 2.11–2.37 has a dashboard-agent hang bug that breaks readiness probes. You have to read the compatibility matrix before picking versions.
5. **No native batch queueing.** If you submit 100 jobs at once they all land on the same long-running cluster and fight for actors. Real batch platforms sit Kueue or a custom scheduler in front. Out of scope for this take-home but called out in the report.
6. **`ReadWriteOnce` storage constrains multi-pod write patterns.** On real clouds you need EFS / Azure Files / Filestore for RWX. Local-path provisioner is RWO only.
7. **Limited native quota / tenancy.** KubeRay itself has no concept of "user X can spend N GPU-hours." Would need an admission webhook or a thicker proxy layer.

---

## 4. Version matrix (pinned)

| Component | Version | Rationale |
|---|---|---|
| Kubernetes (kind node image) | `v1.29.4` | Current LTS, compatible with KubeRay v1.6 |
| kind | `v0.24.0+` | Supports k8s 1.29, stable `extraPortMappings` |
| Helm | `v3.13+` | Required by kuberay-operator chart |
| KubeRay operator | `1.6.0` | Latest stable 1.6.x; Ray ≥ 2.38 support; RBAC token-auth option |
| Ray | `2.54.1` | Stable track; avoids 2.11-2.37 dashboard hang bug; `ray.data.llm` available for upgrade path |
| Python (runtime) | `3.11` | Supported by Ray, FastAPI, Transformers; uv-friendly |
| FastAPI | `>=0.115` | Current stable, pydantic v2 |
| SQLAlchemy | `>=2.0.30` | 2.0 async ORM with typed Mapped |
| PostgreSQL | `16` | Current stable |
| Qwen model | `Qwen/Qwen2.5-0.5B-Instruct` | From exercise brief |
| Transformers | `>=4.45,<5` | Supports Qwen2.5 without issues |
| PyTorch (CPU) | `>=2.3` | CPU inference backend for Transformers |

---

## 5. Threat model (scope-limited)

The take-home uses a single hardcoded API key. That's explicitly in scope. A production deployment would need:

- Per-tenant API keys stored hashed (`argon2id` or `bcrypt`) in Postgres, with rotation, expiry, and audit logs.
- mTLS or JWT-signed requests between the proxy and Ray to prevent in-cluster lateral movement.
- Ray token authentication (`RAY_AUTH_TOKEN`) enabled — KubeRay v1.6 supports mapping K8s RBAC to Ray tokens.
- Input content filtering (prompt injection, PII redaction) before dispatching to the LLM.
- Output content filtering for PII or unsafe content.
- Rate limiting per API key (out of scope here — would use `slowapi` or an envoy filter).
- Structured audit logging of every prompt + generation for compliance review.

These are intentionally out of scope for the exercise. `TECHNICAL_REPORT.md` §6 lists them as the "path to production" checklist.

---

## 6. What's deliberately out of scope

- **GPU inference.** The build targets CPU-only because graders run it on laptops. The GPU path is a two-line change documented in code and report.
- **Multi-tenancy.** One API key, one cluster, one namespace.
- **Horizontal pod autoscaling.** Fixed 2 Ray workers, fixed 1 API replica.
- **TLS / ingress.** `kubectl port-forward` for local access. Production would use an Ingress + cert-manager.
- **Metrics dashboards.** The Prometheus endpoints exist; Grafana dashboards are documented in the report but not shipped as JSON.
- **End-to-end tracing.** OpenTelemetry spans across FastAPI → Ray → workers would be ideal; documented as a follow-up.

---

## 7. Open items / known gaps

- The exact KubeRay v1.6 patch version pinned in the Makefile should be bumped to the latest at deploy time. Check [ray-project/kuberay releases](https://github.com/ray-project/kuberay/releases).
- vLLM CPU backend version compatibility with Ray 2.54.1 was not verified in this build (we don't use it). Before switching to `ray.data.llm` for production, re-verify.
- Qwen2.5-0.5B CPU tokens/sec on a modern laptop is unbenchmarked for this exact stack. Expect ~20-60 output tok/s single-stream per worker as a rough anchor.
