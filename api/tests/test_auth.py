"""
Red tests for src.auth.require_api_key.

Tests drive the auth contract before any implementation exists. We
mount the dependency on a tiny scratch FastAPI router and hit it with
httpx.ASGITransport so the test exercises the full FastAPI depends
pipeline, not just the function in isolation.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

# ─── Helpers ────────────────────────────────────────────────────────
# Each test constructs its own mini-app so there is zero cross-test
# state bleed through FastAPI's dependency cache.


def _build_app() -> FastAPI:
    """A one-route app where the only route requires the API key."""
    from src.auth import require_api_key  # noqa: PLC0415

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def _protected() -> dict[str, str]:
        return {"ok": "true"}

    return app


async def _get(
    app: FastAPI, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], dict[str, object]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/protected", headers=headers or {})
    return r.status_code, dict(r.headers), r.json() if r.content else {}


# ─── Missing header ─────────────────────────────────────────────────
async def test_missing_api_key_returns_401(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """No header at all → 401 Unauthorized."""
    monkeypatch.setenv("API_KEY", api_key)

    status, _headers, body = await _get(_build_app(), headers=None)

    assert status == 401
    assert body.get("detail") == "Missing API key"


async def test_missing_api_key_sets_www_authenticate_header(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """Per RFC 7235, a 401 must include WWW-Authenticate."""
    monkeypatch.setenv("API_KEY", api_key)

    _status, headers, _body = await _get(_build_app(), headers=None)

    assert "www-authenticate" in {k.lower() for k in headers}
    assert headers.get("www-authenticate", headers.get("WWW-Authenticate")) == "APIKey"


# ─── Invalid header ─────────────────────────────────────────────────
async def test_wrong_api_key_returns_401(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """Wrong key value → 401 with 'Invalid' detail, not 403."""
    monkeypatch.setenv("API_KEY", api_key)

    status, _headers, body = await _get(
        _build_app(), headers={"X-API-Key": "not-the-right-key"}
    )

    assert status == 401
    assert body.get("detail") == "Invalid API key"


async def test_wrong_api_key_sets_www_authenticate_header(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """Invalid-key 401 must also carry WWW-Authenticate."""
    monkeypatch.setenv("API_KEY", api_key)

    _status, headers, _body = await _get(
        _build_app(), headers={"X-API-Key": "not-the-right-key"}
    )

    assert headers.get("www-authenticate", headers.get("WWW-Authenticate")) == "APIKey"


# ─── Valid header ───────────────────────────────────────────────────
async def test_valid_api_key_allows_request(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """Matching key → 200 and the protected route runs."""
    monkeypatch.setenv("API_KEY", api_key)

    status, _headers, body = await _get(
        _build_app(), headers={"X-API-Key": api_key}
    )

    assert status == 200
    assert body == {"ok": "true"}


async def test_empty_api_key_header_returns_401(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """An explicit empty string header is still invalid."""
    monkeypatch.setenv("API_KEY", api_key)

    status, _headers, _body = await _get(
        _build_app(), headers={"X-API-Key": ""}
    )

    assert status == 401


# ─── Constant-time compare ──────────────────────────────────────────
def test_require_api_key_uses_constant_time_compare(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """
    Verify the implementation calls hmac.compare_digest.

    This is a white-box test — it asserts an implementation choice — but
    the choice matters for security so we pin it explicitly. If a future
    refactor swaps to plain '==' the test catches it.
    """
    import hmac as real_hmac

    called: list[tuple[bytes, bytes]] = []
    real_compare = real_hmac.compare_digest

    def spy_compare(a: bytes, b: bytes) -> bool:
        called.append((bytes(a), bytes(b)))
        return real_compare(a, b)

    monkeypatch.setattr("src.auth.hmac.compare_digest", spy_compare)
    monkeypatch.setenv("API_KEY", api_key)

    # Drive one valid and one invalid call to force both branches.
    import asyncio

    async def _exercise() -> None:
        await _get(_build_app(), headers={"X-API-Key": api_key})
        await _get(_build_app(), headers={"X-API-Key": "wrong"})

    asyncio.get_event_loop().run_until_complete(_exercise()) if False else asyncio.run(
        _exercise()
    )

    assert len(called) >= 1, "hmac.compare_digest was never called — auth is insecure"


# ─── Header name contract ───────────────────────────────────────────
async def test_api_key_header_name_is_x_api_key(
    monkeypatch: pytest.MonkeyPatch, api_key: str
) -> None:
    """The exercise explicitly specifies X-API-Key. A typo like X-Api-Key
    should STILL work because HTTP headers are case-insensitive, but a
    different header name (like Authorization) should NOT be accepted."""
    monkeypatch.setenv("API_KEY", api_key)

    # Wrong header name: Authorization: Bearer <key>
    status_wrong, _, _ = await _get(
        _build_app(), headers={"Authorization": f"Bearer {api_key}"}
    )
    assert status_wrong == 401

    # Case variation: lowercase still works (HTTP is case-insensitive)
    status_ok, _, _ = await _get(
        _build_app(), headers={"x-api-key": api_key}
    )
    assert status_ok == 200
