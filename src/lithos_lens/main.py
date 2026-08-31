"""Command-line entry point for the Lithos Lens web app."""

from __future__ import annotations

import os
import sys

import uvicorn

from lithos_lens.config import load_config
from lithos_lens.errors import ConfigError, LithosLensError
from lithos_lens.logging import configure_logging

DEFAULT_PORT = 8000
# Every interface. Lens ships as a container and is reached across the
# trusted-network boundary (REQUIREMENTS §5C.1: no auth beyond that boundary),
# so a loopback default would make the published port unreachable. The accepted
# posture stays the default; `LENS_HOST` is what narrows it.
DEFAULT_HOST = "0.0.0.0"  # nosec B104


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


def resolve_host() -> str:
    """Return the interface to bind, honoring the ``LENS_HOST`` env override.

    Defaults to :data:`DEFAULT_HOST` for the reason recorded there. The
    override exists for the case where that posture is not what is wanted:
    fake-Lithos app mode registers ``POST /tasks/events/publish``, an
    unauthenticated write seam with no Origin check, and ``make e2e`` runs TWO
    such instances at once. On every interface that is a way for anyone on the
    segment to fan an event into the very browser tabs the suite is
    photographing — into artifacts a reviewer then reads as evidence. Both
    harness instances therefore pin ``LENS_HOST`` to loopback; see
    ``e2e/playwright.config.ts``, and ``tests/test_fake_lithos.py`` pins that
    they do.

    Residual, stated rather than left to be rediscovered: a fake-mode instance
    started BY HAND still binds every interface unless ``LENS_HOST`` says
    otherwise. Defaulting the bind from the fake-mode flag would close that
    too, but it would mean this module importing that predicate out of the
    LithosClient component — a new dependency edge for the rarer, deliberate
    case, while the automatic one is closed here.

    Blank or unset falls through to the default, as ``LENS_PORT`` does.
    """
    return os.environ.get("LENS_HOST", "").strip() or DEFAULT_HOST


def main() -> None:
    """Load config and run the ASGI server."""

    try:
        config = load_config()
        port = resolve_port()
        host = resolve_host()
    except LithosLensError as exc:
        print(f"lithos-lens: {exc}", file=sys.stderr)
        sys.exit(1)

    configure_logging(config.logging.level)
    uvicorn.run(
        "lithos_lens.main:create_app_from_config",
        factory=True,
        host=host,
        port=port,
        log_config=None,
    )


def create_app_from_config():
    """Uvicorn factory used by :func:`main`."""

    config = load_config()
    configure_logging(config.logging.level)

    from lithos_lens.web import create_app

    return create_app(config)
