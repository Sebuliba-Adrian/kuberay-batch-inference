# syntax=docker/dockerfile:1.7
# -----------------------------------------------------------------------
# Custom Ray worker image for KubeRay batch inference.
#
# Based on the official rayproject/ray CPU image, adds Transformers and
# Qwen2.5-0.5B (pre-downloaded so workers don't pull from HF Hub on every
# cold start), plus the batch_infer.py entrypoint script.
#
# Build:
#   docker build -t local/ray-worker:2.54.1-cpu -f inference/Dockerfile inference/
#
# Load into kind (images live in containerd, not host Docker):
#   kind load docker-image local/ray-worker:2.54.1-cpu --name kuberay-dev
#
# The resulting image is ~2.5 GB (Ray ~1.2 GB + torch CPU ~500 MB + Qwen
# weights ~1 GB). That's the price of reproducibility - runtime HF pulls
# are fragile under rate limits.
# -----------------------------------------------------------------------

FROM rayproject/ray:2.54.1-py310-cpu

# Everything the official image puts the `ray` user at /home/ray.
# Tooling goes into /app which we own and copy into.

# ─── System packages ───────────────────────────────────────────────
# We need build-essential only transiently for native wheel builds
# (sentencepiece on some arches). Drop it in the same layer to keep
# the final image small.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# ─── Python deps ───────────────────────────────────────────────────
USER ray
WORKDIR /home/ray

COPY --chown=ray:users requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# ─── Pre-download Qwen2.5-0.5B-Instruct ────────────────────────────
# Baking the weights into the image means pods start instantly instead
# of paying a ~30s HF Hub download per worker. The trade-off is a
# larger image. For a demo on kind (local images, no registry push),
# larger is fine; for a cloud rollout you'd instead mount an HF cache
# PVC or use a private model registry.
ENV HF_HOME=/home/ray/.cache/huggingface
ENV HF_HUB_DISABLE_TELEMETRY=1
ENV TRANSFORMERS_OFFLINE=0

RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download(\
    repo_id='Qwen/Qwen2.5-0.5B-Instruct', \
    local_dir_use_symlinks=False, \
    allow_patterns=['*.json', '*.model', '*.safetensors', '*.txt', 'tokenizer*'], \
)"

# After the first run, transformers loads from the HF cache under HF_HOME,
# so we don't need a second copy of the weights on disk.

# ─── Batch job code ────────────────────────────────────────────────
USER root
RUN mkdir -p /app/jobs && chown -R ray:users /app

USER ray
COPY --chown=ray:users jobs/ /app/jobs/
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Verify import on image build - fails fast if requirements or code are
# broken, instead of at pod start time.
RUN python -c "import importlib.util, sys; \
s = importlib.util.spec_from_file_location('batch_infer', '/app/jobs/batch_infer.py'); \
m = importlib.util.module_from_spec(s); \
s.loader.exec_module(m); \
print('batch_infer module loaded OK')"

# Default command - the base rayproject/ray image expects its own
# entrypoint (ray start ...) which KubeRay overrides via rayStartParams.
# We don't set CMD here so KubeRay's bootstrap wins.
