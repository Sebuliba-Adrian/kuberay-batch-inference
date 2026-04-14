# GPU Profile

A second deployment profile for the same application, using GPU workers
instead of CPU workers. The FastAPI control plane, Postgres, shared
PVC, batch lifecycle, and client contract are unchanged.

**Scope:** this document describes a minimal, correctness-first GPU
port - same Transformers runtime, just moved onto CUDA. It does
**not** cover swapping to vLLM or restructuring generation for padded
batches; those are follow-up optimizations, not part of the profile
itself.

---

## Why a profile, not auto-adaptation

The Python runtime picks its device at actor init (`QwenPredictor`
checks `torch.cuda.is_available()`). That part *is* automatic. But
the surrounding infrastructure cannot safely autodetect:

- the worker image has to already be CUDA-built (`Dockerfile.gpu`);
- the pod spec has to request `nvidia.com/gpu`;
- the cluster has to have GPU nodes and the NVIDIA device plugin;
- node selectors / tolerations depend on the cluster's labelling.

So we use explicit profiles with matching tooling, not a single
manifest that silently behaves differently depending on where it
lands. Two explicit paths beat one magical one.

---

## What each profile looks like

| | CPU profile (default) | GPU profile |
|---|---|---|
| Worker image | `local/ray-worker:2.54.1-cpu` | `local/ray-worker:2.54.1-gpu` |
| Worker Dockerfile | `inference/Dockerfile` | `inference/Dockerfile.gpu` |
| Worker requirements | `inference/requirements.txt` (torch CPU) | `inference/requirements-gpu.txt` (torch CUDA 12.1) |
| Worker resources | 2 CPU / 5 GiB | 4 CPU / 16 GiB / 1 GPU |
| RayCluster manifest | `k8s/raycluster/raycluster.yaml` | `k8s/raycluster/raycluster.gpu.yaml` |
| Predictor device | `cpu`, dtype `bfloat16` | `cuda`, dtype `bfloat16` (fp16 on T4) |
| `map_batches` | `concurrency=2, batch_size=8, num_gpus=0` | `concurrency=<gpu_count>, batch_size=32, num_gpus=1` |
| Make entrypoint | `make up` | `make up-gpu` |

The head pod is identical between profiles (CPU-only, `num-cpus: "0"`).
Only workers change, because only workers do compute.

---

## Runtime selection

`QwenPredictor.__init__` picks the device at actor boot:

```python
if torch.cuda.is_available():
    self._device = "cuda"
    cap_major = torch.cuda.get_device_capability(0)[0]
    self._dtype = torch.bfloat16 if cap_major >= 8 else torch.float16
else:
    self._device = "cpu"
    self._dtype = torch.bfloat16
```

`run_batch` picks parallelism from the actual cluster:

```python
gpu_count = int(ray.cluster_resources().get("GPU", 0))
if gpu_count > 0:
    concurrency, batch_size, num_gpus_per_actor = gpu_count, 32, 1.0
else:
    concurrency, batch_size, num_gpus_per_actor = 2, 8, 0.0
```

Both paths read from the cluster itself, so the same `batch_infer.py`
runs under either profile without code changes.

---

## Using the GPU profile

Prerequisites (before any of the below works):

- GPU-capable nodes in the cluster.
- NVIDIA device plugin installed and `kubectl get node -o json` shows
  allocatable `nvidia.com/gpu`.
- An image registry the cluster can pull from, **or** a GPU-capable
  kind setup if you really want to stay local (uncommon).

### On a cloud cluster

```bash
# 1. Build and push the GPU worker image to your registry.
docker build -t myregistry/ray-worker:2.54.1-gpu -f inference/Dockerfile.gpu inference/
docker push myregistry/ray-worker:2.54.1-gpu

# 2. Edit k8s/raycluster/raycluster.gpu.yaml:
#    - replace "local/ray-worker:2.54.1-gpu" with the pushed tag
#    - uncomment and fill in the nodeSelector for your GPU node pool
#      (e.g. cloud.google.com/gke-accelerator on GKE)

# 3. Apply, then deploy the API as usual:
kubectl apply -n ray -f k8s/raycluster/raycluster.gpu.yaml
make api
make port-forward
```

### On a GPU-capable kind cluster

```bash
make up-gpu
```

That chains `cluster-up`, `kuberay-install`, `build-images-gpu`,
`load-images-gpu`, `namespace`, `storage`, `postgres`,
`raycluster-gpu`, `api`, and `port-forward`. The image is loaded
straight into kind, no registry needed.

