# 05 - KubeRay: Running Ray on Kubernetes

We now add the compute plane. The goal of this section:

- Install KubeRay into the cluster.
- Understand what an operator and a custom resource are.
- Create a RayCluster (1 head + 2 workers) declaratively.
- Wire the FastAPI pod to submit jobs to it.
- Run the full end-to-end flow: `curl` -> Ray -> `results.jsonl`.

## Where we're going

```
                  kind cluster
 +--------------------------------------------------+
 |                                                  |
 |   +--------+         +---------+                 |
 |   |  API   |<------->| RayHead |<--+             |
 |   +---+----+  :8265  +---------+   | schedules   |
 |       |                            |             |
 |       |           +----------------+--------+    |
 |       |           |                         |    |
 |       |      +---------+               +---------+|
 |       |      | worker1 |               | worker2 ||
 |       |      +---------+               +---------+|
 |       v           |                         |     |
 |   +-------+       v                         v     |
 |   |  PVC  |<------+-------------------------+     |
 |   +-------+   /data/batches shared everywhere     |
 |                                                  |
 +--------------------------------------------------+
```

API talks to head (Jobs REST). Head schedules tasks on workers.
Workers read input.jsonl and write results.jsonl on the PVC. API
reads results.jsonl from the same PVC.

## Operators in 90 seconds

Kubernetes knows built-in resources: Pod, Deployment, Service,
ConfigMap, PVC. It does **not** know what a Ray cluster is.

An **operator** extends Kubernetes with a new resource type.
Installing the KubeRay operator does two things:

1. Registers a **CustomResourceDefinition (CRD)** called
   `RayCluster`, teaching the Kubernetes API server that such a
   resource exists and what its schema looks like.
2. Deploys an **operator pod** that watches for `RayCluster`
   objects and reconciles them into ordinary pods and services.

```
   you apply YAML      KubeRay operator (running pod)
        |                         ^
        v                         |
   +-----------+                  |
   | RayCluster|  watches  ------>|
   |  object   |                  |
   +-----------+                  |
                                  v
                        +--------------------+
                        | creates and manages|
                        |   pods + services  |
                        +--------------------+
```

Without the operator the API server accepts your RayCluster YAML,
stores it in etcd, and does nothing with it. The operator is what
turns "declared" into "running."

## Layer 12: install KubeRay

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update
helm install kuberay-operator kuberay/kuberay-operator \
  --version 1.6.0 \
  --namespace kuberay-system \
  --create-namespace \
  --wait
```

Helm is a Kubernetes package manager. The `kuberay-operator` chart
installs CRDs + RBAC + the operator Deployment in one go.

Verify:

```bash
kubectl get crd | grep ray.io
# rayclusters.ray.io
# rayjobs.ray.io
# rayservices.ray.io

kubectl get pods -n kuberay-system
# kuberay-operator-... Running 1/1
```

Both must pass. The CRDs prove the API server is Ray-aware; the
pod proves something is listening.

## Layer 13: build the Ray worker image

Ray workers need a container image with Ray + Transformers + the
Qwen weights pre-baked. Create `Dockerfile.worker`:

```dockerfile
FROM rayproject/ray:2.54.1-py310-cpu

USER root
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

USER ray
WORKDIR /home/ray

COPY --chown=ray:users worker-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV HF_HOME=/home/ray/.cache/huggingface

RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download(repo_id='Qwen/Qwen2.5-0.5B-Instruct', \
    allow_patterns=['*.json', '*.safetensors', '*.txt', 'tokenizer*']) "

USER root
RUN mkdir -p /app/jobs && chown -R ray:users /app
USER ray
COPY --chown=ray:users batch_infer.py /app/jobs/batch_infer.py
ENV PYTHONPATH=/app
```

And `worker-requirements.txt`:

```txt
--index-url https://pypi.org/simple
--extra-index-url https://download.pytorch.org/whl/cpu

