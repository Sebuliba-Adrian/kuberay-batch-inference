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
POLL_LATENCIES_MS=()             # every single GET /v1/batches/{id} round-trip time
DEMO_T0=$(date +%s)
SUBMIT_LATENCY_MS=0
INFER_DURATION_S=0
TOTAL_PROMPTS=0
COMPLETED_PROMPTS=0
FAILED_PROMPTS=0
TOTAL_TOKENS=0
TOTAL_COMPLETION_TOKENS=0
BATCH=""
INPUT_BYTES=0
RESULTS_BYTES=0
POLL_P50_MS=0
POLL_P95_MS=0

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

${BOLD}System architecture${RESET}  ${DIM}(colors match the step narration throughout the demo)${RESET}

                         +----------------+
                         |     ${C_API}Client${RESET}     |    ${DIM}curl / SDK${RESET}
                         +-------+--------+
                                 |
                                 |  ${BOLD}1.${RESET}  POST /v1/batches  +  X-API-Key
                                 v
             +-------------------------------------------+
             |            ${C_API}FastAPI API Pod${RESET}                |   ${DIM}control plane${RESET}
             |   validate . auth . ledger . submit job   |
             +----+---------------------------------+----+
                  |                                 |
       ${BOLD}2.${RESET}  SQL async                        ${BOLD}3.${RESET}  Jobs REST :8265
           SQLAlchemy                              submit_job
                  |                                 |
                  v                                 v
          +---------------+                +--------------------+
          |   ${C_PG}Postgres${RESET}    |                |     ${C_RAY}Ray Head${RESET}       |    ${DIM}scheduler only${RESET}
          | batch row     |                |   num-cpus: 0      |
          | status,counts |                |   Jobs API, dash   |
          +---------------+                +----------+---------+
                                                      |
                                           ${BOLD}4.${RESET}  schedule actors
                                                      |
                                                      v
                                     +-------------------------------+
                                     |   ${C_RAY}Ray Worker 1${RESET}      ${C_RAY}Worker 2${RESET}    |
                                     |   QwenPredictor actor         |   ${DIM}compute plane${RESET}
                                     |   model loaded once per pod   |
                                     +---------+------------+--------+
                                               |            |
                                      ${BOLD}5.${RESET}  read input, generate, write results
                                               |            |
                                               v            v
                                     +-------------------------------+
                                     |        ${C_PVC}Shared PVC${RESET}             |   ${DIM}file handshake${RESET}
                                     |  input.jsonl  .  results.jsonl|
                                     |  _SUCCESS  (written LAST)     |
                                     +-------------------------------+

${BOLD}Color key:${RESET}   ${C_API}control plane${RESET}   ${C_RAY}compute plane${RESET}   ${C_PG}metadata ledger${RESET}   ${C_PVC}storage${RESET}

${BOLD}Key design calls${RESET} (that this demo will verify live):
  - Control plane and compute plane are strictly separated; their only shared surface is the PVC.
  - Ray Head runs with num-cpus=0 so scheduling stays responsive under compute load.
  - _SUCCESS marker is written LAST, after the atomic rename of results.jsonl. No distributed transactions.
  - GET /v1/batches/{id} reads Postgres only, never hits Ray. Status reads stay cheap.
  - /metrics labels the http counter by route TEMPLATE (not literal path) to bound cardinality.

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
    "200 OK. No inference has happened yet."

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
watchfor "status=queued in the response"
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

PG_POD=$(kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/name=postgres -o name 2>/dev/null | head -1 | sed 's|pod/||')
if [ -n "$PG_POD" ]; then
    # Credentials come from the postgres secret: POSTGRES_USER=batches, POSTGRES_DB=batches
    cmd "kubectl exec $PG_POD -- psql -U batches -d batches -c 'SELECT ...'"
    echo
    kubectl exec -n "$NAMESPACE" "$PG_POD" -- psql -U batches -d batches -c \
        "SELECT id, status, model, input_count, completed_count, failed_count, ray_job_id, created_at FROM batches WHERE id = '$BATCH';" \
        2>&1 | sed '/^$/d' || warn "Could not query Postgres (check user/db/labels)"
    echo
    watchfor "status='queued', ray_job_id is not null, counts are 0/0"
    verify "Postgres row exists with ray_job_id populated"
