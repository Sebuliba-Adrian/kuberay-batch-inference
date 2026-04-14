# 08 - Production Considerations

What you built is production-shaped but not production-ready. This
section enumerates what would change for a real deployment and why
we did not build it.

## The gap list

```
what we have                        what production needs
----------------------------------  ----------------------------------
hostPath PVC (single-node RWX)      cloud RWX: EFS / Filestore /
                                      Azure Files / S3+abstraction
SQLite / bare Postgres Deployment   managed Postgres (Cloud SQL /
                                      RDS / Azure Flexible Server)
port-forward to localhost:8000      Ingress + TLS (cert-manager +
                                      Let's Encrypt)
hardcoded API key in a Secret       External Secrets Operator pulling
                                      from KMS / Vault / Doppler
no metrics                          /metrics + Prometheus + Grafana,
                                      Ray dashboard exposed
no tests                            unit + integration, CI-enforced
                                      100% line+branch coverage
single replica of the API           N replicas behind the Service,
                                      HPA tied to request rate
fixed worker count (2)              autoscaling on RayCluster
                                      (min=2, max=N)
per-prompt generation loop          true batched generate() with
                                      left-padding, or vLLM
locally-built images                CI-built images pushed to GAR /
                                      ACR / ECR / DOCR
make up-gpu-local                   Kustomize overlays or Helm chart
                                      per-cloud
```

Some of these are one-line changes. Others are weeks of work.

## Shared storage: the real portability problem

`hostPath` + `extraMounts` works because kind is one node. The
moment you have multiple nodes, you need **ReadWriteMany**
storage. Default cloud block storage (EBS, PD, Azure managed
disks) is ReadWriteOnce.

```
cloud        | RWX option          | cost floor
-------------+---------------------+------------
GKE          | Filestore CSI       | ~$50/month
AKS          | Azure Files CSI     | cheap (Premium for low latency)
EKS          | EFS CSI             | ~$0.30/GB/month
DOKS         | no native RWX       | need Longhorn or NFS add-on
```

Honest recommendation for a real deployment: replace the PVC with
**object storage**. Change `batch_infer.py` to read/write
`s3://bucket/batches/<id>/input.jsonl` via `ray.data.read_json`
(which supports S3/GCS natively). Costs pennies, scales forever,
no RWX dance. The `_SUCCESS` marker trick works identically on
object storage.

```
   laptop / kind              cloud (recommended)
  ---------------------     --------------------------
   PVC (hostPath RWX)        object storage bucket
         |                         |
         +----> API                +----> API (PUT input.jsonl)
         +----> Ray workers        +----> Ray workers (GET/PUT)
         +----> API (read results) +----> API (stream via signed URL)
```

Same handshake, different backend.

## Ingress and TLS

Expose the API publicly with an Ingress controller (nginx-ingress
Helm chart on any cloud) plus cert-manager issuing Let's Encrypt
certificates.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: batch-api
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  ingressClassName: nginx
  tls:
    - hosts: [api.example.com]
      secretName: batch-api-tls
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: batch-api
                port: { number: 8000 }
```

Point your DNS at the ingress controller's LoadBalancer IP. Done.

## Secrets properly

A hardcoded key in a Kubernetes Secret is visible to anyone with
`kubectl get secret` access. In real systems:

```
  ExternalSecret object (ESO)
        |
        v
  pulls from KMS / Vault / Doppler
        |
        v
  materializes as a native K8s Secret
        |
        v
  your pod reads it via env var as before
```

App code stays identical (`settings.api_key` from env). Only the
secret's origin changes.

## Observability

Install Prometheus + Grafana:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack -n monitoring --create-namespace
```

In the FastAPI app, expose `/metrics`:

```python
from prometheus_client import Counter, CollectorRegistry, generate_latest

REGISTRY = CollectorRegistry()
REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"],
                   registry=REGISTRY)

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(REGISTRY), media_type="text/plain")
```

Counters you will want at minimum:

- `http_requests_total{method,path,status}` - labelled by matched
  route template, never by literal path (cardinality explosion).
