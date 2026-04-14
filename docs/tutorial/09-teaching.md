# 09 - Teaching It To Others

If you can explain this system to someone who has never seen it,
you really understand it. This section gives you the
scripts - literally the sentences - that hold up under questions.

## The 15-second pitch

> FastAPI accepts batch inference requests, writes inputs to a
> shared volume, and submits a distributed job to a Ray cluster
> managed by KubeRay on Kubernetes. Ray workers generate outputs
> in parallel and write them to the same volume. A background
> poller keeps Postgres status in sync so the API can serve cheap
> reads.

## The 60-second pitch (whiteboard)

Draw this, talking through it left to right:

```
  client -> FastAPI -> Postgres  (metadata only)
               |
               +--> Ray head  (via Jobs REST)
               |
               +--> shared PVC (input + output files)

   Ray head --> Ray workers --> shared PVC
```

Then say: "The API never does inference. Ray never touches the
database. The PVC is the only thing both sides share. That
separation is the design."

## The four-word memory aid

- **Receptionist** (FastAPI)
- **Ledger** (Postgres)
- **Factory** (Ray)
- **Building manager** (Kubernetes)

KubeRay is the team that sets up the factory inside the building.

## Questions you should be able to answer

### "Why not just use one FastAPI process with the model loaded?"

Blocks HTTP workers on long jobs, ties API scaling to model
scaling, no parallelism, a crash in generation kills the API.
The control-plane / compute-plane split fixes all of these.

### "Why Postgres if results live on disk?"

Postgres holds **small queryable metadata**: batch id, status,
counts, timestamps, Ray job id. It is the ledger. The PVC holds
**large opaque payloads**: `input.jsonl`, `results.jsonl`. Each
layer uses the tool best suited to it.

### "Why not shove everything in the database?"

Databases are terrible at streaming large NDJSON and at serving
as a handoff medium between processes. Filesystems are great at
both.

### "Why a shared PVC and not S3?"

S3 is better in production. The tutorial uses a PVC because it
works offline on one machine with zero credentials. Switching to
S3 is one change in the storage helper and one change to Ray's
`read_json`.

### "What does the `_SUCCESS` marker actually buy you?"

It turns a multi-file write (`results.jsonl` + stats) into a
one-file atomic signal. The reader can trust `results.jsonl` only
if `_SUCCESS` exists, because `_SUCCESS` is written last. No
distributed transaction needed.

### "What's the difference between Ray and KubeRay?"

Ray is a distributed Python runtime. KubeRay is a Kubernetes
**operator** that teaches K8s how to manage Ray clusters. Without
KubeRay, you could run Ray on Kubernetes but you would be
managing head/worker pods by hand.

### "What is an operator?"

A controller pod running inside the cluster that watches for a
custom resource type (like `RayCluster`) and reconciles it into
ordinary Kubernetes resources (pods, services). The Kubernetes
API server learns the type via a CRD; the operator learns what
to do with it.

### "What's the difference between Ray Jobs and RayJob?"

- **Ray Jobs API**: a runtime REST endpoint on port 8265. Submit
  Python scripts to a long-running cluster. We use this.
- **RayJob (CRD)**: a Kubernetes-native alternative that creates
  a fresh RayCluster per job and tears it down after. Good for
  massive, infrequent batches; bad for per-request overhead.

### "Why `concurrency=2` with `batch_size=8`?"

`concurrency=2` means two `QwenPredictor` actors. We have two
worker pods, so one actor per worker. `batch_size=8` means each
`__call__` is invoked with 8 rows at a time. Small because CPU
memory is tight. On GPU, bump both up.

### "Why bake the model into the worker image?"

Baking `Qwen/Qwen2.5-0.5B-Instruct` into
`local/ray-worker:2.54.1-cpu` means worker pods start instantly.
The alternative, downloading weights on first use, takes 30+s per
worker on first cold-start and is fragile under Hugging Face
rate limits. The tradeoff is a larger image (~2.5 GB vs ~1.5 GB).

### "Why is the head pod's `num-cpus` set to 0?"

Keeps the head as a pure scheduler. No tasks or actors land on
it. That guarantees dashboard and Jobs API responsiveness even
when workers are saturated.

