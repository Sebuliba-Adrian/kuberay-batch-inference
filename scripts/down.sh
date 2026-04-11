#!/usr/bin/env bash
# -------------------------------------------------------------------------
# down.sh — Tear down the full stack, including the kind cluster.
#
# Use `make down` instead if you want to keep the kind cluster alive for
# faster subsequent restarts.
# -------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"
make down || true
make cluster-down || true

echo "[down] Stack torn down. The host data directory /tmp/kuberay-batch-inference/data remains."
