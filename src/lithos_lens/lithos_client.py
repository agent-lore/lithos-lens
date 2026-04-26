"""Lithos connectivity helpers.

Milestone 0 only needs health probing and startup registration semantics. Tool
calls are kept behind a small interface so later milestones can replace the
placeholder MCP path with full request/response tool support without changing
the web layer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from lithos_lens.config import LithosConfig

logger = logging.getLogger(__name__)

LithosHealth = Literal["ok", "degraded", "unreachable"]


class LithosClientProtocol(Protocol):
    """Subset of Lithos operations required by the common core."""

    async def health(self) -> LithosHealth: ...

    async def register_agent(self) -> bool: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class RegistrationResult:
    success: bool
    message: str = ""


class LithosClient:
    """Best-effort Lithos client used by the web app."""

    def __init__(
        self,
        config: LithosConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        self._config = config
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_s)

    async def health(self) -> LithosHealth:
        """Probe Lithos's HTTP health endpoint."""

        try:
            response = await self._http.get(f"{self._config.url.rstrip('/')}/health")
        except httpx.HTTPError:
            logger.info("lithos health probe failed", exc_info=True)
            return "unreachable"

        if response.status_code >= 500:
            return "unreachable"
        if response.status_code >= 400:
            return "degraded"

        try:
            payload = response.json()
        except ValueError:
            return "ok"

        status = payload.get("status")
        return "ok" if status == "ok" else "degraded"

    async def register_agent(self) -> bool:
        """Attempt Lens startup registration.

        Full Lithos tool calls use MCP-over-SSE. If the optional MCP client
        package is not installed yet, this method records a best-effort attempt
        and degrades without preventing app startup.
        """

        try:
            await self._call_tool(
                "lithos_agent_register",
                {
                    "id": self._config.agent_id,
                    "name": "Lithos Lens",
                    "type": "web-ui",
                },
            )
        except Exception:
            logger.info("lithos agent registration failed", exc_info=True)
            return False
        return True

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        endpoint = (
            f"{self._config.url.rstrip('/')}/{self._config.mcp_sse_path.strip('/')}"
        )
        async with (
            sse_client(endpoint) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
        return _decode_tool_result(result)

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()


def _decode_tool_result(result: Any) -> dict[str, Any]:
    blocks = getattr(result, "content", [])
    if blocks and getattr(blocks[0], "text", None):
        return json.loads(blocks[0].text)
    return {}