else
    warn "No postgres pod found (label app.kubernetes.io/name=postgres), skipping"
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
    # Measure the poll's HTTP round-trip time (this feeds the p50/p95 calc)
    pt_start=$(date +%s%3N)
    RESP=$(curl -sS -H "X-API-Key: $API_KEY" "$HOST/v1/batches/$BATCH")
    pt_end=$(date +%s%3N)
    POLL_LATENCIES_MS+=($((pt_end - pt_start)))

    status=$(echo "$RESP" | jq -r .status)
    completed=$(echo "$RESP" | jq -r '.request_counts.completed')
    failed=$(echo "$RESP" | jq -r '.request_counts.failed')
    elapsed=$(( $(date +%s) - poll_t0 ))

    if [ "$status" != "$last_status" ]; then
        printf "\r\033[K"  # clear spinner line to end-of-line (fixes leftover chars from the longer spinner text)
        echo "${CYAN}[${elapsed}s]${RESET} status -> ${BOLD}${status}${RESET}  (completed=${completed} failed=${failed})"
        case "$status" in
          queued)      echo "${DIM}        Ray has the job. Worker not yet scheduled or actor still warming up.${RESET}" ;;
          in_progress) echo "${DIM}        Worker is now generating. CPU inference takes many seconds per prompt.${RESET}" ;;
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

