"""The MCP transport under :class:`~lithos_lens.lithos_client.LithosClient`.

Everything between a typed client method and the wire: the long-lived
MCP-over-SSE session and its reconnect/backoff, the two process-wide bounds on
a call (a deadline and a concurrency gate), and the decode of an MCP result
into a Lithos payload dict. ``lithos_client`` is the typed method surface over
this; nothing else in Lens imports it.

Why it is its own module rather than more of ``lithos_client``: the client was
the ONE module the repo let past its 800-line god-module ceiling, and three
consecutive budget raises (800 -> 830 -> 815 -> 895) each bought transport
hardening. This split hands that allowance back, and it is a real seam rather
than a line-count device — the transport knows nothing about tasks, notes or
records, and the typed methods know nothing about sessions, gates or JSON.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from lithos_lens.config import LithosConfig

if TYPE_CHECKING:
    from mcp import ClientSession

logger = logging.getLogger(__name__)

# Maximum time a tool call waits for the worker to (re)establish the MCP
# session before failing. Tool calls in lens are user-facing and short-lived,
# so we'd rather fail fast than block a page render.
SESSION_WAIT_TIMEOUT_S = 5.0

# Deadline on ONE tool call, session wait included. Nothing else imposes one —
# the MCP session has no per-request timeout, the httpx timeout covers only
# /health, uvicorn sets no request timeout — so without it an unanswered call
# wedges its request task forever, and on a fan-out surface a stalled lookup
# holds a concurrency slot and stalls everything queued behind it: a bounded
# call COUNT with an unbounded DURATION. A stop-loss well above any healthy
# call, not a latency dial. Callers already degrade one failed read per row, so
# timing a call out costs a row rather than a page.
CALL_TIMEOUT_S = 15.0

# How many tool calls this PROCESS may have in flight at once, over every
# request and surface — the round trip AND the decode behind it, so the
# number of decoded payloads resident at once is bounded too. The per-render
# fan-out gates (``task_links.DETAIL_FANOUT_CONCURRENCY``,
# ``epic_strip.EPIC_FANOUT_BATCH``) bound ONE page; the resource is the single
# MCP session below, shared by every page and by every agent's coordination
# traffic, and N concurrent renders is an unauthenticated request rate, not
# something Lens chooses. So this bound lives with the session it protects.
MAX_CONCURRENT_TOOL_CALLS = 16

# Backoff bounds used by the worker when reconnecting after a transport drop.
RECONNECT_BACKOFF_INITIAL_S = 1.0
RECONNECT_BACKOFF_MAX_S = 30.0


class LithosToolError(RuntimeError):
    """Raised when Lithos returns an error envelope from a tool call.

    ``code`` carries the Lithos 0.4 error code (e.g. ``task_not_found``) when
    the envelope supplies one, so callers can distinguish a missing task from a
    missing tool without matching on message text.
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


#: How a single tool call is placed when no session worker is running. Takes
#: ``(name, arguments)`` and returns the RAW MCP result for this module to
#: bound and decode like any other.
OneShotCall = Callable[[str, dict[str, Any]], Awaitable[Any]]


