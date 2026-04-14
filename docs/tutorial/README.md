# Full Tutorial: Building a KubeRay Batch Inference Service From Scratch

Read this series in order. Every file is runnable by the time you
finish the section it introduces. By the end you will have a working
batch inference service that takes LLM prompts over HTTP, runs them
across Ray workers on Kubernetes, and streams results back.

You will also understand every layer well enough to rebuild it from
memory and teach it to someone else.

## What you will build

A service that accepts requests like:

```json
POST /v1/batches
{
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "input": [
    {"prompt": "What is 2+2?"},
    {"prompt": "Name three planets."}
  ],
  "max_tokens": 64
}
```

and returns (immediately):

```json
{"id": "batch_01ABC...", "status": "queued", ...}
```

Then, a few seconds later, `GET /v1/batches/batch_01ABC/results`
returns streamed NDJSON with one generation per prompt.

Behind the API:

- FastAPI validates, authenticates, writes the prompts to a shared
  file, and submits a job to Ray.
- Ray distributes the prompts across worker pods running Qwen2.5.
- Postgres tracks each batch's status.
- KubeRay manages the Ray cluster as native Kubernetes objects.
- It all runs on your laptop in a local Kubernetes cluster.

## Reading order

1. [Foundation](01-foundation.md) - the problem, the stack, prereqs.
2. [Python and Ray local](02-python-and-ray-local.md) - the compute
   core, first on one machine, then distributed.
3. [FastAPI, Postgres, poller](03-fastapi-postgres-poller.md) - the
   control plane.
4. [Containerize and run on Kubernetes](04-docker-and-kubernetes.md)
   - turn it into a cluster deployment.
5. [KubeRay and the operator pattern](05-kuberay.md) - let
   Kubernetes manage the Ray cluster for you.
6. [Deep understanding](06-deep-understanding.md) - operator
   internals, request flow, failure modes.
7. [GPU upgrade](07-gpu.md) - the laptop GPU path via k3d.
8. [Production considerations](08-production.md) - what we did not
   build, and what you would add.
9. [Teaching it to others](09-teaching.md) - mental models, the
   one-paragraph pitch, common questions.

Total reading time: ~3 hours.
Total hands-on time: ~4 hours, most of it waiting for Docker builds.

## Conventions

- Commands assume `bash`. Windows users should use WSL2.
- File paths are relative to your tutorial working directory
  (`~/batch-tutorial/` below), not the repo you cloned.
- The finished repo at
  <https://github.com/Sebuliba-Adrian/kuberay-batch-inference> is
  the reference implementation. Each part of this tutorial tells
  you which file in that repo matches the code you just wrote.
- When in doubt, read the finished file and compare.

## What you need before starting

- A laptop with at least 16 GB RAM.
- Docker Desktop or Docker Engine installed and running.
- Comfort with the terminal and basic Python.
- ~30 GB of free disk space (Docker images are large).

No Kubernetes, Ray, or FastAPI knowledge is assumed.

## Set up a working directory

```bash
mkdir -p ~/batch-tutorial
cd ~/batch-tutorial
python3 -m venv venv
source venv/bin/activate          # Linux / macOS / WSL
# Windows CMD:  venv\Scripts\activate
```

Everything in this tutorial goes inside `~/batch-tutorial/`.

Ready? Start with [01-foundation.md](01-foundation.md).