### Switching profiles on an already-running cluster

The two RayCluster manifests share the same RayCluster name
(`qwen-raycluster`), so swapping profiles is:

```bash
kubectl delete -n ray -f k8s/raycluster/raycluster.yaml
kubectl apply  -n ray -f k8s/raycluster/raycluster.gpu.yaml
```

`RAY_ADDRESS` in the API ConfigMap does not change, so the FastAPI
pod needs no restart.

---

## Verification checklist

After `make up-gpu` (or the equivalent manual steps), confirm each
layer in order:

```bash
# 1. GPU visible to Kubernetes
kubectl get node -o json | jq '.items[].status.allocatable."nvidia.com/gpu"'
# expect: "1" (or higher)

# 2. Worker pods scheduled onto GPU nodes
kubectl get pods -n ray -l ray.io/profile=gpu -o wide

# 3. GPU reachable from inside the worker
kubectl exec -n ray <worker-pod> -- nvidia-smi

# 4. Ray sees the GPU
kubectl exec -n ray <worker-pod> -- python -c \
  "import ray; ray.init(address='auto'); print(ray.cluster_resources())"
# expect: {'CPU': 8.0, 'GPU': 2.0, ...}  (2 workers x 1 GPU each)

# 5. End-to-end batch
curl -X POST http://localhost:8000/v1/batches \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: demo-api-key-change-me-in-production' \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","input":[{"prompt":"2+2?"}],"max_tokens":32}'

# 6. Inspect the worker log for the device banner
kubectl logs -n ray <worker-pod> | grep "Loaded model"
# expect: Loaded model Qwen/... in N.Ns (dtype=torch.bfloat16, device=cuda)
```

If step 1 fails, the NVIDIA device plugin is not installed.
If step 2 fails, check the nodeSelector / tolerations in the manifest.
If step 4 shows `'GPU': 0`, the worker pods started but did not pick
up the GPU - usually a mismatch between `resources.limits` and the
`num-gpus` rayStartParam.

---

## What the GPU profile does not do

- **Does not change the API.** `/v1/batches`, auth, Postgres schema,
  streaming results - all identical.
- **Does not switch to vLLM.** That is a separate, larger change
  and belongs in a Stage 2 migration once the profile is proven.
- **Does not enable autoscaling.** `minReplicas == maxReplicas == 2`
  matches the CPU profile for deterministic dev behavior. Production
  use would turn on `enableInTreeAutoscaling: true`.
- **Does not batch prompts inside `__call__`.** The per-prompt loop
  is preserved from the CPU profile for error isolation. On GPU this
  leaves throughput on the table - true left-padded batched
  `generate` is the biggest optimization lever, worth more than
  swapping engines for a 0.5B model.

---

## Expected gains (order of magnitude)

- Single-prompt latency: ~30-60x faster on an L4 or A10 GPU than on
  a 2-core CPU pod. Most of that is matmul moving from AVX2 to tensor
  cores.
- Throughput: with `batch_size=32` and true batched generation (a
  follow-up optimization), expect another ~10-20x over the per-prompt
  CPU loop. Without batched generation, the GPU sees maybe 2-5x of
  that headroom.
- Tokens per dollar: model-dependent. On Qwen2.5-0.5B, kind + CPU is
  cheaper for small batches; GPU wins decisively above ~100 prompts
  per minute sustained.

Measure before tuning. `scripts/benchmark.py` works unchanged on
either profile; a paired run is the honest comparison.

---

## Files introduced by this profile

```
inference/
  Dockerfile.gpu            # mirrors Dockerfile on a CUDA base
  requirements-gpu.txt      # torch==2.3.1+cu121

k8s/raycluster/
  raycluster.gpu.yaml       # drop-in RayCluster for GPU workers

docs/
  GPU_PROFILE.md            # this file
```

Existing files changed:

```
inference/jobs/batch_infer.py   # device auto-detect in QwenPredictor,
                                # GPU-aware map_batches in run_batch

Makefile                         # build-worker-gpu, load-images-gpu,
                                 # raycluster-gpu, up-gpu,
                                 # build-images-gpu
```

Nothing in `api/`, `k8s/api/`, `k8s/postgres/`, or `k8s/storage/`
changed. That's the point of the profile split: compute-plane
evolution, control-plane stability.
