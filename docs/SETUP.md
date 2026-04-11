# Setup Guide — Fresh Ubuntu 22.04 → Working Curl

End-to-end bring-up of the KubeRay Batch Inference service on a fresh Ubuntu 22.04 machine. Every command here is copy-pasteable.

---

## 0. Prerequisites

- A physical or virtual machine running **Ubuntu 22.04 LTS**
- `sudo` access
- **At least 8 CPU cores, 16 GiB RAM free** for the kind cluster (1 head + 2 Ray worker pods at 2 CPU / 5 GiB each plus Postgres, API, and system pods)
- A reliable network connection (model weights and container images are pulled once during setup)
- About **5 GiB** of free disk space under `/tmp` for the image cache + model weights

---

## 1. Clone the repo

```bash
git clone https://github.com/Sebuliba-Adrian/kuberay-batch-inference.git
cd kuberay-batch-inference
```

---

## 2. Install prerequisites in one shot

```bash
./scripts/setup.sh
```

This script is idempotent — re-running it is safe. It installs:

| Tool    | Purpose                                      | Version pin |
|---------|----------------------------------------------|-------------|
| Docker  | Container runtime                             | latest stable |
| kind    | Kubernetes in Docker                          | v0.24.0 |
| kubectl | k8s CLI                                       | v1.29.4 |
| helm    | Helm chart tool (for the KubeRay operator)    | v3.15.3 |
| uv      | Fast Python package manager (for local dev)   | latest |
| jq      | JSON processor used by smoke-test.sh          | apt |

**Important:** if Docker was newly installed, log out and log back in (or run `newgrp docker`) so your user picks up the `docker` group without `sudo`.

---

## 3. Bring up the full stack

```bash
make up
```

This runs the entire happy path:

1. **`make cluster-up`** — creates the kind cluster `kuberay-dev` with `extraPortMappings` (host `:8000` → NodePort `30800`, host `:8265` → NodePort `30826`) and a hostPath mount at `/tmp/kuberay-batch-inference/data` for the shared PVC.
2. **`make kuberay-install`** — `helm install kuberay-operator` pinned to chart `1.6.0` in the `kuberay-system` namespace.
3. **`make build-images`** — builds two Docker images:
   - `local/batch-api:dev` (the FastAPI proxy, multi-stage build, ~180 MB)
   - `local/ray-worker:2.54.1-cpu` (Ray + Transformers + Qwen2.5-0.5B pre-downloaded, ~2.5 GB)
4. **`make load-images`** — `kind load docker-image` pushes both images into the cluster's containerd so pods don't try to pull from a registry.
5. **`make namespace`** — creates the `ray` namespace.
6. **`make storage`** — applies the shared PVC backed by the kind hostPath mount.
7. **`make postgres`** — deploys Postgres 16 with schema init from `k8s/postgres/init-configmap.yaml`.
8. **`make raycluster`** — applies the RayCluster CRD (1 head + 2 CPU workers) and waits for `status.state=ready`.
9. **`make api`** — deploys the FastAPI proxy Deployment and Service.
10. **`make port-forward`** — starts `kubectl port-forward svc/batch-api 8000:8000` in the foreground.

**This takes ~5-10 minutes the first time** because of the Ray worker image build (torch CPU wheels + Qwen weights). Subsequent `make up` runs are fast.

> **Troubleshooting:** if `make raycluster` times out waiting for `status.state=ready`, check pod events with `kubectl -n ray get events --sort-by=.lastTimestamp`. The most common cause is insufficient CPU/memory on the kind node — raise Docker Desktop's resource limits or tune the `resources.requests` in `k8s/raycluster/raycluster.yaml`.

---

## 4. Run the smoke test

Open a second terminal (the first is occupied by `port-forward`):

```bash
./scripts/smoke-test.sh
```

Expected output:

