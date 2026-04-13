# syntax=docker/dockerfile:1.7
# -----------------------------------------------------------------------
# FastAPI proxy container image.
#
# Multi-stage build:
#   1. builder (python:3.11-slim) - install deps with uv into a venv
#   2. runtime (python:3.11-slim) - copy only the venv and src, drop
#      root, run uvicorn directly
#
# Build:
#   docker build -t local/batch-api:dev -f api/Dockerfile api/
#
# Load into kind (images live in containerd, not host Docker):
#   kind load docker-image local/batch-api:dev --name kuberay-dev
#
# Run locally (non-prod, no lifespan deps):
#   docker run --rm -p 8000:8000 \
#     -e API_KEY=demo \
#     -e POSTGRES_URL=postgresql+asyncpg://u:p@host:5432/db \
#     -e RAY_ADDRESS=http://host:8265 \
#     -e RESULTS_DIR=/data/batches \
#     -v /tmp/batches:/data/batches \
#     local/batch-api:dev
# -----------------------------------------------------------------------

# ─── Builder stage ─────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Pull uv from its official image and drop the binaries into /bin.
COPY --from=ghcr.io/astral-sh/uv:0.5.8 /uv /uvx /bin/

# Build-time envs: bytecode compilation, copy-mode linking (faster
# on overlayfs), and a venv at a known path.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install runtime system libs that some wheels need at build time.
# psycopg2/asyncpg wheels are pre-built so we only need libpq's
# runtime; everything else is pure Python.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpq5 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install deps ONLY - cached layer, re-uses when only app code changes.
COPY pyproject.toml ./
RUN uv venv /app/.venv --python 3.11 \
 && uv pip install --python /app/.venv/bin/python \
      "fastapi>=0.115,<1" \
      "uvicorn[standard]>=0.32,<1" \
      "pydantic>=2.8,<3" \
      "pydantic-settings>=2.5,<3" \
      "sqlalchemy[asyncio]>=2.0.30,<3" \
      "asyncpg>=0.29,<1" \
      "aiofiles>=24.1,<25" \
      "ray[default]==2.54.1" \
      "python-ulid>=3.0,<4" \
      "orjson>=3.10,<4"

# ─── Runtime stage ─────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user, deterministic UID matching the k8s SecurityContext
RUN groupadd -r app --gid 10001 \
 && useradd -r -g app -u 10001 -d /app -s /usr/sbin/nologin app

WORKDIR /app

# Runtime-only system libs (no build tools)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpq5 \
      ca-certificates \
      curl \
 && rm -rf /var/lib/apt/lists/*

# Copy the venv from the builder stage
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Copy the application source
COPY --chown=app:app src /app/src

# Put the venv on PATH, disable pycache to keep the image small
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

USER app
EXPOSE 8000

# Prefer uvicorn directly - Kubernetes handles replicas, no need
# for gunicorn's process manager. --factory tells uvicorn to call
# create_app() to get a fresh FastAPI instance.
CMD ["uvicorn", "src.main:create_app", \
     "--factory", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--no-access-log"]
