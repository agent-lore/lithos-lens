"""Lithos connectivity helpers.

Milestone 0 only needs health probing and startup registration semantics. Tool
calls are kept behind a small interface so later milestones can replace the
placeholder MCP path with full request/response tool support without changing
the web layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

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

if TYPE_CHECKING:
    from mcp import ClientSession

logger = logging.getLogger(__name__)

LithosHealth = Literal["ok", "degraded", "unreachable"]

# Maximum time _call_tool waits for the worker to (re)establish the MCP
# session before failing. Tool calls in lens are user-facing and short-lived,
# so we'd rather fail fast than block a page render.
_SESSION_WAIT_TIMEOUT_S = 5.0

# Backoff bounds used by the worker when reconnecting after a transport drop.
_RECONNECT_BACKOFF_INITIAL_S = 1.0
_RECONNECT_BACKOFF_MAX_S = 30.0


class LithosClientProtocol(Protocol):
    """Subset of Lithos operations required by the common core."""

    async def startup(self) -> None: ...

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
    """Best-effort Lithos client used by the web app.

    Maintains a single, long-lived MCP-over-SSE session that is reused
    across all tool calls. The session is opened and closed by a dedicated
    worker task spawned in :meth:`startup` so that anyio's "cancel scope
    must be exited from the same task that entered it" rule is satisfied.
    Individual ``call_tool`` invocations are cross-task safe because they
    only push JSON-RPC messages onto the session's memory streams.
    """

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
        self._session: ClientSession | None = None
        self._session_ready = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        """Spawn the long-lived MCP session worker task.

        Returns once either the first session is established or the
        configured wait timeout elapses. A failure here does not raise:
        ``health()`` and the per-call session-not-available paths handle
        the degraded case.
        """

        if self._worker_task is not None:
            return
        self._stop_event = asyncio.Event()
        self._session_ready = asyncio.Event()
        self._worker_task = asyncio.create_task(
            self._mcp_worker(), name="lithos-mcp-session"
        )
        try:
            await asyncio.wait_for(
                self._session_ready.wait(), timeout=_SESSION_WAIT_TIMEOUT_S
            )
        except TimeoutError:
            logger.info(
                "lithos MCP session not yet established at startup; "
                "will retry in background"
            )

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
        """Attempt Lens startup registration via the shared MCP session."""

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
        if self._worker_task is None:
            # startup() was never called; fall back to a one-shot session so
            # we don't silently break callers that bypass the lifecycle.
            return await self._call_tool_oneshot(name, arguments)

        if not self._session_ready.is_set():
            try:
                await asyncio.wait_for(
                    self._session_ready.wait(), timeout=_SESSION_WAIT_TIMEOUT_S
                )
            except TimeoutError as exc:
                raise LithosToolError("Lithos MCP session is not available") from exc

        session = self._session
        if session is None:
            raise LithosToolError("Lithos MCP session is not available")
        result = await session.call_tool(name, arguments)
        return _decode_tool_result(result)

    async def _call_tool_oneshot(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        endpoint = self._mcp_endpoint()
        async with (
            sse_client(endpoint) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
        return _decode_tool_result(result)

    async def _mcp_worker(self) -> None:
        """Hold a single MCP session open for the lifetime of the client.

        Reconnects with exponential backoff if the session drops. All
        ``async with`` lifecycle for the session lives inside this task,
        so anyio's cancel-scope-task-affinity rule is satisfied even
        though tool calls are awaited from arbitrary request tasks.
        """

        from mcp import ClientSession
        from mcp.client.sse import sse_client

        endpoint = self._mcp_endpoint()
        backoff = _RECONNECT_BACKOFF_INITIAL_S
        while not self._stop_event.is_set():
            try:
                async with AsyncExitStack() as stack:
                    reader, writer = await stack.enter_async_context(
                        sse_client(endpoint)
                    )
                    session = await stack.enter_async_context(
                        ClientSession(reader, writer)
                    )
                    await session.initialize()
                    self._session = session
                    self._session_ready.set()
                    backoff = _RECONNECT_BACKOFF_INITIAL_S
                    await self._stop_event.wait()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("lithos MCP session lost; reconnecting", exc_info=True)
            finally:
                self._session = None
                self._session_ready.clear()

            if self._stop_event.is_set():
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                return
            except TimeoutError:
                pass
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_S)

    def _mcp_endpoint(self) -> str:
        return f"{self._config.url.rstrip('/')}/{self._config.mcp_sse_path.strip('/')}"

    async def close(self) -> None:
        self._stop_event.set()
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except TimeoutError:
                self._worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._worker_task
            self._worker_task = None
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