- `batch_submitted_total{model}` - for rate dashboards.
- `batch_terminal_total{status}` - completed / failed / cancelled.
- `rayjob_submit_failures_total` - Ray submission 5xx's.

Label the HTTP counter by `request.scope["route"].path` (the
template), not `request.url.path` (the literal), or you will
produce a unique time-series per batch id.

## Testing

The finished repo has 169 pytest cases at 100% line + branch
coverage. Key strategies that are worth copying:

- Use `httpx.AsyncClient` against a test `FastAPI` instance built
  by `create_app()`.
- Swap the Postgres URL to `sqlite+aiosqlite:///:memory:` in
  tests; SQLAlchemy async code is the same.
- Mock the Ray `JobSubmissionClient` with a fake that returns
  deterministic submission ids.
- Write one integration test per state transition in the
  `queued -> in_progress -> completed / failed / cancelled`
  state machine.

The tutorial skipped tests to stay focused, not because they are
optional.

## Autoscaling

Two independent axes:

### API replicas (horizontal pod autoscaling)

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: batch-api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: batch-api
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: 70 }
```

### Ray worker autoscaling

KubeRay's `RayCluster` supports in-tree autoscaling:

```yaml
spec:
  enableInTreeAutoscaling: true
  workerGroupSpecs:
    - groupName: cpu-workers
      minReplicas: 2
      maxReplicas: 20
```

Ray's autoscaler looks at queued tasks and adds workers until the
queue drains, then scales down.

## Inference optimizations we deliberately skipped

1. **vLLM.** Continuous batching + PagedAttention gives 5-10x
   throughput on GPU. The whole reason Ray Data has
   `ray.data.llm.vLLMEngineProcessorConfig` + `build_llm_processor`.
   Worth the swap on GPU; overkill on CPU.

2. **True batched `generate()`.** Our per-prompt loop inside
   `__call__` wastes GPU cycles. With left-padded batched
   generation you get another ~10x on GPU without changing
   engines.

3. **KV-cache reuse.** For prompts that share prefixes (e.g. a
   system prompt), cache the key/value tensors. Big win in RAG.

4. **Speculative decoding.** Use a tiny draft model to speculate
   tokens the big model verifies.

None of these are architectural changes. They are compute-plane
optimizations, all contained in `QwenPredictor` or its
replacement.

## Per-cloud notes

```
GKE:
  - Artifact Registry for images
  - Standard or Autopilot, NVIDIA GPU pools with gke-accelerator label
  - Filestore for RWX, Cloud SQL for Postgres
  - Workload Identity for credentials (no secret files)

AKS:
  - ACR + `az aks update --attach-acr`
  - NCasT4 / NCas v5 node pools, NVIDIA GPU Operator for drivers
  - Azure Files Premium for RWX, Flexible Server for Postgres
  - AAD Workload Identity for credentials

EKS:
  - ECR, IRSA for pod-level IAM
  - EFS for RWX, RDS for Postgres
  - Karpenter for node autoscaling

DOKS:
  - DOCR for images
  - GPU pools (relatively new), native RWX weak - use Spaces (S3)
  - Managed Postgres
```

The per-cloud diff is about **where images live**, **where files
live**, and **how secrets are resolved**. The K8s manifests barely
change.

## Recommended migration order

If you want to take this from laptop to cloud:

1. **Object storage first.** Swap PVC reads/writes for
   `ray.data.read_json("s3://...")` and an `aioboto3`-backed
   storage helper in the API. This is the biggest leap.
2. **Managed Postgres.** Change one URL.
3. **Image registry.** `docker push`, update image tags in
   manifests.
4. **Ingress + TLS.** Add one Ingress + cert-manager.
5. **Secrets via ESO.** Move the API key out of the Git repo.
6. **Observability.** Install the Helm chart, annotate the API
   pod for scrape.
7. **Autoscaling.** Add HPA + flip `enableInTreeAutoscaling`.
8. **CI builds and pushes images.** GitHub Actions + OIDC to the
   cloud, no static credentials.

Skip any of these and the thing still works. Skip all of them and
you have a laptop demo.

Continue to [09-teaching.md](09-teaching.md).
