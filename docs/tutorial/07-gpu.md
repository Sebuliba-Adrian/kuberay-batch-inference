# 07 - GPU Upgrade (Laptop)

CPU inference on Qwen2.5-0.5B is fine for a demo but slow. If your
laptop has an NVIDIA GPU, this section shows how to switch the
compute plane to CUDA without touching the control plane.

## Why this is easy, and why it is hard

**Easy**: `QwenPredictor` checks `torch.cuda.is_available()` and
picks the device. Ray Data schedules actors onto any resource type.
The FastAPI code has nothing to do with the device.

**Hard**: kind (what we used in Part 04) does not expose host GPUs
to pods. No `--gpus` flag, no supported path. We swap the cluster
bootstrapper to **k3d** (same idea, different implementation).

## kind vs k3d

```
  feature          | kind   | k3d
  -----------------+--------+-----
  cluster in Docker| yes    | yes
  GPU passthrough  | no     | yes (--gpus all)
  based on         | kubeadm| k3s
  default storage  | local  | local-path
```

Everything above k3d (Kubernetes API, kubectl, Helm, KubeRay,
manifests) is identical.

## The plan

```
1. Install NVIDIA Container Toolkit on the host
2. Create a k3d cluster with --gpus all
3. Install the NVIDIA device plugin in Kubernetes
4. Build a GPU worker image (CUDA torch)
5. Apply a GPU-flavored RayCluster
6. Same FastAPI, same Postgres, same PVC
```

## Step 1: host prerequisites

### Windows + WSL2

1. Install latest NVIDIA driver in Windows (not WSL).
2. Docker Desktop with WSL2 backend; enable GPU support.
3. Verify inside WSL:
   ```bash
   nvidia-smi
   docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
   ```

### Linux

```bash
# NVIDIA driver
sudo ubuntu-drivers autoinstall && sudo reboot

# NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
```

That last command must print a GPU table. If not, nothing else
will work.

## Step 2: install k3d

```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
k3d version
```

Create the cluster:

```bash
k3d cluster create tutorial-cluster \
  --image rancher/k3s:v1.29.4-k3s1 \
  --gpus all \
  -p "30800:30800@server:0" \
  --k3s-arg "--disable=traefik@server:*" \
  --wait
kubectl cluster-info --context k3d-tutorial-cluster
```

What changed: Docker runs the k3s node container with `--gpus all`,
which gives containerd inside the cluster access to the host's
GPU. The `-p` flag forwards a NodePort we can use later.

## Step 3: install the NVIDIA device plugin

The plugin is a DaemonSet that advertises `nvidia.com/gpu` as an
allocatable resource to the Kubernetes scheduler.

```bash
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.15.0/deployments/static/nvidia-device-plugin.yml
kubectl -n kube-system rollout status ds/nvidia-device-plugin-daemonset --timeout=120s

kubectl get node -o json \
  | jq '.items[0].status.allocatable."nvidia.com/gpu"'
# "1"
```

If it reports `null`, either the NVIDIA Container Toolkit is
mis-installed or Docker Desktop's WSL GPU support is off.

## Step 4: the device-aware predictor

The `QwenPredictor` from Part 02 needs two small changes so the
same class works on CPU or GPU:

```python
def __init__(self, model_name: str, max_tokens: int) -> None:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    self._torch = torch
    self._max_tokens = max_tokens

    if torch.cuda.is_available():
        self._device = "cuda"
        cap_major = torch.cuda.get_device_capability(0)[0]
        # Ampere+ (A100/A10/L4/H100) has bf16 tensor cores.
        # T4 does not, so fall back to fp16 there.
        self._dtype = torch.bfloat16 if cap_major >= 8 else torch.float16
    else:
        self._device = "cpu"
        self._dtype = torch.bfloat16

    # ... tokenizer + model load unchanged except torch_dtype=self._dtype
    # ... then:
    if self._device == "cuda":
        self.model.to(self._device)

def __call__(self, batch):
    # ... same as before, but after tokenization:
    if self._device == "cuda":
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
```

And `run_batch()` picks concurrency based on what the cluster
actually has:

```python
gpu_count = int(ray.cluster_resources().get("GPU", 0))
if gpu_count > 0:
    concurrency, batch_size, num_gpus_per_actor = gpu_count, 32, 1.0
else:
    concurrency, batch_size, num_gpus_per_actor = 2, 8, 0.0

out_ds = ds.map_batches(
    QwenPredictor,
    fn_constructor_kwargs={"model_name": model, "max_tokens": max_tokens},
    concurrency=concurrency,
    batch_size=batch_size,
    num_gpus=num_gpus_per_actor,
)
```

Same file handles both. No branches beyond device detection.

## Step 5: the GPU worker image

