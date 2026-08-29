"""Shared Lithos event subscription and browser fan-out."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from lithos_lens.config import EventsConfig, LithosConfig

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
# What a system-scoped event is allowed to carry to browsers, per type. The
# task-scoped payloads pass through whole because the client reads them, but
# nothing consumes an `agent.registered` payload — `tasks.js` registers no
# listener for it and `requires_refresh=False` drives no reconcile — so
# forwarding the frame verbatim to every subscriber of the unauthenticated
# `/tasks/events` would pay an open-ended exposure for data dropped on the
# floor. `lithos_agent_register` takes caller-supplied `metadata`, and any
# field Lithos adds to the event later would reach every connected browser
# with no change here. Projected to the two fields REQUIREMENTS.md §16.1.1
# documents, so the exposure is bounded by Lens's own contract.
SYSTEM_EVENT_PAYLOAD_FIELDS = {"agent.registered": ("agent_id", "name")}

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
# Dropped events carry an upstream-controlled id into the log sink: warn on the
# first drop, then once per this many, so the sink stays bounded.
DROPPED_EVENT_WARN_INTERVAL = 100


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


class DroppedEventLog:
    """Rate-limited WARNING emitter for dropped upstream events.

    One record per drop would hand a misbehaving upstream an unbounded write
    into the operator's log sink (the id it chooses is what gets written), so
    only the first drop and every `interval`-th one are logged, each carrying
    the running total.
    """

    def __init__(self, interval: int = DROPPED_EVENT_WARN_INTERVAL) -> None:
        self._interval = interval
        self._count = 0

    def record(self, *, event_type: str, event_id: str) -> None:
        self._count += 1
        if self._count != 1 and self._count % self._interval:
            return
        logger.warning(
            "dropping task-scoped lithos event without task_id",
            extra={
                "event_type": event_type,
                "event_id": wire_safe(event_id),
                "dropped_total": self._count,
            },
        )


DROPPED_EVENTS = DroppedEventLog()


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
        # True from the start, not False. `start()` only SCHEDULES `_run`, so
        # Lens serves dashboards while the first upstream handshake is still in
        # flight — a browser can render its snapshot, attach, and report `live`
        # (the polling fallback arms off the browser-to-Lens stream, not off
        # this one) before Lens is receiving anything. A change in that window
        # reaches neither the snapshot nor the un-cursored stream that opens
        # after it, so the first open owes a refresh exactly like any other.
        # Only "nobody was attached yet" makes an interval free of stale tabs,
        # and that is the subscriber question `_publish_refresh` asks.
        self._gap_since_last_open = True
        self._reconnects = 0
        self._attach_refreshes = 0
        self._open_generation = 0
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

    def snapshot_marker(self) -> int:
        """Identifies the upstream stream a server-rendered snapshot was taken under.

        Rendered into the page and handed back on `subscribe`, so the two ends
        of a page load — the snapshot, and the SSE attach that happens only
        after the response carrying the script has completed — can be compared.

        Zero whenever no stream is open, which is the honest answer: a snapshot
        taken while Lens is receiving nothing is not covered by any stream.
        """

        return self._open_generation if self._stream_open else 0

    def subscribe(
        self, *, maxsize: int = 100, since: int | None = None
    ) -> asyncio.Queue[LensEvent]:
        """Attach a browser subscriber, refreshing it if its snapshot predates
        the current stream.

        The gap bookkeeping in `_on_stream_open` reaches subscribers that were
        ALREADY attached. It cannot reach the one that matters most at startup:
        a `/tasks` render that read its snapshot while the first handshake was
        still in flight cannot subscribe until its own response has completed,
        so it attaches AFTER the open that its snapshot missed — and finds the
        gap already discharged (to nobody, if it was the only page loading).

        `since` closes that by comparing markers rather than guessing at
        timings: a snapshot taken under the stream that is still open needs
        nothing, and any other combination — no stream then, a different stream
        now — means an open happened between the two and whatever changed
        across it reached neither.

        `None` means the caller rendered no snapshot (the tests, and any client
        that constructed the URL itself); there is nothing to be stale, so
        nothing is seeded.
        """
        queue: asyncio.Queue[LensEvent] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        if since is not None and since != self.snapshot_marker():
            # Deliberately outside the flap rate limit, which bounds a fan-out
            # to EVERY tab driven by an upstream Lens does not control. This is
            # one event into one brand-new queue, bounded by page loads.
            self._attach_refreshes += 1
            queue.put_nowait(
                LensEvent(
                    id=f"{LENS_REFRESH_EVENT}:attach:{self._attach_refreshes}",
                    type=LENS_REFRESH_EVENT,
                    task_id="",
                    payload={"reason": "snapshot-predates-stream"},
                )
            )
        return queue

    def unsubscribe(self, queue: asyncio.Queue[LensEvent]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event: LensEvent, *, guaranteed: bool = False) -> None:
        """Fan `event` out to every browser subscriber.

        `guaranteed` is for the synthetic reconnect refresh, and ONLY for it.
        An ordinary event is best-effort: a subscriber whose queue is full is
        one the browser is not draining, and the refresh that follows any gap
        repairs whatever it missed. The refresh itself has no such backstop —
        it IS the backstop — so dropping it on a momentarily saturated queue
        (a throttled background tab, a slow link) leaves exactly the stale tab
        it exists to repair, with nothing left to signal it.
        """
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
                continue
            except asyncio.QueueFull:
                pass
            if not guaranteed:
                logger.warning(
                    "lens event subscriber queue full",
                    extra={"event_type": event.type, "event_id": event.id},
                )
                continue
            # Displace the OLDEST queued event to make room. Sound because a
            # refresh subsumes it: the reconcile it triggers refetches the
            # whole board, so the discarded event's effect is re-derived from
            # the server rather than lost. `get_nowait`/`put_nowait` with no
            # await between them cannot be interleaved with the consumer, so
            # the freed slot is still free when it is filled.
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - defensive
                logger.warning(
                    "lens refresh could not be delivered",
                    extra={"event_type": event.type, "event_id": event.id},
                )

    async def _on_stream_open(self) -> None:
        """Called once the upstream stream is established.

        An open that FOLLOWS A GAP results in one synthetic `lens.refresh`
        reaching browsers. `Last-Event-ID` replays Lithos's ring buffer, but a
        gap wider than that buffer cannot be replayed, so a full browser
        refresh is the only correctness guarantee.

        The condition is the gap, not a prior success — and the interval
        BEFORE the first open is a gap too (see `__post_init__`). Lens is up
        and serving while that first handshake is in flight, so browsers
        attach and their EventSource reports `live` (the polling fallback is
        driven by the browser-to-Lens stream, not by Lens's upstream, so it
        never arms), and every Lithos change in that window is invisible to
        them. Keying on "have we ever connected" would ask about the hub's
        history when staleness is a property of the interval.

        Refreshing on the first open costs nothing when nothing is attached:
        `_publish_refresh` broadcasts only to subscribers that exist.
        """
        self._stream_open = True
        self._open_generation += 1
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
        # A broadcast to nobody is not a broadcast: it must not consume the
        # cooldown window (or a sequence number) that a real recipient is owed
        # moments later. This is what makes refreshing on the FIRST open free —
        # at process start there are no dashboards to refetch.
        #
        # Skipping is only SOUND because a tab that attaches after this open
        # is caught by the snapshot marker (see `subscribe`). "Nobody attached
        # at the instant of the open" is not by itself proof that no stale tab
        # is being created: a `/tasks` response can already be in flight.
        if not self._subscribers:
            return
        self._last_refresh_at = _now()
        self._reconnects += 1
        await self.publish(
            LensEvent(
                id=f"{LENS_REFRESH_EVENT}:{self._reconnects}",
                type=LENS_REFRESH_EVENT,
                task_id="",
                payload={"reason": "reconnect"},
            ),
            guaranteed=True,
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
        async for line in response.aiter_lines():
            if line == "":
                if frame:
                    event = parse_lithos_sse_frame(frame)
                    if event is not None:
                        yield event
                    frame = []
                continue
            if line.startswith(":"):
                continue
            frame.append(line)


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
        payload = (
            json.loads("\n".join(data_lines), parse_constant=_reject_constant)
            if data_lines
            else {}
        )
    except ValueError:
        # JSONDecodeError and the `_reject_constant` refusal alike: the frame
        # is unusable, so it is treated as carrying nothing (a task-scoped one
        # then has no `task_id` and is dropped by the normalizer).
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    event = normalize_lithos_event(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
    )
    return event


def _reject_constant(token: str) -> Any:
    """Refuse `NaN`/`Infinity`/`-Infinity`, which Python accepts and JSON does not.

    `json.loads` takes those bare tokens by default and `json.dumps` re-emits
    them, so a non-finite float anywhere in an upstream payload would reach the
    browser inside `as_sse`'s `data:` line — where `JSON.parse` throws. That is
    worse than losing the one event: `tasks.js` commits the dedupe key BEFORE
    parsing, so a Lithos replay of the same frame is discarded at the guard
    rather than retried, and the event's reconcile never runs on a stream that
    looks perfectly healthy. Refusing here keeps the value out of a `LensEvent`
    entirely. `allow_nan=False` at the `as_sse` sink would be too late — it
    raises inside the SSE generator and tears that browser's stream down.
    """

    raise ValueError(f"non-finite JSON constant in lithos event payload: {token}")


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
    forwarded = (
        {
            field: payload[field]
            for field in SYSTEM_EVENT_PAYLOAD_FIELDS.get(event_type, ())
            if field in payload
        }
        if system_scoped
        else payload
    )
    return LensEvent(
        id=wire_safe(event_id) or wire_safe(f"{event_type}:{fallback}"),
        type=event_type,
        task_id=task_id,
        payload=forwarded,
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
