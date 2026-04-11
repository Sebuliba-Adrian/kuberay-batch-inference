# API Reference

Base URL: `http://localhost:8000` (via kind port-forwarding when running locally)

All `/v1/batches` routes require the `X-API-Key` header. `/health` and `/ready` are public for Kubernetes probes.

---

## `GET /health`

Liveness probe. Returns 200 as long as the process is alive and serving HTTP. Does **not** check any external dependency.

**No auth.**

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok"}
```

---

## `GET /ready`

Readiness probe. Pings Postgres and the Ray dashboard. Returns 200 if both are up, 503 if either is down.

**No auth.**

```bash
curl http://localhost:8000/ready
```

**200 OK** (healthy):

```json
{
  "status": "ok",
  "checks": {
    "postgres": "ok",
    "ray": "ok"
  }
}
```

**503 Service Unavailable** (Ray down):

```json
{
  "status": "degraded",
  "checks": {
    "postgres": "ok",
    "ray": "error: ConnectionError"
  }
}
```

---

## `POST /v1/batches`

Submit a new batch inference job. The request body shape matches OpenAI's Batches API closely enough that an OpenAI SDK client could talk to this endpoint with only a `base_url` swap.

### Request

```http
POST /v1/batches HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-API-Key: <your-api-key>

{
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "input": [
    {"prompt": "What is 2+2?"},
    {"prompt": "Hello world"}
  ],
  "max_tokens": 50
}
```

### Request fields

| Field        | Type            | Required | Constraints                                              |
|--------------|-----------------|----------|----------------------------------------------------------|
| `model`      | string          | yes      | 1-128 chars, matches `^[A-Za-z0-9._\-/:]+$`              |
| `input`      | list of objects | yes      | 1-100,000 items                                          |
| `input[].prompt` | string      | yes      | 1-32,000 chars                                           |
| `max_tokens` | int             | no       | 1-8192, default 256                                      |

Unknown top-level fields or unknown fields inside `input[]` are **rejected** (`extra="forbid"`).

### Response — 200 OK

```json
{
  "id": "batch_01JABC...",
  "object": "batch",
  "endpoint": "/v1/batches",
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "status": "queued",
  "created_at": 1744384000,
  "completed_at": null,
  "request_counts": {
    "total": 2,
    "completed": 0,
    "failed": 0
  },
  "error": null
}
```

### Response fields

| Field             | Type          | Description |
|-------------------|---------------|-------------|
| `id`              | string        | Unique batch id, prefixed `batch_` with a ULID suffix (lexicographically sortable) |
| `object`          | string        | Always `"batch"` |
| `endpoint`        | string        | Always `"/v1/batches"` |
| `model`           | string        | Echo of the requested model |
| `status`          | enum          | `queued` \| `in_progress` \| `completed` \| `failed` \| `cancelled` |
| `created_at`      | int           | Unix timestamp in **seconds** (mirrors OpenAI) |
| `completed_at`    | int \| null   | Unix timestamp when the batch reached a terminal state; null while in progress |
| `request_counts`  | object        | `{total, completed, failed}` — progress counters |
| `error`           | string \| null | Human-readable error string if `status=failed`, else null |

### Error responses

| Status | Body                                                           | Meaning |
|--------|----------------------------------------------------------------|---------|
| 401    | `{"detail": "Missing API key"}`                                | No `X-API-Key` header. Sent with `WWW-Authenticate: APIKey`. |
| 401    | `{"detail": "Invalid API key"}`                                | Wrong key. Constant-time compared. |
| 422    | `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`    | Pydantic validation failure (empty input, bad model regex, out-of-range `max_tokens`, unknown field, etc.) |
| 503    | `{"detail": "Ray cluster unavailable"}`                        | Ray dashboard unreachable at submit time. The DB row is still written in `status=failed` so the attempt is traceable. |

---

## `GET /v1/batches/{batch_id}`

Fetch the current state of a batch. Reads from Postgres — does **not** query Ray on the hot path, so this endpoint stays fast and works even when Ray is down.

```bash
curl -H "X-API-Key: $API_KEY" \
  http://localhost:8000/v1/batches/batch_01JABC...
