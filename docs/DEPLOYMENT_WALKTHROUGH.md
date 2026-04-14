# Deployment Walkthrough

The operational companion to `ARCHITECTURE_WALKTHROUGH.md`. That doc
explains *what the pieces are and how they talk*. This one explains
*how to stand the whole thing up*, end to end, and what to run to
verify each step is healthy.

Written against the `demo/single-file-version` branch, but the
Makefile targets, manifest paths, and namespaces are the same on
`main`.

---

## Table of contents

1. [The operator pattern in 90 seconds](#1-the-operator-pattern-in-90-seconds)
2. [CRD vs custom resource vs controller vs operator](#2-crd-vs-custom-resource-vs-controller-vs-operator)
3. [What `kubectl apply -f raycluster.yaml` actually does](#3-what-kubectl-apply--f-rayclusteryaml-actually-does)
4. [The 15-step setup, with verifications](#4-the-15-step-setup-with-verifications)
5. [The `make up` shortcut](#5-the-make-up-shortcut)
6. [What "healthy" looks like at each stage](#6-what-healthy-looks-like-at-each-stage)
7. [Tearing it down](#7-tearing-it-down)
8. [Debugging decision tree](#8-debugging-decision-tree)
9. [What you need to master vs what you can ignore](#9-what-you-need-to-master-vs-what-you-can-ignore)

---

## 1. The operator pattern in 90 seconds

Kubernetes knows built-in kinds (`Pod`, `Deployment`, `Service`). It
does not know what a Ray cluster is. An **operator** extends
Kubernetes with application-specific knowledge:

1. It installs a **CustomResourceDefinition (CRD)** that adds a new
   API kind (e.g. `RayCluster`) to the Kubernetes API server.
2. It runs a **controller** pod that watches objects of that kind and
   reconciles them into real resources (pods, services, etc.).

So "Kubernetes knows RayCluster" means two separate things:

- the CRD is installed (the API server accepts the YAML), and
- the KubeRay operator pod is running (something is watching and
  acting on the YAML).

Install one without the other and `kubectl get rayclusters` still
works, but nothing actually happens when you apply one.

---

## 2. CRD vs custom resource vs controller vs operator

Four terms that get conflated:

| Term | What it is | In this repo |
|---|---|---|
| **CRD** | Schema registration. Teaches the API server a new kind exists. | Installed by the KubeRay Helm chart. |
| **Custom resource** | An instance of that kind (a specific YAML object). | `k8s/raycluster/raycluster.yaml`. |
| **Controller** | A reconciliation loop that watches objects and makes reality match desired state. | The KubeRay operator is one. |
| **Operator** | A controller with domain-specific knowledge, usually packaged for install. | KubeRay. |

One-line chain:

> A CRD defines the type, a custom resource is an instance of that type,
> a controller watches instances, and an operator is the app-aware
> controller that reconciles them into real infrastructure.

---

## 3. What `kubectl apply -f raycluster.yaml` actually does

```
kubectl apply -f raycluster.yaml
  |
  v
API server validates against the RayCluster CRD schema, stores in etcd
  |
  v
KubeRay operator's informer fires: "new RayCluster seen"
  |
  v
Reconcile() reads .spec
  - headGroupSpec   -> creates 1 head Pod + head Service
  - workerGroupSpecs -> creates N worker Pods
  - mounts PVC at /data/batches
  - wires labels, selectors, probes
  |
  v
Updates .status on the RayCluster object as pods come ready
  |
  v
`kubectl get rayclusters -n ray` reports status=ready
```

The YAML you wrote is the *desired intent*. The operator is the
*compiler* that turns it into ordinary Kubernetes objects.

---

## 4. The 15-step setup, with verifications

All `make` targets are defined in the repo `Makefile`. Variables used
below come from the top of that file:

- `CLUSTER_NAME=kuberay-dev`
- `NAMESPACE=ray`
- `KUBERAY_NAMESPACE=kuberay-system`
- `KUBERAY_VERSION=1.6.0`

### Step 1 - Create the local Kubernetes cluster

```bash
make cluster-up
```

Spins up a kind cluster (`kuberay-dev`) using `k8s/kind/kind-config.yaml`,
including extraMounts that back the shared PVC's hostPath.

Verify:

```bash
kubectl cluster-info
kubectl get nodes
```

You should see one node in `Ready` state.

### Step 2 - Install the KubeRay operator

```bash
make kuberay-install
```

Adds the `kuberay` Helm repo, installs `kuberay-operator` at version
`1.6.0` into the `kuberay-system` namespace, and waits for the
deployment rollout.

Verify (two separate checks):

```bash
# A. CRDs installed?
kubectl get crd | grep ray.io
# expect: rayclusters.ray.io, rayjobs.ray.io, rayservices.ray.io

# B. Operator pod running?
kubectl get pods -n kuberay-system
# expect: kuberay-operator-... Running 1/1
```

Both must pass. Step A proves the API server accepts RayCluster
objects; step B proves something is watching them.

### Step 3 - Build the container images

```bash
make build-images
```

Builds two images locally:

- `local/batch-api:dev` from `api/Dockerfile` (FastAPI control plane)
- `local/ray-worker:<ray-version>-cpu` from `inference/Dockerfile`
  (Ray + transformers + Qwen2.5-0.5B weights + `batch_infer.py`)

The worker image is the important one. If it is missing or broken,
Ray worker pods will `ImagePullBackOff`.

Verify:

```bash
docker images | grep -E 'batch-api|ray-worker'
```

### Step 4 - Load images into kind

```bash
make load-images
```

kind runs a separate containerd inside its node, so images built on
the host are invisible until `kind load docker-image` copies them in.

### Step 5 - Create the `ray` namespace

```bash
make namespace
```

### Step 6 - Create shared storage

```bash
make storage
```

Applies `k8s/storage/shared-pvc.yaml` and `chmod 0777`s the hostPath
inside the kind node so non-root pods can write there. Without the
chmod, the API pod's first write to `/data/batches/` returns
`Errno 13`.

### Step 7 - Deploy Postgres

```bash
make postgres
```

Applies the Postgres Deployment, Service, PVC, and init ConfigMap
from `k8s/postgres/`.

Verify:

```bash
kubectl get pods -n ray -l app=postgres
# expect: postgres-... Running 1/1
```

### Step 8 - Apply the RayCluster

```bash
make raycluster
```

This is the operator moment. The command:

1. `kubectl apply -f k8s/raycluster/raycluster.yaml`
2. waits until the RayCluster reaches `status.state=ready`

Behind the scenes KubeRay creates:

- one head Pod (with `num-cpus: "0"` so it stays pure control plane)
- two worker Pods (the compute plane)
- services for the head (Jobs REST on 8265, GCS on 6379, etc.)

Verify:

```bash
kubectl get rayclusters -n ray
kubectl get pods -n ray
kubectl get svc -n ray | grep raycluster
```

You want one `head` pod and two `worker` pods, all `Running`, and the
RayCluster itself reporting `ready`.

### Step 9 - Deploy the FastAPI app

```bash
make api
```

Applies `k8s/api/configmap.yaml`, `secret.yaml`, `deployment.yaml`,
and `service.yaml`. The config map sets `RAY_ADDRESS` to the Ray head
service DNS, which is how FastAPI reaches the Jobs API.

Verify:

```bash
kubectl get pods -n ray -l app=batch-api
kubectl logs -n ray -l app=batch-api --tail=50
```

Look for `"lifespan start"` entries showing DB and Ray client
initialisation, and the poller starting.

### Step 10 - Port-forward the API

```bash
make port-forward
```

Blocks, forwarding `localhost:8000` to the API service inside the
cluster. Leave it running in its own terminal.

### Step 11 - Submit a batch

```bash
curl -X POST http://localhost:8000/v1/batches \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: demo-api-key-change-me-in-production' \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "input": [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}],
    "max_tokens": 50
  }'
```

The default API key comes from `k8s/api/secret.yaml` and is a demo
value. Rotate it before deploying anywhere real.

Response (immediately, status `queued`):

```json
{"id": "batch_01ABC...", "status": "queued", "model": "Qwen/Qwen2.5-0.5B-Instruct", ...}
```

### Step 12 - Watch Ray do the work

In another terminal:

```bash
make dashboard        # Ray dashboard on :8265
# or:
make logs-ray-workers
make logs-ray-head
```

The Ray job reads `input.jsonl` from the shared PVC, runs
`map_batches(QwenPredictor, ...)` across two worker actors, writes
`results.jsonl`, then writes `_SUCCESS`.

### Step 13 - Poll status

```bash
curl -H 'X-API-Key: demo-api-key-change-me-in-production' \
  http://localhost:8000/v1/batches/batch_01ABC.../
```

The poller updates Postgres every 5 seconds, so the status endpoint
is cheap and DB-only.

### Step 14 - Fetch results

```bash
curl -H 'X-API-Key: demo-api-key-change-me-in-production' \
  http://localhost:8000/v1/batches/batch_01ABC.../results
```

Streams `results.jsonl` line by line from the PVC.

### Step 15 - (Optional) monitoring stack

```bash
make monitoring-up
make grafana     # http://localhost:3000  admin/admin
make prometheus  # http://localhost:9090
```

Scrapes the API's `/metrics` endpoint and the Ray head.

---

## 5. The `make up` shortcut

For a one-shot bring-up:

```bash
make up
```

This is defined in the Makefile as:

```
up: cluster-up kuberay-install build-images load-images \
    namespace storage postgres raycluster api port-forward
```

It runs steps 1 through 10 in order. The final target
(`port-forward`) blocks, so you will be left with a working cluster
and a tunnel on `localhost:8000`.

---

## 6. What "healthy" looks like at each stage

Keep this table open while bringing the stack up:

| After step | Command | Expected |
|---|---|---|
| cluster-up | `kubectl get nodes` | 1 node, `Ready` |
| kuberay-install | `kubectl get crd \| grep ray.io` | 3 ray.io CRDs |
| kuberay-install | `kubectl get pods -n kuberay-system` | `kuberay-operator` `Running 1/1` |
| storage | `kubectl get pvc -n ray` | `shared-batches` `Bound` |
| postgres | `kubectl get pods -n ray -l app=postgres` | `Running 1/1` |
| raycluster | `kubectl get rayclusters -n ray` | `ready` |
| raycluster | `kubectl get pods -n ray` | 1 head + 2 workers, all `Running` |
| api | `kubectl get pods -n ray -l app=batch-api` | `Running 1/1`, ready |
| port-forward | `curl localhost:8000/health` | `{"status":"ok"}` |

If any row fails, stop and debug there. Downstream steps will only
make it worse.

---

## 7. Tearing it down

```bash
make down           # removes workloads, keeps the kind cluster
make cluster-down   # nukes the kind cluster entirely
```

`make down` is useful during iteration: it leaves the kind node and
the KubeRay operator in place so you can re-deploy in ~30 seconds
instead of rebuilding from scratch.

---

## 8. Debugging decision tree

When something is wrong, classify the failure first. The layers
fail differently:

```
curl returns connection refused
  -> port-forward died or api pod crashed
  -> kubectl get pods -n ray -l app=batch-api
  -> kubectl logs <api-pod>

curl returns 401
  -> wrong or missing X-API-Key header
  -> check k8s/api/secret.yaml for the current value

curl returns 503 or "Ray unavailable"
  -> Ray head unreachable from the API pod
  -> kubectl get svc -n ray | grep raycluster
  -> kubectl exec -n ray <api-pod> -- curl $RAY_ADDRESS/api/version

Batch stays in queued forever
  -> Ray job submission failed silently, or poller is not running
  -> kubectl logs <api-pod> | grep -i "rayjob\|submit"
  -> kubectl logs <ray-head-pod>

Batch goes to failed
  -> read the _FAILED marker
  -> kubectl exec -n ray <any-ray-pod> -- cat /data/batches/<id>/_FAILED
  -> check Ray worker logs: make logs-ray-workers

Worker pods are Pending
  -> usually resource requests exceed node capacity on kind
  -> kubectl describe pod <worker-pod> (look at Events)

Worker pods are ImagePullBackOff
  -> forgot `make load-images` after rebuilding
  -> re-run `make build-images load-images`

RayCluster never reaches ready
  -> operator is not reconciling
  -> kubectl logs -n kuberay-system -l app.kubernetes.io/name=kuberay-operator
```

Rule of thumb: start from the outside (curl) and move inward layer by
layer. The layer where symptoms first appear is usually where the bug
is.

---

## 9. What you need to master vs what you can ignore

Helm install is necessary but not sufficient. Treat KubeRay like
infrastructure software, not application code.

**Master these** (they decide whether your app works):

- RayCluster manifest semantics: head vs workers, resources, volumes,
  labels, `num-cpus: "0"` on the head.
- How FastAPI reaches the head (`RAY_ADDRESS`, service DNS).
- How the shared PVC is mounted into both the API and the Ray pods.
- What `make build-images && make load-images` does and why kind
  needs the second command.
- `kubectl get pods / svc / rayclusters` and how to read their
  status fields.
- Where each component's logs live (`make logs-api`,
  `make logs-ray-head`, `make logs-ray-workers`).

**Safe to ignore** unless you are debugging KubeRay itself:

- KubeRay's Go source, reconciler internals, controller-runtime
  event handling.
- How CRD OpenAPI validation is evaluated server-side.
- Helm chart internals beyond the values you override.

The split is the same one you already have with systemd or Nginx on
a VPS: configure it well, inspect it confidently, but do not read
its source unless you must.
