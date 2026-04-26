"""Telemetry hooks.

Milestone 0 keeps OpenTelemetry optional. The request middleware records the
place where spans are created once OTEL packages are enabled; with telemetry
disabled it is a low-cost pass-through.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from lithos_lens.config import TelemetryConfig

RequestHandler = Callable[[Request], Awaitable[Response]]


def install_request_middleware(app: Any, config: TelemetryConfig) -> None:
    """Install request instrumentation middleware."""

    @app.middleware("http")
    async def lens_request(request: Request, call_next: RequestHandler) -> Response:
        response = await call_next(request)
        if config.enabled:
            response.headers["x-lithos-lens-telemetry"] = "enabled"
        return response
