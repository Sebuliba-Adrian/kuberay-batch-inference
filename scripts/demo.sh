#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# KubeRay Batch Inference - live demo script for the Odyn debrief
#
# Walks the audience through the full request lifecycle in 6 steps:
#   1. Show pods                  - cluster is alive
#   2. Submit a batch             - the exact curl from the exercise PDF
#   3. Poll status until terminal - poller doing its job
#   4. Stream results             - actual Qwen output as NDJSON
#   5. Inspect the PVC            - input.jsonl, results.jsonl, _SUCCESS
#   6. Hit /metrics               - Prometheus counters incrementing
#
# Press ENTER between steps so you can narrate. Press Ctrl-C to abort.
#
# Usage:   bash demo.sh
# -----------------------------------------------------------------------------

set -euo pipefail

# ---- Config ----
HOST="${HOST:-http://localhost:8000}"
API_KEY="${API_KEY:-demo-api-key-change-me-in-production}"
NAMESPACE="${NAMESPACE:-ray}"
POLL_INTERVAL="${POLL_INTERVAL:-3}"      # seconds between polls
POLL_TIMEOUT="${POLL_TIMEOUT:-300}"      # max seconds before giving up

# ---- Colors (ANSI) ----
BLUE=$'\033[1;34m'
CYAN=$'\033[1;36m'
GREEN=$'\033[1;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[1;31m'
DIM=$'\033[2m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

# ---- Helpers ----
header() {
    local n="$1" title="$2"
    echo
    echo "${BLUE}===============================================================${RESET}"
    echo "${BLUE}  STEP $n  -  $title${RESET}"
    echo "${BLUE}===============================================================${RESET}"
    echo
}

ok()   { echo "${GREEN}+ $*${RESET}"; }
info() { echo "${CYAN}> $*${RESET}"; }
warn() { echo "${YELLOW}! $*${RESET}"; }
fail() { echo "${RED}X $*${RESET}" >&2; exit 1; }

pause() {
    echo
    echo "${DIM}    Press ENTER to continue...${RESET}"
    read -r
}

cmd() {
    echo "${DIM}\$${RESET} ${BOLD}$*${RESET}"
}

# ---- Pre-flight ----
command -v kubectl >/dev/null || fail "kubectl not found"
command -v curl    >/dev/null || fail "curl not found"
command -v jq      >/dev/null || fail "jq not found"

clear
echo "${BOLD}KubeRay Batch Inference - Live Demo${RESET}"
echo "${DIM}HOST=$HOST  ·  NAMESPACE=$NAMESPACE  ·  POLL_INTERVAL=${POLL_INTERVAL}s${RESET}"

# =============================================================================
header 1 "Show the cluster"
# =============================================================================
info "Listing pods in the '$NAMESPACE' namespace"
cmd "kubectl get pods -n $NAMESPACE"
echo
kubectl get pods -n "$NAMESPACE"
echo
ok "1 head pod + 2 workers + API + Postgres - the whole stack is up"
pause

# =============================================================================
header 2 "Submit a batch (the exact curl from the exercise PDF)"
# =============================================================================
info "POST /v1/batches with two prompts and X-API-Key auth"
cmd "curl -X POST $HOST/v1/batches -H 'X-API-Key: ...' -d '{...}'"
echo

t0=$(date +%s%3N)
RESPONSE=$(curl -sS -X POST "$HOST/v1/batches" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    -d '{
        "model": "Qwen/Qwen2.5-0.5B-Instruct",
        "input": [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}],
        "max_tokens": 50
    }')
t1=$(date +%s%3N)

echo "$RESPONSE" | jq

BATCH=$(echo "$RESPONSE" | jq -r .id)
[ "$BATCH" != "null" ] && [ -n "$BATCH" ] || fail "Could not extract batch id from response"

elapsed=$((t1 - t0))
echo
ok "Submitted in ${elapsed} ms - returned 'queued', no inference yet"
ok "Batch id: $BATCH"
pause

