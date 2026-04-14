# Notes

Session-focused notes capturing the architecture walkthrough, glossary,
request flow, and practical mental models established during the
walkthrough.

This document is written against the current checked-out branch:
`demo/single-file-version`.

It complements:

- `docs/ARCHITECTURE.md` - design rationale and trade-offs
- `docs/ARCHITECTURE_WALKTHROUGH.md` - onboarding walkthrough

This file is specifically about what was clarified in order,
for someone coming from a VPS/LAMP background and learning FastAPI,
Kubernetes, Ray, and KubeRay at the same time.

---

## 1. Branch and repo reality

The current branch is:

```text
demo/single-file-version
```

Important implication:

- the FastAPI control plane is intentionally collapsed into one file:
  `api/src/app.py`
- the README still reflects the original modular shape from `main`
- this branch is meant to demonstrate that the API can run as one file
  without changing behavior

Practical reading rule for this branch:

- start with `api/src/app.py`
- treat the README's split-module references as historical context

---

## 2. First-principles stack summary

The application is a distributed offline batch-inference system.

Its main layers are:

| Layer | What it is | What it does here |
|---|---|---|
| FastAPI | Python web framework | Exposes the external HTTP API, validates requests, authenticates, persists metadata, submits Ray jobs, streams results |
| Postgres | relational database | Stores batch metadata only: ids, status, counts, timestamps, paths, Ray job id |
| Ray | distributed Python runtime | Executes the actual batch inference work |
| KubeRay | Kubernetes operator for Ray | Creates and manages the Ray cluster on Kubernetes |
| Kubernetes | container orchestration platform | Runs all services as pods, mounts storage, provides service discovery, restarts crashed containers |
| Shared PVC | shared mounted storage | Lets the API and Ray workers exchange batch input/output files |

One-sentence summary:

> FastAPI is the control plane, Ray is the compute plane, Postgres is the
> ledger, shared storage is the handoff medium, Kubernetes runs everything,
> and KubeRay manages the Ray cluster inside Kubernetes.

---

## 3. VPS/LAMP mental translation

The user explicitly said they are used to VPS/LAMP-style deployments.
The most useful translation we established is:

| VPS/LAMP concept | Equivalent here |
|---|---|
| one server box | Kubernetes node |
| system service | pod / deployment |
| `.env` / Apache config / systemd env | ConfigMap + Secret |
| local folder on disk | PersistentVolume / PVC |
| localhost service connections | Kubernetes service DNS |
| queue worker / cron / background script | Ray job |
| app writes file, worker reads same file | API pod and Ray workers share `/data/batches` through a PVC |

The key onboarding point:

- the architecture is not conceptually alien
- the same concerns exist
- they are just separated into managed services instead of one machine

---

## 4. What each Ray term means

We explicitly distinguished several Ray-related terms because they sound
similar but mean different things.

### 4.1 Ray

Ray is the distributed Python framework.

It provides the runtime and scheduler that let Python code execute across
multiple workers instead of one process.

### 4.2 Ray cluster

A Ray cluster is one running Ray environment:

- one head node
- one or more worker nodes

In this repo, the cluster is defined in:

- `k8s/raycluster/raycluster.yaml`

### 4.3 KubeRay

KubeRay is the Kubernetes operator for Ray.

It teaches Kubernetes how to manage Ray-specific resources such as:

- `RayCluster`
- `RayJob`
- `RayService`

This repo uses KubeRay so Kubernetes can create and maintain a Ray cluster
declaratively from YAML.

### 4.4 Ray Jobs

Ray Jobs is the job-submission interface for a running Ray cluster.

It answers:

- how do I submit work to Ray?
- how do I get a job id back?
- how do I check job status?

This repo uses the Ray Jobs API through `JobSubmissionClient`.

FastAPI submits:

```text
python /app/jobs/batch_infer.py --batch-id ... --model ... --max-tokens ...
```

### 4.5 Ray Data

Ray Data is Ray's distributed dataset processing layer.

It answers:

- how do I read JSONL into a dataset?
- how do I split it into blocks?
- how do I process it in parallel?
- how do I write the output back?

This repo uses:

- `ray.data.read_json(...)`
- `repartition(...)`
- `map_batches(...)`
- `write_json(...)`

### 4.6 Ray Serve

