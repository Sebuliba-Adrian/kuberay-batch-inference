"""
X-API-Key authentication dependency.

FastAPI's built-in APIKeyHeader is configured with auto_error=False so
we can return a 401 (not 403) with WWW-Authenticate per RFC 7235.
hmac.compare_digest protects against timing side channels.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from .config import Settings, get_settings

log = logging.getLogger(__name__)

API_KEY_HEADER_NAME = "X-API-Key"

_header_scheme = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "APIKey"},
    )


async def require_api_key(
    provided: Annotated[str | None, Depends(_header_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """FastAPI dependency - raises 401 on missing or invalid key."""
    # Missing or explicitly empty header
    if not provided:
        log.info("auth: request rejected - missing %s header", API_KEY_HEADER_NAME)
        raise _unauthorized("Missing API key")

    # Constant-time compare against the configured secret
    expected = settings.API_KEY.get_secret_value().encode()
    actual = provided.encode()
    if not hmac.compare_digest(actual, expected):
        log.info("auth: request rejected - invalid %s", API_KEY_HEADER_NAME)
        raise _unauthorized("Invalid API key")
