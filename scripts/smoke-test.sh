#!/usr/bin/env bash
# -------------------------------------------------------------------------
# smoke-test.sh — Run the exact curl from the exercise PDF and verify
# the full happy path: submit → poll → fetch results.
#
# Requires the stack to be running (`make up`) and port-forwarded to :8000.
# -------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# 1. Load API key from .env (fall back to demo key if missing)
if [[ -f .env ]]; then
  API_KEY=$(grep -E '^API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '"'"'")
else
  API_KEY="demo-api-key-change-me-in-production"
fi
: "${API_KEY:?API_KEY not set}"

HOST="${HOST:-http://localhost:8000}"

log()  { printf "\033[0;32m[smoke]\033[0m %s\n" "$*"; }
fail() { printf "\033[0;31m[smoke]\033[0m %s\n" "$*" >&2; exit 1; }

# 2. Health check
log "Health check..."
curl -fsS "$HOST/health" | jq . || fail "health check failed"

# 3. Unauthenticated POST -> must be 401
log "Auth check: POST without X-API-Key must return 401..."
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$HOST/v1/batches" \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-0.5B-Instruct", "input": [{"prompt": "x"}], "max_tokens": 5}')
if [[ "$code" != "401" ]]; then fail "expected 401, got $code"; fi
log "✓ unauthenticated request rejected with 401"

# 4. Wrong key -> must be 401
log "Auth check: POST with wrong X-API-Key must return 401..."
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$HOST/v1/batches" \
  -H "X-API-Key: wrong-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-0.5B-Instruct", "input": [{"prompt": "x"}], "max_tokens": 5}')
if [[ "$code" != "401" ]]; then fail "expected 401, got $code"; fi
log "✓ invalid key rejected with 401"

# 5. The exact curl from the exercise PDF
log "Submitting batch (exercise PDF payload)..."
response=$(curl -fsS -X POST "$HOST/v1/batches" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "input": [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}],
    "max_tokens": 50
  }')
echo "$response" | jq .
batch_id=$(echo "$response" | jq -r .id)
[[ "$batch_id" =~ ^batch_ ]] || fail "invalid batch id: $batch_id"
log "✓ submitted batch $batch_id"

# 6. Poll status until terminal (max ~5 minutes)
log "Polling status..."
for i in $(seq 1 60); do
  status=$(curl -fsS "$HOST/v1/batches/$batch_id" -H "X-API-Key: $API_KEY" | jq -r .status)
  printf "  [%02d] status=%s\n" "$i" "$status"
  case "$status" in
    completed) break ;;
    failed|cancelled) fail "batch ended in terminal non-success state: $status" ;;
  esac
  sleep 5
done
[[ "$status" == "completed" ]] || fail "batch did not complete within 5 minutes"
log "✓ batch completed"

# 7. Fetch results
log "Fetching results..."
curl -fsS "$HOST/v1/batches/$batch_id/results" -H "X-API-Key: $API_KEY"
echo
log "✓ smoke test passed"
