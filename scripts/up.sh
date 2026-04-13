#!/usr/bin/env bash
# -------------------------------------------------------------------------
# up.sh - Bring up the entire stack in the correct order.
#
# This wraps `make up` with pre-flight checks and ensures the host data
# directory exists before kind creates the cluster (otherwise kind creates
# it with root ownership and pods can't write to it).
#
# Designed to be re-runnable: each step is idempotent.
# -------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log()  { printf "\033[0;32m[up]\033[0m %s\n" "$*"; }
die()  { printf "\033[0;31m[up]\033[0m %s\n" "$*" >&2; exit 1; }

# 1. Verify we're in the repo root (kind-config.yaml uses a relative path)
cd "$REPO_ROOT"

# 2. Pre-flight tool check
for t in docker kind kubectl helm make; do
  command -v "$t" >/dev/null 2>&1 || die "$t not found - run scripts/setup.sh"
done

# 3. Pre-create the host data directory so kind doesn't create it as root
DATA_DIR="/tmp/kuberay-batch-inference/data"
log "Ensuring $DATA_DIR exists and is writable..."
mkdir -p "$DATA_DIR"
chmod 0777 "$DATA_DIR"

# 4. Pre-create an .env if the user hasn't already, using the example
if [[ ! -f .env ]]; then
  log "Creating .env from .env.example"
  cp .env.example .env
fi

# 5. Delegate the actual lifecycle to the Makefile - `make up` is the
#    single source of truth for the happy path
log "Handing off to 'make up'..."
make up
