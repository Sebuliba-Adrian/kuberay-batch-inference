"""
Stdlib logging setup.

Deliberately minimal: one format string, one stdout handler, one
level. No structlog, no JSON formatter library - for a take-home the
extra ceremony obscures intent. Production would swap for
python-json-logger or structlog behind the same entry point.
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """
    (Re)configure the root logger.

    Idempotent: repeated calls replace the handler rather than stacking
    them, so calling ``configure_logging`` from both ``create_app`` and
    the lifespan never duplicates output.
    """
    root = logging.getLogger()

    # Drop any pre-existing handlers (uvicorn installs its own on import)
    # so our format string wins consistently.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Quiet noisy third-party loggers that would otherwise drown out
    # our own INFO messages in the dev console.
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
