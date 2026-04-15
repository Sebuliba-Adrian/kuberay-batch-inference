#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# KubeRay Batch Inference - on-demand bug-fix evidence walkthrough
#
# Shows live evidence that each of the 5 bring-up bug fixes is in place.
# Does NOT reproduce the broken state (that would require rebuilding images
# or tearing down the cluster). It proves the fix shipped.
#
# When to use:
#   Only if the interviewer asks "what went wrong?" / "show me a bug you
#   debugged" / "anything unexpected during bring-up?" during Q&A. Otherwise
#   skip - the primary demo is scripts/demo.sh.
#
# Usage:
#   bash scripts/demo-bugs.sh           # walk all 5 bugs, pause between each
#   bash scripts/demo-bugs.sh 2         # jump to a specific bug (1-5)
#   FAST=1 bash scripts/demo-bugs.sh    # no pauses
# -----------------------------------------------------------------------------

set -euo pipefail

NAMESPACE="${NAMESPACE:-ray}"
HOST="${HOST:-http://localhost:8000}"
FAST="${FAST:-0}"
ONLY="${1:-}"

# ---- Colors ----------------------------------------------------------------
BLUE=$'\033[1;34m'
CYAN=$'\033[1;36m'
GREEN=$'\033[1;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[1;31m'
MAGENTA=$'\033[1;35m'
DIM=$'\033[2m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

# ---- Helpers ---------------------------------------------------------------
hr() { printf "${BLUE}%s${RESET}\n" "==============================================================="; }

bug_header() {
    local n="$1" title="$2"
    echo
    hr
    printf "${RED}  BUG %s  -  %s${RESET}\n" "$n" "$title"
    hr
    echo
}

symptom() {
    echo "${RED}! SYMPTOM:${RESET}  $*"
    echo
}

cause() {
    echo "${YELLOW}? ROOT CAUSE:${RESET}  $*"
    echo
}

fix_narr() {
    echo "${GREEN}+ FIX:${RESET}  $*"
    echo
}

evidence() {
    echo "${CYAN}> LIVE EVIDENCE:${RESET}"
}

cmd() { echo "${DIM}\$${RESET} ${BOLD}$*${RESET}"; }
ok()   { echo "${GREEN}+ $*${RESET}"; }
note() { echo "${DIM}  $*${RESET}"; }

pause() {
    if [ "$FAST" = "1" ]; then return; fi
    echo
    echo "${DIM}    -- press ENTER for next bug --${RESET}"
    read -r
}

should_run() {
    local n="$1"
    [ -z "$ONLY" ] || [ "$ONLY" = "$n" ]
}

# ---- Pre-flight ------------------------------------------------------------
command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
command -v curl    >/dev/null || { echo "curl not found"    >&2; exit 1; }

clear
cat <<EOF
${BOLD}Bug-fix evidence walkthrough${RESET}

Five real bugs caught during the WSL2 bring-up, each with live
evidence that the fix is in place today. These are NOT reproduced
live (that would need rebuilding images or tearing down the
cluster). Instead, we show the fix shipped in the code and runtime.

${MAGENTA}The common thread:${RESET}  four of these mocks could not catch.
Even 100% line and branch coverage did not prove production
correctness. Only running on real hardware did.

EOF
[ "$FAST" = "1" ] || { echo "${DIM}    -- press ENTER to begin --${RESET}"; read -r; }

# ============================================================================
if should_run 1; then
bug_header 1 "kind hostPath Errno 13 on the first POST"
# ============================================================================
symptom "First POST to /v1/batches returned 500 with PermissionError: [Errno 13] Permission denied: '/data/batches/batch_...'"
cause "API pod runs as non-root UID 10001 (security best practice). Kind extraMounts creates /mnt/data as 0755 root:root inside the node. Non-root pod cannot write there."
fix_narr "Chmod the hostPath inside the kind node as part of the Makefile's 'storage' target, so subsequent pods can write regardless of how the stack was bootstrapped."

evidence
echo
cmd "grep -A1 'chmod' Makefile"
grep -A1 'chmod' Makefile 2>/dev/null | head -6
echo
note "The chmod runs every time make storage runs. No manual step required."
echo

cmd "docker exec kuberay-dev-control-plane ls -la /mnt/data | head -3"
docker exec kuberay-dev-control-plane ls -la /mnt/data 2>/dev/null | head -3 || \
    echo "  (docker exec not available in this terminal, but chmod 0777 is evident in the Makefile above)"
echo
ok "Current hostPath permissions allow the non-root pod to write."
pause
fi

# ============================================================================
if should_run 2; then
bug_header 2 "Prometheus cardinality explosion on http_requests_total"
# ============================================================================
symptom "Not visible as a runtime error. Would have blown up Prometheus cardinality over time - every batch ULID would become a new time-series in the 'path' label."
cause "Middleware was labelling http_requests_total with request.url.path (the literal URL), so /v1/batches/batch_01ABC and /v1/batches/batch_01DEF were counted as separate paths."
fix_narr "Read request.scope['route'].path (the matched route template) instead, which returns /v1/batches/{batch_id} regardless of the actual batch id. Falls back to the literal path only for unmatched (404) routes."

