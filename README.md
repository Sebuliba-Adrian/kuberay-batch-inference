# KubeRay Batch Inference

[![CI](https://github.com/Sebuliba-Adrian/kuberay-batch-inference/actions/workflows/ci.yaml/badge.svg)](https://github.com/Sebuliba-Adrian/kuberay-batch-inference/actions/workflows/ci.yaml)
[![Tests](https://img.shields.io/badge/tests-166_passing-brightgreen?style=flat-square)](api/tests)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen?style=flat-square)](docs/TECHNICAL_REPORT.md#3-evaluation-strategy-and-results)
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

## Quick Demo

```bash
# 1. One-shot bring-up: kind cluster + KubeRay + Ray cluster + Postgres + API
make up

# 2. Submit a batch (this is the exact call from the exercise PDF)
curl -X POST http://localhost:8000/v1/batches \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $(cat .env | grep API_KEY | cut -d= -f2)" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "input": [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}],
    "max_tokens": 50
  }'
# -> 200 OK
# {
#   "id": "batch_01JABCD...",
#   "object": "batch",
#   "endpoint": "/v1/batches",
#   "model": "Qwen/Qwen2.5-0.5B-Instruct",
#   "status": "queued",
#   "created_at": 1744380000,
#   "request_counts": {"total": 2, "completed": 0, "failed": 0}
# }

# 3. Poll status
curl http://localhost:8000/v1/batches/batch_01JABCD... \
  -H "X-API-Key: $API_KEY"

# 4. Stream results (newline-delimited JSON)
curl http://localhost:8000/v1/batches/batch_01JABCD.../results \
  -H "X-API-Key: $API_KEY"
# {"id":"0","prompt":"What is 2+2?","response":"2+2 equals 4.","finish_reason":"stop"}
# {"id":"1","prompt":"Hello world","response":"Hello! How can I help...","finish_reason":"stop"}
```

## Architecture

```text
          ┌──────────────────────── kind cluster ────────────────────────┐
          │                                                              │
          │  ┌─────────────┐       ┌──────────────────────────────────┐  │
          │  │ PostgreSQL  │       │       KubeRay operator           │  │
          │  │ (job meta)  │       │  ┌────────────────────────────┐  │  │
          │  └──────▲──────┘       │  │     RayCluster             │  │  │
          │         │              │  │  ┌──────┐   ┌────────────┐ │  │  │
  curl    │  ┌──────┴──────┐       │  │  │ Head │   │ Workers x2 │ │  │  │
  :8000 ──┼─►│   FastAPI   │──────►│  │  │ 8265 │   │ Ray Data + │ │  │  │
  X-API   │  │   proxy     │ Jobs  │  │  │      │   │ Transformers │  │  │
          │  └──────┬──────┘  API  │  │  └──────┘   └──────┬─────┘ │  │  │
          │         │              │  └─────────────────────┼──────┘  │  │
          │         │              └────────────────────────┼─────────┘  │
          │         ▼                                       ▼            │
          │  ┌─────────────────── Shared PVC (RWX) ──────────────────┐   │
          │  │  /data/batches/<id>/input.jsonl                       │   │
          │  │  /data/batches/<id>/results.jsonl                     │   │
          │  └────────────────────────────────────────────────────────┘   │
          └──────────────────────────────────────────────────────────────┘
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the decision log, trade-offs, and answers to the exercise's five key questions.

## Components

| Component | Path | Tech |
|---|---|---|
| FastAPI proxy | `api/` | Python 3.11, FastAPI, SQLAlchemy 2.0 async, pydantic-settings, uv |
| Ray inference job | `inference/` | Ray 2.54.1, Ray Data `map_batches`, HuggingFace Transformers, Qwen2.5-0.5B |
| Ray cluster | `k8s/raycluster/` | KubeRay v1.6 `RayCluster` CRD, CPU-only, 1 head + 2 workers |
| Local Kubernetes | `k8s/kind/` | kind v0.24+, k8s 1.29, NodePort for host access |
| Storage | `k8s/postgres/`, `k8s/storage/` | Postgres 16, PVC (hostPath-backed on kind) |
| Scripts | `scripts/` | bash automation for Ubuntu 22.04 |

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — Decision log, architecture trade-offs, answers to the 5 key exercise questions
- [`docs/SETUP.md`](docs/SETUP.md) — End-to-end Ubuntu 22.04 setup from a fresh machine
- [`docs/API.md`](docs/API.md) — REST API reference with curl examples
- [`docs/TECHNICAL_REPORT.md`](docs/TECHNICAL_REPORT.md) — Full technical report (dataset analysis, method comparison, evaluation, production monitoring)
- [`docs/PRESENTATION.md`](docs/PRESENTATION.md) — Talk track for the 30-45 min demo

## Repository layout

```
kuberay-batch-inference/
├── README.md
├── LICENSE
├── Makefile                    # Single entry point for all ops
├── .env.example
├── .gitignore
├── docs/
│   ├── ARCHITECTURE.md
│   ├── SETUP.md
│   ├── API.md
│   ├── TECHNICAL_REPORT.md
│   └── PRESENTATION.md
├── k8s/
│   ├── kind/kind-config.yaml
│   ├── kuberay/values.yaml
│   ├── raycluster/raycluster.yaml
│   ├── postgres/{deployment,service,pvc,init-configmap}.yaml
│   ├── storage/shared-pvc.yaml
│   └── api/{deployment,service,configmap,secret.example}.yaml
├── api/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/
│   │   ├── main.py             # FastAPI entrypoint + lifespan
│   │   ├── config.py           # pydantic-settings
│   │   ├── auth.py             # X-API-Key dependency
│   │   ├── models.py           # Pydantic request/response
│   │   ├── db.py               # SQLAlchemy async engine + Batch model
│   │   ├── ray_client.py       # JobSubmissionClient wrapper
│   │   ├── storage.py          # JSONL read/write on shared PVC
│   │   └── routes/
│   │       ├── health.py
│   │       └── batches.py
│   └── tests/
│       ├── conftest.py
│       ├── test_auth.py
│       ├── test_models.py
│       ├── test_batches.py
│       └── test_health.py
├── inference/
│   ├── Dockerfile              # Custom Ray worker image
│   ├── requirements.txt
│   └── jobs/
│       └── batch_infer.py      # Ray Data pipeline entrypoint
├── scripts/
│   ├── setup.sh                # Fresh Ubuntu 22.04 → prereqs installed
│   ├── up.sh                   # Boot cluster + deploy everything
│   ├── down.sh                 # Tear down cluster
│   ├── build-images.sh         # Build + kind-load custom images
│   └── smoke-test.sh           # The exact curl from the exercise
└── .github/
    └── workflows/
        ├── ci.yaml             # lint + test + image build
        └── lint.yaml
```

## License

MIT — see [`LICENSE`](LICENSE).