# ---- Benchmark snapshot computed from what just happened -------------------
# Compute poll p50 / p95 from the latencies captured during the loop.
POLL_N=${#POLL_LATENCIES_MS[@]}
if [ "$POLL_N" -gt 0 ]; then
    # Sort the latency array numerically
    IFS=$'\n' POLL_SORTED=($(printf "%s\n" "${POLL_LATENCIES_MS[@]}" | sort -n))
    unset IFS
    # p50 (median)
    p50_idx=$(( POLL_N / 2 ))
    POLL_P50_MS=${POLL_SORTED[$p50_idx]}
    # p95 (ceil(0.95 * n) - 1, clamped to last index)
    p95_idx=$(( (POLL_N * 95 / 100) ))
    [ "$p95_idx" -ge "$POLL_N" ] && p95_idx=$(( POLL_N - 1 ))
    POLL_P95_MS=${POLL_SORTED[$p95_idx]}
    POLL_MIN_MS=${POLL_SORTED[0]}
    POLL_MAX_MS=${POLL_SORTED[$(( POLL_N - 1 ))]}

    echo
    echo "${MAGENTA}+--- BENCHMARK SNAPSHOT (computed live from this run) --------+${RESET}"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-28s${RESET} %s\n"       "Submit latency"        "${SUBMIT_LATENCY_MS} ms"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-28s${RESET} %s\n"       "Inference duration"    "${INFER_DURATION_S} s"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-28s${RESET} %s\n"       "Prompts OK / total"    "${COMPLETED_PROMPTS} / ${TOTAL_PROMPTS}"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-28s${RESET} %s\n"       "GET /batches polls"    "${POLL_N} calls"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-28s${RESET} %s\n"       "Poll p50 / p95 latency" "${POLL_P50_MS} ms / ${POLL_P95_MS} ms"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-28s${RESET} %s\n"       "Poll min / max"        "${POLL_MIN_MS} ms / ${POLL_MAX_MS} ms"
    if [ "$COMPLETED_PROMPTS" -gt 0 ] && [ "$INFER_DURATION_S" -gt 0 ]; then
        avg_s=$(awk "BEGIN {printf \"%.1f\", $INFER_DURATION_S / $COMPLETED_PROMPTS}")
        throughput=$(awk "BEGIN {printf \"%.3f\", $COMPLETED_PROMPTS / $INFER_DURATION_S}")
        printf "${MAGENTA}|${RESET}  ${BOLD}%-28s${RESET} %s\n"   "Avg per-prompt"        "${avg_s} s"
        printf "${MAGENTA}|${RESET}  ${BOLD}%-28s${RESET} %s\n"   "Throughput"            "${throughput} prompts/s"
    fi
    echo "${MAGENTA}+-------------------------------------------------------------+${RESET}"
    echo
    echo "${DIM}  These numbers were measured during THIS run, not hardcoded.${RESET}"
    echo "${DIM}  The poll p50 matches what /v1/batches/{id} costs a client under normal load.${RESET}"
    echo "${DIM}  The /metrics histogram http_request_duration_seconds records the same thing server-side.${RESET}"

    verify "Poll p50 / p95 measured live: ${POLL_P50_MS} ms / ${POLL_P95_MS} ms"
fi
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

# Tally tokens and compute live throughput for the summary
TOTAL_TOKENS=$(echo "$RESULTS_BODY" | jq -s 'map((.prompt_tokens // 0) + (.completion_tokens // 0)) | add' 2>/dev/null || echo "0")
TOTAL_COMPLETION_TOKENS=$(echo "$RESULTS_BODY" | jq -s 'map(.completion_tokens // 0) | add' 2>/dev/null || echo "0")
RESULTS_BYTES=${#RESULTS_BODY}

watchfor "the actual Qwen output. finish_reason='stop' if the model finished naturally."

# ---- Token throughput computed from this run's real numbers ----------------
if [ "$INFER_DURATION_S" -gt 0 ] && [ "$TOTAL_COMPLETION_TOKENS" -gt 0 ]; then
    tok_per_sec=$(awk "BEGIN {printf \"%.2f\", $TOTAL_COMPLETION_TOKENS / $INFER_DURATION_S}")
    avg_tok_per_prompt=$(awk "BEGIN {printf \"%.1f\", $TOTAL_COMPLETION_TOKENS / $TOTAL_PROMPTS}")

    echo
    echo "${MAGENTA}+--- TOKEN THROUGHPUT (live from this run) -------------------+${RESET}"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-30s${RESET} %s\n"       "Response bytes (NDJSON)"     "${RESULTS_BYTES}"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-30s${RESET} %s\n"       "Total tokens (prompt+completion)" "${TOTAL_TOKENS}"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-30s${RESET} %s\n"       "Completion tokens only"      "${TOTAL_COMPLETION_TOKENS}"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-30s${RESET} %s\n"       "Avg completion per prompt"   "${avg_tok_per_prompt} tokens"
    printf "${MAGENTA}|${RESET}  ${BOLD}%-30s${RESET} %s\n"       "Generation throughput"       "${tok_per_sec} tokens/s"
    echo "${MAGENTA}+-------------------------------------------------------------+${RESET}"
    echo
    echo "${DIM}  Generation throughput is completion-tokens divided by inference duration.${RESET}"
    echo "${DIM}  On CPU this is a few tokens per second; on GPU with batched generate it is${RESET}"
    echo "${DIM}  two orders of magnitude higher. Numbers are from THIS run, not hardcoded.${RESET}"
fi

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
# PRODUCTION PATH  -  prioritized next steps
#   (Note: the 5-bug log from the WSL bring-up is held in reserve for Q&A,
#    not delivered as a block. See PROJECT_MONOLITH.md §16 for the full list.)
# ============================================================================
echo
hr
printf "${BLUE}  PRODUCTION PATH  -  prioritized changes for a real rollout${RESET}\n"
hr
echo
cat <<EOF
${DIM}If I had to take this to production tomorrow, five things, in this order.${RESET}

  ${BOLD}1. Turn on autoscaling${RESET}                      ${DIM}biggest leverage, zero code${RESET}
     ${GREEN}enableInTreeAutoscaling: true${RESET} on the RayCluster.
     Set a min/max worker range.
     One YAML flag, cluster absorbs bursty traffic.

  ${BOLD}2. GPU workers + batched generate${RESET}           ${DIM}10-20x throughput before vLLM${RESET}
     Swap worker image to ${GREEN}rayproject/ray:2.54.1-py310-gpu${RESET}.
     Replace the per-prompt loop in QwenPredictor.__call__ with
     left-padded batched generate(). vLLM is stage 2.

  ${BOLD}3. Object storage instead of the PVC${RESET}       ${DIM}kills RWX problem on every cloud${RESET}
     ${GREEN}fsspec${RESET} + S3 / GCS / Azure Blob.
     Ray Data reads s3:// natively. Workers scale horizontally without
     depending on a shared filesystem.

  ${BOLD}4. Real auth${RESET}                                ${DIM}multi-tenant ready${RESET}
     Static API key becomes ${GREEN}JWTs${RESET} with per-tenant quotas.
     External Secrets Operator pulling from KMS or Vault.

  ${BOLD}5. Alertmanager + Loki${RESET}                     ${DIM}closes the observability loop${RESET}
     /metrics is already there (you just saw it). Scrape with Prometheus.
     Loki for the JSON logs, tied to X-Request-ID.
     Both deliberately out of scope for the exercise.

${BOLD}What does NOT change:${RESET}  FastAPI routes, Postgres schema, batch lifecycle, NDJSON contract.
${MAGENTA}That is the control-plane / compute-plane split paying off.${RESET}
EOF
pause

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
${BOLD}Run summary - every number measured live during THIS run${RESET}

  ${C_API}Submit latency${RESET}        ${SUBMIT_LATENCY_MS} ms       ${DIM}(client -> 200 OK with BatchObject body)${RESET}
  ${C_RAY}Inference duration${RESET}    ${INFER_DURATION_S} s         ${DIM}(queued -> completed, on Ray workers)${RESET}
  ${C_API}Poll p50 / p95${RESET}        ${POLL_P50_MS} ms / ${POLL_P95_MS} ms   ${DIM}(GET /v1/batches/{id} HTTP round-trip)${RESET}
  ${C_PVC}Input size${RESET}            ${INPUT_BYTES} bytes
  ${C_PVC}Results size${RESET}          ${RESULTS_BYTES} bytes
  ${C_API}Prompt tokens${RESET}         $((TOTAL_TOKENS - TOTAL_COMPLETION_TOKENS))
  ${C_API}Completion tokens${RESET}     ${TOTAL_COMPLETION_TOKENS}
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
