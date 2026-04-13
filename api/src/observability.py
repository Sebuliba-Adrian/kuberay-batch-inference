"""
Observability primitives for the FastAPI proxy.

Three concerns, one module:

1. Request IDs. A ContextVar carries the current request_id through the
   call stack so log records and metrics can attach it without threading
   it through every function signature.
2. Structured logging. JSON one event per line, with request_id and
   batch_id whenever they're in scope. Production log aggregators
   (Loki, CloudWatch, Datadog) parse this format natively.
3. Prometheus metrics. Counters and a histogram exposed at /metrics in
   the standard text exposition format. No external dependencies on a
   running Prometheus server; this is the FastAPI-side counterpart to
   Ray's built-in :8080 Prometheus endpoint that the monitoring stack
   in k8s/monitoring/ already scrapes.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

# ContextVars survive across await boundaries, so async middlewares and
# downstream handlers see the same value.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
batch_id_var: ContextVar[str] = ContextVar("batch_id", default="-")

# Dedicated registry so we don't pollute the global one (helps tests and
# avoids "duplicated timeseries" errors on app reload).
REGISTRY = CollectorRegistry()

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the API.",
    ["method", "path", "status"],
    registry=REGISTRY,
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)
batch_submitted_total = Counter(
    "batch_submitted_total",
    "Batches accepted by the API.",
    ["model"],
    registry=REGISTRY,
)
batch_terminal_total = Counter(
    "batch_terminal_total",
    "Batches that reached a terminal state.",
    ["model", "status"],
    registry=REGISTRY,
)
rayjob_submit_failures_total = Counter(
    "rayjob_submit_failures_total",
    "Ray submit_job calls that returned an error.",
    ["reason"],
    registry=REGISTRY,
)


class JsonFormatter(logging.Formatter):
    """One-line JSON per log record. Adds request_id and batch_id from ctx."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "batch_id": batch_id_var.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Replace the root handler with a JSON one. Safe to call repeatedly."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Generate a request ID per request, set it in the ContextVar, return
    it on the X-Request-ID response header. Honors a client-supplied
    X-Request-ID header so traces propagate across hops.

    Also records request count + duration into Prometheus.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        batch_token = batch_id_var.set("-")
        start = time.perf_counter()
        literal_path = request.url.path
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
        except Exception:
            http_requests_total.labels(request.method, literal_path, "500").inc()
            raise
        finally:
            # Prefer the matched route template (e.g. /v1/batches/{batch_id})
            # over the literal URL path so Prometheus label cardinality stays
            # bounded regardless of how many distinct batch IDs flow through.
            route = request.scope.get("route")
            path_label = getattr(route, "path", None) or literal_path
            duration = time.perf_counter() - start
            http_request_duration_seconds.labels(request.method, path_label).observe(duration)
            request_id_var.reset(token)
            batch_id_var.reset(batch_token)
        http_requests_total.labels(request.method, path_label, str(status_code)).inc()
        response.headers["X-Request-ID"] = rid
        return response


def render_metrics() -> tuple[bytes, str]:
    """Return Prometheus text exposition body and content-type header."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