torch==2.3.1+cpu
transformers>=4.45,<5
accelerate>=0.34,<1
safetensors>=0.4.5
huggingface_hub>=0.25,<1
sentencepiece>=0.2.0
```

Also copy your Ray pipeline script (the one from
`02-python-and-ray-local.md`) into this directory renamed as
`batch_infer.py`, with two additions: `argparse` to accept
`--batch-id`, and `_SUCCESS` / `_FAILED` markers. The finished
version is at `inference/jobs/batch_infer.py` in the repo; open it
now and copy it in wholesale.

Build and load:

```bash
docker build -t local/ray-worker:2.54.1-cpu -f Dockerfile.worker .
kind load docker-image local/ray-worker:2.54.1-cpu --name tutorial-cluster
```

Expect 3-8 minutes on first run (torch CPU wheels + Qwen weights).

## Layer 14: the RayCluster manifest

Create `raycluster.yaml`:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata: { name: ray-cluster-sa, namespace: tutorial }
---
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: qwen-raycluster
  namespace: tutorial
spec:
  rayVersion: "2.54.1"
  enableInTreeAutoscaling: false

  headGroupSpec:
    serviceType: ClusterIP
    rayStartParams:
      dashboard-host: "0.0.0.0"
      num-cpus: "0"          # head is scheduler-only
    template:
      spec:
        serviceAccountName: ray-cluster-sa
        containers:
          - name: ray-head
            image: local/ray-worker:2.54.1-cpu
            ports:
              - { name: gcs,       containerPort: 6379 }
              - { name: dashboard, containerPort: 8265 }
              - { name: client,    containerPort: 10001 }
            resources:
              requests: { cpu: "1", memory: "2Gi" }
              limits:   { cpu: "2", memory: "3Gi" }
            volumeMounts:
              - { name: batches, mountPath: /data/batches }
        volumes:
          - name: batches
            persistentVolumeClaim: { claimName: batches-pvc }

  workerGroupSpecs:
    - groupName: cpu-workers
      replicas: 2
      minReplicas: 2
      maxReplicas: 2
      template:
        spec:
          serviceAccountName: ray-cluster-sa
          containers:
            - name: ray-worker
              image: local/ray-worker:2.54.1-cpu
              resources:
                requests: { cpu: "2", memory: "5Gi" }
                limits:   { cpu: "2", memory: "5Gi" }
              volumeMounts:
                - { name: batches, mountPath: /data/batches }
          volumes:
            - name: batches
              persistentVolumeClaim: { claimName: batches-pvc }
```

Apply and wait:

```bash
kubectl apply -f raycluster.yaml

# Wait for the cluster state to become "ready"
kubectl -n tutorial get rayclusters -w
# Watch until STATE column reports "ready"
# Ctrl-C once it's ready
```

Inspect what the operator created:

```bash
kubectl get pods -n tutorial
# qwen-raycluster-head-xxx              Running
# qwen-raycluster-cpu-workers-xxx       Running
# qwen-raycluster-cpu-workers-yyy       Running
# batch-api-xxx                         Running

kubectl get svc -n tutorial
# qwen-raycluster-head-svc              ClusterIP   10.x.x.x   6379/TCP,8265/TCP,...
# batch-api                             ClusterIP   ...
```

You wrote one YAML object (`RayCluster`). KubeRay produced four
pods and two services. That is the operator pattern in action.

### Why `num-cpus: "0"` on the head

The head pod is the scheduler, the Jobs REST server, and the
dashboard. It has no business running inference. Setting
`num-cpus: "0"` tells Ray's scheduler "this node has zero
compute capacity" - actors cannot land there, only tasks land
on workers.

### Head vs worker, in one sentence each

- **Head**: scheduler, Jobs API, dashboard. Does not run model
  inference.
- **Worker**: runs `QwenPredictor` actors. Loads the model once
  per actor, processes batches.

## Layer 15: wire the API to submit Ray jobs

Now connect the two. Add to your API Deployment's `env:`:

```yaml
- name: RAY_ADDRESS
  value: http://qwen-raycluster-head-svc.tutorial.svc.cluster.local:8265
```

Then in `app.py`, inside `create_batch`, replace the TODO with the
real submission:

```python
from ray.job_submission import JobSubmissionClient
from starlette.concurrency import run_in_threadpool

_ray_client = JobSubmissionClient(settings.ray_address)

# Inside create_batch, after writing the DB row:
entrypoint = (
    f"python /app/jobs/batch_infer.py "
    f"--batch-id {batch_id} "
    f"--model {req.model} "
    f"--max-tokens {req.max_tokens}"
)
submission_id = await run_in_threadpool(
    _ray_client.submit_job,
    entrypoint=entrypoint,
    runtime_env={"working_dir": "/app"},
)
row.ray_job_id = submission_id
await session.commit()
```

Rebuild and reload:

```bash
docker build -t local/batch-api:dev -f Dockerfile.api .
kind load docker-image local/batch-api:dev --name tutorial-cluster
kubectl -n tutorial rollout restart deploy/batch-api
```

### What just got wired

