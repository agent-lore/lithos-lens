"""Shared Lithos event subscription and browser fan-out."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from lithos_lens.config import EventsConfig, LithosConfig
from lithos_lens.errors import EventSubscriberLimit

logger = logging.getLogger(__name__)

EventStatus = Literal["live", "reconnecting", "disabled"]

# Task-scoped upstream types: a `task_id` is required (an event without one is
# dropped) and every one of them forces a dashboard refresh — the payloads are
# too sparse to drive a complete UI patch on their own (`task.updated` carries
# nothing but the id).
TASK_EVENT_TYPES = {
    "task.created",
    "task.claimed",
    "task.released",
    "task.completed",
    "task.cancelled",
    "task.updated",
    "task.reopened",
    "finding.posted",
}
# System-scoped upstream types: no task scope at all. They are forwarded with
# `task_id=""` and `requires_refresh=False` because they invalidate the
# agent-dropdown data only and must not move the board under the operator.
SYSTEM_EVENT_TYPES = {"agent.registered"}
CONSUMED_EVENT_TYPES = TASK_EVENT_TYPES | SYSTEM_EVENT_TYPES

# `lens.*` is the reserved namespace for Lens-internal synthetic events; it
# never collides with an upstream type and is never sent upstream. That
# guarantee only holds on the wire because `as_sse` sanitizes what it
# interpolates: an SSE frame is newline-delimited, so an id or type carrying
# CR/LF would let an upstream payload forge whole extra frames — including a
# `lens.*` one — into Lens's own browser control channel.
LENS_REFRESH_EVENT = "lens.refresh"
# Shortest gap between two synthetic reconnect refreshes. Each one costs every
# open dashboard a full refetch, so a flapping upstream must not be able to use
# them as a fan-out amplifier against Lens and Lithos. A refresh that lands
# inside the window is deferred to its end, never dropped: each disconnected
# interval is a distinct gap that only a refresh can close.
LENS_REFRESH_MIN_INTERVAL_S = 5.0
# Ids are upstream-controlled and end up in an SSE frame, a browser dedupe key
# and an outbound request header; cap them well above any real ULID.
EVENT_ID_MAX_LENGTH = 200
# Upstream drives both how often these conditions fire and the text they carry
# into the log sink: warn on the first occurrence, then once per this many, so
# the sink stays bounded. See :class:`RateLimitedWarning`.
WARN_REPEAT_INTERVAL = 100
# Ceiling on concurrent browser subscribers. Each one pins a queue and a live
# generator, and every publish is O(subscribers) — so an unbounded count is
# both a memory and a latency amplifier, driven by whoever can reach the port
# rather than by Lens (it takes unauthenticated requests across the
# trusted-network boundary, same premise as MAX_CONCURRENT_RENDERS).
#
# Set well above any real tab count, because stale subscribers are normal: a
# departed peer is only discovered by a failing WRITE, so a browser reloading
# in a loop leaves one behind per reload for up to SSE_KEEPALIVE_S. The cap has
# to clear that churn without refusing the operator.
MAX_EVENT_SUBSCRIBERS = 128
# Ceiling on one upstream frame, counted in characters of accumulated line text
# — what is actually retained (a str costs 1-4 bytes per character), and
# measurable without re-encoding every line to ask.
#
# Two exposures, one bound: a `data:` line that never terminates, and a frame
# whose lines never stop arriving. Each is otherwise limited only by available
# memory, and what it produces is then held in up to MAX_EVENT_SUBSCRIBERS x
# the queue depth of slots.
#
# Deliberately NOT the whole-response ceiling that was considered and rejected
# for the MCP client (docs/architecture.toml records that call): there the
# response IS the answer, so a cap answers a large-but-legitimate read with
# "unavailable". Here a frame is a notification Lens reads one field of, the
# board reconciles on the next event or the poll fallback regardless, and no
# real event is within three orders of magnitude of this size.
MAX_EVENT_FRAME_CHARS = 64 * 1024


def wire_safe(value: str) -> str:
    """`value` reduced to something that cannot break out of its SSE frame line."""

    return "".join(char for char in value if char.isprintable())[:EVENT_ID_MAX_LENGTH]


def is_replay_cursor(value: str) -> bool:
    """Whether `value` is safe to send back upstream as a `Last-Event-ID` header.

    Deliberately narrower than "printable": h11 refuses non-ASCII, control
    characters and leading/trailing whitespace, and that refusal lands in the
    reconnect loop — costing a replay window — so anything it would reject is
    never recorded as a cursor. Applied to the id exactly as Lithos sent it: a
    cursor Lens rewrote is a position that never existed upstream, and what
    Lithos replays for one is unspecified.
    """

    return (
        bool(value)
        and len(value) <= EVENT_ID_MAX_LENGTH
        and value.isascii()
        and value.isprintable()
        and value == value.strip()
    )


class RateLimitedWarning:
    """WARNING emitter for one condition whose rate Lens does not choose.

    Every bound in this module refuses something, and each refusal is worth a
    record — but who drives them is upstream or whoever can reach the port, so
    one record per occurrence is an unbounded write into the operator's log
    sink. Only the first occurrence and every `interval`-th are logged, each
    carrying that condition's running total, and every field value goes through
    `wire_safe` on the way, because the log is one more sink upstream text
    reaches.

    One instance per condition rather than one shared counter: a condition that
    fires constantly would otherwise swallow the FIRST occurrence of a rare
    one, which is the record most worth having.
    """

    def __init__(self, message: str, *, interval: int = WARN_REPEAT_INTERVAL) -> None:
        self._message = message
        self._interval = interval
        self._count = 0

    def record(self, **fields: str) -> None:
        self._count += 1
        if self._count != 1 and self._count % self._interval:
            return
        logger.warning(
            self._message,
            extra={
                **{name: wire_safe(value) for name, value in fields.items()},
                "occurrences": self._count,
            },
        )


#: A task-scoped frame arrived with no `task_id` — dropped at normalization.
DROPPED_EVENTS = RateLimitedWarning("dropping task-scoped lithos event without task_id")
#: A subscriber queue was full — that ONE browser misses this event; the others
#: still get it, and its own reconnect/poll path is what recovers its board.
UNDELIVERED_EVENTS = RateLimitedWarning("lens event subscriber queue full")
#: A frame exceeded MAX_EVENT_FRAME_CHARS — dropped whole, never parsed.
OVERSIZED_FRAMES = RateLimitedWarning("dropping oversized lithos event frame")
#: A browser asked for the stream at MAX_EVENT_SUBSCRIBERS — refused with a 503.
#: Rate-limited like the rest: the requests that trip this are exactly the ones
#: arriving faster than Lens wants them.
REFUSED_SUBSCRIBERS = RateLimitedWarning(
    "refusing an event-stream subscriber at capacity"
)


@dataclass(frozen=True)
class LensEvent:
    id: str
    type: str
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    requires_refresh: bool = True
    #: The upstream frame's own `id:` field, empty when Lens synthesized the id
    #: from the payload — only a real upstream id is a replay cursor.
    upstream_id: str = ""

    def as_sse(self) -> str:
        # Both interpolated fields are sanitized at this sink, so no
        # LensEvent — however it was constructed — can emit a second frame.
        event_id = wire_safe(self.id)
        event_type = wire_safe(self.type)
        data = json.dumps(
            {
                "id": event_id,
                "type": event_type,
                "task_id": self.task_id,
                "payload": self.payload,
                "requires_refresh": self.requires_refresh,
            },
            separators=(",", ":"),
        )
        return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"


@dataclass
class EventHub:
    config: EventsConfig
    lithos: LithosConfig
    status: EventStatus = "disabled"
    #: Id of the last event received from Lithos, replayed via `Last-Event-ID`.
    last_event_id: str = ""

    def __post_init__(self) -> None:
        self._subscribers: set[asyncio.Queue[LensEvent]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._gap_since_last_open = False
        self._reconnects = 0
        self._stream_open = False
        self._last_refresh_at = float("-inf")
        self._pending_refresh: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self.config.enabled:
            self.status = "disabled"
            return
        if self._task is not None and not self._task.done():
            return
        self.status = "reconnecting"
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="lithos-events")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        if self._pending_refresh is not None:
            self._pending_refresh.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pending_refresh
            self._pending_refresh = None
        self.status = "disabled"
        for queue in list(self._subscribers):
            self._subscribers.discard(queue)

    def subscribe(self, *, maxsize: int = 100) -> asyncio.Queue[LensEvent]:
        """Register a browser queue, refusing past :data:`MAX_EVENT_SUBSCRIBERS`.

        Refusing is the point. The caller turns this into a 503, the browser's
        EventSource fails over to polling, and the board is degraded rather
        than the process being one subscriber further towards falling over.
        Enforced HERE rather than at the route so a second caller cannot
        acquire an unbounded queue by not knowing about the cap.
        """
        if len(self._subscribers) >= MAX_EVENT_SUBSCRIBERS:
            REFUSED_SUBSCRIBERS.record(limit=str(MAX_EVENT_SUBSCRIBERS))
            raise EventSubscriberLimit(
                f"event subscriber limit reached ({MAX_EVENT_SUBSCRIBERS})"
            )
        queue: asyncio.Queue[LensEvent] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[LensEvent]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event: LensEvent) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Rate-limited: a subscriber that stops draining turns every
                # upstream event into a record, at an upstream-chosen rate
                # times a client-chosen number of stalled tabs.
                UNDELIVERED_EVENTS.record(event_type=event.type, event_id=event.id)

    async def _on_stream_open(self) -> None:
        """Called once the upstream stream is established.

        An open that FOLLOWS A GAP results in one synthetic `lens.refresh`
        reaching browsers. `Last-Event-ID` replays Lithos's ring buffer, but a
        gap wider than that buffer cannot be replayed, so a full browser
        refresh is the only correctness guarantee.

        The condition is the gap, not a prior success. Only the very first
        attempt is refresh-free, because nothing has been missed yet: the page
        a browser holds was server-rendered moments earlier. A first attempt
        that FAILED breaks that — Lens is up and serving, so browsers attach
        and their EventSource reports `live` (the polling fallback is driven by
        the browser-to-Lens stream, not by Lens's upstream, so it never arms),
        and every Lithos change during the retry window is invisible to them.
        Keying on "have we ever connected" would ask about the hub's history
        when staleness is a property of the interval.
        """
        self._stream_open = True
        self.status = "live"
        if self._gap_since_last_open:
            await self._schedule_refresh()
            self._gap_since_last_open = False

    async def _schedule_refresh(self) -> None:
        """Broadcast the reconnect backstop, at most once per cooldown window.

        Trailing edge, not a drop: a reconnect inside the window arms one
        deferred broadcast for the end of it, and any further reconnects
        coalesce into that same pending one. So a flapping upstream still costs
        each dashboard at most one refetch per window, while every disconnected
        interval — including one whose replay cursor was dropped — is still
        answered by a refresh.
        """
        delay = self._last_refresh_at + LENS_REFRESH_MIN_INTERVAL_S - _now()
        if delay <= 0:
            await self._publish_refresh()
            return
        if self._pending_refresh is None or self._pending_refresh.done():
            self._pending_refresh = asyncio.create_task(
                self._deferred_refresh(delay), name="lens-refresh"
            )

    async def _deferred_refresh(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._publish_refresh()

    async def _publish_refresh(self) -> None:
        self._last_refresh_at = _now()
        self._reconnects += 1
        await self.publish(
            LensEvent(
                id=f"{LENS_REFRESH_EVENT}:{self._reconnects}",
                type=LENS_REFRESH_EVENT,
                task_id="",
                payload={"reason": "reconnect"},
            )
        )

    async def _run(self) -> None:
        backoff = self.config.reconnect_backoff_ms or (1000,)
        attempt = 0
        while not self._stop.is_set():
            self._stream_open = False
            try:
                async for event in _stream_lithos_events(
                    self.lithos,
                    last_event_id=self.last_event_id,
                    on_connect=self._on_stream_open,
                ):
                    attempt = 0
                    if is_replay_cursor(event.upstream_id):
                        self.last_event_id = event.upstream_id
                    await self.publish(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.info("lithos event stream disconnected", exc_info=True)
            if not self._stream_open and self.last_event_id:
                # The attempt died before the stream came up. Whatever the
                # cause, retrying the same cursor forever would leave the hub
                # permanently dark, so drop it — the `lens.refresh` backstop
                # already covers the replay this gives up.
                logger.info(
                    "dropping lithos replay cursor after a failed connect",
                    extra={"last_event_id": self.last_event_id},
                )
                self.last_event_id = ""
            # This iteration ended — cleanly, by error, or without ever opening.
            # Either way there is now an interval in which browsers were
            # attached and receiving nothing, so the next open owes them a
            # refresh (see _on_stream_open).
            self._gap_since_last_open = True
            self.status = "reconnecting"
            delay_ms = backoff[min(attempt, len(backoff) - 1)]
            attempt += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay_ms / 1000)
            except TimeoutError:
                continue


async def _stream_lithos_events(
    lithos: LithosConfig,
    *,
    last_event_id: str = "",
    on_connect: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[LensEvent]:
    endpoint = _events_url(lithos)
    headers = {"Last-Event-ID": last_event_id} if last_event_id else None
    timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream("GET", endpoint, headers=headers) as response,
    ):
        response.raise_for_status()
        if on_connect is not None:
            await on_connect()
        frame: list[str] = []
        frame_chars = 0
        poisoned = False
        async for line in _iter_sse_lines(response, max_chars=MAX_EVENT_FRAME_CHARS):
            if line is None:
                # A line the reader had to cut short. What survived cannot be
                # parsed honestly — a truncated `data:` line is not a smaller
                # event, it is a different one — so the whole frame goes.
                poisoned = True
                frame = []
                continue
            if line == "":
                if poisoned:
                    OVERSIZED_FRAMES.record()
                elif frame:
                    event = parse_lithos_sse_frame(frame)
                    if event is not None:
                        yield event
                frame = []
                frame_chars = 0
                poisoned = False
                continue
            if poisoned or line.startswith(":"):
                continue
            frame_chars += len(line)
            if frame_chars > MAX_EVENT_FRAME_CHARS:
                # Lines that each fitted but together did not.
                poisoned = True
                frame = []
                continue
            frame.append(line)


async def _iter_sse_lines(
    response: httpx.Response, *, max_chars: int
) -> AsyncIterator[str | None]:
    """SSE lines from `response`, with the PENDING line bounded.

    httpx's own `aiter_lines` buffers until a newline arrives, so a stream that
    opens a line and never terminates it is bounded only by memory — and a
    frame-level cap cannot reach that, because the line has already been
    materialized by the time it is handed over. Reading bytes and splitting
    here is what makes the buffer cappable at all.

    Past `max_chars` the rest of that physical line is discarded and a single
    `None` is yielded in its place, so the caller can drop the frame it belongs
    to whole rather than parse it from the part that fitted.

    Lines split on LF with a trailing CR stripped, so LF and CRLF streams both
    read correctly. A lone-CR terminator — legal in SSE, unused in practice —
    reads as one long line instead, which the cap drops rather than misparses.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    dropping = False
    async for chunk in response.aiter_bytes():
        text = decoder.decode(chunk)
        while text:
            head, newline, text = text.partition("\n")
            if not newline:
                # No terminator in what is left: hold it for the next chunk,
                # unless holding it is what the cap exists to prevent.
                pending += head
                if len(pending) > max_chars:
                    pending = ""
                    dropping = True
                break
            line, pending = pending + head, ""
            if dropping or len(line) > max_chars:
                # The length test here is this reader's own contract — it never
                # yields a line longer than its cap — not a second memory
                # bound. A line that terminated inside a chunk was already
                # paid for, and the caller's frame budget would refuse it
                # anyway. The check that saves memory is the one above, on
                # `pending`, which runs while the line is still arriving.
                dropping = False
                yield None
                continue
            yield line.removesuffix("\r")


def parse_lithos_sse_frame(lines: list[str]) -> LensEvent | None:
    event_id = ""
    event_type = "message"
    data_lines: list[str] = []
    for line in lines:
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            event_id = value
        elif field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)

    if event_type not in CONSUMED_EVENT_TYPES:
        return None
    try:
        payload = json.loads("\n".join(data_lines)) if data_lines else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    event = normalize_lithos_event(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
    )
    return event


