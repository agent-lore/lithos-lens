"""Command-line entry point for the Lithos Lens web app."""

from __future__ import annotations

import os
import sys

import uvicorn

from lithos_lens.config import load_config
from lithos_lens.errors import ConfigError, LithosLensError
from lithos_lens.logging import configure_logging

DEFAULT_PORT = 8000


def resolve_port() -> int:
    """Return the port to bind, honoring the ``LENS_PORT`` env override.

    Defaults to :data:`DEFAULT_PORT`. Exposed so the e2e harness (and anyone
    running fake-Lithos app mode) can bind a non-default port to avoid a
    collision without editing config. An out-of-range or non-integer value is a
    :class:`ConfigError`.
    """
    raw = os.environ.get("LENS_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ConfigError(f"LENS_PORT must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"LENS_PORT must be in 1..65535, got {port}")
    return port


# The request-side half of the fan-out bound. ``LithosClient`` caps how many
# tool calls this process may have in flight (``MAX_CONCURRENT_TOOL_CALLS``),
# which protects the shared MCP session; this caps how many requests may be
# waiting to make them. Lens takes unauthenticated requests across the
# trusted-network boundary, so the arrival rate is not Lens's to choose, and a
# request that only queues still costs a task, a socket and a render budget.
# Past this, uvicorn answers 503 immediately — a fast, honest refusal in front
# of a saturated backend, rather than a queue that grows until the process
# does.
MAX_CONCURRENT_REQUESTS = 128


def main() -> None:
    """Load config and run the ASGI server."""

    try:
        config = load_config()
        port = resolve_port()
    except LithosLensError as exc:
        print(f"lithos-lens: {exc}", file=sys.stderr)
        sys.exit(1)

    configure_logging(config.logging.level)
    # Bind all interfaces intentionally: Lens ships as a container and is reached
    # across the trusted-network boundary (REQUIREMENTS.md — no auth beyond that
    # boundary). Not a listen-address vulnerability here.
    uvicorn.run(
        "lithos_lens.main:create_app_from_config",
        factory=True,
        host="0.0.0.0",  # nosec B104
        port=port,
        log_config=None,
        limit_concurrency=MAX_CONCURRENT_REQUESTS,
    )


def create_app_from_config():
    """Uvicorn factory used by :func:`main`."""

    config = load_config()
    configure_logging(config.logging.level)

    from lithos_lens.web import create_app

    return create_app(config)
