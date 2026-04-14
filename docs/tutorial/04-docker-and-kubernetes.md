# 04 - Docker and Kubernetes

We have a working Python app. Now we turn it into containers and
run it inside a local Kubernetes cluster. Ray is still out of the
picture - this part is purely about packaging and deployment.

## Why bother

So far, "running the app" means:

```
host:  virtualenv + uvicorn process + SQLite file + Qwen weights in ~/.cache
```

This works on one laptop. Two problems:

1. The next person trying to run it has to reproduce your
   virtualenv, download weights, install the right Python version.
2. You can't run the API and multiple Ray workers together on the
   same machine without them colliding.

Containers solve (1). Kubernetes solves (2).

## The target picture

```
                   +-------------------------------+
                   |     kind cluster (Docker)     |
                   |                               |
                   |   +-------+     +---------+   |
                   |   |  API  |<--->|   DB    |   |
                   |   +---+---+     +---------+   |
                   |       |                       |
                   |       v                       |
                   |   +-------+                   |
                   |   |  PVC  |<------------------+--- shared volume
                   |   +-------+                   |
                   |                               |
                   +-------------------------------+
                              ^
                              |
                     kubectl port-forward
                              |
                         localhost:8000
```

One Docker container called `kind-control-plane` runs a whole
Kubernetes node. Inside that node we run the API and Postgres
pods. They share a PVC for batch files. You reach the API by
port-forwarding.

## Layer 6: Dockerize the API

Create `Dockerfile.api` in `~/batch-tutorial/`:

```dockerfile
# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.8 /uv /bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN uv venv /app/.venv --python 3.11 \
 && uv pip install --python /app/.venv/bin/python -r requirements.txt

FROM python:3.11-slim AS runtime

RUN groupadd -r app --gid 10001 \
 && useradd -r -g app -u 10001 -d /app -s /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app.py /app/app.py

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

And `requirements.txt`:

```txt
fastapi>=0.115,<1
uvicorn[standard]>=0.32,<1
pydantic>=2.8,<3
pydantic-settings>=2.5,<3
sqlalchemy[asyncio]>=2.0.30,<3
asyncpg>=0.29,<1
aiosqlite>=0.20,<1
aiofiles>=24.1,<25
python-ulid>=3.0,<4
httpx>=0.27,<1
ray[default]==2.54.1
```

Build:

```bash
docker build -t local/batch-api:dev -f Dockerfile.api .
```

### Two-stage build explained

```
+---------+       wheels+venv       +---------+
| builder |------------------------>|  runtime |
|  slim + |                         |  slim +  |
|  uv +   |                         |  venv +  |
|  build  |                         |  app.py  |
+---------+                         +---------+
   ~200 MB                            ~350 MB
 (discarded)                        (what ships)
```

Keep build tools out of the runtime image. Only the final venv and
your app ship. Smaller surface, fewer vulnerabilities, faster pulls.

### Non-root user

The `USER app` line is a hard rule for Kubernetes: never run as
root in a pod. Read any production K8s security doc; this is the
first item.

## Layer 7: create a kind cluster

kind creates a whole Kubernetes cluster inside one Docker
container. No VMs, no installers, just `docker run`.

Create `kind-config.yaml`:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: tutorial-cluster
nodes:
  - role: control-plane
    extraMounts:
      - hostPath: /tmp/batches
        containerPath: /mnt/data
```

The `extraMounts` block mounts `/tmp/batches` from your host into
the kind node at `/mnt/data`. That path is where the shared PVC
will live. Without this mount, pods on the node can read and write
but the files are invisible to you on the host.

Create the cluster:

```bash
mkdir -p /tmp/batches && chmod 0777 /tmp/batches
kind create cluster --config kind-config.yaml
kubectl cluster-info --context kind-tutorial-cluster
```

You now have a Kubernetes cluster. Let's verify:

```bash
kubectl get nodes
# NAME                             STATUS   ROLES           AGE
# tutorial-cluster-control-plane   Ready    control-plane   30s
```

One node, `Ready`. That is your whole cluster.

### What kind just did

```
  your laptop
  +--------------------------------------------------+
  | docker                                           |
  |  +--------------------------------------------+  |
  |  | tutorial-cluster-control-plane (container) |  |
  |  |                                            |  |
  |  | ubuntu + containerd + kubelet + kubeadm    |  |
  |  | control plane:                             |  |
  |  |   - API server                             |  |
  |  |   - etcd                                   |  |
  |  |   - scheduler                              |  |
  |  |   - controller-manager                     |  |
  |  |                                            |  |
  |  | all pods you create run here too           |  |
  |  +--------------------------------------------+  |
  |                                                  |
  | /tmp/batches  <->  /mnt/data (via extraMounts)   |
  +--------------------------------------------------+
```

One Docker container. Full Kubernetes.

## Layer 8: load your image into kind

kind runs its own containerd. Images built by your host Docker are
invisible to pods in the cluster. You must explicitly load them:

```bash
kind load docker-image local/batch-api:dev --name tutorial-cluster
```

This is a ~30-second copy. Rebuild the image and re-run this
whenever you change code.

## Layer 9: the shared PVC

Kubernetes lets pods mount volumes. For our app we need a single
directory that both the API pod and (later) the Ray worker pods
can read and write. That needs **ReadWriteMany** access mode.

Create `storage.yaml`:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: batches-pv
spec:
  capacity:
    storage: 10Gi
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  claimRef:
    namespace: tutorial
    name: batches-pvc
  storageClassName: manual
  hostPath:
    path: /mnt/data
    type: DirectoryOrCreate
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: batches-pvc
  namespace: tutorial
