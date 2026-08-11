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
from lithos_lens.knowledge import (
    RelatedNeighborhood,
    SearchResult,
    normalize_related,
    normalize_search_result,
)
from lithos_lens.task_graph import (
    BlockedTaskRecord,
    EdgeRecord,
    normalize_blocked_task,
    normalize_edge,
)
from lithos_lens.tasks import (
    AgentRecord,
    FindingRecord,
    NoteRecord,
    NoteSummary,
    TaskRecord,
    TaskStatusRecord,
    normalize_agent,
    normalize_finding,
    normalize_note,
    normalize_note_summary,
    normalize_task,
    normalize_task_status,
    note_updated_sort_key,
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

# recent_notes walks lithos_list in pages of this size (via offset) and sorts
# client-side, because the tool has no ordering parameter — removed once
# upstream lithos_list grows server-side ordering (task e0e31654).
RECENT_NOTES_FETCH_PAGE = 500

# Runaway guard for the pagination loop, NOT an expected bound: the walk
# normally terminates on the response's `total` (~6 pages for today's ~2.9k
# note corpus). This only stops a server reporting an absurd total from
# turning one landing-page render into an unbounded crawl.
_RECENT_NOTES_MAX_PAGES = 40


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
        with_claims: bool = False,
    ) -> list[TaskRecord]: ...

    async def task_ready(
        self,
        *,
        limit: int | None = None,
        with_claims: bool = False,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[TaskRecord]: ...

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]: ...

    async def task_get(self, task_id: str) -> TaskRecord: ...

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]: ...

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]: ...

    async def task_status(self, task_id: str) -> TaskStatusRecord | None: ...

    async def list_findings(
        self, task_id: str, *, since: str | None = None
    ) -> list[FindingRecord]: ...

    async def stats(self) -> dict[str, Any]: ...

    async def list_agents(self) -> list[AgentRecord]: ...

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None: ...

    async def read_note_by_path(self, path: str) -> NoteRecord | None: ...

    async def related(self, knowledge_id: str) -> RelatedNeighborhood: ...

    async def list_notes(
        self,
        *,
        title_contains: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]: ...

    async def recent_notes(
        self,
        *,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]: ...

    async def search_notes(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[SearchResult]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class RegistrationResult:
    success: bool
    message: str = ""


class LithosToolError(RuntimeError):
    """Raised when Lithos returns an error envelope from a tool call.

    ``code`` carries the Lithos 0.4 error code (e.g. ``task_not_found``) when
    the envelope supplies one, so callers can distinguish a missing task from a
    missing tool without matching on message text.
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


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
        with_claims: bool = False,
    ) -> list[TaskRecord]:
        # Upstream lithos_task_list currently defaults with_claims to False,
        # matching the Lens default — but the flag is ALWAYS sent explicitly so
        # an upstream default flip can't silently invert it (the exact bug
        # shape task_ready had; see its docstring).
        arguments: dict[str, Any] = {"with_claims": with_claims}
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

    async def task_ready(
        self,
        *,
        limit: int | None = None,
        with_claims: bool = False,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[TaskRecord]:
        """List the ready frontier via ``lithos_task_ready``.

        ``with_claims`` defaults to False here — a deliberate divergence from
        upstream Lithos, whose ``lithos_task_ready`` defaults it to True: Lens
        is a read-only UI with no claims consumer on this path, so the heavier
        claims payload is cost without a use. Because the upstream default is
        True, the flag is ALWAYS sent explicitly; omitting it when False would
        silently invert the Lens default.
        """
        arguments: dict[str, Any] = {"with_claims": with_claims}
        if limit is not None:
            arguments["limit"] = limit
        if project:
            arguments["project"] = project
        if tags:
            arguments["tags"] = tags
        payload = await self._call_tool("lithos_task_ready", arguments)
        _raise_for_error(payload)
        return [
            normalize_task(task)
            for task in payload.get("tasks", [])
            if isinstance(task, dict)
        ]

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]:
        arguments: dict[str, Any] = {}
        if limit is not None:
            arguments["limit"] = limit
        if project:
            arguments["project"] = project
        if tags:
            arguments["tags"] = tags
        payload = await self._call_tool("lithos_task_blocked", arguments)
        _raise_for_error(payload)
        return [
            normalize_blocked_task(task)
            for task in payload.get("tasks", [])
            if isinstance(task, dict)
        ]

    async def task_get(self, task_id: str) -> TaskRecord:
        """Fetch a single task via ``lithos_task_get``.

        Never returns None: Lithos answers a missing task with an error
        envelope (code ``task_not_found``), surfaced by ``_raise_for_error``
        as a coded :class:`LithosToolError`. A success payload that carries
        neither a valid task (a dict with a non-empty string ``id``) nor a
        supported legacy ``tasks`` list envelope is a broken response and
        raises ``code="invalid_response"`` rather than returning a None that
        callers can't tell apart from "absent".
        """
        payload = await self._call_tool("lithos_task_get", {"task_id": task_id})
        _raise_for_error(payload)
        raw = payload.get("task")
        if raw is None:
            # Supported legacy envelope is strictly {"tasks": [<task>, ...]};
            # any other container shape falls through to invalid_response
            # rather than leaking a KeyError/TypeError from indexing.
            legacy = payload.get("tasks")
            if isinstance(legacy, list) and legacy:
                raw = legacy[0]
        if not isinstance(raw, dict) or not _is_nonempty_str(raw.get("id")):
            raise LithosToolError(
                f"lithos_task_get returned no valid task payload for '{task_id}'",
                code="invalid_response",
            )
        return normalize_task(raw)

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]:
        arguments: dict[str, Any] = {"task_id": task_id}
        if recursive:
            arguments["recursive"] = True
        if include_closed:
            arguments["include_closed"] = True
        payload = await self._call_tool("lithos_task_children", arguments)
        _raise_for_error(payload)
        return [
            normalize_task(task)
            for task in payload.get("tasks", [])
            if isinstance(task, dict)
        ]

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]:
        arguments: dict[str, Any] = {"task_id": task_id, "direction": direction}
        if types:
            arguments["types"] = types
        payload = await self._call_tool("lithos_task_edge_list", arguments)
        _raise_for_error(payload)
        return [
            normalize_edge(edge)
            for edge in payload.get("edges", [])
            if isinstance(edge, dict)
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

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        arguments: dict[str, Any] = {
            "id": knowledge_id,
            "agent_id": self._config.agent_id,
        }
        if max_length is not None:
            arguments["max_length"] = max_length
        payload = await self._call_tool("lithos_read", arguments)
        _raise_for_error(payload)
        return normalize_note(payload)

    async def read_note_by_path(self, path: str) -> NoteRecord | None:
        """Resolve a note by ``path`` (the wiki-link resolver's existence probe).

        A missing path is the common case for a title-style wiki target, so the
        coded ``doc_not_found`` envelope is answered with ``None`` rather than a
        raised error; any other failure propagates. The truncated
        ``max_length=1`` read still returns complete frontmatter (§6.3), so the
        id needed for the redirect is always present on a hit.
        """
        arguments: dict[str, Any] = {
            "path": path,
            "agent_id": self._config.agent_id,
            "max_length": 1,
        }
        try:
            payload = await self._call_tool("lithos_read", arguments)
            _raise_for_error(payload)
        except LithosToolError as exc:
            if exc.code == "doc_not_found":
                return None
            raise
        return normalize_note(payload)

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        """Fetch a note's related neighborhood via ``lithos_related``.

        The tool accepts only ``id`` / ``include`` / ``depth`` / ``namespace``
        (FastMCP rejects unexpected arguments outright), so exactly
        ``{"id", "depth"}`` is sent — §6.5 pins the panel to ``depth=1``.
        """
        payload = await self._call_tool(
            "lithos_related",
            {"id": knowledge_id, "depth": 1},
        )
        _raise_for_error(payload)
        return normalize_related(payload)

    async def list_notes(
        self,
        *,
        title_contains: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]:
        """List notes via ``lithos_list`` (wiki-link title disambiguation).

        Only the arguments an active filter needs are sent; ``lithos_list``
        answers ``{"items": [...], "total": ...}`` with lightweight rows
        (``id``/``title``/``path``/``updated``/``tags``, no body).
        """
        arguments: dict[str, Any] = {}
        if title_contains:
            arguments["title_contains"] = title_contains
        if tags:
            arguments["tags"] = tags
        if limit is not None:
            arguments["limit"] = limit
        payload = await self._call_tool("lithos_list", arguments)
        _raise_for_error(payload)
        # lithos_list returns {"items": [...], "total": ...} — "items" has been
        # its one and only container key since the very first implementation
        # (verified against the Lithos source and its full git history; the
        # previously accepted "notes"/"documents"/"results" aliases never
        # existed — "results" is lithos_search's key).
        rows: Any = payload.get("items") or []
        return [normalize_note_summary(item) for item in rows if isinstance(item, dict)]

    async def recent_notes(
        self,
        *,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]:
        """Most-recently-updated notes (the /knowledge landing browse list).

        ``lithos_list`` has NO ordering parameter at the pinned Lithos version:
        ``knowledge.list_all`` returns metadata-cache insertion order, and
        ``limit``/``offset`` slice that order — so a small direct ``limit``
        would return an arbitrary old page, not the newest notes (server-side
        ``order_by`` is upstream Lithos task e0e31654). Until that lands,
        newest-first is computed here over the WHOLE (filtered) corpus, not one
        page (PR #39 review: with ~2.9k notes, a note updated today but
        inserted late would otherwise never surface): pages of
        ``RECENT_NOTES_FETCH_PAGE`` rows are walked via ``offset`` until the
        response's ``total`` is exhausted. After each page the accumulator is
        re-sorted newest-first and, when ``limit`` is given, truncated — so
        memory stays bounded by page size + ``limit`` however large the corpus.
        """
        rows: list[NoteSummary] = []
        seen_ids: set[str] = set()
        offset = 0
        pages = 0
        fetched = 0
        while pages < _RECENT_NOTES_MAX_PAGES:
            arguments: dict[str, Any] = {}
            if tags:
                arguments["tags"] = tags
            arguments["limit"] = RECENT_NOTES_FETCH_PAGE
            arguments["offset"] = offset
            payload = await self._call_tool("lithos_list", arguments)
            _raise_for_error(payload)
            items: Any = payload.get("items") or []
            page_rows = [
                normalize_note_summary(item) for item in items if isinstance(item, dict)
            ]
            # Dedup by id across pages: a concurrent corpus mutation can shift
            # the insertion-ordered window between requests, re-serving a row
            # an earlier page already delivered.
            fresh = [row for row in page_rows if row.id not in seen_ids]
            seen_ids.update(row.id for row in fresh)
            rows.extend(fresh)
            rows.sort(key=lambda s: note_updated_sort_key(s.updated), reverse=True)
            if limit is not None:
                del rows[limit:]
            fetched += len(page_rows)
            pages += 1
            # Advance by the REQUESTED page span, never by rows received:
            # upstream slices the matching ids offset:offset+limit BEFORE
            # reading documents and then skips unreadable ones, so a short (or
            # even empty) page has still consumed a full window server-side.
            # Advancing by received rows would re-read the overlap (duplicate
            # cards) or stop early on an all-unreadable page.
            offset += RECENT_NOTES_FETCH_PAGE
            total = payload.get("total")
            if isinstance(total, int):
                if offset >= total:
                    break
            elif not page_rows:
                # No trustworthy total: an empty page is the only stop signal.
                break
        else:
            logger.warning(
                "recent_notes stopped at the %d-page runaway guard "
                "(%d rows fetched); the recent list may be incomplete",
                _RECENT_NOTES_MAX_PAGES,
                fetched,
            )
        logger.debug("recent_notes fetched %d rows in %d page(s)", fetched, pages)
        return rows

    async def search_notes(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[SearchResult]:
        """Hybrid-search notes via ``lithos_search`` (the /knowledge query path).

        ``mode="hybrid"`` is always sent (§7.1); only the arguments an active
        filter needs ride along. ``lithos_search`` answers
        ``{"results": [...]}`` — "results" is its container key (distinct from
        ``lithos_list``'s "items"). Snippets carry raw markdown and are rendered
        escaped by the template, never through the markdown renderer.
        """
        arguments: dict[str, Any] = {"query": query, "mode": "hybrid"}
        if tags:
            arguments["tags"] = tags
        if limit is not None:
            arguments["limit"] = limit
        payload = await self._call_tool("lithos_search", arguments)
        _raise_for_error(payload)
        rows: Any = payload.get("results") or []
        return [
            normalize_search_result(item) for item in rows if isinstance(item, dict)
        ]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._worker_task is None:
            # startup() was never called; fall back to a one-shot session so
            # we don't silently break callers that bypass the lifecycle.
            result = await self._call_tool_oneshot(name, arguments)
            return _decode_tool_result(result)

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

    async def _call_tool_oneshot(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run one tool call over a throwaway session.

        Returns the RAW MCP result; decoding stays in ``_call_tool`` so both
        transport paths share the same error handling.
        """
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        endpoint = self._mcp_endpoint()
        async with (
            sse_client(endpoint) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            return await session.call_tool(name, arguments)

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


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _decode_tool_result(result: Any) -> dict[str, Any]:
    """Decode an MCP tool result into the Lithos payload dict.

    Every failure mode surfaces as a coded :class:`LithosToolError` — callers
    must never see a raw ``JSONDecodeError``. ``code="tool_error"`` marks an
    MCP-level error result (``isError``, plain text — e.g. the live server's
    FastMCP output-schema validation rejecting a tool's own error envelope);
    ``code="invalid_response"`` marks a success result whose body isn't a
    JSON object.
    """
    blocks = getattr(result, "content", [])
    text = str(getattr(blocks[0], "text", "") or "") if blocks else ""
    if getattr(result, "isError", False):
        raise LithosToolError(text or "Lithos tool call failed", code="tool_error")
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LithosToolError(
            f"Lithos returned a non-JSON tool result: {text[:200]}",
            code="invalid_response",
        ) from exc
    if not isinstance(payload, dict):
        raise LithosToolError(
            "Lithos returned a non-object tool result", code="invalid_response"
        )
    return payload


def _raise_for_error(payload: dict[str, Any]) -> None:
    if payload.get("status") == "error":
        code = str(payload.get("code") or "")
        message = str(payload.get("message") or code or "Lithos error")
        raise LithosToolError(message, code=code)