### "Could this run without Kubernetes?"

Yes, with Docker Compose. But you lose:
- Declarative cluster state (KubeRay RayCluster YAML)
- Self-healing (pod restarts)
- Autoscaling
- Service discovery via DNS
- The whole "portable to any cloud" story

For a laptop demo, compose is fine. For anything real, K8s.

### "Why not vLLM?"

vLLM is the right GPU upgrade path. We skipped it because the CPU
backend (`vllm-cpu`) is fragile on small models (see vllm#29233
and vllm#10439 specifically around Qwen2.5-0.5B). On a real GPU
cluster, swap to vLLM. Documented as Stage 2 in the migration
path.

### "How do you scale this?"

Three axes:

1. **API replicas**: `replicas: N` + HPA tied to request rate.
2. **Ray workers**: `enableInTreeAutoscaling: true` + min/max
   replicas on the worker group.
3. **Per-worker throughput**: raise `batch_size`, swap to vLLM,
   add GPUs.

### "What happens if a worker pod dies mid-batch?"

Ray detects the dead actor, reschedules if possible, else the job
fails. Fail writes `_FAILED` with a traceback. The poller sees
`_FAILED` and sets `status=failed` in the DB. The client retries
by submitting the same inputs.

### "How do you know the poller is running?"

Two signals: active batches transitioning out of `queued` on
their own, and `/metrics` exposing a gauge or timestamp of the
last successful poll. Add both if you deploy for real.

### "Why five states and not three?"

`queued -> in_progress -> completed` covers the happy path.
`failed` and `cancelled` distinguish "infrastructure broke" from
"client asked to stop." Clients need to know the difference for
retry logic: retry on `failed`, do not retry on `cancelled`.

## The teaching cheat sheet

Pin this somewhere while explaining to others:

```
FastAPI      = receptionist (accept, validate, ledger, dispatch)
Postgres     = ledger (small, queryable, durable)
PVC          = warehouse (large files, handshake medium)
Ray head     = dispatcher (scheduler, Jobs API, no compute)
Ray workers  = factory (model loaded once, prompts processed)
KubeRay      = foreman (creates and maintains the Ray cluster)
Kubernetes   = building manager (keeps pods alive, wires services)

Input contract:  JSONL rows {"id","prompt"}
Output contract: JSONL rows {"id","prompt","response","finish_reason",
                              "prompt_tokens","completion_tokens","error"}
Markers:         _SUCCESS (good), _FAILED (bad), written LAST
States:          queued -> in_progress -> completed | failed | cancelled
```

## What you can say honestly

- "I built this from scratch; here is what each piece does and why
  it is there."
- "The control plane and compute plane are decoupled through file
  contracts, so either side can scale or be replaced independently."
- "I know what the gaps are between this and a production
  deployment." (See Part 08.)
- "I can show you the same code running on CPU and GPU with one
  runtime check in `QwenPredictor`." (See Part 07.)

## Final check: can you rebuild it from scratch?

Close this document. Try to answer these without looking:

1. What are the five layers of the stack?
2. Which piece writes `input.jsonl`? Which piece reads it?
3. What signals that a batch is complete? Who reads that signal?
4. What does `make up` do, step by step?
5. What is the difference between a CRD and a custom resource?
6. Why does the head pod have `num-cpus: "0"`?
7. What is the failure-recovery path if the API pod dies mid-
   submission?
8. What has to change to run this on GPU instead of CPU?

If four or more of those are fuzzy, re-read the relevant section.
If you can explain all eight fluently, you can teach this to
anyone.

---

## What to do next

- Run `make up` in the finished repo and poke at every
  component. Break things on purpose and watch recovery.
- Read `docs/ARCHITECTURE.md` and `docs/TECHNICAL_REPORT.md` for
  the design-decision rationale and benchmark numbers.
- Pick one thing from Part 08's gap list (vLLM, S3 storage, HPA,
  tests) and implement it on a feature branch.
- Write your own operator for something smaller (a `WebApp` CRD
  that reconciles to a Deployment + Service). That hammer-nail
  reflex is the last piece to learn.

You have done the hard part. The rest is incremental.