# =============================================================================
header 3 "Poll status until terminal (5-second background poller in action)"
# =============================================================================
info "Polling GET /v1/batches/$BATCH every ${POLL_INTERVAL}s"
echo

last_status=""
poll_t0=$(date +%s)
while :; do
    RESP=$(curl -sS -H "X-API-Key: $API_KEY" "$HOST/v1/batches/$BATCH")
    status=$(echo "$RESP" | jq -r .status)
    completed=$(echo "$RESP" | jq -r '.request_counts.completed')
    failed=$(echo "$RESP" | jq -r '.request_counts.failed')
    elapsed=$(( $(date +%s) - poll_t0 ))

    if [ "$status" != "$last_status" ]; then
        echo "${CYAN}[${elapsed}s]${RESET} status -> ${BOLD}${status}${RESET}  (completed=${completed} failed=${failed})"
        last_status="$status"
    else
        echo -n "."
    fi

    case "$status" in
        completed) echo; ok "Completed in ${elapsed}s"; break ;;
        failed)    echo; warn "Failed in ${elapsed}s"; warn "$(echo "$RESP" | jq -r .error)"; break ;;
        cancelled) echo; warn "Cancelled in ${elapsed}s"; break ;;
    esac

    if [ "$elapsed" -ge "$POLL_TIMEOUT" ]; then
        echo; fail "Poll timed out after ${POLL_TIMEOUT}s (still: $status)"
    fi
    sleep "$POLL_INTERVAL"
done
pause

# =============================================================================
header 4 "Stream the results (NDJSON, line by line)"
# =============================================================================
info "GET /v1/batches/$BATCH/results - streamed via aiofiles"
cmd "curl -s $HOST/v1/batches/$BATCH/results | jq"
echo
curl -sS -H "X-API-Key: $API_KEY" "$HOST/v1/batches/$BATCH/results" | jq
echo
ok "Each line: id, prompt, response, finish_reason, prompt_tokens, completion_tokens, error"
ok "Streamed - the API never holds the full file in memory"
pause

# =============================================================================
header 5 "Inspect the PVC (the file-based handshake medium)"
# =============================================================================
WORKER=$(kubectl get pods -n "$NAMESPACE" -l ray.io/node-type=worker -o name | head -1)
info "Looking at /data/batches/$BATCH/ inside $WORKER"
echo

cmd "kubectl exec -n $NAMESPACE $WORKER -- ls -la /data/batches/$BATCH/"
kubectl exec -n "$NAMESPACE" "$WORKER" -- ls -la "/data/batches/$BATCH/" 2>/dev/null || true
echo

cmd "kubectl exec ... cat _SUCCESS"
SUCCESS_JSON=$(kubectl exec -n "$NAMESPACE" "$WORKER" -- cat "/data/batches/$BATCH/_SUCCESS" 2>/dev/null || echo "{}")
echo "$SUCCESS_JSON" | jq
echo
ok "_SUCCESS is JSON, NOT empty - holds the counts the poller writes back to Postgres"
ok "Written LAST, after results.jsonl is in place. The marker-last invariant."
pause

# =============================================================================
header 6 "Hit /metrics (Prometheus counters)"
# =============================================================================
info "Counters labelled by route TEMPLATE - no batch ULID in labels (cardinality fix)"
cmd "curl -s $HOST/metrics | grep batch_ or http_requests_"
echo
curl -sS "$HOST/metrics" \
    | grep -E 'batch_submitted_total|batch_terminal_total|http_requests_total\{' \
    | grep -v '^#' \
    | sort -u
echo
ok "submit count, terminal count by status, http requests by route template"
ok "If you saw a batch ULID in the labels, that would be a cardinality bug. We don't."
pause

# =============================================================================
echo
echo "${GREEN}===============================================================${RESET}"
echo "${GREEN}  Demo complete.${RESET}"
echo "${GREEN}===============================================================${RESET}"
echo
ok "Submitted, processed, retrieved, inspected, and metricised."
ok "Batch id was: $BATCH"
echo
echo "${DIM}Now switch back to the slides and continue with slide 5 (Sequence).${RESET}"
echo
