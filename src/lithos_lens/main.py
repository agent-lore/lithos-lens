"""Command-line entry point for the Lithos Lens web app."""

from __future__ import annotations

import sys

import uvicorn

from lithos_lens.config import load_config
from lithos_lens.errors import LithosLensError
from lithos_lens.logging import configure_logging


def main() -> None:
    """Load config and run the ASGI server."""

    try:
        config = load_config()
    except LithosLensError as exc:
        print(f"lithos-lens: {exc}", file=sys.stderr)
        sys.exit(1)

    configure_logging(config.logging.level)
    uvicorn.run(
        "lithos_lens.main:create_app_from_config",
        factory=True,
        host="0.0.0.0",
        port=8000,
        log_config=None,
    )


def create_app_from_config():
    """Uvicorn factory used by :func:`main`."""

    config = load_config()
    configure_logging(config.logging.level)

    from lithos_lens.web import create_app

    return create_app(config)