spec:
  accessModes: [ReadWriteMany]
  resources:
    requests:
      storage: 10Gi
  storageClassName: manual
  volumeName: batches-pv
```

Create the namespace first and apply the PVC:

```bash
kubectl create namespace tutorial
kubectl apply -f storage.yaml
kubectl get pvc -n tutorial
# NAME           STATUS   VOLUME       CAPACITY   ACCESS MODES
# batches-pvc    Bound    batches-pv   10Gi       RWX
```

### PV vs PVC vs StorageClass

```
+-----------+         claims          +-----------+
|    PVC    |------------------------>|    PV     |
| (my pod   |                         | (actual   |
|  wants    |                         |  disk on  |
|  storage) |                         |  a node)  |
+-----------+                         +-----------+
      |
      v
 StorageClass (the "how" of provisioning -
               e.g. AWS EBS, NFS, hostPath)
```

A **PersistentVolume** is a disk. A **PersistentVolumeClaim** is a
pod's request for a disk. A **StorageClass** is the recipe for
creating PVs on demand. We bypass StorageClass here by declaring
the PV statically.

## Layer 10: deploy Postgres

For a real deployment we use Postgres. For the tutorial we keep
SQLite (already in `app.py`). Jump to deploying the API pod.

If you want to swap in Postgres, `k8s/postgres/` in the finished
repo shows the Deployment + Secret + init ConfigMap pattern.

## Layer 11: deploy the API

Create `api.yaml`:

```yaml
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: batch-api-config
  namespace: tutorial
data:
  RESULTS_DIR: /data/batches
  DATABASE_URL: sqlite+aiosqlite:///data/batches/tutorial.db
---
apiVersion: v1
kind: Secret
metadata:
  name: batch-api-secret
  namespace: tutorial
type: Opaque
stringData:
  API_KEY: demo-api-key-change-me
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: batch-api
  namespace: tutorial
spec:
  replicas: 1
  selector:
    matchLabels: { app: batch-api }
  template:
    metadata:
      labels: { app: batch-api }
    spec:
      containers:
        - name: api
          image: local/batch-api:dev
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8000
          env:
            - { name: RESULTS_DIR,  valueFrom: { configMapKeyRef: { name: batch-api-config, key: RESULTS_DIR } } }
            - { name: DATABASE_URL, valueFrom: { configMapKeyRef: { name: batch-api-config, key: DATABASE_URL } } }
            - { name: API_KEY,      valueFrom: { secretKeyRef: { name: batch-api-secret, key: API_KEY } } }
          volumeMounts:
            - name: batches
              mountPath: /data/batches
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 5
      volumes:
        - name: batches
          persistentVolumeClaim:
            claimName: batches-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: batch-api
  namespace: tutorial
spec:
  selector: { app: batch-api }
  ports:
    - port: 8000
      targetPort: 8000
```

Apply:

```bash
kubectl apply -f api.yaml
kubectl -n tutorial rollout status deploy/batch-api
kubectl get pods -n tutorial
```

### The shape of a deployment

```
+---------------------------------------------+
| Deployment "batch-api"                      |
|   replicas: 1                               |
|   selector: {app: batch-api}                |
|                                             |
|   Pod template                              |
|   +---------------------------------+       |
|   | container: api                  |       |
|   |   image: local/batch-api:dev    |       |
|   |   env: from ConfigMap + Secret  |       |
|   |   volumeMount: /data/batches    |       |
|   |   readiness: GET /health        |       |
|   +---------------------------------+       |
+---------------------------------------------+

+---------------------------------------------+
| Service "batch-api"                         |
|   selects pods with label app=batch-api     |
|   port 8000 -> pod port 8000                |
+---------------------------------------------+
```

The Deployment owns pods. The Service owns a stable virtual IP
that routes to whichever pods match the label. Pods come and go;
the Service IP stays constant.

## Port-forward and test

```bash
kubectl -n tutorial port-forward svc/batch-api 8000:8000
```

In another terminal:

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}

curl -s -X POST http://localhost:8000/v1/batches \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: demo-api-key-change-me' \
  -d '{"input":[{"prompt":"Hi"}]}' | jq
```

Same endpoint, same response. But now:

- The API runs as a container under Kubernetes.
- Its filesystem is ephemeral; the PVC persists batch files.
- The pod auto-restarts on crashes.
- Adding replicas is one line (`replicas: 3`).

## What you have now

```
  local/batch-api:dev (your Docker image)
           |
           v
  kind cluster (one Docker container)
           |
           v
  tutorial namespace
      |
      +-- Deployment "batch-api" -> Pod -> container (uvicorn)
      |                                          |
      |                                          v
      +-- PersistentVolumeClaim "batches-pvc" -> /data/batches
      |
      +-- Service "batch-api" -> localhost:8000 (via port-forward)
```

No Ray. No KubeRay. Just FastAPI on Kubernetes.

## What's in the finished repo

| Tutorial file | Finished repo file |
|---|---|
| `Dockerfile.api` | `api/Dockerfile` |
| `requirements.txt` | `api/pyproject.toml` |
| `kind-config.yaml` | `k8s/kind/kind-config.yaml` |
| `storage.yaml` | `k8s/storage/shared-pvc.yaml` |
| `api.yaml` | `k8s/api/*.yaml` (split per resource) |

The finished `Makefile` wraps all of this as:

```bash
make cluster-up      # creates kind cluster
make build-images    # docker build
make load-images     # kind load docker-image
make namespace       # kubectl create ns
make storage         # apply PVC + chmod
make api             # apply API manifests
make port-forward    # blocks
```

All steps you just ran by hand.

## Verify

```bash
kubectl get all -n tutorial
# deployment, pod, service, replicaset all Ready
```

Continue to [05-kuberay.md](05-kuberay.md).
