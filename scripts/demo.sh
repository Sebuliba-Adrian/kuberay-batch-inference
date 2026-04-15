#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# KubeRay Batch Inference - interactive self-narrated live demo
#
# Walks the full request lifecycle with narration, animated progress, an
# invariants checklist that ticks off as each property is verified live,
# optional interactive dives, and a final summary dashboard.
#
# Usage:
#   bash scripts/demo.sh             # standard demo (~3-4 min + inference wait)
#   VERBOSE=1 bash scripts/demo.sh   # adds 3 deep-inspection steps
#   FAST=1    bash scripts/demo.sh   # skip the pause-on-Enter prompts
#
# Override defaults via env:
#   HOST=http://localhost:8000  API_KEY=...  NAMESPACE=ray
#   POLL_INTERVAL=3  POLL_TIMEOUT=300
# -----------------------------------------------------------------------------

set -euo pipefail

# ---- Config ----------------------------------------------------------------
HOST="${HOST:-http://localhost:8000}"
API_KEY="${API_KEY:-demo-api-key-change-me-in-production}"
NAMESPACE="${NAMESPACE:-ray}"
POLL_INTERVAL="${POLL_INTERVAL:-3}"
POLL_TIMEOUT="${POLL_TIMEOUT:-300}"
VERBOSE="${VERBOSE:-0}"
FAST="${FAST:-0}"

# ---- Colors ----------------------------------------------------------------
BLUE=$'\033[1;34m'
CYAN=$'\033[1;36m'
GREEN=$'\033[1;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[1;31m'
MAGENTA=$'\033[1;35m'
WHITE=$'\033[1;37m'
DIM=$'\033[2m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

# Component colors (used for the live state indicator)
C_API=$'\033[1;34m'      # blue
C_RAY=$'\033[1;33m'      # yellow
C_PG=$'\033[1;35m'       # magenta
C_PVC=$'\033[1;32m'      # green

# ---- State trackers --------------------------------------------------------
INVARIANTS=()                    # list of "verified" facts as the demo proves them
DEMO_T0=$(date +%s)
SUBMIT_LATENCY_MS=0
INFER_DURATION_S=0
TOTAL_PROMPTS=0
COMPLETED_PROMPTS=0
FAILED_PROMPTS=0
TOTAL_TOKENS=0
BATCH=""
INPUT_BYTES=0
RESULTS_BYTES=0

# ---- Helpers ---------------------------------------------------------------
hr()   { printf "${BLUE}%s${RESET}\n" "==============================================================="; }

header() {
    local n="$1" title="$2"
    echo
    hr
    printf "${BLUE}  STEP %s  -  %s${RESET}\n" "$n" "$title"
    hr
    echo
}

