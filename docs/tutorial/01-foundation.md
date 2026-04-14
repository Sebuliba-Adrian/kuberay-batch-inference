# 01 - Foundation

Before writing a line of code, understand the problem and the stack
you will build to solve it. You will refer back to this page many
times.

## The problem

We want to run an LLM (Qwen2.5-0.5B-Instruct) over batches of
prompts. A client sends many prompts at once, does not wait on HTTP,
and comes back later for the results. Think of a nightly OCR job,
a content moderation queue, or an embedding pipeline. This is
**offline batch inference**, not online chat.

A naive design would be: one FastAPI process that loads the model
and calls `model.generate()` inside the request handler. That
works for five prompts on a laptop. It breaks for real workloads
because:

- HTTP workers get blocked by long-running generation.
- Scaling the API and scaling the model are tied together.
- Failures in inference crash the API process.
- You cannot easily distribute work across multiple machines.

So the design separates two concerns:

- **Control plane** - validates requests, authenticates, tracks
  status. Small, cheap, stateless, horizontally scalable.
- **Compute plane** - loads the model, runs generation, writes
  results. Heavy, GPU-friendly, scaled independently.

The control plane submits jobs to the compute plane and comes back
later to read what the compute plane produced.

## The stack

Five moving parts. Remember the one-sentence pitch:

> FastAPI is the receptionist, Ray is the factory, Postgres is
> the ledger, Kubernetes is the building manager, and KubeRay is
> the team that sets up the Ray factory inside that building.

### FastAPI

Python web framework. Defines `POST /v1/batches` and
`GET /v1/batches/{id}`. Validates JSON using Pydantic models,
authenticates with an API key, and returns JSON or streams
NDJSON back. Lives in one Python file (roughly `app.py`) and runs
as a single container.

### Ray

Python distributed computing framework. Think of it as Celery's
bigger sibling, designed for ML workloads: stateful actors that
hold a loaded model between requests, parallel data processing
primitives, GPU-aware scheduling. We use:

- **Ray Jobs** - a REST API the FastAPI pod calls to submit
  Python scripts to a running Ray cluster.
- **Ray Data** - `map_batches` distributes rows across workers.

### Postgres

Relational database. Stores metadata about each batch: id,
status, counts, timestamps, Ray job id, file paths. Does **not**
store the LLM outputs - those are too large and are stored on
the filesystem.

### Kubernetes

Container orchestrator. Runs all of the above as pods, restarts
them if they crash, mounts shared storage, wires networking.
Locally we use `kind` (Kubernetes in Docker), which creates a
whole cluster inside one Docker container on your laptop.

### KubeRay

Kubernetes operator for Ray. Extends Kubernetes with a
`RayCluster` custom resource. You write one YAML file saying
"I want 1 head pod and 2 worker pods," and KubeRay creates and
manages those pods for you. This is the part that makes Ray feel
native to Kubernetes.

## Picture

```
          +----------------------+
          |        Client        |
          | curl / SDK / browser |
          +----------+-----------+
                     | HTTPS (API key)
                     v
          +----------------------------------+
          |         FastAPI API Pod          |
          |  validate / auth / ledger row    |
          |  submit Ray job / stream results |
          +-----+--------------+-------------+
                |              |
         SQL    |              |  Ray Jobs REST (:8265)
                |              |
                v              v
      +----------------+   +----------------------+
      |  Postgres Pod  |   |    Ray Head Pod      |
      | batch ledger   |   | scheduler + Jobs API |
      +----------------+   +----------+-----------+
                                      |
                                      | schedules
                                      v
                        +------------------------------+
                        |       Ray Worker Pods        |
                        |  QwenPredictor actors        |
                        |  model loaded once per actor |
                        +---------------+--------------+
                                        |
                                        | shared file I/O
                                        v
                       +-----------------------------------+
                       |        Shared Storage PVC         |
                       |  /data/batches/<batch_id>/        |
                       |     input.jsonl                   |
                       |     results.jsonl                 |
                       |     _SUCCESS | _FAILED            |
                       +-----------------------------------+
```

Memorize this picture. Everything else is details.

## VPS / LAMP translation table

If you are coming from a single-VM background, these equivalences
will save you hours of confusion.

| VPS concept | Kubernetes equivalent |
|---|---|
| The machine itself | A Kubernetes node |
| `systemd` service | Deployment |
| `.env` file | ConfigMap (non-secret) + Secret (credentials) |
| A folder on disk | PersistentVolume mounted via PVC |
| `127.0.0.1:5432` | Service DNS, e.g. `postgres.ray.svc.cluster.local` |
| `systemctl restart` on crash | Controller restarts the pod automatically |
| Apache + PHP app | FastAPI pod |
| Cron job / worker script | Ray job on the Ray cluster |

Same operational questions, different mechanical answers.

## Prerequisites

You need these installed before the next section. Commands are for
Ubuntu 22.04 / WSL2; adjust for macOS/Linux as needed.

### Python 3.11

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip
python3 --version
```

### Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker
docker run hello-world
```

Windows users: install Docker Desktop with WSL2 backend.

### kubectl

```bash
curl -LO "https://dl.k8s.io/release/v1.29.4/bin/linux/amd64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl
kubectl version --client
```

### Helm

```bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm version --short
```

### kind

```bash
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.27.0/kind-linux-amd64
chmod +x kind
sudo mv kind /usr/local/bin/
kind version
```

### Supporting tools

```bash
sudo apt install -y jq curl make
```

## Verify

```bash
docker version         | head -3
kubectl version --client
helm version --short
kind version
python3 --version
```

Five green lines? Continue to
[02-python-and-ray-local.md](02-python-and-ray-local.md).
