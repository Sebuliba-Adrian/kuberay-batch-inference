"""Tests for observability: request_id middleware, JSON logging, /metrics."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path, api_key):
    """
    A TestClient over a freshly-built app. We patch the lifespan so we
    don't need a real Postgres or Ray. Tests that need lifespan wiring
    use their own harness.
    """
    from src import db, ray_client
    from src.config import get_settings
    from src.main import create_app

    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("POSTGRES_URL", f"sqlite+aiosqlite:///{tmp_path}/obs.db")
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("RAY_ADDRESS", "http://fake-ray:8265")

    class _FakeRay:
        def __init__(self, addr):
            self.addr = addr

        def submit_job(self, **kw):
            return "raysubmit_1"

        def get_job_status(self, sid):
            return "RUNNING"

        def list_jobs(self):
            return []

    ray_client.reset()
    ray_client.set_client_factory(_FakeRay)

    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


class TestRequestIdMiddleware:
    def test_generates_request_id_when_missing(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        rid = r.headers.get("X-Request-ID")
        assert rid is not None
        assert len(rid) == 32  # uuid4().hex

    def test_honors_client_request_id(self, client):
        r = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
        assert r.headers["X-Request-ID"] == "trace-abc-123"

    def test_records_request_id_header_on_404(self, client):
        r = client.get("/does-not-exist")
        assert r.status_code == 404
        assert "X-Request-ID" in r.headers


class TestMiddlewareExceptionPath:
    def test_metrics_recorded_when_handler_raises(self):
        from fastapi import FastAPI
        from src.observability import RequestIdMiddleware

        sub = FastAPI()
        sub.add_middleware(RequestIdMiddleware)

        @sub.get("/boom")
        def boom():
            raise RuntimeError("forced")

        with TestClient(sub, raise_server_exceptions=False) as c:
            r = c.get("/boom")
            assert r.status_code == 500


class TestJsonFormatter:
    def test_emits_valid_json_with_context(self):
        from src.observability import JsonFormatter, batch_id_var, request_id_var

        token_r = request_id_var.set("rid-1")
        token_b = batch_id_var.set("batch_xyz")
        try:
            record = logging.LogRecord(
                name="t",
                level=logging.INFO,
                pathname="x.py",
                lineno=1,
                msg="hello %s",
                args=("world",),
                exc_info=None,
            )
            line = JsonFormatter().format(record)
            payload = json.loads(line)
            assert payload["msg"] == "hello world"
            assert payload["request_id"] == "rid-1"
            assert payload["batch_id"] == "batch_xyz"
            assert payload["level"] == "INFO"
        finally:
            request_id_var.reset(token_r)
            batch_id_var.reset(token_b)

    def test_includes_exception(self):
        import sys

        from src.observability import JsonFormatter

        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                name="t",
                level=logging.ERROR,
                pathname="x.py",
                lineno=1,
                msg="bad",
                args=(),
                exc_info=sys.exc_info(),
            )
        line = JsonFormatter().format(record)
        payload = json.loads(line)
        assert "ValueError" in payload["exc"]


class TestSetupLogging:
    def test_replaces_root_handlers(self):
        from src.observability import JsonFormatter, setup_logging

        root = logging.getLogger()
        sentinel = logging.NullHandler()
        root.addHandler(sentinel)
        setup_logging(level="DEBUG")
        assert sentinel not in root.handlers
        assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)


class TestMetricsEndpoint:
    def test_metrics_returns_prometheus_text(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        body = r.text
        assert "http_requests_total" in body
        assert "http_request_duration_seconds" in body

    def test_metrics_increments_after_request(self, client):
        client.get("/health")
        body = client.get("/metrics").text
        assert "/health" in body

    def test_metrics_path_label_is_templated_not_literal(self, client, api_key):
        client.get("/v1/batches/batch_ABC123", headers={"X-API-Key": api_key})
        body = client.get("/metrics").text
        assert "/v1/batches/{batch_id}" in body
        assert "/v1/batches/batch_ABC123" not in body

    def test_metrics_path_label_falls_back_to_literal_for_unmatched(self, client):
        client.get("/nope-no-route-here")
        body = client.get("/metrics").text
        assert "/nope-no-route-here" in body


class TestRenderMetrics:
    def test_returns_bytes_and_content_type(self):
        from src.observability import render_metrics

        body, ct = render_metrics()
        assert isinstance(body, bytes)
        assert "text/plain" in ct