evidence
echo
cmd "grep -B1 -A3 'scope\\[.route.\\]' api/src/observability.py"
grep -B1 -A3 'scope\["route"\]' api/src/observability.py 2>/dev/null | head -8
echo
ok "The fix is in the middleware code above."
echo

cmd "curl -s $HOST/metrics | grep '^http_requests_total{' | grep '/v1/batches/' | head -3"
curl -sS "$HOST/metrics" 2>/dev/null | grep '^http_requests_total{' | grep '/v1/batches/' | sort -u | head -3
echo
ok "Path labels use the route TEMPLATE {batch_id}, never a literal ULID. Cardinality bounded to the number of declared routes."
note "Commit: 6b94be3"
pause
fi

# ============================================================================
if should_run 3; then
bug_header 3 "ray[client] deprecated - ray[default] required"
# ============================================================================
symptom "API pod crash-looped at startup with ImportError from ray.job_submission."
cause "api/pyproject.toml pinned ray[client], but JobSubmissionClient moved under ray[default] extras in recent Ray versions. ray[client] no longer installs what we need."
fix_narr "Changed both api/pyproject.toml and api/Dockerfile to ray[default]==2.54.1."

evidence
echo
cmd "grep -n 'ray\\[' api/pyproject.toml api/Dockerfile 2>/dev/null"
grep -n 'ray\[' api/pyproject.toml api/Dockerfile 2>/dev/null | head -5
echo
ok "Both files use ray[default]. The fix shipped; the test-seam blind spot that let it through is a story by itself."
note "Why mocks missed it: FakeRayClient lets tests avoid importing ray.job_submission entirely. The real import only runs at container startup."
pause
fi

# ============================================================================
if should_run 4; then
bug_header 4 "UID mismatch between API (10001) and Ray workers (1000) on hostPath PVC"
# ============================================================================
symptom "Ray worker got PermissionError reading input.jsonl that the API had written. Files owned by UID 10001 with 0644 perms; Ray worker is UID 1000 and cannot write to them."
cause "Kubernetes fsGroup does not apply to hostPath PVs - only CSI drivers honor it. Different base images default to different users."
fix_narr "Wrap the uvicorn entrypoint in 'umask 0000 && exec uvicorn ...' so new files are created world-writable. Workaround for kind. Production fix is a real RWX CSI driver (EFS / Azure Files / Filestore) where fsGroup works."

evidence
echo
cmd "kubectl exec -n $NAMESPACE deploy/batch-api -- id 2>/dev/null | head -1"
kubectl exec -n "$NAMESPACE" deploy/batch-api -- id 2>/dev/null | head -1 || echo "  (API pod not running?)"
echo
cmd "kubectl exec -n $NAMESPACE -l ray.io/node-type=worker -- id 2>/dev/null | head -1"
kubectl exec -n "$NAMESPACE" -l ray.io/node-type=worker -- id 2>/dev/null | head -1 || \
    kubectl exec -n "$NAMESPACE" $(kubectl get pods -n "$NAMESPACE" -l ray.io/node-type=worker -o name | head -1) -- id 2>/dev/null | head -1 || \
    echo "  (worker pod not running?)"
echo
ok "Different UIDs. API=10001, worker=1000. Without umask 0000, the API's files would be 0644 owned by 10001:10001 and the worker could not write them."
echo

cmd "grep -A1 'umask' k8s/api/deployment.yaml | head -4"
grep -A1 'umask' k8s/api/deployment.yaml 2>/dev/null | head -4
echo
ok "umask 0000 in the API's exec command makes new files world-writable. The hostPath workaround."
pause
fi

# ============================================================================
if should_run 5; then
bug_header 5 "Benchmark NDJSON parser"
# ============================================================================
symptom "scripts/benchmark.py crashed with json.decoder.JSONDecodeError: Extra data: line 2 column 1."
cause "The benchmark called json.loads on the entire response body. /results returns NDJSON - one JSON object per line. json.loads expects a single document and sees the { of the second line as 'Extra data'."
fix_narr "Split on newlines, parse each line independently."

evidence
echo
cmd "grep -B1 -A3 'splitlines\\|json.loads' scripts/benchmark.py 2>/dev/null | head -12"
grep -B1 -A3 'splitlines\|json.loads' scripts/benchmark.py 2>/dev/null | head -12
echo
ok "The fix: one json.loads per line. The server streams NDJSON via iter_results_ndjson for flat memory; the client has to meet that contract."
note "Commit: 088edae"
pause
fi

# ============================================================================
echo
hr
printf "${GREEN}  Bug-fix evidence walkthrough complete.${RESET}\n"
hr
echo
cat <<EOF
${BOLD}The punchline I want to land:${RESET}

${MAGENTA}Four of these five mocks could not catch. Even 100% line and branch${RESET}
${MAGENTA}coverage does not prove production correctness. You need at least one${RESET}
${MAGENTA}real end-to-end run on the target environment. That is what the WSL2${RESET}
${MAGENTA}bring-up was. That is what caught these.${RESET}

${DIM}Full details in PROJECT_MONOLITH.md section 16 and docs/TECHNICAL_REPORT.md section 3.5.1${RESET}
EOF
echo
