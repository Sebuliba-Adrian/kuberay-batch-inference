"""
Minimal end-to-end benchmark for the batch inference API.

Submits a fixed-size batch, polls until terminal, measures submit
latency, poll latency distribution, wall time, inference window,
throughput, and average response length. Use it to sanity-check
the stack after `make up` and to gather rough numbers for the
Technical Report.

Usage:
    python scripts/benchmark.py
    # Overrides via env:
    HOST=http://localhost:8000 API_KEY=... python scripts/benchmark.py
"""

import json
import os
import statistics
import time
import urllib.request

HOST = os.environ.get("HOST", "http://127.0.0.1:8000")
API_KEY = os.environ.get("API_KEY", "demo-api-key-change-me-in-production")

PROMPTS = [
    "What is 2+2?",
    "Capital of Japan?",
    "Translate 'hello' to French.",
    "Define recursion in one phrase.",
    "What does HTTP stand for?",
    "Square root of 144?",
    "Largest planet in the solar system?",
    "Who wrote Hamlet?",
    "Symbol for gold?",
    "Define entropy briefly.",
    "What is 9 squared?",
    "Year WWII ended?",
    "Boiling point of water in C?",
    "Speed of light in km/s?",
    "Define osmosis.",
]
N = len(PROMPTS)
MAX_TOKENS = 32


def _post(path: str, body: bytes) -> dict:
    req = urllib.request.Request(
        f"{HOST}{path}",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req).read())


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{HOST}{path}", headers={"X-API-Key": API_KEY})
    return json.loads(urllib.request.urlopen(req).read())


def main() -> None:
    body = json.dumps(
        {
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "input": [{"prompt": p} for p in PROMPTS],
            "max_tokens": MAX_TOKENS,
        }
    ).encode()

    t0 = time.time()
    submit_response = _post("/v1/batches", body)
    batch_id = submit_response["id"]
    t_submit_ms = (time.time() - t0) * 1000
    print(f"submitted={batch_id} submit_latency={t_submit_ms:.0f}ms")

    last_status = None
    poll_latencies: list[float] = []
    while True:
        pt0 = time.time()
        status = _get(f"/v1/batches/{batch_id}")
        poll_latencies.append((time.time() - pt0) * 1000)
        if status["status"] != last_status:
            print(f"  [{time.time() - t0:6.1f}s] {status['status']}")
            last_status = status["status"]
        if status["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(3)

    t_total = time.time() - t0
    started_at = status.get("started_at") or status.get("created_at")
    finished_at = status.get("completed_at")
    inference_s = finished_at - started_at if (started_at and finished_at) else None

    result = _get(f"/v1/batches/{batch_id}/results")
    if isinstance(result, dict):
        output = result.get("output", [])
    else:
        # NDJSON stream path: not used by main repo, but keep tolerant
        output = []

    ok = sum(1 for r in output if not r.get("error") and r.get("response"))
    words = [len((r.get("response") or "").split()) for r in output if r.get("response")]

    print()
    print("=" * 60)
    print(f"BENCHMARK RESULTS ({N} prompts, max_tokens={MAX_TOKENS})")
    print("=" * 60)
    print(f"final status:           {status['status']}")
    print(f"successful:             {ok}/{N}")
    print(f"submit latency:         {t_submit_ms:.0f} ms (POST /v1/batches)")
    if poll_latencies:
        p50 = statistics.median(poll_latencies)
        p95 = (
            statistics.quantiles(poll_latencies, n=20)[18]
            if len(poll_latencies) >= 20
            else max(poll_latencies)
        )
        print(f"poll p50 / p95 latency: {p50:.1f} / {p95:.1f} ms (GET /v1/batches/{{id}})")
    print(f"wall time (submit->done): {t_total:.1f} s")
    if inference_s:
        print(f"inference window:       {inference_s} s")
        if ok > 0:
            print(f"throughput:             {ok / inference_s:.3f} prompts/s")
            print(f"per-prompt avg:         {inference_s / ok:.1f} s/prompt")
    if words:
        print(f"avg response words:     {statistics.mean(words):.1f}")
    print("sample output:")
    for r in output[:2]:
        print(f"  Q: {r.get('prompt', '')}")
        print(f"  A: {(r.get('response') or '').strip()[:140]}")


if __name__ == "__main__":
    main()
