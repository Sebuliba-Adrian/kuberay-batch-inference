# 06 - Deep Understanding

You have a working system. This section is about being able to
draw it on a whiteboard from memory and explain any failure mode.

## The stack as five layers

```
+-----------------------------------------------+
| Layer 5: KubeRay                              |
|   translates RayCluster YAML into real pods   |
+-----------------------------------------------+
| Layer 4: Ray cluster                          |
|   head schedules, workers run actors          |
+-----------------------------------------------+
| Layer 3: FastAPI control plane                |
|   validate, auth, ledger, submit, stream      |
+-----------------------------------------------+
| Layer 2: Kubernetes                           |
|   pods, services, PVCs, ConfigMaps, secrets   |
+-----------------------------------------------+
| Layer 1: Linux + Docker                       |
|   containers, filesystems, networking         |
+-----------------------------------------------+
```

Every layer provides exactly the abstractions the layer above it
needs, and no more. That is why each one is replaceable.

## Four words that get mixed up

- **CRD (CustomResourceDefinition)**: the schema. Tells the API
  server "a new kind called X exists and looks like this."
- **Custom resource**: an instance of X. The YAML you apply.
- **Controller**: a reconciliation loop. Watches objects of a
  kind, compares desired state to actual, and acts.
- **Operator**: a controller with application-specific knowledge.
  KubeRay's controller knows Ray, so it is an operator.

```
  CRD           defines type
    \
     \
   custom resource      an instance
       \
        \
     controller/operator        makes it real
         \
          \
        ordinary K8s resources (Pods, Services, ...)
```

## Where Ray's vocabulary fits

```
Ray (everything)
  |
  +-- Ray Core          tasks, actors, object store
  +-- Ray Data          distributed datasets, map_batches
  +-- Ray Jobs          REST API to submit scripts to a cluster
  +-- Ray Serve         online model serving (not used here)
  +-- Ray AIR           high-level ML workflows (not used here)
  +-- RayCluster CRD    KubeRay's Kubernetes-native cluster object
  +-- RayJob CRD        KubeRay's ephemeral "one job, one cluster"
                        (also not used here - we use the Jobs REST
                         API against a long-running RayCluster)
```

Two things called "Ray Jobs":

1. **Ray Jobs API** - runtime REST endpoint on `:8265`. We use
   this via `JobSubmissionClient`.
2. **RayJob CRD** - Kubernetes-native alternative that spawns a
   fresh RayCluster per job. We deliberately do not use it
   because cold-starting a cluster per batch adds ~60s of
   overhead.

## Request flow, exhaustively

```
Time  | Component       | Action
------+-----------------+--------------------------------------------
t=0   | Client          | POST /v1/batches with prompts
t+1ms | FastAPI         | Pydantic validates CreateBatchRequest
t+2ms | FastAPI         | require_api_key checks X-API-Key
t+5ms | FastAPI         | _new_batch_id() -> batch_01ULID...
t+10  | FastAPI + PVC   | write input.jsonl to /data/batches/<id>/
t+15  | FastAPI + DB    | INSERT Batch(status=queued)
t+20  | FastAPI + Ray   | JobSubmissionClient.submit_job (via 8265)
                           runs in threadpool (blocking SDK)
t+30  | Ray head        | Accepts entrypoint, assigns job_id
t+30  | FastAPI + DB    | UPDATE batches SET ray_job_id=...
t+35  | FastAPI         | Return 202 with BatchObject JSON
------+-----------------+--------------------------------------------
(async)
t+100 | Ray head        | Schedules batch_infer.py onto a worker
t+200 | Ray worker      | ray.init(address="auto")
t+5s  | Ray worker      | Loads Qwen into a QwenPredictor actor
t+6s  | Ray Data        | read_json -> dataset
t+6s  | Ray Data        | map_batches starts invoking __call__
t+10s | Ray worker      | Writes results.jsonl (staging -> rename)
t+10s | Ray worker      | Writes _SUCCESS
------+-----------------+--------------------------------------------
t+5s  | Poller          | Tick: lists active batches
(+5s) |                 | _poll_one reads _SUCCESS
      | Poller + DB     | UPDATE batches SET status=completed
------+-----------------+--------------------------------------------
t+later | Client        | GET /v1/batches/<id>/results
      | FastAPI + PVC   | Stream NDJSON back line-by-line
```