```
[smoke] Health check...
{
  "status": "ok"
}
[smoke] Auth check: POST without X-API-Key must return 401...
[smoke] ✓ unauthenticated request rejected with 401
[smoke] Auth check: POST with wrong X-API-Key must return 401...
[smoke] ✓ invalid key rejected with 401
[smoke] Submitting batch (exercise PDF payload)...
{
  "id": "batch_01J...",
  "object": "batch",
  "endpoint": "/v1/batches",
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "status": "queued",
  "created_at": 1744384000,
  ...
}
[smoke] ✓ submitted batch batch_01J...
[smoke] Polling status...
  [01] status=queued
  [02] status=in_progress
  ...
  [NN] status=completed
[smoke] ✓ batch completed
[smoke] Fetching results...
{"id":"0","prompt":"What is 2+2?","response":"...","finish_reason":"stop"}
{"id":"1","prompt":"Hello world","response":"...","finish_reason":"stop"}
[smoke] ✓ smoke test passed
```

---

## 5. Run the exact exercise curl by hand

The exercise brief specifies this curl:

```bash
curl -X POST http://localhost:8000/v1/batches \
  -H "Content-Type: application/json" \
  -H "X-API-Key: demo-api-key-change-me-in-production" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "input": [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}],
    "max_tokens": 50
  }'
```

---

## 6. Observe the system

### Ray dashboard
```bash
# In a third terminal
make dashboard
# Opens http://localhost:8265 in your browser
```

The Ray dashboard shows active jobs, actors, per-node resource usage, and per-job logs.

### Pod logs

```bash
make logs-api              # FastAPI proxy
make logs-ray-head         # Ray head pod (scheduler + dashboard)
make logs-ray-workers      # Ray worker pods
```

### Direct kubectl

```bash
kubectl -n ray get pods
kubectl -n ray get raycluster
kubectl -n ray get batches -o yaml   # (via kubectl-sql or similar)
kubectl -n ray logs deploy/batch-api
```

---

## 7. Tear down

```bash
make down              # removes everything except the kind cluster
make cluster-down      # nukes the kind cluster too
```

The shared PVC host directory (`/tmp/kuberay-batch-inference/data`) is **not** deleted by either command — remove it manually with `rm -rf /tmp/kuberay-batch-inference` if you want a totally clean slate.

---

## 8. Running the API test suite without the cluster

The entire API test suite (163 tests, 100% line + branch coverage) runs in seconds without any cluster:

```bash
cd api
uv venv --python 3.11
uv pip install -e ".[dev]"
uv run pytest tests --cov=src --cov-branch --cov-report=term-missing
```

Or via the Makefile:

```bash
make install  # set up the venv + install deps
make test     # run pytest
make ci       # lint + typecheck + test
```

---

## 9. Common issues

### `ErrImagePull` / `ImagePullBackOff` on the Ray pods

You forgot `make load-images` (or the images were rebuilt and not reloaded). Fix:

```bash
make load-images
kubectl -n ray delete pod -l ray.io/cluster=qwen-raycluster
```

### `OOMKilled` on Ray worker pods

Qwen2.5-0.5B in Transformers needs ~3.5 GiB during model init. The worker pods are sized for 5 GiB. If Docker Desktop is capped at 8 GiB total, the second worker can't fit. Options:

- Raise Docker Desktop's memory limit to 16 GiB
- Reduce worker replicas to 1 in `k8s/raycluster/raycluster.yaml` (`workerGroupSpecs[0].replicas: 1`)

### RayCluster stuck in `status.state=unknown`

Check:

```bash
kubectl -n ray get endpoints qwen-raycluster-head-svc
kubectl -n ray describe raycluster qwen-raycluster
```

If the head pod is `Ready` but the workers are `Pending`, you're out of node resources. If the head pod is `Running` but not `Ready`, the Ray dashboard isn't answering yet — wait 30 more seconds.

### `/ready` returns 503 forever

```bash
curl http://localhost:8000/ready | jq
```

Look at `checks.postgres` and `checks.ray`. The failing one tells you which dependency to debug.

### Tests hang on Windows

Python 3.12 + pytest-asyncio works cleanly on Windows. If you see hangs, you're almost certainly running against Python 3.11 via an old venv — recreate with `uv venv --python 3.11 --clear`.