```

### Response — 200 OK

Same shape as `POST /v1/batches` response. `status` reflects the latest state as seen by the background status poller (5 s polling interval by default).

### Error responses

| Status | Body                                           | Meaning |
|--------|------------------------------------------------|---------|
| 401    | `{"detail": "Missing API key"}`                | No `X-API-Key`. |
| 401    | `{"detail": "Invalid API key"}`                | Wrong key. |
| 404    | `{"detail": "Batch not found: batch_..."}`     | Unknown id. |

---

## `GET /v1/batches/{batch_id}/results`

Stream the batch's inference results as newline-delimited JSON (`application/x-ndjson`). Each line is one result row in the same order as the input prompts.

**Only valid when `status = completed`.** Any other state returns 409.

```bash
curl -H "X-API-Key: $API_KEY" \
  http://localhost:8000/v1/batches/batch_01JABC.../results
```

### Response — 200 OK

`Content-Type: application/x-ndjson`

```
{"id":"0","prompt":"What is 2+2?","response":"2 + 2 equals 4.","finish_reason":"stop","prompt_tokens":6,"completion_tokens":8,"error":null}
{"id":"1","prompt":"Hello world","response":"Hello! How can I help you today?","finish_reason":"stop","prompt_tokens":4,"completion_tokens":9,"error":null}
```

### Result row fields

| Field               | Type           | Description |
|---------------------|----------------|-------------|
| `id`                | string         | Zero-indexed position in the original input list |
| `prompt`            | string         | Echo of the input prompt |
| `response`          | string \| null | Model generation (null if the row errored) |
| `finish_reason`     | string         | `stop` if the model emitted EOS, `length` if it hit `max_tokens` |
| `prompt_tokens`     | int            | Tokens in the input (for billing / metrics) |
| `completion_tokens` | int            | Tokens generated |
| `error`             | string \| null | Row-level error message (null on success) |

### Error responses

| Status | Body                                                        | Meaning |
|--------|-------------------------------------------------------------|---------|
| 401    | `{"detail": "Missing API key"}`                             | No `X-API-Key`. |
| 401    | `{"detail": "Invalid API key"}`                             | Wrong key. |
| 404    | `{"detail": "Batch not found: batch_..."}`                  | Unknown id. |
| 409    | `{"detail": "Batch not complete (status=in_progress)"}`    | Batch hasn't reached `completed` yet. Also returned for `failed` and `cancelled`. |
| 500    | `{"detail": "Results file missing"}`                        | The DB says completed but the file isn't on the shared PVC — data-plane corruption. |

---

## OpenAPI / Swagger

The live OpenAPI schema is available at `/openapi.json`. Interactive docs:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

## Complete worked example

```bash
API_KEY="demo-api-key-change-me-in-production"

# 1. Submit
RESPONSE=$(curl -fsS -X POST http://localhost:8000/v1/batches \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "input": [{"prompt": "What is 2+2?"}, {"prompt": "Hello world"}],
    "max_tokens": 50
  }')
BATCH_ID=$(echo "$RESPONSE" | jq -r .id)
echo "Batch id: $BATCH_ID"

# 2. Poll until completed
while true; do
  STATUS=$(curl -fsS "http://localhost:8000/v1/batches/$BATCH_ID" \
    -H "X-API-Key: $API_KEY" | jq -r .status)
  echo "Status: $STATUS"
  case "$STATUS" in
    completed) break ;;
    failed|cancelled) echo "terminal non-success: $STATUS"; exit 1 ;;
  esac
  sleep 3
done

# 3. Fetch results
curl -fsS "http://localhost:8000/v1/batches/$BATCH_ID/results" \
  -H "X-API-Key: $API_KEY"
```
