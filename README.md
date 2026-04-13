# KubeRay Batch Inference

[![CI](https://github.com/Sebuliba-Adrian/kuberay-batch-inference/actions/workflows/ci.yaml/badge.svg)](https://github.com/Sebuliba-Adrian/kuberay-batch-inference/actions/workflows/ci.yaml)
[![Tests](https://img.shields.io/badge/tests-169_passing-brightgreen?style=flat-square)](api/tests)
[![Coverage](https://img.shields.io/badge/coverage-100%25_line_%2B_branch-brightgreen?style=flat-square)](docs/TECHNICAL_REPORT.md#3-evaluation-strategy-and-results)
[![Ubuntu](https://img.shields.io/badge/Ubuntu-22.04_CI_verified-E95420?style=flat-square&logo=ubuntu&logoColor=white)](.github/workflows/ci.yaml)
[![TDD](https://img.shields.io/badge/TDD-red→green→refactor-red?style=flat-square)](docs/TECHNICAL_REPORT.md#3-evaluation-strategy-and-results)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

[![Python](https://img.shields.io/badge/python-3.11-blue?style=flat-square&logo=python&logoColor=white)](api/pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Ray](https://img.shields.io/badge/Ray-2.54.1-028CF0?style=flat-square&logo=ray&logoColor=white)](https://docs.ray.io/)
[![KubeRay](https://img.shields.io/badge/KubeRay-1.6.0-326CE5?style=flat-square&logo=kubernetes&logoColor=white)](https://github.com/ray-project/kuberay)
[![Kubernetes](https://img.shields.io/badge/kubernetes-1.29-326CE5?style=flat-square&logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![Postgres](https://img.shields.io/badge/postgres-16-336791?style=flat-square&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/docker-multi--stage-2496ED?style=flat-square&logo=docker&logoColor=white)](api/Dockerfile)

Production-shaped reference implementation of a **distributed offline LLM batch inference service** built on [KubeRay](https://github.com/ray-project/kuberay), [Ray Data](https://docs.ray.io/en/latest/data/data.html), and [FastAPI](https://fastapi.tiangolo.com/). Target model: [`Qwen/Qwen2.5-0.5B-Instruct`](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct).

The service exposes an OpenAI-shaped Batches API at `POST /v1/batches`, authenticates with a static `X-API-Key` header, submits distributed inference jobs to a long-running [`RayCluster`](https://docs.ray.io/en/latest/cluster/kubernetes/getting-started/raycluster-quick-start.html) via the [Ray Jobs API](https://docs.ray.io/en/latest/cluster/running-applications/job-submission/index.html), stores job metadata in PostgreSQL, and returns results as streamed `application/x-ndjson` read back from a shared PVC.

## Status

| What | State |
|---|---|
| **Tests** | 169 passing, 100% line + branch coverage on 464 statements / 78 branches |
| **CI** | Green on `ubuntu-22.04` runner (lint + typecheck + test + kubeconform + docker build) |
| **Runtime** | End-to-end verified on a real kind + KubeRay cluster - Ray Data pipeline producing real `Qwen2.5-0.5B` inference from the exact curl in the exercise PDF |
| **Spec compliance** | Ubuntu 22.04 requirement verified empirically by the CI runner on every commit |
| **TDD discipline** | Every line in `api/src/` driven by a failing test first. `--cov-fail-under=100` gate enforced in CI. |

## Getting Started

A full walkthrough from a clean laptop to real Qwen output in your terminal. For more detail, exhaustive troubleshooting, and the WSL variant, see [`docs/SETUP.md`](docs/SETUP.md).

### 1. Prerequisites

Target environment: **Ubuntu 22.04** (per the exercise spec). Ubuntu 24.04, WSL2 (Ubuntu 22.04 or 24.04), and macOS with Docker Desktop all work too.

| Tool | Min version | Why |
|---|---|---|
| Docker | 24+ | Container runtime for kind |
| kind | 0.24+ | Local single-node Kubernetes cluster |
| kubectl | 1.29+ | Kubernetes CLI |
| Helm | 3.15+ | Installs the KubeRay operator |
| Python | 3.11 | API runtime (3.12 also works; `uv` pins 3.11 in CI) |
| Make | any | Orchestration |
| jq | any | Pretty-prints JSON in smoke-test.sh |

**Hardware:** at least **8 CPU cores** and **16 GiB RAM free**. The Ray head + 2 workers alone reserve 5 CPU / 13 GiB.

If you don't have any of these yet, skip to step 3 - the setup script installs everything.

### 2. Clone the repo

```bash
git clone https://github.com/Sebuliba-Adrian/kuberay-batch-inference.git
cd kuberay-batch-inference
```

### 3. One-shot install on a fresh Ubuntu host

```bash
bash scripts/setup.sh
```

Idempotent. Installs Docker, kind, kubectl, Helm, uv, and jq. Asks for `sudo` once. If Docker was freshly installed, log out and back in (or `newgrp docker`) so your user picks up the `docker` group without `sudo`.

Verify:

```bash
docker ps                   # should work without sudo
kind --version              # 0.24+
kubectl version --client    # 1.29+
helm version                # 3.15+
python3 --version           # 3.11+
```

### 4. Bring up the full stack

One command boots everything - kind cluster, KubeRay operator, RayCluster, Postgres, shared PVC, API, and port-forward.

```bash
make up
```

First run takes **~5-10 minutes**. Most of that is the Ray worker image build, which bakes Qwen2.5-0.5B-Instruct into the container so runtime never depends on Hugging Face being reachable. Subsequent `make up` runs reuse the existing cluster and skip steps that already succeeded.

Verify the stack is healthy:

```bash
kubectl -n ray get pods
# NAME                             READY   STATUS    RESTARTS   AGE
# batch-api-xxxxxxxxxx-xxxxx       1/1     Running   0          2m
# postgres-xxxxxxxxxx-xxxxx        1/1     Running   0          2m
# qwen-raycluster-head-xxxxx       1/1     Running   0          2m
# qwen-raycluster-worker-xxxxx     1/1     Running   0          2m
# qwen-raycluster-worker-xxxxx     1/1     Running   0          2m

curl http://localhost:8000/health
# {"status":"ok"}

curl http://localhost:8000/ready | jq .
# {
#   "status": "ok",
#   "checks": { "postgres": "ok", "ray": "ok" }
# }
```

### 5. Submit the exact exercise-PDF curl

Export the API key (default value - override via `API_KEY` env var on the Deployment for real use):

```bash
export API_KEY="demo-api-key-change-me-in-production"
```

Submit:

```bash
curl -X POST http://localhost:8000/v1/batches \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "input": [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}],
    "max_tokens": 50
  }' | jq .
```

Response:

```json
{
  "id": "batch_01JABCD...",
  "object": "batch",
  "endpoint": "/v1/batches",
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "status": "queued",
  "created_at": 1744380000,
  "request_counts": {"total": 2, "completed": 0, "failed": 0}
}
```

Save the id:

```bash
BATCH=batch_01JABCD...
```

### 6. Verify authentication

```bash
# Missing header returns 401
curl -i -X POST http://localhost:8000/v1/batches \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","input":[{"prompt":"x"}],"max_tokens":5}'

# Wrong key returns 401
curl -i -X POST http://localhost:8000/v1/batches \
  -H "Content-Type: application/json" \
  -H "X-API-Key: wrong-key" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","input":[{"prompt":"x"}],"max_tokens":5}'
```

Both return `401 Unauthorized` with `WWW-Authenticate: APIKey` per RFC 7235.

### 7. Poll status

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/v1/batches/$BATCH | jq .
```

You'll see `status` progress: `queued` → `in_progress` → `completed`. The 5 s background poller drives the transitions; the GET handler reads Postgres only, never Ray directly, so status is fast and available even if Ray blinks.

### 8. Fetch results

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/v1/batches/$BATCH/results
```

Streamed as `application/x-ndjson` (one JSON object per line). These are **real Qwen2.5-0.5B-Instruct generations captured from the live end-to-end run** - not mocks:

```
{"id":"0","prompt":"What is 2+2?","response":"The answer to 2 + 2 is 4. This is a simple addition problem...","finish_reason":"stop","prompt_tokens":6,"completion_tokens":24,"error":null}
{"id":"1","prompt":"Hello world","response":"Hello! How can I help you today?","finish_reason":"stop","prompt_tokens":5,"completion_tokens":8,"error":null}
```

### 9. One-command end-to-end smoke test

The everything-at-once verification: runs the exact spec curl, both auth negatives, polls until terminal, fetches results:

```bash
make smoke-test
```

### 10. Observability: `/metrics` and structured logs

The FastAPI proxy exposes Prometheus metrics at `/metrics` (no auth required, cheap to scrape):

```bash
curl -s http://localhost:8000/metrics | grep -E "^(http_requests_total|batch_submitted_total|batch_terminal_total)" | head
```

You get:

- `http_requests_total{method,path,status}` - HTTP request counter
- `http_request_duration_seconds{method,path}` - request latency histogram
- `batch_submitted_total{model}` - batches accepted
- `batch_terminal_total{model,status}` - batches that reached a terminal state
- `rayjob_submit_failures_total{reason}` - Ray submit_job errors

Every response carries an `X-Request-ID` header (echoed if the client supplied one, otherwise a `uuid4().hex` is generated) and logs are emitted as one-JSON-object-per-line with `request_id` and `batch_id` context attached.

### 11. Benchmark

A scripted load test that submits a 15-prompt batch, polls until terminal, and reports submit latency, poll latency distribution, wall time, and throughput:

```bash
python scripts/benchmark.py
```

### 12. (Optional) Monitoring stack - Prometheus + Grafana + Ray dashboards

Only needed if you want live metrics panels during the demo.

```bash
make monitoring-up      # installs Prometheus + Grafana into the monitoring namespace
make grafana            # port-forwards Grafana to localhost:3000 (admin/admin)
```

Then refresh `http://localhost:8265/#/metrics` in the Ray dashboard - the Metrics tab now iframes Grafana panels.

### 13. Tear down

```bash
make down               # remove the app, keep the cluster for fast rebuilds
make cluster-down       # nuke the kind cluster entirely
```

### Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `make up` hangs at "Waiting for RayCluster to become ready" | Worker image build didn't finish / Qwen download slow | `kubectl -n ray describe rayclusters/qwen-raycluster` - check worker pod events |
| API pod crash-loops on boot | Postgres not ready yet | `kubectl -n ray rollout status deploy/postgres --timeout=180s` first |
| `401 Unauthorized` with correct key | `.env` file has a stale key; Deployment uses the Secret | Check `kubectl -n ray get secret batch-api-secret -o jsonpath='{.data.API_KEY}' \| base64 -d` |
| Ray worker `OOMKilled` | 5 GiB worker memory too low for your Qwen build | Edit `k8s/raycluster/raycluster.yaml` → worker resources → `memory: "7Gi"` |
| Nothing on `localhost:8000` | Port-forward not running | `make port-forward` in a separate terminal |

Full troubleshooting in [`docs/SETUP.md`](docs/SETUP.md) §7.

## Architecture

![System architecture](docs/images/01-architecture.png)

End-to-end topology inside a single kind cluster. The FastAPI proxy, Postgres, KubeRay-managed RayCluster, and optional Prometheus/Grafana sub-stack all share one PVC for batch JSONL.

![Request flow](docs/images/02-request-flow.png)

Numbered steps along both sides of the split: green (FastAPI) handles auth, persistence, and job submission; blue (Ray) runs the distributed inference and writes results back to the shared PVC.

![Batch lifecycle](docs/images/03-state-machine.png)

The five states of a Batch row, mapped to Ray `JobStatus`. All transitions are driven by the async background poller on a 5 s tick; the `POST` handler only writes the initial `queued` row.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the decision log, trade-offs, and answers to the exercise's five key questions.

## Components

| Component | Path | Tech |
|---|---|---|
| FastAPI proxy | `api/` | Python 3.11, FastAPI 0.115, SQLAlchemy 2.0 async, pydantic-settings v2, uv |
| Ray inference job | `inference/` | Ray 2.54.1, Ray Data `map_batches`, HuggingFace Transformers, Qwen2.5-0.5B |
| Ray cluster | `k8s/raycluster/` | KubeRay v1.6.0 `RayCluster` CRD (`ray.io/v1`), CPU-only, 1 head + 2 workers |
| Local Kubernetes | `k8s/kind/` | kind v0.27.0, k8s 1.29.4, NodePort 30800 for host access via `extraPortMappings` |
| Storage | `k8s/postgres/`, `k8s/storage/` | Postgres 16, RWX PVC backed by kind `hostPath` + `extraMounts` |
| Monitoring (optional) | `k8s/monitoring/`, `scripts/install-monitoring.sh` | Prometheus + Grafana via Helm, scraping Ray metrics on `:8080`, Ray's default dashboards auto-provisioned |
| Scripts | `scripts/` | bash automation for Ubuntu 22.04 / 24.04 |
| CI | `.github/workflows/ci.yaml` | ruff + mypy + pytest (`--cov-fail-under=100`) + kubeconform + docker buildx on `ubuntu-22.04` |

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - Decision log, architecture trade-offs, answers to the 5 key exercise questions
- [`docs/SETUP.md`](docs/SETUP.md) - End-to-end Ubuntu 22.04 setup from a fresh machine
- [`docs/API.md`](docs/API.md) - REST API reference with curl examples
- [`docs/TECHNICAL_REPORT.md`](docs/TECHNICAL_REPORT.md) - Full technical report (dataset analysis, method comparison, evaluation, production monitoring)

## Repository layout

```
kuberay-batch-inference/
├── README.md
├── LICENSE
├── Makefile                      # Single entry point for all ops
├── .env.example
├── .gitignore
├── .gitattributes                # LF line endings for shell, YAML, Dockerfile
├── docs/
│   ├── ARCHITECTURE.md           # Decision log, trade-offs, threat model
│   ├── SETUP.md                  # End-to-end Ubuntu walkthrough
│   ├── API.md                    # REST reference with curl examples
│   └── TECHNICAL_REPORT.md       # 5-question deep dive + monitoring plan
├── k8s/
│   ├── kind/kind-config.yaml     # single-node kind with extraPortMappings + extraMounts
│   ├── kuberay/values.yaml       # operator Helm values
│   ├── raycluster/raycluster.yaml  # RayCluster CRD + ServiceAccount + RBAC
│   ├── postgres/                 # deployment, service, pvc, secret, init-configmap
│   ├── storage/shared-pvc.yaml   # RWX PV+PVC backed by hostPath
│   └── api/                      # deployment, service, configmap, secret
├── api/
│   ├── Dockerfile                # Multi-stage build: uv venv → runtime, UID 10001
│   ├── pyproject.toml            # ruff, mypy, pytest config
│   ├── src/
│   │   ├── main.py               # FastAPI factory + lifespan wire-up
│   │   ├── config.py             # pydantic-settings (SecretStr API_KEY)
│   │   ├── auth.py               # X-API-Key dependency (hmac.compare_digest)
│   │   ├── models.py             # Pydantic v2 request/response schemas
│   │   ├── db.py                 # SQLAlchemy 2.0 async + Batch model
│   │   ├── ray_client.py         # Async wrapper around JobSubmissionClient
│   │   ├── storage.py            # JSONL read/write on shared PVC
│   │   ├── logging_config.py     # stdout formatter setup
│   │   └── routes/
│   │       ├── health.py         # /health liveness + /ready dependency probe
│   │       └── batches.py        # POST + GET status + GET results + poller
│   └── tests/                    # 15 test files, 169 cases, 100% line + branch
│       ├── conftest.py
│       ├── test_config.py
│       ├── test_auth.py
│       ├── test_models.py
│       ├── test_db.py
│       ├── test_storage.py
│       ├── test_ray_client.py
│       ├── test_main_health.py
│       ├── test_ready.py
│       ├── test_batches_post.py
│       ├── test_batches_get.py
│       ├── test_batches_results.py
│       ├── test_batches_internals.py
│       ├── test_status_poller.py
│       ├── test_lifespan.py
│       ├── test_e2e_happy_path.py
│       └── test_e2e_failure_paths.py
├── inference/
│   ├── Dockerfile                # Custom Ray worker: base + transformers + Qwen weights
│   ├── requirements.txt
│   └── jobs/
│       └── batch_infer.py        # Ray Data map_batches pipeline entrypoint
├── scripts/
│   ├── setup.sh                  # Fresh Ubuntu 22.04/24.04 → prereqs installed
│   ├── up.sh                     # Boot cluster + deploy everything (wraps make up)
│   ├── down.sh                   # Tear down stack (wraps make down)
│   └── smoke-test.sh             # The exact curl from the exercise PDF + auth checks
└── .github/
    └── workflows/
        └── ci.yaml               # runs-on: ubuntu-22.04 - lint + typecheck + test + kubeconform + docker
```

## License

MIT - see [`LICENSE`](LICENSE).