```
                    RAY_ADDRESS env var
                         |
                         v
 FastAPI pod --------> Ray head svc (ClusterIP)
                         |
                         v
                    Ray Jobs REST API (:8265)
                         |
                         v
                   head schedules the job
                         |
                         v
              worker pod runs batch_infer.py
                         |
                         v
                 PVC: results.jsonl
                         |
                         v
                   _SUCCESS marker
                         |
                         v
              API poller notices, updates DB
```

## Layer 16: run it end to end

Make sure `kubectl port-forward svc/batch-api 8000:8000` is still
running. Submit:

```bash
curl -s -X POST http://localhost:8000/v1/batches \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: demo-api-key-change-me' \
  -d '{
    "model":"Qwen/Qwen2.5-0.5B-Instruct",
    "input":[
      {"prompt":"What is 2+2?"},
      {"prompt":"Name three planets."}
    ],
    "max_tokens":48
  }' | jq
```

In another terminal, watch worker logs:

```bash
kubectl logs -n tutorial -l ray.io/node-type=worker --tail=50 -f
```

Within a minute you should see `Loaded model Qwen/...`, then per
prompt generation, then `Batch ... done: total=2 completed=2 failed=0`.

Poll status:

```bash
BATCH=<the id you got>
curl -s -H 'X-API-Key: demo-api-key-change-me' \
  http://localhost:8000/v1/batches/$BATCH | jq .status
# "queued" -> "in_progress" -> "completed"
```

Stream results:

```bash
curl -s -H 'X-API-Key: demo-api-key-change-me' \
  http://localhost:8000/v1/batches/$BATCH/results | jq
```

Each line: `{id, prompt, response, finish_reason, prompt_tokens, ...}`.

You now have a complete batch inference service running on
Kubernetes with KubeRay. Congratulations.

## What happened, start to finish

```
1. curl POST /v1/batches
        |
        v
2. FastAPI (batch-api pod)
        |
        +-- Pydantic validates
        |   Depends(require_api_key) authenticates
        +-- write /data/batches/<id>/input.jsonl
        |   (on the shared PVC)
        +-- INSERT into batches (status=queued)
        +-- Ray JobSubmissionClient.submit_job
        |   -> HTTP POST to Ray head :8265
        +-- UPDATE batches SET ray_job_id=...
        +-- return 202 Accepted
                  |
                  v
3. (seconds later) Ray head schedules the job
        |
        v
4. batch_infer.py runs on a worker
        |
        +-- ray.init(address="auto")
        +-- ray.data.read_json(input.jsonl)
        +-- ds.map_batches(QwenPredictor, concurrency=2, batch_size=8)
        |   -> two worker actors load Qwen once each
        |   -> process 1 row at a time inside __call__
        +-- write /data/batches/<id>/results.jsonl
        +-- write /data/batches/<id>/_SUCCESS
                  |
                  v
5. FastAPI poller ticks (every 5s)
        |
        +-- sees _SUCCESS on disk
        +-- UPDATE batches SET status=completed, counts=...
                  |
                  v
6. curl GET /v1/batches/<id>         (status=completed)
7. curl GET /v1/batches/<id>/results (stream NDJSON)
```

Every arrow is a decoupling. No component does another's work.

## What you wrote vs what the repo does

This is the same system as the finished repo. Differences:

- Tests. The repo has 169 tests at 100% line + branch coverage
  via `pytest`. The tutorial has none.
- Observability. The repo exposes `/metrics` with Prometheus
  counters, attaches `X-Request-ID` via middleware, and logs JSON
  with a ContextVar-scoped `batch_id`. The tutorial has plain
  logging.
- Splitting. On `main`, the single `app.py` is split into
  `api/src/config.py`, `api/src/db.py`, `api/src/routes/`, etc.
  On `demo/single-file-version` the whole thing is back in one
  file, like our tutorial.
- Dashboard NodePort, monitoring stack, a benchmark script, and a
  few more `make` targets.

None of that changes the architecture. What you just built is the
architecture.

## Teardown between sessions

```bash
# Delete workloads only, keep the cluster:
kubectl delete -n tutorial -f api.yaml
kubectl delete -n tutorial -f raycluster.yaml

# Or nuke the cluster:
kind delete cluster --name tutorial-cluster
```

## Verify

```bash
kubectl get rayclusters -n tutorial     # ready
kubectl get pods -n tutorial            # head, 2 workers, api, all Running
curl localhost:8000/health              # ok
ls /tmp/batches/                        # one dir per batch submitted
```

Continue to [06-deep-understanding.md](06-deep-understanding.md).