Ray Serve is Ray's online serving layer for live model endpoints.

It is not the main tool used here, because this app is an offline batch
system, not an online inference-serving app.

### 4.7 Important combined sentence

The key sentence we established in-session was:

> KubeRay runs Ray on Kubernetes, Ray Jobs starts work on the cluster,
> and Ray Data does the parallel batch processing inside that work.

---

## 5. Who talks to whom

We drew and clarified the interaction topology:

```text
Client -> FastAPI API pod
FastAPI API pod -> Postgres
FastAPI API pod -> Ray head
FastAPI API pod -> shared storage
Ray head -> Ray workers
Ray workers -> shared storage
Client <- FastAPI API pod
```

Important constraints clarified in the session:

- the client does not talk to Ray directly
- Ray workers do not talk to Postgres directly
- FastAPI is the bridge between the external API world and the Ray world

This is a clean separation of control plane and compute plane.

---

## 6. Shared storage: what goes there and why

One major topic in the session was the shared PVC mounted at:

```text
/data/batches
```

We clarified that this is where the actual batch payload files live.

### 6.1 What goes into shared storage

For each batch, the app creates:

```text
/data/batches/<batch_id>/
```

Inside that directory, the important files are:

- `input.jsonl`
- `results.jsonl`
- `_SUCCESS`
- `_FAILED`

### 6.2 What each file means

`input.jsonl`

- written by the FastAPI API
- contains one prompt record per line
- this is what Ray reads

`results.jsonl`

- written by Ray after inference
- contains one output record per line
- this is what FastAPI streams back to the client

`_SUCCESS`

- written last, after a successful run
- signals that `results.jsonl` is complete and safe to read

`_FAILED`

- written if the batch job crashes at top level
- includes error information

### 6.3 Why shared storage exists

The core reason:

- API and Ray are in different pods
- pods do not share a filesystem by default
- the API needs to write inputs that Ray can read
- Ray needs to write outputs that the API can later serve

So the PVC is the file-based handshake between:

- control plane (FastAPI)
- compute plane (Ray)

### 6.4 Why not store all of this in Postgres

We explicitly clarified this in the session:

- Postgres is good for metadata
- Postgres is not the right place for large NDJSON payload exchange

So the design is:

- Postgres stores batch metadata
- shared storage stores batch payload files

That is a deliberate separation of concerns.

---

## 7. Step-by-step flow of `POST /v1/batches`

One of the main walkthroughs in the session was the exact request flow.

The route handler is:

- `create_batch()` in `api/src/app.py`

### 7.1 Before the route body runs

FastAPI has already done:

- request parsing
- request validation using Pydantic models
- API-key authentication

So by the time `create_batch()` runs:

- the payload is already typed and validated
- authentication has already passed

### 7.2 What `create_batch()` does

Step by step:

1. generate a `batch_id`
2. write `input.jsonl` under `/data/batches/<batch_id>/`
3. insert a Postgres row with `status="queued"`
4. build the Ray job command for `batch_infer.py`
5. submit that job through Ray Jobs
6. store the Ray `submission_id` in Postgres
7. return a batch object to the client

Important conclusion from the walkthrough:

- FastAPI does not run the model itself
- FastAPI just accepts the batch and dispatches compute work

That is the control-plane role.

---

## 8. How Ray crunches the batch file

We also walked through the batch worker script:

- `inference/jobs/batch_infer.py`

### 8.1 What happens when Ray runs the job

The job:

1. receives `batch_id`, `model`, and `max_tokens`
2. locates `/data/batches/<batch_id>/input.jsonl`
3. reads it with `ray.data.read_json(...)`
4. repartitions the dataset
5. processes it with `map_batches(QwenPredictor, ...)`
6. writes `results.jsonl`
7. writes `_SUCCESS` or `_FAILED`

### 8.2 How the actual model work happens

The session clarified the role of `QwenPredictor`:

- `__init__` loads tokenizer and model once per actor
- `__call__` processes a chunk of prompt rows

This means the model is not reloaded for every prompt.

That was an important practical learning:

- expensive model load is amortized across many rows
- worker actors stay warm

### 8.3 How Ray Data helps

We clarified that Ray is not treating the input as one giant monolithic
file. Instead:

