"""Shared Lithos event subscription and browser fan-out."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from lithos_lens import metrics
from lithos_lens.config import EventsConfig, LithosConfig
from lithos_lens.errors import EventSubscriberLimit, UnsupportedEventEncoding

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

# The bounded label set for `lens_events_published_total`. `publish` is a
# PUBLIC method and normalization is not on all of its paths — fake-Lithos app
# mode's `POST /tasks/events/publish` builds a LensEvent straight from request
# JSON — so the type is mapped here rather than trusted. Fake mode can be run
# with an OTLP endpoint configured, which would otherwise let an arbitrary
# request mint an arbitrary Prometheus series.
METRIC_EVENT_TYPES = CONSUMED_EVENT_TYPES | {LENS_REFRESH_EVENT}
UNKNOWN_EVENT_TYPE_LABEL = "other"


def metric_event_type(event_type: str) -> str:
    """``event_type`` if it is one Lens recognizes, else ``other``."""
    return event_type if event_type in METRIC_EVENT_TYPES else UNKNOWN_EVENT_TYPE_LABEL


# Shortest gap between two synthetic reconnect refreshes. Each one costs every
# open dashboard a full refetch, so a flapping upstream must not be able to use
# them as a fan-out amplifier against Lens and Lithos. A refresh that lands
# inside the window is deferred to its end, never dropped: each disconnected
# interval is a distinct gap that only a refresh can close.
LENS_REFRESH_MIN_INTERVAL_S = 5.0
# Ids are upstream-controlled and end up in an SSE frame, a browser dedupe key
# and an outbound request header; cap them well above any real ULID.
EVENT_ID_MAX_LENGTH = 200
# The only content coding Lens will read: see `_require_identity_encoding`.
IDENTITY_ENCODING = "identity"
# SSE terminates a line with CR, LF or CRLF — all three, and nothing else. Not
# `str.splitlines`, which also breaks on FF, VT and the Unicode separators and
# would split lines the grammar does not.
_SSE_TERMINATOR = re.compile(r"[\r\n]")
# Shortest gap between two records of the SAME condition. Upstream drives both
# how often these conditions fire and the text they carry into the log sink, so
# the ceiling has to be one Lens sets in TIME: a "every Nth occurrence" sampler
# still scales its output with the input rate, which is the term that is not
# Lens's to choose. See :class:`RateLimitedWarning`.
WARN_MIN_INTERVAL_S = 60.0
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
    sink.

    Bounded in TIME, not by counting. Sampling every Nth occurrence divides the
    output by N but leaves it proportional to the input, so the rate of writes
    is still set by whoever is driving the condition; a time gate caps it at
    one record per `min_interval_s` no matter what arrives. The first
    occurrence always lands (that is the record worth having), and each
    subsequent one reports how many were suppressed since the last, so the
    volume is still legible from the log.

    One instance per condition rather than one shared gate: a condition that
    fires constantly would otherwise swallow the FIRST occurrence of a rare
    one. Every field value goes through `wire_safe` on the way, because the log
    is one more sink upstream text reaches.

    `clock` is monotonic and injectable — wall time can step, and a test that
    has to wait a minute to observe a bound is a test nobody runs.
    """

    def __init__(
        self,
        message: str,
        *,
        min_interval_s: float = WARN_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._message = message
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._count = 0
        self._suppressed = 0
        self._last_record_at = float("-inf")

    def record(self, **fields: str) -> None:
        self._count += 1
        now = self._clock()
        if now - self._last_record_at < self._min_interval_s:
            self._suppressed += 1
            return
        suppressed, self._suppressed = self._suppressed, 0
        self._last_record_at = now
        logger.warning(
            self._message,
            extra={
                **{name: wire_safe(value) for name, value in fields.items()},
                "occurrences": self._count,
                "suppressed_since_last": suppressed,
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
#: The upstream answered a request for `identity` with a content-encoded body.
#: A standing misconfiguration, retried at the reconnect backoff — so it needs
#: the gate more than the bursty conditions do, not less.
REFUSED_ENCODINGS = RateLimitedWarning("refusing a content-encoded lithos event stream")


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
        # Registered before the first connect attempt, and read at collection
        # rather than written at each transition: a gauge written only on
        # transitions stops being exported once the status settles, and an
        # absent series reads as "not deployed" during the outage it would be
        # consulted for.
        metrics.register_event_stream_up(lambda: 1.0 if self.status == "live" else 0.0)
        metrics.register_event_subscribers(lambda: float(len(self._subscribers)))

    async def start(self) -> None:
        if not self.config.enabled:
            self._set_status("disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._set_status("reconnecting")
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
        self._set_status("disabled")
        for queue in list(self._subscribers):
            self._subscribers.discard(queue)

    def _set_status(self, status: EventStatus) -> None:
        """Move the hub's status through one place.

        `lens_event_stream_up` reads `self.status` at collection time, so the
        gauge can no longer disagree with the field the way it could when each
        of the five assignment sites had to remember to publish it. This stays
        a single funnel anyway: the status is what `/health` reports, and one
        place to change it is one place to read when it is wrong.
        """
        self.status = status

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
            metrics.events_dropped().add(1, {"reason": "subscriber_limit"})
            raise EventSubscriberLimit(
                f"event subscriber limit reached ({MAX_EVENT_SUBSCRIBERS})"
            )
        queue: asyncio.Queue[LensEvent] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[LensEvent]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event: LensEvent) -> None:
        metrics.events_published().add(1, {"type": metric_event_type(event.type)})
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Rate-limited: a subscriber that stops draining turns every
                # upstream event into a record, at an upstream-chosen rate
                # times a client-chosen number of stalled tabs.
                #
                # The counter alongside is not redundant. Rate limiting is
                # right for the log and it costs the RATE: `occurrences` is a
                # running total, not something to graph or alert on. A counter
                # is cheap per occurrence, so the log keeps the readable detail
                # and this carries how often it is happening.
                UNDELIVERED_EVENTS.record(event_type=event.type, event_id=event.id)
                metrics.events_dropped().add(1, {"reason": "subscriber_queue_full"})
            else:
                metrics.events_delivered().add(1)

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
        self._set_status("live")
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
            self._set_status("reconnecting")
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
    headers = {"Accept-Encoding": IDENTITY_ENCODING}
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream("GET", endpoint, headers=headers) as response,
    ):
        response.raise_for_status()
        _require_identity_encoding(response)
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
                    metrics.events_dropped().add(1, {"reason": "oversized_frame"})
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


def _require_identity_encoding(response: httpx.Response) -> None:
    """Refuse a content-encoded stream, because Lens cannot bound one.

    The reader takes `aiter_raw`, so every chunk is exactly what one socket
    read delivered and the pending-line cap sees a line WHILE it is still
    arriving. Content encoding defeats that at a layer below the cap: httpx
    decodes a chunk whole before yielding it, and gzip of a repetitive line
    runs about 1000:1 — one 64 KiB socket read becomes 64 MiB resident before
    anything here could look at it. The size term would be back in the hands of
    whoever chose the compressor, which is the exposure this module is closing.

    So Lens asks for `identity` and reads raw. A conformant server honours
    that; one that does not — or a proxy compressing an event stream, which
    also breaks its streaming semantics — gets refused rather than read
    unbounded. Loud rather than silent: the reconnect loop retries, the status
    chip reads `reconnecting`, and the log names the encoding, because the fix
    is a configuration change on the other end and nothing Lens can do about it
    at read time is honest.
    """
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if not encoding or encoding == IDENTITY_ENCODING:
        return
    REFUSED_ENCODINGS.record(content_encoding=encoding)
    metrics.events_dropped().add(1, {"reason": "content_encoding_refused"})
    raise UnsupportedEventEncoding(
        f"lithos event stream is {encoding}-encoded; Lens requests identity "
        "so that one socket read stays one bounded chunk"
    )


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

    `aiter_raw` rather than `aiter_bytes`: a chunk here is exactly one socket
    read, with no content decoding between the wire and the cap. Today that is
    belt-and-braces — `_require_identity_encoding` has already refused anything
    httpx would decode, so no test can tell the two apart, and none pretends to
    — but it makes "this reader never sees an inflated byte" a property of the
    call rather than of a check somebody could later relax.

    Terminators are CR, LF and CRLF, all three, per the SSE grammar — the same
    set httpx's reader normalized. A CR that lands at the end of a chunk is not
    resolved until the next one arrives, since only that tells it apart from
    the first half of a CRLF.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    dropping = False
    held_cr = False
    async for chunk in response.aiter_raw():
        text = decoder.decode(chunk)
        if not text:
            # A chunk that split a multi-byte character. Nothing is resolved by
            # it — including a held CR, whose LF may still be coming.
            continue
        if held_cr:
            held_cr = False
            if text.startswith("\n"):
                text = text[1:]  # the LF half of a CRLF the chunk boundary cut
                if not text:
                    continue
        while text:
            terminator = _SSE_TERMINATOR.search(text)
            if terminator is None:
                # No terminator in what is left: hold it for the next chunk,
                # unless holding it is what the cap exists to prevent.
                pending += text
                if len(pending) > max_chars:
                    pending = ""
                    dropping = True
                break
            line, text = pending + text[: terminator.start()], text[terminator.end() :]
            pending = ""
            if terminator.group() == "\r":
                if text.startswith("\n"):
                    text = text[1:]
                elif not text:
                    held_cr = True
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
            yield line


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
        metrics.events_dropped().add(1, {"reason": "no_task_id"})
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
