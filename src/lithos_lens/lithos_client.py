"""The typed Lithos method surface, plus the HTTP health probe.

One method per Lithos tool: build the arguments, place the call, raise on the
error envelope, normalize the payload into Lens records. Everything below a
call — the shared MCP session, the deadline and concurrency gate on it, the
reconnect, and the decode of a raw MCP result — belongs to
:mod:`lithos_lens.mcp_transport`, which this module owns one instance of.
``health()`` is the exception that stays here: it probes the plain HTTP
``/health`` endpoint, not an MCP tool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from lithos_lens.config import LithosConfig
from lithos_lens.knowledge import (
    RelatedNeighborhood,
    SearchResult,
    normalize_related,
    normalize_search_result,
)
from lithos_lens.mcp_transport import (
    CALL_TIMEOUT_S,
    MAX_CONCURRENT_TOOL_CALLS,
    LithosToolError,
    MCPTransport,
    raise_for_error,
)
from lithos_lens.normalizers import (
    normalize_agent,
    normalize_finding,
    normalize_note,
    normalize_note_summary,
    normalize_task,
    normalize_task_status,
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
    note_updated_sort_key,
)

logger = logging.getLogger(__name__)

LithosHealth = Literal["ok", "degraded", "unreachable"]

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
        resolved_since: str | None = None,
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


class LithosClient:
    """Best-effort Lithos client used by the web app.

    Owns one :class:`~lithos_lens.mcp_transport.MCPTransport` — a single,
    long-lived MCP-over-SSE session shared by every tool call — and adds the
    typed methods over it. Best-effort in the sense that no method here
    protects the page: a failed read raises a coded
    :class:`~lithos_lens.mcp_transport.LithosToolError` that callers degrade
    per row.
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
        self._transport = MCPTransport(
            config,
            # A BOUND method, resolved once: a subclass overriding the escape
            # hatch is honoured (that is how the test doubles feed canned MCP
            # results through the real gate, deadline and decoder), a later
            # reassignment on the instance is not.
            oneshot=self._call_tool_oneshot,
            # The bounds are the transport's to APPLY but this module's to
            # SET: it owns the session they protect and it is the name other
            # components' comments cite. Read here, so setting them here (or
            # patching them in a test) is what sets them.
            call_timeout_s=CALL_TIMEOUT_S,
            max_concurrent_calls=MAX_CONCURRENT_TOOL_CALLS,
        )

    async def startup(self) -> None:
        """Open the long-lived MCP session; see :meth:`MCPTransport.startup`.

        Does not raise on failure: ``health()`` and the per-call
        session-not-available paths handle the degraded case.
        """

        await self._transport.startup()

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
        resolved_since: str | None = None,
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
        if resolved_since:
            # Terminal window: resolved_at >= value, NULL-resolved dropped.
            arguments["resolved_since"] = resolved_since
        payload = await self._call_tool("lithos_task_list", arguments)
        raise_for_error(payload)
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
        raise_for_error(payload)
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
        raise_for_error(payload)
        return [
            normalize_blocked_task(task)
            for task in payload.get("tasks", [])
            if isinstance(task, dict)
        ]

    async def task_get(self, task_id: str) -> TaskRecord:
        """Fetch a single task via ``lithos_task_get``.

        Never returns None: Lithos answers a missing task with an error
        envelope (code ``task_not_found``), surfaced by ``raise_for_error``
        as a coded :class:`LithosToolError`. A success payload that carries
        neither a valid task (a dict with a non-empty string ``id``) nor a
        supported legacy ``tasks`` list envelope is a broken response and
        raises ``code="invalid_response"`` rather than returning a None that
        callers can't tell apart from "absent".
        """
        payload = await self._call_tool("lithos_task_get", {"task_id": task_id})
        raise_for_error(payload)
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
        raise_for_error(payload)
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
        raise_for_error(payload)
        return [
            normalize_edge(edge)
            for edge in payload.get("edges", [])
            if isinstance(edge, dict)
        ]

    async def task_status(self, task_id: str) -> TaskStatusRecord | None:
        payload = await self._call_tool("lithos_task_status", {"task_id": task_id})
        raise_for_error(payload)
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
        raise_for_error(payload)
        return [
            normalize_finding(finding, task_id)
            for finding in payload.get("findings", [])
            if isinstance(finding, dict)
        ]

    async def stats(self) -> dict[str, Any]:
        payload = await self._call_tool("lithos_stats", {})
        raise_for_error(payload)
        return payload

    async def list_agents(self) -> list[AgentRecord]:
        payload = await self._call_tool("lithos_agent_list", {})
        raise_for_error(payload)
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
        raise_for_error(payload)
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
            raise_for_error(payload)
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
        raise_for_error(payload)
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
        raise_for_error(payload)
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
        result-record memory stays bounded by page size + ``limit``; the
        cross-page dedup id set is the one O(corpus) piece (a few thousand
        strings today).
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
            raise_for_error(payload)
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
        raise_for_error(payload)
        rows: Any = payload.get("results") or []
        return [
            normalize_search_result(item) for item in rows if isinstance(item, dict)
        ]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Place one tool call on the transport, which bounds and decodes it.

        Kept as a method (rather than callers reaching for the transport) so
        the deadline, the process-wide gate and the decode sit behind ONE name
        for every typed method above — and so the contract guardrail in
        ``tests/test_lithos_contracts.py`` can find every tool this client
        calls by scanning for ``self._call_tool("<literal>")``.
        """
        return await self._transport.call_tool(name, arguments)

    async def _call_tool_oneshot(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run one tool call over a throwaway session.

        The lifecycle escape hatch: taken only when ``startup()`` was never
        called, so callers that bypass the lifecycle don't silently break.
        Returns the RAW MCP result — the transport bounds and decodes both
        paths alike, so this one gets the same treatment as the shared session.
        """
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with (
            sse_client(self._transport.endpoint) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            return await session.call_tool(name, arguments)

    async def close(self) -> None:
        await self._transport.close()
        if self._owns_http_client:
            await self._http.aclose()


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)