Create `Dockerfile.worker.gpu`:

```dockerfile
FROM rayproject/ray:2.54.1-py310-gpu

USER root
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

USER ray
WORKDIR /home/ray

COPY --chown=ray:users worker-requirements-gpu.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV HF_HOME=/home/ray/.cache/huggingface
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download(repo_id='Qwen/Qwen2.5-0.5B-Instruct', \
    allow_patterns=['*.json','*.safetensors','*.txt','tokenizer*'])"

USER root
RUN mkdir -p /app/jobs && chown -R ray:users /app
USER ray
COPY --chown=ray:users batch_infer.py /app/jobs/batch_infer.py
ENV PYTHONPATH=/app

# Build-time sanity: did we install CUDA wheels?
RUN python -c "import torch; assert torch.version.cuda, 'not a CUDA build'"
```

And `worker-requirements-gpu.txt`:

```txt
--index-url https://pypi.org/simple
--extra-index-url https://download.pytorch.org/whl/cu121

torch==2.3.1+cu121
transformers>=4.45,<5
accelerate>=0.34,<1
safetensors>=0.4.5
huggingface_hub>=0.25,<1
sentencepiece>=0.2.0
```

Build and import:

```bash
docker build -t local/ray-worker:2.54.1-gpu -f Dockerfile.worker.gpu .
k3d image import local/ray-worker:2.54.1-gpu -c tutorial-cluster
```

Note: `k3d image import` replaces `kind load docker-image`. The
only cluster-bootstrapper-specific detail in the whole GPU flow.

## Step 6: the GPU RayCluster

Copy your `raycluster.yaml` to `raycluster.gpu.yaml` and change:

- worker `image:` -> `local/ray-worker:2.54.1-gpu`
- worker `resources.requests` and `.limits`:
  ```yaml
  cpu: "4"
  memory: "16Gi"
  nvidia.com/gpu: "1"
  ```
- worker `rayStartParams`:
  ```yaml
  num-gpus: "1"
  ```
- drop the OMP/MKL/OPENBLAS env vars (meaningless on GPU)

The head stays CPU-only - no reason to waste a GPU on the scheduler.

Apply:

```bash
kubectl apply -f raycluster.gpu.yaml
kubectl -n tutorial get rayclusters -w
```

Wait for `ready`. The workers will be `Pending` until the GPU is
assigned, then `Running`.

## Step 7: run a batch on GPU

In one terminal, live-watch GPU usage:

```bash
watch -n 1 nvidia-smi
```

In another:

```bash
curl -s -X POST http://localhost:8000/v1/batches \
  -H 'X-API-Key: demo-api-key-change-me' \
  -d '{"input":[
    {"prompt":"What is 2+2?"},
    {"prompt":"Name three planets."},
    {"prompt":"Explain RAG in one line."},
    {"prompt":"Write a haiku about GPUs."}
  ],"max_tokens":64}'
```

`nvidia-smi` spikes from 0% to some busy number within 5-10s.
Check the worker log:

```bash
kubectl logs -n tutorial -l ray.io/node-type=worker | grep "Loaded model"
# Loaded model Qwen/... (dtype=torch.bfloat16, device=cuda)
```

On an RTX 3060+ the same 4-prompt batch that took ~30s on CPU
finishes in ~2s.

## What exactly changed

```
  CPU path                       GPU path
  -----------------------        -----------------------
  kind cluster                   k3d cluster (--gpus all)
  no device plugin               NVIDIA device plugin DaemonSet
  CPU worker image               GPU worker image (CUDA torch)
  resources: cpu + memory        resources: + nvidia.com/gpu: 1
  ray start: no num-gpus         ray start: num-gpus=1
  QwenPredictor: CPU device      QwenPredictor: picks cuda if available
  map_batches: concurrency=2     map_batches: concurrency=gpu_count
                                   batch_size=32, num_gpus=1
```

FastAPI, Postgres, PVC, all manifests for the control plane: zero
changes.

This is the whole reason the profile split was worth doing.

## Diagram: the same system, two profiles

```
                  +-----------------------+
                  |     FastAPI + DB      |
                  +-----------+-----------+
                              |
         +--------------------+--------------------+
         |                                         |
         v                                         v
   +-----------+                            +-----------+
   |  CPU      |                            |  GPU      |
   |  workers  |                            |  workers  |
   |  x2       |                            |  x2       |
   |  bf16 CPU |                            |  bf16 CUDA|
   +-----+-----+                            +-----+-----+
         |                                         |
         +-----> same PVC <------------------------+
                 same QwenPredictor class
                 same batch_infer.py
                 same map_batches pipeline
```

Two profiles share everything except the worker image, one
manifest, and one runtime check.

Continue to [08-production.md](08-production.md).
