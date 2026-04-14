# Local GPU Tutorial (From Zero)

A self-contained walkthrough for running this KubeRay batch inference
stack on a single laptop with an NVIDIA GPU. Assumes no prior
experience with Kubernetes, Ray, or KubeRay. Takes ~30 minutes
start to finish, most of it image builds.

By the end you will have:

- A local Kubernetes cluster that can see your laptop's GPU.
- The KubeRay operator running.
- A Ray cluster (1 head + 2 GPU workers).
- A FastAPI service submitting batch inference jobs.
- Qwen2.5-0.5B-Instruct running CUDA generation on your GPU.
- `curl` commands to prove each layer works.

If you already have the repo running on CPU and just want GPU, skip
to [Step 7](#step-7-up-gpu-local).

---

## Table of contents

1. [Why k3d and not kind](#1-why-k3d-and-not-kind)
2. [What each tool does (60-second tour)](#2-what-each-tool-does-60-second-tour)
3. [Host prerequisites (Windows / Linux)](#3-host-prerequisites)
4. [Install the CLI tools](#4-install-the-cli-tools)
5. [Clone and check out the branch](#5-clone-and-check-out-the-branch)
6. [Verify the GPU is visible to Docker](#6-verify-the-gpu-is-visible-to-docker)
7. [Run `make up-gpu-local`](#7-up-gpu-local)
8. [What just happened: step-by-step](#8-what-just-happened-step-by-step)
9. [Verify each layer](#9-verify-each-layer)
10. [Submit a batch and watch the GPU](#10-submit-a-batch-and-watch-the-gpu)
11. [Inspect results](#11-inspect-results)
12. [Teardown](#12-teardown)
13. [Troubleshooting](#13-troubleshooting)
14. [Going further](#14-going-further)

---

## 1. Why k3d and not kind

kind ("Kubernetes in Docker") is what this repo's `make up` uses by
default for CPU. kind does not expose the host GPU to its node
container - there is no `--gpus` flag and no supported path for
GPU passthrough as of 2026.

k3d is a near-identical tool built on top of k3s (Rancher's minimal
Kubernetes). It accepts `--gpus all`, which propagates through the
NVIDIA Container Toolkit into the k3s containerd. That is the path
of least resistance for a laptop GPU.

For the control plane (FastAPI, Postgres, KubeRay) there is no
difference. Only the cluster bootstrapper changes.

---

## 2. What each tool does (60-second tour)

If any of these are unfamiliar, skim this table; otherwise skip.

| Tool | Role | Runs where |
|---|---|---|
| **Docker / containerd** | Runs containers. | Your laptop. |
| **NVIDIA Container Toolkit** | Lets containers see the host GPU. | Your laptop (host). |
| **k3d** | Creates a local Kubernetes cluster inside one Docker container. | Your laptop. |
| **kubectl** | Talks to the cluster's API server. | Your laptop. |
| **Helm** | Installs pre-packaged Kubernetes apps (we use it for KubeRay). | Your laptop. |
| **KubeRay operator** | A pod inside the cluster that knows how to create Ray clusters from YAML. | Inside the cluster. |
| **Ray head** | Scheduler + Jobs REST API. | Inside the cluster. |
| **Ray workers** | GPU pods that actually run `QwenPredictor`. | Inside the cluster. |
| **FastAPI API** | Accepts `POST /v1/batches`, writes `input.jsonl`, submits a Ray job, streams `results.jsonl`. | Inside the cluster. |
| **Postgres** | Tracks batch status and metadata. | Inside the cluster. |
| **Shared PVC** | A directory (`/data/batches`) mounted into both the API and Ray worker pods. | Inside the cluster. |

If you want a deeper dive, read:

- `docs/ARCHITECTURE.md` - the design rationale and trade-offs.
- `docs/GPU_PROFILE.md` - what the GPU profile changes vs. CPU.
- `docs/TECHNICAL_REPORT.md` - benchmark numbers and production considerations.

---

## 3. Host prerequisites

### Windows 11 / WSL2

1. Install the latest **NVIDIA GeForce driver** from nvidia.com **in
   Windows itself**. Do not install drivers inside WSL - Windows
   shares the driver with WSL automatically.
2. Install **WSL2** with Ubuntu 22.04:
   ```powershell
   wsl --install -d Ubuntu-22.04
   ```
3. Install **Docker Desktop** with the WSL2 backend and enable
   GPU support (Settings -> Resources -> WSL Integration, and
   Settings -> General -> "Use the WSL 2 based engine").
4. Inside your WSL2 Ubuntu shell, verify:
   ```bash
   nvidia-smi           # should list your GPU
   docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
   ```
   The second command proves Docker can reach the GPU. If it fails,
   re-check Docker Desktop's "WSL Integration" setting.

### Linux (Ubuntu 22.04)

1. Install the NVIDIA driver:
   ```bash
   sudo ubuntu-drivers autoinstall
   sudo reboot
   nvidia-smi           # verify
   ```
2. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker "$USER"
   newgrp docker
   ```
3. Install the **NVIDIA Container Toolkit**:
   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
     | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
     | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
     | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt update && sudo apt install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```
4. Verify:
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
   ```

### macOS

Apple Silicon GPUs do **not** work with the NVIDIA / CUDA path. For
macOS, either use the CPU profile (`make up`) or run the Colab
notebook at `notebooks/qwen_ray_data_demo.ipynb` on a free T4.

---

## 4. Install the CLI tools

From inside WSL2 Ubuntu (Windows users) or your Linux host:

```bash
# kubectl
curl -LO "https://dl.k8s.io/release/v1.29.4/bin/linux/amd64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl

# helm
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# k3d
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash

# jq (used by the Makefile's verification step)
sudo apt install -y jq make
```

Verify:

```bash
kubectl version --client
helm version --short
k3d version
```

---

## 5. Clone and check out the branch

```bash
git clone https://github.com/Sebuliba-Adrian/kuberay-batch-inference.git
cd kuberay-batch-inference
git checkout feat/local-gpu-k3d
```

---

## 6. Verify the GPU is visible to Docker

Run this before anything else. If it fails, nothing downstream will
work:

```bash
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
```

Expected: a table showing your GPU name, memory, driver version, and
CUDA version. If you get `could not select device driver "" with
capabilities: [[gpu]]`, the NVIDIA Container Toolkit is not
installed correctly - go back to Step 3.

---

## 7. `up-gpu-local`

One command stands the whole thing up:

```bash
make up-gpu-local
```

This takes 10-25 minutes on first run (most of it is image builds
downloading PyTorch + CUDA wheels + the 1 GB Qwen2.5-0.5B weights).
Subsequent runs finish in ~2 minutes because Docker caches layers.

The target blocks at the end with `kubectl port-forward` so the
API is reachable at `http://localhost:8000`. Leave it running in
its own terminal.

---

## 8. What just happened: step-by-step

`up-gpu-local` is a Makefile target that chains nine steps in order.
Read this section if you want to understand what the one-liner did.

### 8.1 `cluster-up-k3d`

Created a k3d cluster named `kuberay-dev` backed by k3s, passing
`--gpus all` to expose your GPU to the k3s containerd. Two NodePorts
(`30800`, `30826`) are forwarded to the host for later use.

What you now have: one Docker container (`k3d-kuberay-dev-server-0`)
running a full Kubernetes node, with access to your GPU.

### 8.2 `nvidia-plugin`

Applied the official NVIDIA device plugin DaemonSet. This is a pod
that runs on every node and advertises `nvidia.com/gpu` as an
allocatable resource to the Kubernetes scheduler. Without it, pods
cannot request a GPU even if the host has one.

Verify:

```bash
kubectl get node -o json | jq '.items[0].status.allocatable."nvidia.com/gpu"'
# expect: "1"
```

### 8.3 `kuberay-install`

Installed the KubeRay operator via Helm into the `kuberay-system`
namespace. Helm pulled the chart, which did two things:

1. Registered the `RayCluster`, `RayJob`, and `RayService` CRDs -
   teaching the Kubernetes API server what those kinds are.
2. Deployed the KubeRay operator pod, which watches for objects of
   those kinds and reconciles them into real resources.

Verify:

```bash
kubectl get crd | grep ray.io
kubectl get pods -n kuberay-system
```

### 8.4 `build-images-gpu`

Built two Docker images locally:

- `local/batch-api:dev` - the FastAPI control plane
  (`api/Dockerfile`).
- `local/ray-worker:2.54.1-gpu` - Ray + CUDA torch + transformers +
  Qwen weights baked in (`inference/Dockerfile.gpu`).

The GPU worker image is ~5 GB. Expect this to be the slowest step on
first run.

### 8.5 `load-images-gpu-k3d`

k3d's containerd is inside the server Docker container, so images
built on the host are invisible until `k3d image import` copies them
in. kind's equivalent is `kind load docker-image`.

### 8.6 `namespace`, `storage-k3d`, `postgres`

Standard Kubernetes setup:

- Created the `ray` namespace.
- Applied the shared PVC (a hostPath PV at `/mnt/data` inside the
  k3d server container) and chmod'd it `0777` so non-root pods can
  write there.
- Deployed Postgres with a Secret, Service, and init ConfigMap.

### 8.7 `raycluster-gpu`

Applied `k8s/raycluster/raycluster.gpu.yaml`. This is a
`RayCluster` custom resource. The KubeRay operator saw it, read
`.spec`, and created:

- 1 head pod (CPU only, `num-cpus: "0"`).
- 2 GPU worker pods, each requesting `nvidia.com/gpu: 1`.
- ClusterIP services for the head's Jobs REST API (`:8265`), GCS
  (`:6379`), and metrics.

The Makefile waits until the `RayCluster` object reports
`status.state == ready`.

### 8.8 `api`

Applied the FastAPI Deployment, ConfigMap, Secret, and Service.
`RAY_ADDRESS` in the ConfigMap points at the Ray head's cluster DNS
name - that is how the API submits jobs.

### 8.9 `port-forward`

Bound `localhost:8000` to the API Service. This blocks, which is why
you saw the terminal stop scrolling.

---

## 9. Verify each layer

Open a second terminal (the first is running `port-forward`).

```bash
# 1. Cluster is up
kubectl cluster-info --context k3d-kuberay-dev

# 2. GPU is visible to Kubernetes
kubectl get node -o json | jq '.items[0].status.allocatable'
# look for "nvidia.com/gpu": "1"

# 3. KubeRay operator is running
kubectl get pods -n kuberay-system

# 4. Ray cluster is ready
kubectl get rayclusters -n ray
# STATE should be "ready"

# 5. Ray pods (1 head + 2 GPU workers)
kubectl get pods -n ray -o wide
# All should be Running 1/1

# 6. GPUs reachable from inside a worker
WORKER=$(kubectl get pods -n ray -l ray.io/profile=gpu -o name | head -1)
kubectl exec -n ray "$WORKER" -- nvidia-smi

# 7. Ray sees the GPUs
kubectl exec -n ray "$WORKER" -- python -c \
  "import ray; ray.init(address='auto'); print(ray.cluster_resources())"
# expect: {'CPU': 8.0, 'GPU': 2.0, ...}

# 8. API is reachable
curl -s http://localhost:8000/health
# expect: {"status":"ok"}
```

If any step fails, see [Troubleshooting](#13-troubleshooting).

---

## 10. Submit a batch and watch the GPU

In one terminal, watch GPU usage live:

```bash
watch -n 1 nvidia-smi
```

In another terminal, submit a batch:

```bash
curl -X POST http://localhost:8000/v1/batches \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: demo-api-key-change-me-in-production' \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "input": [
      {"prompt": "What is 2+2?"},
      {"prompt": "Name three planets."},
      {"prompt": "Summarize Kubernetes in one sentence."},
      {"prompt": "Write a haiku about GPUs."}
    ],
    "max_tokens": 64
  }'
```

The response returns immediately with a batch id and
`status=queued`:

```json
{"id": "batch_01ABC...", "status": "queued", ...}
```

Within 5-10 seconds, `nvidia-smi` should show GPU utilization jump
from 0% to some busy number as the `QwenPredictor` actors pick up
the work.

Check the worker log to see the device banner:

```bash
kubectl logs -n ray "$WORKER" | grep "Loaded model"
# expect: Loaded model Qwen/... (dtype=torch.bfloat16, device=cuda)
```

Poll the status:

```bash
BATCH=batch_01ABC...      # replace with the id you got
curl -s -H 'X-API-Key: demo-api-key-change-me-in-production' \
  http://localhost:8000/v1/batches/$BATCH | jq
```

Status progresses `queued` -> `in_progress` -> `completed`. On a
laptop GPU (RTX 3060+) this usually takes 3-8 seconds for four
prompts.

---

## 11. Inspect results

```bash
curl -s -H 'X-API-Key: demo-api-key-change-me-in-production' \
  http://localhost:8000/v1/batches/$BATCH/results
```

NDJSON is streamed one line per prompt. Pipe through `jq` to pretty-
print:

```bash
curl -s -H 'X-API-Key: demo-api-key-change-me-in-production' \
  http://localhost:8000/v1/batches/$BATCH/results | jq
```

Each line looks like:

```json
{
  "id": "0",
  "prompt": "What is 2+2?",
  "response": "The answer is 4.",
  "finish_reason": "stop",
  "prompt_tokens": 18,
  "completion_tokens": 6,
  "error": null
}
```

The files also live directly on the PVC if you want to look:

```bash
kubectl exec -n ray "$WORKER" -- ls /data/batches/$BATCH
# input.jsonl  results.jsonl  _SUCCESS
```

---

## 12. Teardown

```bash
# Stop the port-forward (Ctrl+C in its terminal).
# Remove workloads but keep the cluster (fast re-deploy):
make down

# Or nuke the whole thing:
make cluster-down-k3d
```

The Docker images stay on your host; `docker image prune` cleans
those up separately.

---

## 13. Troubleshooting

### `docker run --gpus all` fails

The NVIDIA Container Toolkit is not installed or not configured for
Docker. On Linux, re-run Step 3. On Windows/WSL2, check that
Docker Desktop has GPU support enabled in Settings.

### `make up-gpu-local` hangs on "Waiting for RayCluster to become ready"

Most common cause: the GPU worker pods are `Pending` because the
node does not advertise `nvidia.com/gpu` yet. Check:

```bash
kubectl get pods -n ray
kubectl describe pod -n ray <pending-worker> | tail -20
```

Look at the Events section. If you see
`0/1 nodes are available: 1 Insufficient nvidia.com/gpu`, the
device plugin is not running. Re-run `make nvidia-plugin`.

### `ImagePullBackOff` on a worker

The GPU image was not imported into k3d. Run:

```bash
make load-images-gpu-k3d
kubectl delete pod -n ray <pod-name>   # force a restart
```

### `nvidia-smi` inside the worker shows the GPU, but
`ray.cluster_resources()` shows `'GPU': 0`

Ray was not told about the GPU at start. Check the worker's
`rayStartParams` in `k8s/raycluster/raycluster.gpu.yaml` - it
should include `num-gpus: "1"`. If it does but Ray still shows 0,
restart the workers:

```bash
kubectl delete pods -n ray -l ray.io/profile=gpu
```

### Batches stay `queued` forever

Look at the API pod's logs:

```bash
kubectl logs -n ray -l app=batch-api --tail=100 | grep -iE 'error|submit'
```

The most common cause is the API cannot reach `RAY_ADDRESS`. Verify:

```bash
kubectl exec -n ray <api-pod> -- curl -s $RAY_ADDRESS/api/version
```

### Out of disk space

The GPU worker image is ~5 GB, Postgres, Ray images, and kind
artifacts can add another ~10 GB. Run:

```bash
docker system df
docker image prune -a
```

### GPU memory OOM during generation

Qwen2.5-0.5B uses <2 GB VRAM, so an OOM means something else on the
host is holding the GPU. Close Chrome / games / other ML tools and
retry. For a longer fix, reduce `batch_size` in
`inference/jobs/batch_infer.py` (the GPU path uses `batch_size=32`;
drop to `16` if VRAM is tight).

---

## 14. Going further

You now have a complete, working, GPU-backed KubeRay batch inference
stack on your laptop. Natural next experiments:

- **Benchmark.** Run `scripts/benchmark.py` against
  `localhost:8000` and compare tokens/sec to the CPU profile.
- **Scale up.** Bump `replicas` in `raycluster.gpu.yaml` if your
  laptop has 2+ GPUs (uncommon) or run the same image on a cloud
  cluster - see `docs/GPU_PROFILE.md`.
- **Swap in vLLM.** The biggest throughput win for GPU is replacing
  the per-prompt `generate()` loop with vLLM's continuous batching.
  Documented as Stage 2 in `docs/ARCHITECTURE.md`.
- **Monitor.** `make monitoring-up && make grafana` installs
  Prometheus + Grafana + prebuilt Ray dashboards. The GPU worker
  image exposes torch/CUDA metrics via Ray's built-in exporter.
- **Write your own operator.** Pick any higher-level abstraction
  (a `WebApp` or `BatchPool` CRD, say), define the schema, and write
  a controller that reconciles it into ordinary Deployments and
  Services. Kubebuilder and Operator SDK are the usual scaffolds.

---

## What you learned

- **k3d vs. kind:** same idea (K8s in Docker), but k3d does GPUs.
- **Operator pattern:** CRDs teach Kubernetes the type exists; the
  KubeRay operator teaches it what to do.
- **Profile split:** same code, different manifests + images
  selected at deploy time - explicit beats "magic auto-adapt."
- **Device auto-detect:** `QwenPredictor` picks CUDA vs. CPU from
  `torch.cuda.is_available()`; `run_batch` picks concurrency from
  `ray.cluster_resources()`.
- **File-based handshake:** the API and Ray workers communicate
  through a shared PVC using NDJSON + `_SUCCESS` markers, not
  through the database and not over HTTP.

That same pattern scales unchanged from your laptop to GKE / AKS /
DOKS - only the cluster bootstrapper and the storage class change.