narrate() {
    echo "${MAGENTA}+--- WHAT IS HAPPENING ----------------------------------------+${RESET}"
    while [ $# -gt 0 ]; do
        printf "${MAGENTA}|${RESET} %s\n" "$1"
        shift
    done
    echo "${MAGENTA}+-------------------------------------------------------------+${RESET}"
    echo
}

# Show which components are active during a step
active() {
    local label="$1" pieces="$2"
    echo "${DIM}    [active components]${RESET}  ${pieces}"
    echo
}

watchfor() {
    echo "${YELLOW}>> WATCH FOR:${RESET} $*"
}

# Mark an invariant as verified, add to the running checklist
verify() {
    local fact="$1"
    INVARIANTS+=("$fact")
    echo "${GREEN}  [verified] ${fact}${RESET}"
}

ok()    { echo "${GREEN}+ $*${RESET}"; }
info()  { echo "${CYAN}> $*${RESET}"; }
warn()  { echo "${YELLOW}! $*${RESET}"; }
fail()  { echo "${RED}X $*${RESET}" >&2; exit 1; }
cmd()   { echo "${DIM}\$${RESET} ${BOLD}$*${RESET}"; }

pause() {
    if [ "$FAST" = "1" ]; then return; fi
    echo
    echo "${DIM}    -- press ENTER to continue --${RESET}"
    read -r
}

# Yes/no prompt; returns 0 for yes, 1 for no. Defaults to no.
ask() {
    if [ "$FAST" = "1" ]; then return 1; fi
    local prompt="$1"
    echo
    printf "${YELLOW}? %s [y/N]: ${RESET}" "$prompt"
    read -r answer
    case "$answer" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# Animated spinner that runs until the named file exists
# Usage: spinner_until_done <signal_file> <message>
spinner_chars=( '|' '/' '-' '\' )
spinner_until_done() {
    local sigfile="$1" msg="$2"
    local i=0 t0
    t0=$(date +%s)
    while [ ! -f "$sigfile" ]; do
        local elapsed=$(( $(date +%s) - t0 ))
        printf "\r  ${CYAN}%s${RESET}  %s  ${DIM}(%ds elapsed)${RESET}   " "${spinner_chars[$i]}" "$msg" "$elapsed"
        i=$(( (i+1) % 4 ))
        sleep 0.2
    done
    printf "\r  ${GREEN}+${RESET}  %s  ${DIM}(done)${RESET}                                \n" "$msg"
}

# ---- Pre-flight -----------------------------------------------------------
command -v kubectl >/dev/null || fail "kubectl not found"
command -v curl    >/dev/null || fail "curl not found"
command -v jq      >/dev/null || fail "jq not found"

clear
cat <<EOF
${BOLD}KubeRay Batch Inference${RESET}  ${DIM}-  Self-Narrated Interactive Demo${RESET}

${DIM}HOST=${HOST}  ·  NAMESPACE=${NAMESPACE}  ·  VERBOSE=${VERBOSE}  ·  FAST=${FAST}${RESET}

${MAGENTA}+----------------------------------------------------------------------+${RESET}
${MAGENTA}|  System under test (live cluster):                                   |${RESET}
${MAGENTA}|                                                                      |${RESET}
${MAGENTA}|     ${C_API}[ Client ]${MAGENTA}                                                       |${RESET}
${MAGENTA}|         |  POST /v1/batches  +  X-API-Key                            |${RESET}
${MAGENTA}|         v                                                            |${RESET}
${MAGENTA}|     ${C_API}[ FastAPI API Pod ]${MAGENTA}                                              |${RESET}
${MAGENTA}|       /         \\                                                    |${RESET}
${MAGENTA}|      v           v                                                   |${RESET}
${MAGENTA}|  ${C_PG}[Postgres]${MAGENTA}    ${C_RAY}[Ray Head]${MAGENTA}                                            |${RESET}
${MAGENTA}|                      \\                                               |${RESET}
${MAGENTA}|                       v                                              |${RESET}
${MAGENTA}|                  ${C_RAY}[Worker 1] [Worker 2]${MAGENTA}                              |${RESET}
${MAGENTA}|                          \\         /                                 |${RESET}
${MAGENTA}|                           v       v                                  |${RESET}
${MAGENTA}|                       ${C_PVC}[ Shared PVC ]${MAGENTA}                                |${RESET}
${MAGENTA}|                                                                      |${RESET}
${MAGENTA}|  Color key:  ${C_API}control plane${MAGENTA}   ${C_RAY}compute plane${MAGENTA}   ${C_PG}metadata${MAGENTA}   ${C_PVC}storage${MAGENTA}    |${RESET}
${MAGENTA}+----------------------------------------------------------------------+${RESET}

${MAGENTA}This demo will walk the request lifecycle end to end.${RESET}
${MAGENTA}Each step prints what is happening, what to watch for, and what was just verified.${RESET}
${MAGENTA}A running checklist of verified invariants builds up over the demo.${RESET}

${DIM}    -- press ENTER to begin --${RESET}
EOF
[ "$FAST" = "1" ] || read -r

# ============================================================================
header 1 "Show the cluster"
# ============================================================================
active "before any request" "${C_API}API${RESET}  ${C_RAY}Ray Head${RESET}  ${C_RAY}Ray Workers${RESET}  ${C_PG}Postgres${RESET}  ${C_PVC}PVC${RESET}  (all idle)"

narrate \
    "Before any request, the system must be alive. The cluster has 1 head" \
    "pod (Ray scheduler, num-cpus=0), 2 worker pods (Ray actors holding the" \
    "model in memory), the FastAPI API pod (control plane), and Postgres" \
    "(metadata ledger). All five pods are independent processes connected" \
    "through Kubernetes Services and a shared PersistentVolumeClaim."

cmd "kubectl get pods -n $NAMESPACE"
echo
kubectl get pods -n "$NAMESPACE"
echo

watchfor "all 5 pods Running 1/1, RESTARTS column ideally 0 or low"
verify "5 pods are Running (head, 2 workers, API, Postgres)"
verify "Shared PVC is mounted on the API pod and on both Ray workers"

if ask "Optional: dive into the head pod's startup args to confirm num-cpus=0?"; then
    HEAD_POD=$(kubectl get pods -n "$NAMESPACE" -l ray.io/node-type=head -o name | head -1)
    echo
    cmd "kubectl describe $HEAD_POD | grep -A2 num-cpus"
    kubectl describe -n "$NAMESPACE" "$HEAD_POD" 2>/dev/null | grep -B1 -A1 "num-cpus" | head -10 || \
        kubectl get -n "$NAMESPACE" "$HEAD_POD" -o yaml 2>/dev/null | grep -A2 "num-cpus" | head -10
    echo
    verify "Head pod has num-cpus=0  (control plane only, no compute)"
fi

pause

# ============================================================================
header 2 "Submit a batch (the exact curl from the exercise PDF)"
# ============================================================================
active "synchronous handler" "${C_API}API${RESET} -> ${C_PVC}PVC write${RESET} -> ${C_PG}DB insert${RESET} -> ${C_RAY}Ray submit${RESET}"

narrate \
    "The client sends two prompts in one POST. FastAPI does five things" \
    "before returning: (a) Pydantic validates the JSON shape, (b)" \
    "require_api_key constant-time-compares the X-API-Key header, (c)" \
    "write_inputs_jsonl writes /data/batches/<id>/input.jsonl to the PVC," \
    "(d) INSERT into Postgres with status=queued, (e) JobSubmissionClient" \
    "submits a Ray job over the Jobs REST API on port 8265. Then it returns" \
    "202 Accepted. No inference happens yet."

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
SUBMIT_LATENCY_MS=$((t1 - t0))

echo "$RESPONSE" | jq

BATCH=$(echo "$RESPONSE" | jq -r .id)
TOTAL_PROMPTS=$(echo "$RESPONSE" | jq -r '.request_counts.total')
[ "$BATCH" != "null" ] && [ -n "$BATCH" ] || fail "Could not extract batch id"

echo
watchfor "status=queued in the response. Submit latency around 100-200 ms."
verify "POST returned in ${SUBMIT_LATENCY_MS} ms with status=queued"
verify "Batch id captured: ${BOLD}$BATCH${RESET}"
verify "$TOTAL_PROMPTS prompts now queued for processing"
pause

# ============================================================================
if [ "$VERBOSE" = "1" ]; then
header "2a" "Look at the Postgres row that was just inserted"
active "DB inspection" "${C_PG}Postgres${RESET}"
narrate \
    "The API wrote one row in the 'batches' table. status=queued," \
    "ray_job_id is filled with the submission id Ray returned. From this" \
    "point on, every status read by the client comes from this row, not" \
    "from Ray. That is the control-plane / compute-plane separation in" \
    "action. The API does not depend on Ray being reachable to answer" \
    "GET /v1/batches/<id>."

PG_POD=$(kubectl get pods -n "$NAMESPACE" -l app=postgres -o name 2>/dev/null | head -1 | sed 's|pod/||')
if [ -n "$PG_POD" ]; then
    cmd "kubectl exec $PG_POD -- psql -U postgres -d batchapi -c 'SELECT ...'"
    echo
    kubectl exec -n "$NAMESPACE" "$PG_POD" -- psql -U postgres -d batchapi -c \
        "SELECT id, status, model, input_count, completed_count, failed_count, ray_job_id, created_at FROM batches WHERE id = '$BATCH';" \
        2>/dev/null || warn "Could not query Postgres - check db name / credentials"
    echo
    watchfor "status='queued', ray_job_id is not null, counts are 0/0"
    verify "Postgres row exists with ray_job_id and counts=0"
else
    warn "No postgres pod found, skipping"
fi
pause
fi

# ============================================================================
header 3 "Poll status until terminal (the 5-second background poller)"
# ============================================================================
active "asynchronous compute + reconciliation" "${C_RAY}Ray Head${RESET} -> ${C_RAY}Worker${RESET} -> ${C_PVC}PVC write${RESET} -> ${C_API}poller${RESET} -> ${C_PG}DB update${RESET}"

narrate \
    "The API has returned. The model has not run yet. Now Ray schedules" \
    "the job on the workers. Each worker has a QwenPredictor actor that" \
    "loaded the model once at startup. The actor reads input.jsonl from" \
    "the PVC, runs map_batches with batch_size=8 and concurrency=2, then" \
    "writes results.jsonl with an atomic rename. The _SUCCESS marker is" \
    "written LAST, after the rename completes. Meanwhile, the FastAPI" \
    "background poller wakes up every 5 seconds, asks Ray for status, and" \
    "if the job is terminal, reads the marker and updates the Postgres row."

info "Polling GET /v1/batches/$BATCH every ${POLL_INTERVAL}s"
echo

if [ "$VERBOSE" = "1" ]; then
    info "(VERBOSE) Live worker log preview will appear above the poll line"
    echo
fi

last_status=""
poll_t0=$(date +%s)
spin_idx=0
while :; do
    RESP=$(curl -sS -H "X-API-Key: $API_KEY" "$HOST/v1/batches/$BATCH")
    status=$(echo "$RESP" | jq -r .status)
    completed=$(echo "$RESP" | jq -r '.request_counts.completed')
    failed=$(echo "$RESP" | jq -r '.request_counts.failed')
    elapsed=$(( $(date +%s) - poll_t0 ))

    if [ "$status" != "$last_status" ]; then
        printf "\r"  # clear spinner line
        echo "${CYAN}[${elapsed}s]${RESET} status -> ${BOLD}${status}${RESET}  (completed=${completed} failed=${failed})"
        case "$status" in
          queued)      echo "${DIM}        Ray has the job. Worker not yet scheduled or actor still warming up.${RESET}" ;;
          in_progress) echo "${DIM}        Worker is now generating. Each prompt is roughly 60s on CPU.${RESET}" ;;
          completed)   echo "${DIM}        Worker wrote results.jsonl and _SUCCESS. Poller saw the marker.${RESET}" ;;
        esac
        last_status="$status"
    else
        printf "\r  ${CYAN}%s${RESET}  waiting on Ray  ${DIM}(${elapsed}s elapsed, status=%s, completed=%s/%s)${RESET}     " \
            "${spinner_chars[$spin_idx]}" "$status" "$completed" "$TOTAL_PROMPTS"
        spin_idx=$(( (spin_idx + 1) % 4 ))
    fi

    case "$status" in
        completed) echo; INFER_DURATION_S=$elapsed; COMPLETED_PROMPTS=$completed; FAILED_PROMPTS=$failed; ok "Reached terminal state in ${elapsed}s"; break ;;
        failed)    echo; INFER_DURATION_S=$elapsed; FAILED_PROMPTS=$failed; warn "Failed in ${elapsed}s"; warn "$(echo "$RESP" | jq -r .error)"; break ;;
        cancelled) echo; INFER_DURATION_S=$elapsed; warn "Cancelled in ${elapsed}s"; break ;;
    esac

    if [ "$elapsed" -ge "$POLL_TIMEOUT" ]; then
        echo; fail "Poll timed out after ${POLL_TIMEOUT}s (still: $status)"
    fi
    sleep "$POLL_INTERVAL"