Every timestamp is a decoupling. The client's `POST` returned in
35ms. The Ray job ran for 10s. The poller noticed 5s later. The
client came back whenever they wanted.

## The filesystem is the source of truth

```
/data/batches/<batch_id>/
    input.jsonl      <- API writes, workers read
    results.jsonl    <- workers write, API reads
    _SUCCESS         <- workers write LAST; presence means "safe"
    _FAILED          <- workers write on exception; has traceback
```

Why markers and not the DB?

- The filesystem handshake lets Ray and FastAPI avoid a two-phase
  commit. If Ray crashes between writing `results.jsonl` and
  touching `_SUCCESS`, the reader sees no `_SUCCESS` and knows
  the results are untrusted.
- Re-running a batch simply overwrites the files. The DB row's
  `status` is restorable from the filesystem at any time.
- It scales: swap the PVC for S3 or GCS, the marker pattern is
  the same.

## Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| API pod crashes after writing input, before submit | On restart, poller sees `queued` rows with no `ray_job_id` -> resubmit | Automatic |
| One prompt errors inside `__call__` | Row-level `error` field set, other rows OK | Client inspects `error` column |
| Ray worker OOMs mid-generation | Ray marks job failed -> worker writes `_FAILED` | Client retries |
| Ray head unreachable at submit time | `submit_job` raises -> 503 back to client | Client retries |
| Postgres unavailable at boot | `lifespan` retries, `/ready` returns 503 | Automatic |
| `_SUCCESS` written but results.jsonl partial | Impossible: `_SUCCESS` is written last, after rename | N/A |
| Worker pod deleted mid-job | Ray reassigns to a surviving worker if possible; else job fails | `_FAILED` + retry |

The `_SUCCESS`-written-last invariant is what makes most of this
recoverable without distributed transactions.

## Who talks to whom

```
  Client  ---->  FastAPI  ---->  Postgres          (metadata only)
                    |
                    +---->  Ray head (Jobs REST)   (control plane)
                    |
                    +---->  PVC (files)            (data plane)

                  Ray head  ---->  Ray workers     (task scheduling)

                  Ray workers  ---->  PVC          (input + output files)
```

The client never talks to Ray. Ray never talks to Postgres. The
PVC is the only shared surface between the control plane and the
compute plane.

## Operator pattern, one more time

```
  your YAML:
  +---------------------+
  | kind: RayCluster    |
  | spec:               |
  |   head: ...         |
  |   workers:          |
  |     replicas: 2     |
  +---------------------+
         |
         | kubectl apply
         v
  +---------------------+
  | Kubernetes API      |
  | validates schema,   |
  | stores in etcd      |
  +---------------------+
         |
         | event: "RayCluster created"
         v
  +---------------------+
  | KubeRay operator    |
  | (reconcile loop)    |
  +----+----+----+------+
       |    |    |
       v    v    v
  +----+ +------+ +--------+
  |head|  |svc  | |workers |
  +----+ +------+ +--------+
```

The operator is just a pod running Go code that watches, compares,
and calls the Kubernetes API. You could write one; tools like
Kubebuilder and Operator SDK scaffold the boilerplate.

## What makes this system "production-shaped"

Six properties, all intentional:

1. **Control plane / compute plane split.** API scales independently
   of inference.
2. **Immediate acceptance, async processing.** Client never holds
   an HTTP connection for 10 minutes.
3. **Durable ledger.** Postgres tracks every batch; nothing is
   lost on pod restart.
4. **Filesystem handshake with markers.** No distributed
   transaction needed.
5. **Idempotent retry.** Re-running a batch overwrites files
   deterministically.
6. **K8s-native cluster definition.** RayCluster YAML is reviewable,
   version-controllable, portable across clouds.

If you remember nothing else, remember those six.

## The one-paragraph pitch

> This service exposes a FastAPI API for submitting LLM batches.
> When a batch arrives, the API validates it, authenticates it,
> writes the inputs to a shared PVC, stores a metadata row in
> Postgres, and submits a distributed job to a long-running Ray
> cluster managed by KubeRay on Kubernetes. Ray workers load the
> Qwen model once per actor, process the prompts in parallel using
> `ray.data.map_batches`, and write NDJSON results back to the
> PVC. A FastAPI background poller keeps Postgres status in sync
> with Ray, and clients later fetch status and stream results
> through the API.

If you can say that without looking, you understand the system.

Continue to [07-gpu.md](07-gpu.md).