class MCPTransport:
    """One long-lived MCP-over-SSE session, plus the bounds on calls over it.

    The session is opened and closed by a dedicated worker task spawned in
    :meth:`startup` so that anyio's "cancel scope must be exited from the same
    task that entered it" rule is satisfied. Individual ``call_tool``
    invocations are cross-task safe because they only push JSON-RPC messages
    onto the session's memory streams.

    ``oneshot`` is supplied by the owner rather than implemented here. It is
    the lifecycle escape hatch — the path taken only when :meth:`startup` was
    never called — so it belongs to whoever owns the lifecycle, and it is the
    seam the client's test doubles substitute to drive a canned MCP result
    through the real gate, deadline and decoder. It is resolved ONCE, here, so
    a subclass override is honoured and a later reassignment on the instance
    is not.

    The three bounds are constructor arguments, not module globals read at call
    time, so the owning module's constants stay the single place they are set.
    """

    def __init__(
        self,
        config: LithosConfig,
        *,
        oneshot: OneShotCall,
        call_timeout_s: float = CALL_TIMEOUT_S,
        max_concurrent_calls: int = MAX_CONCURRENT_TOOL_CALLS,
        session_wait_s: float = SESSION_WAIT_TIMEOUT_S,
    ) -> None:
        self._config = config
        self._oneshot = oneshot
        self._call_timeout_s = call_timeout_s
        self._session_wait_s = session_wait_s
        self._session: ClientSession | None = None
        self._session_ready = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._call_gate = asyncio.Semaphore(max_concurrent_calls)

    @property
    def endpoint(self) -> str:
        """The MCP SSE endpoint this transport connects to."""

        return f"{self._config.url.rstrip('/')}/{self._config.mcp_sse_path.strip('/')}"

    async def startup(self) -> None:
        """Spawn the long-lived MCP session worker task.

        Returns once either the first session is established or the
        configured wait timeout elapses. A failure here does not raise: the
        caller's health probe and the per-call session-not-available paths
        handle the degraded case.
        """

        if self._worker_task is not None:
            return
        self._stop_event = asyncio.Event()
        self._session_ready = asyncio.Event()
        self._worker_task = asyncio.create_task(
            self._worker(), name="lithos-mcp-session"
        )
        try:
            await asyncio.wait_for(
                self._session_ready.wait(), timeout=self._session_wait_s
            )
        except TimeoutError:
            logger.info(
                "lithos MCP session not yet established at startup; "
                "will retry in background"
            )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call under the deadline, around the process-wide gate
        :meth:`_invoke` holds — so both bounds cover every tool, both transport
        paths and every SURFACE. Queue time is inside the deadline on purpose:
        a queued call has not answered yet. The deadline sheds the CALLER's
        wait, not the work (see the decoder's note).
        """
        try:
            return await asyncio.wait_for(
                self._invoke(name, arguments), timeout=self._call_timeout_s
            )
        except TimeoutError as exc:
            raise LithosToolError(
                f"lithos tool '{name}' did not answer within {self._call_timeout_s}s",
                code="timeout",
            ) from exc

    async def _invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._call_gate:
            if self._worker_task is None:
                # startup() was never called; fall back to a one-shot session
                # so we don't silently break callers that bypass the lifecycle.
                result = await self._oneshot(name, arguments)
            else:
                session = await self.live_session()
                result = await session.call_tool(name, arguments)
            # Inside the SAME gate slot as the round trip: the parse is the
            # CPU half of a call, so a bound that ends at the network half is
            # not a bound on the call. It is NOT moved off the loop — see the
            # residual note in `decode_tool_result` for why that would not buy
            # what it looks like it buys.
            return decode_tool_result(result)

    async def live_session(self, *, wait_s: float | None = None) -> Any:
        """The worker's MCP session, waiting up to ``wait_s`` for startup.

        ``wait_s=0`` asks only whether a session exists right now: the probe
        runs after other reads already failed, so waiting again buys nothing
        and doubles an unauthenticated request's hold during an outage.
        """
        if wait_s is None:
            wait_s = self._session_wait_s
        if not self._session_ready.is_set():
            if wait_s <= 0:
                raise LithosToolError("Lithos MCP session is not available")
            try:
                await asyncio.wait_for(self._session_ready.wait(), timeout=wait_s)
            except TimeoutError as exc:
                raise LithosToolError("Lithos MCP session is not available") from exc

        session = self._session
        if session is None:
            raise LithosToolError("Lithos MCP session is not available")
        return session

    async def _worker(self) -> None:
        """Hold a single MCP session open for the lifetime of the transport.

        Reconnects with exponential backoff if the session drops. All
        ``async with`` lifecycle for the session lives inside this task,
        so anyio's cancel-scope-task-affinity rule is satisfied even
        though tool calls are awaited from arbitrary request tasks.
        """

        from mcp import ClientSession
        from mcp.client.sse import sse_client

        endpoint = self.endpoint
        backoff = RECONNECT_BACKOFF_INITIAL_S
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
                    backoff = RECONNECT_BACKOFF_INITIAL_S
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
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)

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


def decode_tool_result(result: Any) -> dict[str, Any]:
    """Decode an MCP tool result into the Lithos payload dict.

    Every failure mode surfaces as a coded :class:`LithosToolError` — callers
    must never see a raw ``JSONDecodeError``. ``code="tool_error"`` marks an
    MCP-level error result (``isError``, plain text — e.g. the live server's
    FastMCP output-schema validation rejecting a tool's own error envelope);
    ``code="invalid_response"`` marks a success result whose body isn't a
    JSON object.

    There is deliberately NO size ceiling here (T1-S7 review, round 5), and one
    must not be added. The graph reads take no limit parameter, so a single
    response's row count is agent-controlled — the very input the task detail
    page exists to survive: it renders a bounded first page of blockers plus a
    tail stating the TRUE remainder, and it can only count that remainder from
    a response it parsed. Refusing the oversized edge list would answer exactly
    that case with "blockers unavailable", and any finite ceiling is a
    deployment assumption dressed as an input-domain restriction. Cost is
    bounded where the cost is instead — the per-row fan-out, its concurrency
    and its wall clock (see ``task_links``).

    THE RESIDUAL, stated plainly because it is not covered: this parse is
    synchronous AND it holds the GIL for its whole duration. ``json.loads``
    runs one C call with no bytecode boundaries in it, so while it runs NO
    other Python in this process runs — not another request, not the event
    loop, not the deadline in :meth:`MCPTransport.call_tool`, which cannot fire
    until the parse it would interrupt has finished. Measured on this project's
    Python 3.12: a 26MB response parses in ~130ms, during which a 1ms ticker
    thread advanced TWICE (it advances ~123 times over the same span of
    ``time.sleep``).

    An earlier revision ran this on a ThreadPoolExecutor and claimed the loop
    stayed responsive. It does not: a thread hop moves where the GIL is held,
    not whether it is held, so the pool bought a bound on width and nothing on
    latency, plus 16 threads and an uncancellable work item. It was removed
    rather than re-documented (PR #50 review). Real isolation would need
    process separation or a GIL-releasing decoder, and neither is worth its
    complexity for the sizes a healthy Lithos returns.

    So: an ACCEPTED RISK, owned upstream, closing when the graph reads grow a
    row limit. What bounds it today is that Lens is behind a trusted-network
    boundary and the responses are as large as the graph an agent built.
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


def raise_for_error(payload: dict[str, Any]) -> None:
    """Raise the coded :class:`LithosToolError` a Lithos error envelope names."""

    if payload.get("status") == "error":
        code = str(payload.get("code") or "")
        message = str(payload.get("message") or code or "Lithos error")
        raise LithosToolError(message, code=code)