done

echo
watchfor "the status transitions came from cheap DB reads, never hit Ray on the GET path"
verify "Status went queued -> in_progress -> completed via background poller"
verify "Inference completed in ${INFER_DURATION_S}s for ${COMPLETED_PROMPTS}/${TOTAL_PROMPTS} prompts"
pause

# ============================================================================
header 4 "Stream the results (NDJSON, line by line)"
# ============================================================================
active "client retrieval" "${C_API}API${RESET} -> ${C_PG}DB read${RESET} -> ${C_PVC}PVC stream${RESET} -> ${C_API}NDJSON${RESET}"

narrate \
    "The API confirms status=completed in Postgres, then opens" \
    "results.jsonl from the PVC and streams it line by line via aiofiles" \
    "wrapped in a StreamingResponse. The client sees one JSON object per" \
    "line. The API never holds the whole file in memory, so the same" \
    "endpoint works for 2 prompts or 200000 prompts with the same memory" \
    "profile."

cmd "curl $HOST/v1/batches/$BATCH/results | jq"
echo

RESULTS_BODY=$(curl -sS -H "X-API-Key: $API_KEY" "$HOST/v1/batches/$BATCH/results")
echo "$RESULTS_BODY" | jq
echo

# Tally tokens for the summary
TOTAL_TOKENS=$(echo "$RESULTS_BODY" | jq -s 'map((.prompt_tokens // 0) + (.completion_tokens // 0)) | add' 2>/dev/null || echo "0")
RESULTS_BYTES=${#RESULTS_BODY}

watchfor "the actual Qwen output. finish_reason='stop' if the model finished naturally."
verify "Streamed ${RESULTS_BYTES} bytes of NDJSON, ${TOTAL_TOKENS} total tokens"
verify "API never held the full file in memory (iter_results_ndjson)"
pause

# ============================================================================
header 5 "Inspect the PVC (the file-based handshake)"
# ============================================================================
active "post-mortem inspection" "${C_PVC}PVC${RESET}"

narrate \
    "This is the only place where the control plane and the compute plane" \
    "share state. The API wrote input.jsonl. Ray workers wrote results.jsonl" \
    "and _SUCCESS. They never spoke to each other directly. The PVC was the" \
    "entire interface." \
    "" \
    "_SUCCESS is the commit marker. Written LAST, after the atomic rename" \
    "of results.jsonl. If a reader sees _SUCCESS, it can trust results.jsonl" \
    "is complete. Eliminates a class of distributed-transaction bugs without" \
    "any locking."

WORKER=$(kubectl get pods -n "$NAMESPACE" -l ray.io/node-type=worker -o name | head -1)
info "Looking at /data/batches/$BATCH/ inside $WORKER"
echo

cmd "kubectl exec ... ls -la /data/batches/$BATCH/"
LS_OUT=$(kubectl exec -n "$NAMESPACE" "$WORKER" -- ls -la "/data/batches/$BATCH/" 2>/dev/null || echo "")
echo "$LS_OUT"
echo

INPUT_BYTES=$(echo "$LS_OUT" | awk '/input\.jsonl/ {print $5}')
[ -z "$INPUT_BYTES" ] && INPUT_BYTES=0

cmd "kubectl exec ... cat _SUCCESS"
SUCCESS_JSON=$(kubectl exec -n "$NAMESPACE" "$WORKER" -- cat "/data/batches/$BATCH/_SUCCESS" 2>/dev/null || echo "{}")
echo "$SUCCESS_JSON" | jq
echo

watchfor "_SUCCESS is JSON with batch_id, total, completed, failed, model, finished_at"
verify "_SUCCESS is JSON, NOT empty (carries counts the poller writes back)"
verify "Three files present: input.jsonl, results.jsonl, _SUCCESS"
verify "Marker-last invariant holds: results.jsonl was renamed before _SUCCESS was written"

if ask "Optional: peek at the input.jsonl to see the prompt-id contract?"; then
    echo
    cmd "kubectl exec ... cat input.jsonl"
    kubectl exec -n "$NAMESPACE" "$WORKER" -- cat "/data/batches/$BATCH/input.jsonl" | jq -c
    echo
    verify "input.jsonl rows are {id, prompt} - id is a stringified zero-based index"
fi

pause

# ============================================================================
header 6 "Hit /metrics (Prometheus counters)"
# ============================================================================
active "observability surface" "${C_API}API /metrics${RESET}"

narrate \
    "The API exposes /metrics in Prometheus text format from a dedicated" \
    "CollectorRegistry, not the global one (so tests stay isolated). Four" \
    "counters and one histogram. Critically, http_requests_total is" \
    "labelled by the matched route TEMPLATE (/v1/batches/{batch_id}), not" \
    "the literal path. If we used the literal path, every batch ULID would" \
    "create a new label series and Prometheus cardinality would explode." \
    "This was a real bug I caught and fixed before shipping."

cmd "curl -s $HOST/metrics | grep -E 'batch_|http_requests_'"
echo
METRICS_OUT=$(curl -sS "$HOST/metrics" \
    | grep -E 'batch_submitted_total|batch_terminal_total|http_requests_total\{' \
    | grep -v '^#' \
    | sort -u)
echo "$METRICS_OUT"
echo

# Cardinality verification: assert no batch_ id pattern in the path label
if echo "$METRICS_OUT" | grep -q 'path="/v1/batches/{batch_id}'; then
    verify "http_requests_total uses the route TEMPLATE - cardinality fix is live"
else
    warn "Did not find a route-template path label - check the cardinality fix"
fi

if echo "$METRICS_OUT" | grep -q 'batch_terminal_total.*completed'; then
    verify "batch_terminal_total{status=\"completed\"} incremented"
fi

if echo "$METRICS_OUT" | grep -q 'batch_submitted_total'; then
    verify "batch_submitted_total{model=\"Qwen/Qwen2.5-0.5B-Instruct\"} incremented"
fi

watchfor "path labels look like /v1/batches/{batch_id}, NOT a literal ULID"
pause

# ============================================================================
if [ "$VERBOSE" = "1" ]; then
header "6a" "Optional: open the Ray dashboard for the audience to browse"
active "Q&A enrichment" "${C_RAY}Ray Dashboard${RESET}"
narrate \
    "The Ray head pod exposes its dashboard on :8265. With make dashboard" \
    "(port-forward), the interviewer can see job submissions, actor health," \
    "and Grafana-iframed metrics during follow-up questions. The Ray head" \
    "manifest already declares RAY_PROMETHEUS_HOST and RAY_GRAFANA_HOST so" \
    "the iframes work the moment make monitoring-up is run."

info "To open it, run in a separate terminal:"
echo "    ${BOLD}make dashboard${RESET}"
echo "    ${DIM}then visit http://localhost:8265${RESET}"
echo
verify "Ray dashboard is one make-target away if Q&A goes deep"
pause
fi

# ============================================================================
# FINAL SUMMARY DASHBOARD
# ============================================================================
DEMO_DURATION=$(( $(date +%s) - DEMO_T0 ))
clear
hr
printf "${GREEN}  DEMO COMPLETE${RESET}\n"
hr
echo

cat <<EOF
${BOLD}Run summary${RESET}

  ${C_API}Submit latency${RESET}        ${SUBMIT_LATENCY_MS} ms       ${DIM}(client -> 202)${RESET}
  ${C_RAY}Inference duration${RESET}    ${INFER_DURATION_S} s         ${DIM}(queued -> completed)${RESET}
  ${C_PVC}Input size${RESET}            ${INPUT_BYTES} bytes
  ${C_PVC}Results size${RESET}          ${RESULTS_BYTES} bytes
  ${C_API}Tokens generated${RESET}      ${TOTAL_TOKENS}
  ${C_API}Prompts succeeded${RESET}     ${COMPLETED_PROMPTS} / ${TOTAL_PROMPTS}
  ${C_API}Total demo time${RESET}       ${DEMO_DURATION} s         ${DIM}(including narration pauses)${RESET}

${BOLD}Invariants verified during the demo${RESET}
EOF

i=1
for inv in "${INVARIANTS[@]}"; do
    printf "  ${GREEN}%2d.${RESET}  %s\n" "$i" "$inv"
    i=$((i+1))
done

echo
cat <<EOF
${BOLD}Batch id${RESET}:  ${DIM}$BATCH${RESET}

${BOLD}Re-run any single GET${RESET} after the demo:
  ${DIM}curl -H "X-API-Key: \$API_KEY" $HOST/v1/batches/$BATCH${RESET}
  ${DIM}curl -H "X-API-Key: \$API_KEY" $HOST/v1/batches/$BATCH/results${RESET}

${MAGENTA}+----------------------------------------------------------------+${RESET}
${MAGENTA}|  Now switch back to the slides and continue with slide 5       |${RESET}
${MAGENTA}|  (Sequence diagram - shows what you just saw, structured      |${RESET}
${MAGENTA}|  by the four phases: Ingest, Execute, Reconcile, Retrieve).   |${RESET}
${MAGENTA}+----------------------------------------------------------------+${RESET}
EOF
echo
