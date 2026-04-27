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
from lithos_lens.tasks import (
    AgentRecord,
    FindingRecord,
    NoteRecord,
    TaskRecord,
    TaskStatusRecord,
    normalize_agent,
    normalize_finding,
    normalize_note,
    normalize_task,
    normalize_task_status,
)

logger = logging.getLogger(__name__)

LithosHealth = Literal["ok", "degraded", "unreachable"]


class LithosClientProtocol(Protocol):
    """Subset of Lithos operations required by the common core."""

    async def health(self) -> LithosHealth: ...

    async def register_agent(self) -> bool: ...

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
    ) -> list[TaskRecord]: ...

    async def task_status(self, task_id: str) -> TaskStatusRecord | None: ...

    async def list_findings(
        self, task_id: str, *, since: str | None = None
    ) -> list[FindingRecord]: ...

    async def stats(self) -> dict[str, Any]: ...

    async def list_agents(self) -> list[AgentRecord]: ...

    async def read_note(self, knowledge_id: str) -> NoteRecord | None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class RegistrationResult:
    success: bool
    message: str = ""


class LithosToolError(RuntimeError):
    """Raised when Lithos returns an error envelope from a tool call."""


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

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
    ) -> list[TaskRecord]:
        arguments: dict[str, Any] = {}
        if agent:
            arguments["agent"] = agent
        if status:
            arguments["status"] = status
        if tags:
            arguments["tags"] = tags
        if since:
            arguments["since"] = since
        payload = await self._call_tool("lithos_task_list", arguments)
        _raise_for_error(payload)
        return [
            normalize_task(task)
            for task in payload.get("tasks", [])
            if isinstance(task, dict)
        ]

    async def task_status(self, task_id: str) -> TaskStatusRecord | None:
        payload = await self._call_tool("lithos_task_status", {"task_id": task_id})
        _raise_for_error(payload)
        tasks = payload.get("tasks", [])
        if not tasks:
            return None
        raw = tasks[0]
        return normalize_task_status(raw) if isinstance(raw, dict) else None

    async def list_findings(
        self, task_id: str, *, since: str | None = None
    ) -> list[FindingRecord]:
        arguments: dict[str, Any] = {"task_id": task_id}
        if since:
            arguments["since"] = since
        payload = await self._call_tool("lithos_finding_list", arguments)
        _raise_for_error(payload)
        return [
            normalize_finding(finding, task_id)
            for finding in payload.get("findings", [])
            if isinstance(finding, dict)
        ]

    async def stats(self) -> dict[str, Any]:
        payload = await self._call_tool("lithos_stats", {})
        _raise_for_error(payload)
        return payload

    async def list_agents(self) -> list[AgentRecord]:
        payload = await self._call_tool("lithos_agent_list", {})
        _raise_for_error(payload)
        return [
            normalize_agent(agent)
            for agent in payload.get("agents", [])
            if isinstance(agent, dict)
        ]

    async def read_note(self, knowledge_id: str) -> NoteRecord | None:
        payload = await self._call_tool(
            "lithos_read",
            {"id": knowledge_id, "agent_id": self._config.agent_id},
        )
        _raise_for_error(payload)
        return normalize_note(payload)

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


def _raise_for_error(payload: dict[str, Any]) -> None:
    if payload.get("status") == "error":
        message = str(payload.get("message") or payload.get("code") or "Lithos error")
        raise LithosToolError(message)
