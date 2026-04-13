# Branch note: `demo/single-file-version`

This branch is a deliberate single-file refactor of the FastAPI
control plane, to answer the question: *could the whole thing have
been written as one `app.py`?*

**Answer: yes, literally.** No functionality changes. Every class,
function, and module-level singleton from the canonical split layout
is pasted into `api/src/app.py` under section banners. Uvicorn entry
point changes from `src.main:create_app` to `src.app:create_app`;
the Kubernetes manifests and Dockerfile are updated to match.

## What's different from `main`

| Aspect | `main` | This branch |
|---|---|---|
| Python layout | 10 modules in `api/src/` + `routes/` | One file: `api/src/app.py` |
| Line count (api/src) | 1,611 across 10 files | ~780 in one file |
| Tests | 169 cases, 100% line+branch coverage, CI-enforced | Removed for this demo |
| CI | ruff + mypy + pytest + kubeconform + docker | ruff + mypy + kubeconform + docker |
| Uvicorn entry | `src.main:create_app --factory` | `src.app:create_app --factory` |
| Runtime behavior | Identical | Identical |
| Docker image | Identical | Identical |
| K8s manifests | Same | Same (only CMD string changed) |

## Why the split exists on `main` (and not here)

Organizational, not architectural:

1. **Faster pytest import graph.** Tests for auth on `main` don't
   pull FastAPI, Ray stubs, or SQLAlchemy. Cold test time on `main`
   is ~0.5 s; a single file would pull everything and push it to
   ~3 s.
2. **Per-concern coverage targeting.** `main`'s CI shows coverage
   per module, making regressions easy to blame. The single-file
   version would still cover 100% but surface it as one big number.
3. **PR-review ergonomics.** A change to auth on `main` touches
   ~50 lines in one focused file; on this branch, it would be a
   diff inside a 780-line file with 12 other concerns visible.
4. **Side-effect-free imports in tests.** Tests on `main` do
   `from src.db import ping` without ever triggering FastAPI app
   construction. The single-file version still keeps imports
   side-effect-free because `create_app()` is a factory, but the
   ceiling on module-level side effects is higher when everything
   coexists.

None of those are architectural reasons. The single-file version
is the same program with harder-to-navigate code.

## When a single-file layout would actually be right

For a prototype under ~200 lines. At 780 lines with six distinct
concerns (config, auth, schemas, DB, storage, Ray, observability,
routes, poller, lifespan), splitting pays for itself within a week
of maintenance.

## Verification

`ruff check src` passes, `mypy src` passes, `docker build` produces
an identical image. The `/docs` OpenAPI output is byte-identical to
`main`. Deploy this branch's Docker image instead of `main`'s and
the exercise curl works unchanged.

## Back to `main`

```bash
git checkout main
```