- `input.jsonl` becomes a dataset
- the dataset is partitioned
- partitions are distributed across workers
- each worker processes its share

So "Ray crunching the batch file" means:

> Ray converts the input JSONL into a distributed dataset, splits it into
> chunks, runs model inference over those chunks on worker actors, and
> writes the results back as JSONL.

---

## 9. Ray head vs Ray worker

This distinction was explicitly clarified in the session.

### 9.1 Ray head

The Ray head is the coordinator.

Responsibilities:

- accept job submissions
- expose the Jobs API
- expose the Ray dashboard
- track cluster state
- schedule work onto workers

In this repo, the head is configured with:

```text
num-cpus: "0"
```

That means it is intentionally kept out of the heavy compute path.

### 9.2 Ray workers

The Ray workers do the actual heavy work.

Responsibilities:

- host `QwenPredictor` actors
- load the Qwen model
- process prompt rows
- write batch outputs

### 9.3 Core conclusion

The main practical sentence from the session:

> The Ray head is the dispatcher and scheduler; the Ray workers are the
> factory machines doing the real inference work.

That was one of the most important mental-model clarifications.

---

## 10. FastAPI concepts clarified in-session

The user also said they were relatively new to FastAPI, so part of the
session focused on basic FastAPI concepts as they appear in this repo.

### 10.1 App factory

`create_app()` builds and returns the FastAPI app.

This is the bootstrapping entrypoint used by Uvicorn.

### 10.2 Lifespan

`lifespan()` is the startup/shutdown hook.

In this repo, it:

- configures logging
- initializes the DB engine
- ensures the schema exists
- initializes the Ray client
- starts the background status poller

### 10.3 Pydantic request/response models

FastAPI uses typed Python classes for:

- request validation
- response serialization

That means request shape and validation rules live in code rather than in
manual parsing logic.

### 10.4 Dependencies

FastAPI dependencies are used here for authentication:

- `Depends(require_api_key)`

This is the equivalent of route-level middleware or controller guards.

### 10.5 Streaming responses

The session also clarified that FastAPI can stream files line-by-line
using `StreamingResponse`, which is how the app returns `results.jsonl`
without loading everything into memory.

---

## 11. Background poller and status reconciliation

We explicitly covered how the API knows when the job has finished.

The answer:

- a background poller runs inside the FastAPI process
- it periodically asks Ray for job status
- it also checks `_SUCCESS` / `_FAILED`
- it updates Postgres accordingly

This means:

- `GET /v1/batches/{id}` can stay fast and DB-only
- the client does not have to query Ray directly

This was a useful architectural learning point:

- request-time APIs should avoid doing expensive control-plane work if a
  background reconciliation loop can keep state fresh instead

---

## 12. Final distilled mental model

At the end of the session, the architecture was reduced to a practical
mental model that should be retained:

| Component | Mental role |
|---|---|
| FastAPI | receptionist / control plane |
| Postgres | ledger |
| Shared storage | warehouse / handoff folder |
| Ray head | dispatcher / scheduler |
| Ray workers | factory machines |
| KubeRay | Ray-specific cluster manager for Kubernetes |
| Kubernetes | building manager / container orchestrator |

And the one-paragraph explanation:

> The client submits a batch to FastAPI. FastAPI validates it,
> authenticates it, writes the input prompts to shared storage, stores a
> metadata row in Postgres, and submits a Ray job. Ray workers then read
> the shared input file, process the prompts in parallel using Ray Data
> and the Qwen model, and write the outputs back to shared storage. A
> background poller in FastAPI tracks Ray job status and updates Postgres.
> The client later calls the API again to fetch the batch status and
> stream the results.

---

## 13. Recommended reading order after this walkthrough

If continuing from this session, the best order is:

1. `api/src/app.py`
2. `k8s/api/deployment.yaml`
3. `k8s/storage/shared-pvc.yaml`
4. `k8s/postgres/`
5. `inference/jobs/batch_infer.py`
6. `k8s/raycluster/raycluster.yaml`
7. `docs/ARCHITECTURE_WALKTHROUGH.md`
8. `docs/ARCHITECTURE.md`

That order preserves the learning path established during the walkthrough:

- understand the API first
- then storage and persistence
- then the worker script
- then the cluster wiring
- then the higher-level design rationale