def normalize_lithos_event(
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> LensEvent | None:
    """Normalize one upstream event, scope-aware.

    Task-scoped types keep the drop-if-no-`task_id` rule; system-scoped types
    carry no task at all and pass through with an empty `task_id` and no
    refresh. A future `task_edge.upserted` maps to `to_task_id` here (the
    knowledge `edge.upserted` is deliberately not consumed).
    """
    system_scoped = event_type in SYSTEM_EVENT_TYPES
    task_id = "" if system_scoped else str(payload.get("task_id") or "")
    if not system_scoped and not task_id:
        DROPPED_EVENTS.record(event_type=event_type, event_id=event_id)
        return None
    # The synthesized fallback is a dedupe key made of payload data (a
    # caller-chosen agent id, say) — never a replay position, and never trusted
    # raw on the wire. `wire_safe` guards the wire only: an id it had to rewrite
    # is not the position Lithos sent, so it is not carried as a cursor either.
    fallback = task_id or str(payload.get("agent_id") or "")
    return LensEvent(
        id=wire_safe(event_id) or wire_safe(f"{event_type}:{fallback}"),
        type=event_type,
        task_id=task_id,
        payload=payload,
        requires_refresh=not system_scoped,
        upstream_id=event_id if is_replay_cursor(event_id) else "",
    )


def _now() -> float:
    """The running loop's monotonic clock (never wall time — it can step)."""

    return asyncio.get_running_loop().time()


def _events_url(lithos: LithosConfig) -> str:
    path = lithos.sse_events_path.strip("/")
    base = f"{lithos.url.rstrip('/')}/{path}"
    params = urlencode({"types": ",".join(sorted(CONSUMED_EVENT_TYPES))})
    return f"{base}?{params}"
