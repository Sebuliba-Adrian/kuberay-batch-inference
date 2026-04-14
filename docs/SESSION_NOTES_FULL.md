# Session Notes

Comprehensive notes capturing the full learning conversation around this
repository, from initial architecture walkthrough through Kubernetes,
KubeRay, Ray, Helm, CRDs/operators, storage flow, GPU migration thinking,
and later branch review.

This is not a raw chat transcript. It is a structured, complete
reconstruction of what was discussed, what was clarified, what mental
models were introduced, what branch changes were reviewed, and what
conclusions were reached.

The discussion happened across multiple branches, starting from the
single-file FastAPI branch and later moving to a local GPU / k3d branch.

---

## Table of contents

1. [Starting point](#1-starting-point)
2. [Initial architecture walkthrough](#2-initial-architecture-walkthrough)
3. [Branch reality and codebase shape](#3-branch-reality-and-codebase-shape)
4. [First-principles explanation of the stack](#4-first-principles-explanation-of-the-stack)
5. [VPS and LAMP translation](#5-vps-and-lamp-translation)
6. [Shared storage and why it exists](#6-shared-storage-and-why-it-exists)
7. [How a batch flows through the system](#7-how-a-batch-flows-through-the-system)
8. [How Ray crunches the batch file](#8-how-ray-crunches-the-batch-file)
9. [What KubeRay, Ray, Ray Jobs, and Ray Data are](#9-what-kuberay-ray-ray-jobs-and-ray-data-are)
10. [Operators, CRDs, custom resources, and controllers](#10-operators-crds-custom-resources-and-controllers)
11. [Toy operator example](#11-toy-operator-example)
12. [Helm, charts, and when to use Helm](#12-helm-charts-and-when-to-use-helm)
13. [Ray head vs Ray workers](#13-ray-head-vs-ray-workers)
14. [FastAPI concepts clarified during the session](#14-fastapi-concepts-clarified-during-the-session)
15. [GPU migration discussion](#15-gpu-migration-discussion)
16. [Whether seamless CPU/GPU adaptation should have been a goal](#16-whether-seamless-cpugpu-adaptation-should-have-been-a-goal)
17. [Post-assignment learning mindset](#17-post-assignment-learning-mindset)
18. [Documenting the session in the repo](#18-documenting-the-session-in-the-repo)
19. [Notes naming cleanup](#19-notes-naming-cleanup)
20. [ASCII and em-dash cleanup](#20-ascii-and-em-dash-cleanup)
21. [Switch to the GPU branch](#21-switch-to-the-gpu-branch)
22. [Review of the GPU / k3d branch](#22-review-of-the-gpu--k3d-branch)
23. [Second review after fixes](#23-second-review-after-fixes)
24. [Final distilled architecture summary](#24-final-distilled-architecture-summary)
25. [Practical takeaways from the session](#25-practical-takeaways-from-the-session)
26. [Suggested next steps after this session](#26-suggested-next-steps-after-this-session)

---

## 1. Starting point

The session began with a request to:

> Go through the app and fully understand.

The initial approach was to inspect:

- repo structure
- top-level docs
- FastAPI application code
- Ray job code
- Kubernetes manifests
- build and deployment tooling

The core goal at that point was not to change code, but to build a
faithful, end-to-end understanding of the system as it actually exists.

---

## 2. Initial architecture walkthrough

The first phase focused on:

- identifying the major components
- locating the real entrypoints
- distinguishing docs intent from branch reality
- understanding the request path, compute path, and persistence path

Top-level repo inspection established the following major areas:

- `api/` for the FastAPI control plane
- `inference/` for the Ray batch job code
- `k8s/` for Kubernetes manifests
- `docs/` for design rationale and walkthroughs
- `scripts/` for setup and operational helpers

The README and architecture docs provided the intended system shape:

- FastAPI front end
- Postgres metadata store
- shared PVC for batch files
- KubeRay-managed Ray cluster
- optional monitoring stack

Then the code and manifests were checked to confirm how that shape is
implemented on the current branch.

---

## 3. Branch reality and codebase shape

Very early in the review, an important branch-specific fact surfaced:

- the current branch at that time was `demo/single-file-version`
- the FastAPI control plane had been collapsed into a single file:
  `api/src/app.py`

The branch note explained that this was a deliberate refactor showing
that the control plane could live in one file without changing runtime
behavior.

This led to an important onboarding conclusion:

- on this branch, the most useful reading entrypoint is
  `api/src/app.py`
- the README still reflects the original split-module `main` layout
- the live source of truth for this branch is the single-file app

That insight mattered because it simplified later explanations for
someone new to FastAPI and Kubernetes.

---

## 4. First-principles explanation of the stack

Once the user clarified that they were new to Ray, Kubernetes, KubeRay,
and relatively new to FastAPI, the explanation shifted from code review
to first principles.

The system was reduced to five layers:

| Layer | What it is | Role in this app |
|---|---|---|
| FastAPI | Python web framework | External API, auth, validation, orchestration |
| Postgres | relational database | Stores batch metadata and status |
| Ray | distributed Python runtime | Executes the batch inference work |
| Kubernetes | container orchestration platform | Runs the services and mounts storage |
| KubeRay | Kubernetes operator for Ray | Teaches Kubernetes how to run Ray clusters |

This led to the first strong summary sentence:

> FastAPI is the control plane, Ray is the compute plane, Postgres is
> the ledger, shared storage is the handoff medium, Kubernetes runs
> everything, and KubeRay manages the Ray cluster inside Kubernetes.

That framing became the base for the rest of the conversation.

---

## 5. VPS and LAMP translation

The user said they were used to deploying to VPS server LAMP stacks.

That prompted a translation layer between familiar VPS concepts and
Kubernetes concepts.

The discussion mapped:

| VPS/LAMP concept | Kubernetes equivalent |
|---|---|
| one server box | Kubernetes node |
| system service | pod / deployment |
| `.env` / service env | ConfigMap + Secret |
| local disk folder | PersistentVolume / PVC |
| localhost/internal hostnames | Kubernetes service DNS |
| queue worker / cron script | Ray job |
| app and worker sharing one local folder | API pod and Ray workers sharing a PVC |

The practical lesson was:

- the architecture is not conceptually alien
- the same operational problems still exist
- Kubernetes just answers them with different primitives

This was one of the most important onboarding moments, because it
anchored the rest of the system in already familiar deployment ideas.

---

## 6. Shared storage and why it exists

One of the first detailed questions from the user was about shared
storage:

> What is the stuff that goes into shared storage and why and what and
> how is it used for?

That led to a careful explanation of the batch file contract.

The shared storage is mounted at:

```text
/data/batches
```

For each batch, the system creates:

```text
/data/batches/<batch_id>/
```

The important files are:

- `input.jsonl`
- `results.jsonl`
- `_SUCCESS`
- `_FAILED`

### `input.jsonl`

- written by FastAPI
- contains the input prompts
- one JSON record per line
- read by Ray workers

### `results.jsonl`

- written by the Ray job
- contains one output row per input row
- streamed back by FastAPI

### `_SUCCESS`

- written last after a successful run
- means output is complete and safe to serve

### `_FAILED`

- written on job-level failure
- contains failure information and traceback context

The core explanation given was:

- pods do not share a filesystem by default
- the API needs to hand input files to Ray
- Ray needs to hand output files back to the API
- the shared PVC is the file-based handshake between control plane and
  compute plane

This yielded another key mental model:

- Postgres = ledger
- shared PVC = warehouse / job file handoff folder

---

## 7. How a batch flows through the system

The next major topic was the full request lifecycle.

The user wanted the flow drawn and explained end to end.

The sequence was described as:

1. Client sends `POST /v1/batches`
2. FastAPI validates input and checks the API key
3. FastAPI writes `input.jsonl`
4. FastAPI inserts a Postgres row with `status=queued`
5. FastAPI submits a Ray job
6. Ray reads the batch file from shared storage
7. Ray workers run inference
8. Ray writes `results.jsonl`
9. Ray writes `_SUCCESS` or `_FAILED`
10. FastAPI background poller updates Postgres status
11. Client polls `GET /v1/batches/{id}`
12. Client later fetches `GET /v1/batches/{id}/results`

This was later summarized in architecture language as:

> The API submits a Ray job to the Ray cluster via the Ray head; then,
> within that job, Ray Data reads, partitions, and distributes the batch
> input across Ray workers, which perform the actual model inference and
> write the results.

That became one of the strongest summary sentences from the session.

---

## 8. How Ray crunches the batch file

The user then asked specifically:

> Also how does ray crunch on the batch file?

That led to a closer explanation of `inference/jobs/batch_infer.py`.

The explanation covered:

- the Ray job entrypoint script
- how `input.jsonl` is read
- how it becomes a Ray Data dataset
- how the dataset is repartitioned
- how `map_batches(QwenPredictor, ...)` distributes work
- how `QwenPredictor` loads and uses the model
- how results are written back out

The key learning points were:

- Ray does not just read a giant file monolithically
- Ray Data turns the file into a dataset
- the dataset is partitioned into blocks
- worker-side actors process those blocks
- the outputs are re-materialized as JSONL

`QwenPredictor` was explained as:

- `__init__` loads tokenizer and model once per actor
- `__call__` processes chunks of prompt rows

This gave the user the practical mental model that the model load is
amortized across many prompts rather than repeated for every row.

---

## 9. What KubeRay, Ray, Ray Jobs, and Ray Data are

The session then shifted into terminology, because the user asked:

> By the way, in all this what is KubeRay, Ray Data, Ray Jobs, etc...?

A clean glossary was introduced:

- `Ray` = distributed Python computing framework
- `Ray cluster` = one running Ray environment (head + workers)
- `KubeRay` = Kubernetes operator for Ray
- `Ray Jobs` = interface for submitting entrypoint jobs to a running Ray cluster
- `Ray Data` = distributed dataset processing layer
- `Ray Core` = lower-level tasks/actors foundation
- `Ray Serve` = online serving layer, not the main path in this repo

The key sentence used to tie these together was:

> KubeRay runs Ray on Kubernetes, Ray Jobs starts work on the cluster,
> and Ray Data does the parallel batch processing inside that work.

That sentence became a recurring reference point for understanding the
system architecture.

---

## 10. Operators, CRDs, custom resources, and controllers

The conversation then moved into Kubernetes extensibility, starting with:

> What is an operator?

That led to a more general explanation of four related terms:

- CRD
- custom resource
- controller
- operator

### CRD

Explained as:

- `CustomResourceDefinition`
- the schema/type registration that adds a new Kubernetes kind

### Custom resource

Explained as:

- one actual object of that custom kind
- e.g. one `RayCluster` YAML instance

### Controller

Explained as:

- a reconciliation loop
- watches desired state vs actual state
- takes action to make reality match the declared spec

### Operator

Explained as:

- a controller with application-specific operational knowledge
- e.g. KubeRay knows what a Ray cluster should look like and how to
  manage it on Kubernetes

The key teaching point was:

- CRD teaches Kubernetes the type exists
- operator teaches Kubernetes what to do with instances of that type

That answered the user's deeper question:

> How does Kubernetes even know how to use RayCluster, which is foreign to it?

The answer given was:

- by default it does not
- the CRD installs the new type into the API
- the operator watches and reconciles that type into real resources

---

## 11. Toy operator example

The user then asked to see an example of a custom operator-like resource.

A toy custom resource called `WebApp` was introduced.

The explanation showed:

- a hypothetical CRD for `WebApp`
- a custom resource instance such as:

```yaml
apiVersion: apps.example.com/v1
kind: WebApp
metadata:
  name: my-site
spec:
  image: nginx:1.27
  replicas: 2
  port: 80
```

- the reconciliation idea:
  - create/update a Deployment
  - create/update a Service
  - reflect readiness/status back onto the `WebApp`

The toy operator pseudocode explained the pattern:

1. read custom resource
2. build desired child resources
3. create or update those child resources
4. read their status
5. update custom resource status

This made the operator pattern much more concrete and linked the theory
back to how KubeRay works with `RayCluster`.

---

## 12. Helm, charts, and when to use Helm

Later in the session, the user asked about Helm:

> Tell me more about helm and its charts...

That led to a detailed distinction between:

- `kubectl apply`
- `kustomize`
- `helm`

### `kubectl apply`

- apply exact YAML directly
- simplest
- most transparent

### `kustomize`

- patch and compose raw YAML
- best for overlays and environment variants

### `helm`

- package manager
- template renderer
- release manager

Then Helm itself was clarified:

- Helm does not itself extend Kubernetes capabilities
- Helm is often the installer for software that extends Kubernetes

This was applied directly to KubeRay:

- Helm installs the KubeRay chart
- the chart contains the CRDs and operator
- the CRDs and operator are what extend Kubernetes
- Helm is the delivery mechanism

The user also asked when Helm is the best option, from merely available
to effectively unavoidable.

The answer ladder was:

1. small app, one environment:
   - `kubectl apply` may be better
2. owned app, multiple overlays:
   - `kustomize` is often a good fit
3. reusable configurable package:
   - Helm becomes strongly useful
4. third-party infrastructure software:
   - Helm is often the sensible supported path
5. large org/platform standardization:
   - Helm may become effectively unavoidable

This also produced a repo-specific insight:

- using Helm for KubeRay makes sense
- using plain manifests for the app itself also makes sense

That was identified as a pragmatic split rather than an inconsistency.

---

## 13. Ray head vs Ray workers

The difference between Ray head and Ray workers was explicitly discussed.

The explanation emphasized:

- Ray head = scheduler, Jobs API, cluster coordinator, dashboard
- Ray workers = actual compute pods doing inference

The head in this repo is configured not to do compute:

- `num-cpus: "0"`

That led to a strong operational statement:

> Ray head is the dispatcher and scheduler; Ray workers are the factory
> machines doing the real inference work.

This also helped explain the earlier architecture summary sentence
cleanly.

---

## 14. FastAPI concepts clarified during the session

Because the user said they were relatively new to FastAPI, several
framework-specific ideas were explained along the way.

Those included:

- `create_app()` as the app factory
- `lifespan()` as the startup/shutdown hook
- Pydantic request/response models
- dependencies via `Depends(...)`
- middleware
- `StreamingResponse`

The key FastAPI behaviors clarified were:

- request validation happens before route logic
- authentication can be route-level dependency logic
- startup code is handled through FastAPI lifespan
- the app can stream large outputs without loading them fully into memory

This made the control-plane side much easier to reason about for someone
without a deep FastAPI background.

---

## 15. GPU migration discussion

Later, the conversation turned toward:

> What if we ever wanna use GPUs and they become available?

That triggered a deeper architecture discussion.

The high-level conclusion was:

- the overall architecture could remain the same
- the main changes would be in the compute plane
- specifically:
  - worker pod spec
  - worker image
  - inference runtime/backend

The first migration points identified were:

- `k8s/raycluster/raycluster.yaml`
- `inference/Dockerfile`
- `inference/jobs/batch_infer.py`

The distinction between two stages was emphasized:

### Stage 1

- keep current architecture
- keep Ray Jobs
- keep Ray Data
- keep Transformers/PyTorch
- move workers to GPU-backed execution

### Stage 2

- consider vLLM or more GPU-optimized inference backends
- revisit batching and throughput optimization

The user then asked:

> vLLm being used?

The answer was:

- no, not currently
- the current code uses Transformers/PyTorch
- vLLM is a plausible future GPU-oriented optimization path

---

## 16. Whether seamless CPU/GPU adaptation should have been a goal

The discussion then moved from implementation to architecture scope:

> Should we have build the whole thing from ground up and make it able to
> seemlessly adapt to any environment, whether with or whether without GPUs?

The answer given was deliberately pragmatic:

- not as a first goal
- build one target environment well
- keep enough clean seams to evolve later
- avoid overengineering a universal abstraction too early

The recommended direction was:

- explicit CPU profile
- explicit GPU profile
- not one magical auto-adapting mode

This produced another principle:

> Build for one environment. Design so you can support more than one.
> Do not pretend to support all of them until you actually test and own them.

That captured the tradeoff between adaptability and engineering clarity.

---

## 17. Post-assignment learning mindset

At one point, the user clarified:

> Actually, the assignment was already submitted, i must say that i, now
> trying to learn and expand my knowledge out side the assignment.

This changed the framing significantly.

The guidance shifted from:

- was this enough for the assignment?

to:

- how can this repo now serve as a learning platform?

The suggested post-assignment learning path included:

1. make CPU/GPU deployment profiles explicit
2. understand Ray backend tradeoffs better
3. study Helm/Kustomize/deployment tooling strategy
4. analyze the production gap
5. build a toy operator separately to internalize CRD/operator mechanics

This was framed as a much better long-term use of the project than
trying to retrofit premature universality only for assignment scope.

---

## 18. Documenting the session in the repo

The user then asked to document the session.

A first session-focused documentation file was added under `docs/`.

The initial version was named with a tool-specific title, which then led
to a follow-up change request from the user.

The content of that session notes file captured:

- architecture walkthrough
- stack explanation
- storage explanation
- request flow
- Ray vocabulary
- FastAPI concepts
- learning order

This was the first step toward turning the conversation into durable
project documentation.

---

## 19. Notes naming cleanup

The user specifically requested:

> Do not call it Codex session, just call it Notes or something not
> directly refrening to codex

The document was then renamed and retitled to remove the explicit tool
reference.

It became:

- `docs/NOTES.md`

with a neutral `Notes` title and adjusted wording.

This was a small but important documentation cleanup requested directly
by the user.

---

## 20. ASCII and em-dash cleanup

The user then raised a style concern:

> Hope no long em dashes used at all

The notes file was checked programmatically for non-ASCII/em-dash style
characters and cleaned so the final notes file was ASCII-only.

The result:

- no em dashes in `docs/NOTES.md`

This became part of the documentation hygiene work during the session.

---

## 21. Switch to the GPU branch

Much later, the user said:

> I made some changes, pull in

The branch state was checked and it turned out the user was now on:

```text
feat/local-gpu-k3d
```

The worktree was clean, so the changes had already been committed or
the branch had already been switched.

At that point, the task shifted into a review of the user’s new GPU/k3d
work.

---

## 22. Review of the GPU / k3d branch

The branch diff against the earlier single-file baseline showed the
addition of:

- `inference/Dockerfile.gpu`
- `inference/requirements-gpu.txt`
- GPU-aware changes in `inference/jobs/batch_infer.py`
- `k8s/raycluster/raycluster.gpu.yaml`
- Make targets for GPU and k3d workflow
- `docs/GPU_PROFILE.md`
- `docs/LOCAL_GPU_TUTORIAL.md`

The review initially found a few concrete issues:

### Issue 1

`up-gpu-local` still passed indirectly through the kind-specific storage
target due to `raycluster-gpu` depending on `storage` rather than a
bootstrapper-agnostic flow.

### Issue 2

There were stale docs references:

- missing `docs/DEPLOYMENT_WALKTHROUGH.md`
- incorrect make target references such as `load-worker-gpu`

### Issue 3

`up-gpu` looked like a supported path even though the docs also stated
that standard kind does not support host GPU passthrough.

These were framed as:

- user-visible workflow problems
- docs/tooling coherence problems
- not fundamental design flaws

The overall branch direction was still judged positively:

- clean profile split
- sensible local GPU path via k3d
- coherent GPU architecture evolution

---

## 23. Second review after fixes

The user then said:

> changes made, how about now?

The branch was reviewed again focusing on the specific earlier findings.

The updated branch had:

- fixed the storage target dependency issue
- cleaned up stale docs references
- clarified the intent and limitations of `up-gpu`

The second review found no material remaining issues in those earlier
problem areas.

The conclusion at that point was:

- the branch was much more coherent
- the local GPU story was believable
- the docs and Make targets were aligned enough to support actual use

Residual caution remained:

- no end-to-end runtime validation happened in the current environment
- review was still static / code-and-doc based rather than executed on
  a real GPU machine

But from a structure and review standpoint, the updated branch was in
good shape.

---

## 24. Final distilled architecture summary

Across the conversation, several versions of the architecture summary
appeared. The strongest final wording was:

> The API submits a Ray job to the Ray cluster via the Ray head; then,
> within that job, Ray Data reads, partitions, and distributes the batch
> input across Ray workers, which perform the actual model inference and
> write the results.

This sentence successfully captures:

- FastAPI role
- Ray Jobs role
- Ray head role
- Ray Data role
- Ray workers role
- storage/result flow

It became one of the central summary statements from the full session.

---

## 25. Practical takeaways from the session

The major practical takeaways were:

### Architecture

- separate control plane from compute plane
- keep metadata in Postgres
- keep payload exchange in shared storage
- use FastAPI for orchestration, not heavy inference

### Kubernetes / KubeRay

- Kubernetes does not natively know `RayCluster`
- CRDs extend the API
- operators add behavior
- KubeRay is the Ray-specific operator

### Deployment tooling

- use Helm when package distribution/configuration/lifecycle is the hard part
- use plain manifests or Kustomize when direct YAML ownership is simpler
- for this repo, Helm for KubeRay and plain manifests for the app is a
  good split

### GPU evolution

- CPU-first was a reasonable initial scope
- explicit CPU/GPU profiles are better than hidden environment magic
- GPU migration should first preserve architecture, then optimize backend

### Learning strategy

- after the assignment, use the repo as a lab
- study one layer at a time
- do not chase universal abstraction before understanding the runtime

---

## 26. Suggested next steps after this session

Based on the full conversation, the natural next steps are:

1. Read the system again in this order:
   - `api/src/app.py`
   - `k8s/api/`
   - `k8s/storage/`
   - `k8s/postgres/`
   - `inference/jobs/batch_infer.py`
   - `k8s/raycluster/`

2. Compare the CPU and GPU profiles explicitly.

3. Run the local GPU tutorial end to end on real hardware.

4. Benchmark CPU vs GPU profile behavior.

5. Explore the next compute backend step:
   - better batching
   - possible vLLM evaluation

6. Build a toy operator separately to make CRDs/controllers/operators
   fully second nature.

7. If desired, evolve the documentation further into:
   - a line-by-line FastAPI code walkthrough
   - a line-by-line Ray worker walkthrough
   - a deployment operations checklist

---

## Final note

This session moved from:

- architecture comprehension
- to first-principles explanation
- to Kubernetes extensibility concepts
- to deployment tooling strategy
- to GPU evolution thinking
- to live branch review and validation

The through-line across the entire discussion was:

- understand the architecture clearly
- reduce jargon into operationally meaningful concepts
- prefer explicitness over magic
- keep the system understandable while expanding capability

