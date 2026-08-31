"""Task event normalization, scope rules, and reconnect replay tests."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import tracemalloc
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from lithos_lens import events as events_module
from lithos_lens.config import EventsConfig, LithosConfig
from lithos_lens.errors import EventSubscriberLimit
from lithos_lens.events import (
    CONSUMED_EVENT_TYPES,
    EVENT_ID_MAX_LENGTH,
    LENS_REFRESH_EVENT,
    EventHub,
    LensEvent,
    RateLimitedWarning,
    is_replay_cursor,
    parse_lithos_sse_frame,
)
from tests.conftest import metric_value


class _FakeClock:
    """A monotonic clock a test advances by hand.

    The warning gate is bounded in TIME, so observing it needs control of time:
    a burst has to land inside one window, and the record after it has to land
    outside one, and neither should cost the suite a real minute.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cursor(headers: dict[str, str] | None) -> str | None:
    """The replay cursor on an outbound request, if it carried one."""

    return None if headers is None else headers.get("Last-Event-ID")


@pytest.fixture
def fresh_drop_log(monkeypatch: pytest.MonkeyPatch) -> RateLimitedWarning:
    """Give a test its own drop-log counter (the module instance is process-wide)."""

    drop_log = RateLimitedWarning("dropping task-scoped lithos event without task_id")
    monkeypatch.setattr(events_module, "DROPPED_EVENTS", drop_log)
    return drop_log


def test_lithos_task_event_is_normalized_with_original_event_id() -> None:
    event = parse_lithos_sse_frame(
        [
            "id: evt-1",
            "event: task.claimed",
            'data: {"task_id":"task-1","agent":"agent-a","aspect":"docs"}',
        ]
    )

    assert event is not None
    assert event.id == "evt-1"
    assert event.type == "task.claimed"
    assert event.task_id == "task-1"
    assert event.payload["agent"] == "agent-a"
    assert event.requires_refresh is True


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        ("task.updated", '{"task_id":"task-1"}'),
        (
            "task.reopened",
            '{"task_id":"task-1","agent":"agent-a","prior_status":"completed"}',
        ),
    ],
)
def test_lifecycle_events_are_consumed_and_force_a_refresh(
    event_type: str, data: str
) -> None:
    # Both payloads are too sparse to patch the board from (task.updated
    # carries nothing but the id), so both must force the reconcile.
    event = parse_lithos_sse_frame(
        ["id: evt-2", f"event: {event_type}", f"data: {data}"]
    )

    assert event is not None
    assert event.type == event_type
    assert event.task_id == "task-1"
    assert event.requires_refresh is True


def test_system_event_passes_through_unscoped_without_a_refresh() -> None:
    # agent.registered has no task scope at all: it invalidates the agent
    # dropdown data only and must never move the board.
    event = parse_lithos_sse_frame(
        [
            "id: evt-3",
            "event: agent.registered",
            'data: {"agent_id":"agent-a","name":"Agent A"}',
        ]
    )

    assert event is not None
    assert event.type == "agent.registered"
    assert event.task_id == ""
    assert event.requires_refresh is False
    assert event.payload["agent_id"] == "agent-a"


def test_task_scoped_event_without_task_id_is_dropped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
    fresh_drop_log: RateLimitedWarning,
) -> None:
    with caplog.at_level(logging.WARNING, logger=events_module.__name__):
        event = parse_lithos_sse_frame(
            [
                "id: evt-4",
                "event: task.reopened",
                'data: {"agent":"agent-a"}',
            ]
        )

    assert event is None
    assert "task_id" in caplog.text


def test_dropped_event_warnings_are_rate_limited(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The malformed-event rate is upstream's; the log-write rate must not be.

    Bounded in time rather than by sampling every Nth drop: a 1-in-N sampler
    divides the output but leaves it proportional to the input, so a faster
    upstream still buys a faster log. Whatever arrives inside one window, the
    window costs one record.
    """
    clock = _FakeClock()
    monkeypatch.setattr(
        events_module,
        "DROPPED_EVENTS",
        RateLimitedWarning("dropping task-scoped lithos event", clock=clock),
    )

    def drop(index: int) -> None:
        assert (
            parse_lithos_sse_frame(
                [f"id: evt-drop-{index}", "event: task.updated", "data: {}"]
            )
            is None
        )

    with caplog.at_level(logging.WARNING, logger=events_module.__name__):
        for index in range(25):  # a burst, entirely inside one window
            drop(index)
        assert len(caplog.records) == 1, "a burst costs its window one record"

        clock.advance(events_module.WARN_MIN_INTERVAL_S + 1)
        drop(25)
        # Ten times the burst, still one record per window: the output does not
        # follow the input rate.
        for index in range(26, 276):
            drop(index)

    assert [record.__dict__["occurrences"] for record in caplog.records] == [1, 26]
    # Nothing is lost from the record — the suppressed count carries the volume
    # the second line stands for.
    suppressed = [record.__dict__["suppressed_since_last"] for record in caplog.records]
    assert suppressed == [0, 24]


def test_payload_derived_id_cannot_inject_a_second_sse_frame() -> None:
    # The agent registration id is caller-chosen upstream, and a frame carries
    # no `id:` of its own, so the payload string becomes the minted event id.
    injected = 'x\nevent: task.completed\ndata: {"task_id":"task-1"}\n\nid: y'
    event = parse_lithos_sse_frame(
        [
            "event: agent.registered",
            "data: " + json.dumps({"agent_id": injected, "name": "Agent A"}),
        ]
    )

    assert event is not None
    assert "\n" not in event.id
    frame = event.as_sse()
    # One frame, and exactly one `event:` line — the injected text survives
    # only as inert junk inside the single id line.
    assert frame.count("\n\n") == 1
    assert frame.endswith("\n\n")
    assert [line for line in frame.splitlines() if line.startswith("event: ")] == [
        "event: agent.registered"
    ]


def test_as_sse_never_emits_more_than_one_frame() -> None:
    # Defence at the sink: whatever built the event, the wire stays one frame.
    event = LensEvent(
        id="a\r\nevent: task.completed\ndata: {}\n\nid: b",
        type="task.updated\nevent: lens.refresh",
        task_id="task-1",
    )

    frame = event.as_sse()

    assert frame.count("\n\n") == 1
    assert frame.splitlines()[:2] == [
        "id: aevent: task.completeddata: {}id: b",
        "event: task.updatedevent: lens.refresh",
    ]


@pytest.mark.parametrize(
    ("value", "usable"),
    [
        ("01JC2N8Q7R", True),
        ("evt 42", True),  # interior spaces are legal in a header value
        ("", False),
        ("café", False),  # httpx cannot even encode it
        ("a\x00b", False),  # h11: illegal header value
        (" lead", False),  # h11 rejects leading/trailing whitespace
        ("trail ", False),
        ("   ", False),
        ("x" * (EVENT_ID_MAX_LENGTH + 1), False),
    ],
)
def test_is_replay_cursor_matches_what_the_wire_accepts(
    value: str, usable: bool
) -> None:
    assert is_replay_cursor(value) is usable


@pytest.mark.parametrize("event_type", ["note.created", "edge.upserted"])
def test_non_task_events_are_ignored(event_type: str) -> None:
    # edge.upserted is the KNOWLEDGE graph event (note uuids in the payload) —
    # there is no upstream task-edge event, so it must not reach task surfaces.
    event = parse_lithos_sse_frame(
        [
            "id: evt-5",
            f"event: {event_type}",
            'data: {"id":"note-1","from_id":"note-1","to_id":"note-2"}',
        ]
    )

    assert event is None


@pytest.mark.anyio
async def test_event_hub_fans_out_to_browser_subscribers() -> None:
    hub = EventHub(EventsConfig(enabled=False), LithosConfig())
    first = hub.subscribe()
    second = hub.subscribe()
    event = LensEvent(
        id="evt-6",
        type="finding.posted",
        task_id="task-1",
        payload={"finding_id": "finding-1"},
    )

    await hub.publish(event)

    assert await asyncio.wait_for(first.get(), timeout=0.1) == event
    assert await asyncio.wait_for(second.get(), timeout=0.1) == event
    hub.unsubscribe(first)
    hub.unsubscribe(second)


@pytest.mark.anyio
async def test_a_stalled_subscriber_cannot_flood_the_log_with_dropped_events(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subscriber that stops draining must cost a bounded number of records.

    Nothing about this rate is Lens's: a slept laptop, a throttled background
    tab or a dropped NAT mapping fills its queue and never empties it, and from
    then on EVERY upstream event is undeliverable to it — at whatever rate
    Lithos emits, times however many tabs are in that state. One record per
    drop turns that into an unbounded write into the operator's log sink.
    """
    clock = _FakeClock()
    monkeypatch.setattr(
        events_module,
        "UNDELIVERED_EVENTS",
        RateLimitedWarning("lens event subscriber queue full", clock=clock),
    )
    hub = EventHub(EventsConfig(enabled=False), LithosConfig())
    stalled = hub.subscribe(maxsize=1)

    async def publish(index: int) -> None:
        await hub.publish(
            LensEvent(id=f"evt-{index}", type="task.updated", task_id="task-1")
        )

    with caplog.at_level(logging.WARNING, logger=events_module.__name__):
        for index in range(25):  # one slot filled, then 24 undeliverable
            await publish(index)
        assert len(caplog.records) == 1

        clock.advance(events_module.WARN_MIN_INTERVAL_S + 1)
        for index in range(25, 125):
            await publish(index)

    hub.unsubscribe(stalled)
    # Two windows, two records, whatever the upstream rate was in between.
    assert [record.__dict__["occurrences"] for record in caplog.records] == [1, 25]
    assert [record.__dict__["suppressed_since_last"] for record in caplog.records] == [
        0,
        23,
    ]


@pytest.mark.anyio
async def test_the_hub_refuses_subscribers_past_its_ceiling() -> None:
    """Each subscriber pins a queue and makes every publish O(subscribers).

    Lens takes unauthenticated requests across a trusted-network boundary, so
    the number of open streams is whoever-can-reach-the-port's to choose. The
    ceiling counts LIVE subscribers rather than a high-water mark, because the
    normal case for hitting it is churn — a tab that reloads leaves its old
    subscriber behind until a write discovers the peer is gone.
    """
    hub = EventHub(EventsConfig(enabled=False), LithosConfig())
    queues = [hub.subscribe() for _ in range(events_module.MAX_EVENT_SUBSCRIBERS)]

    with pytest.raises(EventSubscriberLimit):
        hub.subscribe()

    hub.unsubscribe(queues[0])
    replacement = hub.subscribe()  # the freed slot is usable again
    hub.unsubscribe(replacement)
    for queue in queues[1:]:
        hub.unsubscribe(queue)


def test_refusing_subscribers_does_not_hand_back_an_unbounded_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is worth recording, but not once per attempt.

    Whoever is opening connections fast enough to sit at the ceiling is
    precisely who would choose how often this fires — so a warning per refused
    request would give back, at the refusal, the same unbounded log write the
    ceiling exists to prevent.
    """
    clock = _FakeClock()
    monkeypatch.setattr(events_module, "MAX_EVENT_SUBSCRIBERS", 0)
    monkeypatch.setattr(
        events_module,
        "REFUSED_SUBSCRIBERS",
        RateLimitedWarning("refusing an event-stream subscriber", clock=clock),
    )
    hub = EventHub(EventsConfig(enabled=False), LithosConfig())

    def refused() -> None:
        with pytest.raises(EventSubscriberLimit):
            hub.subscribe()

    with caplog.at_level(logging.WARNING, logger=events_module.__name__):
        for _ in range(25):  # hammering the ceiling inside one window
            refused()
        assert len(caplog.records) == 1

        clock.advance(events_module.WARN_MIN_INTERVAL_S + 1)
        refused()

    assert [record.__dict__["occurrences"] for record in caplog.records] == [1, 26]
    assert [record.__dict__["suppressed_since_last"] for record in caplog.records] == [
        0,
        24,
    ]


def _as_chunks(entry: Iterable[str | bytes]) -> Iterator[bytes]:
    """Prepared stream content as byte chunks.

    A ``str`` entry is one LF-terminated line — the shape almost every test
    wants. A ``bytes`` entry is passed through verbatim, which is how a test
    puts a line boundary somewhere other than where the reader expects it, or
    omits one entirely.

    Lazy, so a test measuring what the READER holds is not measuring its own
    prepared input alongside it.
    """

    for chunk in entry:
        yield chunk if isinstance(chunk, bytes) else f"{chunk}\n".encode()


class _FakeStreamResponse:
    """Stands in for the upstream ``/events`` response.

    Yields BYTES, as httpx does. The reader splits lines itself so that the
    pending one can be capped, and a fake handing over ready-made lines would
    leave that splitter — and the cap living inside it — untested. Chunk
    boundaries are the test's to choose, which is how a line arriving in
    pieces, or never terminating at all, gets exercised.
    """

    def __init__(
        self,
        chunks: Iterator[bytes],
        *,
        hold: bool,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._chunks = chunks
        self._hold = hold
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._hold:
            # Keep the connection open so the hub stays on this attempt until
            # the test stops it; otherwise it would reconnect in a tight loop.
            await asyncio.Event().wait()


class _FakeStreamContext:
    def __init__(
        self,
        chunks: Iterator[bytes],
        *,
        hold: bool,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._response = _FakeStreamResponse(chunks, hold=hold, headers=headers)

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _recording_httpx_client(
    requests: list[tuple[str, dict[str, str] | None]],
    connections: list[Iterable[str | bytes] | Exception],
    *,
    response_headers: dict[str, str] | None = None,
) -> type:
    """An ``httpx.AsyncClient`` stand-in for the upstream ``/events`` stream.

    Records every outbound GET and serves one prepared connection per attempt:
    a frame batch to replay — as lines (LF appended to each) or as raw byte
    chunks when the test is about how the bytes are cut — or an exception to
    raise instead of connecting (as httpx does for a header it refuses to
    send). Once the entries run out the connection is held open.
    """

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def stream(
            self, _method: str, url: str, headers: dict[str, str] | None = None
        ) -> _FakeStreamContext:
            attempt = len(requests)
            requests.append((url, headers))
            entry = connections[attempt] if attempt < len(connections) else None
            if isinstance(entry, Exception):
                raise entry
            return _FakeStreamContext(
                _as_chunks(entry or []),
                hold=entry is None,
                headers=response_headers,
            )

    return _Client


def _reopened_frame(event_id: str) -> list[str | bytes]:
    return [
        f"id: {event_id}",
        "event: task.reopened",
        'data: {"task_id":"task-1","agent":"agent-a","prior_status":"completed"}',
        "",
    ]


@pytest.mark.anyio
async def test_upstream_subscription_filters_the_consumed_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        _recording_httpx_client(requests, [_reopened_frame("evt-7")]),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    await hub.start()
    try:
        event = await asyncio.wait_for(subscriber.get(), timeout=2)
    finally:
        await hub.stop()

    # The nine consumed types are pushed upstream as the server-side filter;
    # edge.upserted is the knowledge-graph event and is never subscribed to.
    url, headers = requests[0]
    types = parse_qs(urlparse(url).query)["types"][0].split(",")
    assert set(types) == CONSUMED_EVENT_TYPES
    assert "agent.registered" in types
    assert "edge.upserted" not in types
    # Nothing to replay on a first connect.
    assert headers is not None
    # Nothing to replay on a first connect, and identity is asked for on every
    # connect (see test_the_stream_is_requested_unencoded).
    assert _cursor(headers) is None
    assert event.type == "task.reopened"
    assert event.task_id == "task-1"


@pytest.mark.anyio
async def test_reconnect_replays_from_last_event_id_and_broadcasts_a_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        _recording_httpx_client(requests, [_reopened_frame("evt-8")]),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    await hub.start()
    try:
        # First connection: one real event, then the stream ends and the hub
        # dials again.
        first = await asyncio.wait_for(subscriber.get(), timeout=2)
        refresh = await asyncio.wait_for(subscriber.get(), timeout=2)
    finally:
        await hub.stop()

    assert first.id == "evt-8"
    assert hub.last_event_id == "evt-8"
    assert len(requests) == 2
    assert _cursor(requests[1][1]) == "evt-8"
    # Replay is bounded by Lithos's ring buffer, so the reconnect also carries
    # a synthetic full-refresh backstop — unscoped, and never on first connect.
    assert refresh.type == "lens.refresh"
    assert refresh.task_id == ""
    assert refresh.requires_refresh is True
    assert subscriber.empty()


@pytest.mark.parametrize(
    ("frame", "description"),
    [
        (
            [
                "event: agent.registered",
                'data: {"agent_id":"worker-a","name":"Agent A"}',
            ],
            "id synthesized from the payload, never an upstream cursor",
        ),
        (
            [
                "id: evt-café",
                "event: task.updated",
                'data: {"task_id":"task-1"}',
            ],
            "non-ASCII id: httpx/h11 refuse it as a header",
        ),
        (
            [
                "id: evt-\x0042",
                "event: task.updated",
                'data: {"task_id":"task-1"}',
            ],
            "control character: sanitizing it would invent a position upstream"
            " never issued",
        ),
        (
            [
                "id: " + "x" * (EVENT_ID_MAX_LENGTH + 50),
                "event: task.updated",
                'data: {"task_id":"task-1"}',
            ],
            "over-long id: truncating it would invent a position too",
        ),
    ],
)
@pytest.mark.anyio
async def test_ids_that_are_not_usable_cursors_are_never_replayed(
    monkeypatch: pytest.MonkeyPatch, frame: list[str], description: str
) -> None:
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        _recording_httpx_client(requests, [[*frame, ""]]),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    await hub.start()
    try:
        event = await asyncio.wait_for(subscriber.get(), timeout=2)
        refresh = await asyncio.wait_for(subscriber.get(), timeout=2)
    finally:
        await hub.stop()

    # The event still reaches browsers; it just never becomes a replay cursor,
    # which would wedge every later reconnect on a value the wire rejects.
    assert event.type in CONSUMED_EVENT_TYPES
    assert refresh.type == LENS_REFRESH_EVENT
    assert hub.last_event_id == ""
    assert len(requests) == 2
    assert _cursor(requests[1][1]) is None, description


@pytest.mark.anyio
async def test_a_first_connect_that_had_to_retry_still_delivers_the_backstop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop is owed to a GAP, not to a prior success.

    The first connect is refresh-free because nothing was missed: the page a
    browser holds was server-rendered moments earlier. That reasoning stops
    applying the instant an attempt FAILS. Lens is up and serving, so browsers
    attach and their EventSource reports `live` — the polling fallback is
    driven by the browser-to-Lens stream, not by Lens's upstream, so it never
    arms. Every Lithos change during the retry window is invisible to those
    tabs, and without a backstop on the eventual open it stays invisible until
    something unrelated triggers a reconcile.

    Keying on "have we ever connected" asks the wrong question: that is a
    property of the hub's history, and staleness is a property of the interval.

    The successful attempt HOLDS the stream open (no frame batch to exhaust),
    so the refresh asserted here can only have come from that open — not from a
    later reconnect after a batch ended.
    """
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        # Upstream is down when Lens starts: the first attempt never opens a
        # stream, so `_on_stream_open` is not reached on it at all. The second
        # runs off the end of the list and holds open.
        _recording_httpx_client(requests, [httpx.ConnectError("upstream refused")]),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    await hub.start()
    try:
        received = await asyncio.wait_for(subscriber.get(), timeout=2)
        # and exactly one, not a burst
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(subscriber.get(), timeout=0.5)
    finally:
        await hub.stop()

    assert received.id == f"{LENS_REFRESH_EVENT}:1"
    assert len(requests) == 2


@pytest.mark.anyio
async def test_the_very_first_connect_still_sends_no_backstop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control, so the assertion above means something: with no
    failed attempt there is no gap, and a refresh on every process start would
    make every dashboard refetch for nothing.

    Same shape as the test above minus the failure — one attempt, held open."""
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx, "AsyncClient", _recording_httpx_client(requests, [])
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    await hub.start()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(subscriber.get(), timeout=0.5)
    finally:
        await hub.stop()

    assert len(requests) == 1


@pytest.mark.anyio
async def test_a_connect_failure_drops_the_replay_cursor_and_still_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        _recording_httpx_client(
            requests,
            [
                _reopened_frame("evt-20"),
                _reopened_frame("evt-21"),
                # However the cursor is refused (h11 raises this for a header
                # it will not put on the wire), retrying it forever would leave
                # live updates dark for the process lifetime.
                httpx.LocalProtocolError("Illegal header value"),
                _reopened_frame("evt-22"),
            ],
        ),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    await hub.start()
    try:
        received = [
            await asyncio.wait_for(subscriber.get(), timeout=2) for _ in range(5)
        ]
    finally:
        await hub.stop()

    assert _cursor(requests[1][1]) == "evt-20"
    assert _cursor(requests[2][1]) == "evt-21"
    # The third attempt died before the stream opened, so the cursor goes and
    # the fourth resumes from nothing — which is exactly why that reconnect
    # MUST still deliver a refresh, even though it lands inside the cooldown.
    assert _cursor(requests[3][1]) is None
    assert [event.id for event in received] == [
        "evt-20",
        f"{LENS_REFRESH_EVENT}:1",
        "evt-21",
        "evt-22",
        f"{LENS_REFRESH_EVENT}:2",
    ]


@pytest.mark.anyio
async def test_flapping_reconnect_refreshes_are_coalesced_but_never_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        _recording_httpx_client(
            requests,
            [_reopened_frame(f"evt-3{index}") for index in range(4)],
        ),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    await hub.start()
    try:
        received = [
            await asyncio.wait_for(subscriber.get(), timeout=2) for _ in range(6)
        ]
    finally:
        await hub.stop()

    # Four reconnects, two refreshes: the flap rate does not reach the tabs
    # (each refresh costs every one of them a full refetch), but neither is the
    # signal discarded — the reconnects inside the window coalesce into one
    # broadcast delivered on the trailing edge, after the last event.
    assert [event.id for event in received] == [
        "evt-30",
        f"{LENS_REFRESH_EVENT}:1",
        "evt-31",
        "evt-32",
        "evt-33",
        f"{LENS_REFRESH_EVENT}:2",
    ]
    assert subscriber.empty()


async def _events_from_stream(
    monkeypatch: pytest.MonkeyPatch, chunks: Iterable[str | bytes], count: int
) -> list[LensEvent]:
    """Run the hub over ONE prepared upstream connection and take `count` events."""

    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        _recording_httpx_client(requests, [chunks]),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    await hub.start()
    try:
        return [
            await asyncio.wait_for(subscriber.get(), timeout=2) for _ in range(count)
        ]
    finally:
        await hub.stop()


@pytest.mark.anyio
async def test_a_single_oversized_line_is_cut_and_its_frame_dropped(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    metric_reader: InMemoryMetricReader,
) -> None:
    """The exposure a frame-level cap alone cannot reach.

    httpx's own ``aiter_lines`` buffers until a newline arrives, so a `data:`
    line that never terminates is already materialized by the time anything
    downstream could measure it. Reading bytes and splitting here is what makes
    that buffer cappable at all — and the observable proof that the cut
    happened is this frame being refused rather than parsed, since every byte
    of it does arrive in the end.

    The frame goes whole. A truncated ``data:`` line is not a smaller event, it
    is a different one, so parsing what fitted would be inventing an upstream
    message. The next frame on the same connection still arrives: the reader
    resynchronizes on the blank line rather than wedging.
    """
    monkeypatch.setattr(events_module, "MAX_EVENT_FRAME_CHARS", 200)
    monkeypatch.setattr(
        events_module, "OVERSIZED_FRAMES", RateLimitedWarning("oversized frame")
    )
    # Fresh too, so a frame dropped for some OTHER reason cannot satisfy the
    # count below: the payload here is deliberately well-formed and carries a
    # real task_id, so the only thing between it and the board is the cap.
    monkeypatch.setattr(
        events_module, "DROPPED_EVENTS", RateLimitedWarning("dropped event")
    )
    chunks: list[str | bytes] = [
        "id: evt-too-long-a-line",
        "event: task.updated",
        b'data: {"task_id":"task-1","pad":"' + b"x" * 300,
        b"x" * 300,
        b'"}\n',
        "",
        *_reopened_frame("evt-after-the-cut"),
    ]

    with caplog.at_level(logging.WARNING, logger=events_module.__name__):
        received = await _events_from_stream(monkeypatch, chunks, 1)

    assert received[0].id == "evt-after-the-cut"
    # Exactly one frame refused — the trailing one is well inside the cap, so
    # the reader is delivering again rather than dropping everything after.
    assert [record.__dict__["occurrences"] for record in caplog.records] == [1]
    # And the counter agrees. The log line is rate-limited, so it carries a
    # running total rather than a rate; this is the graphable half.
    assert (
        metric_value(
            metric_reader, "lens_events_dropped_total", reason="oversized_frame"
        ).value
        == 1
    )


@pytest.mark.anyio
async def test_a_frame_whose_lines_together_exceed_the_cap_is_dropped_whole(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: every line fits, the frame does not.

    SSE lets one payload span any number of ``data:`` lines, so bounding the
    line alone leaves the frame unbounded — the accumulation has to be counted
    across the whole frame as well.
    """
    monkeypatch.setattr(events_module, "MAX_EVENT_FRAME_CHARS", 200)
    monkeypatch.setattr(
        events_module, "OVERSIZED_FRAMES", RateLimitedWarning("oversized frame")
    )
    monkeypatch.setattr(
        events_module, "DROPPED_EVENTS", RateLimitedWarning("dropped event")
    )
    # SSE joins `data:` lines with a newline and JSON allows one between
    # tokens, so this splits into a payload that genuinely PARSES, carrying a
    # real task_id. Junk spread across the lines would be dropped at
    # normalization instead, and the test would still pass with the cap gone.
    # Every line here is inside the cap; only their sum is not.
    chunks: list[str | bytes] = [
        "id: evt-too-many-lines",
        "event: task.updated",
        'data: {"task_id":"task-1",',
        f'data: "pad":"{"x" * 150}",',
        f'data: "pad2":"{"x" * 150}"}}',
        "",
        *_reopened_frame("evt-after-the-pile"),
    ]

    with caplog.at_level(logging.WARNING, logger=events_module.__name__):
        received = await _events_from_stream(monkeypatch, chunks, 1)

    assert received[0].id == "evt-after-the-pile"
    assert [record.__dict__["occurrences"] for record in caplog.records] == [1]


@pytest.mark.anyio
async def test_a_frame_inside_the_cap_is_untouched_by_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound that ate real events would be worse than the one it replaced.

    Dropping a frame costs the board an update, so the cap has to sit clear of
    anything Lithos actually sends. This is the floor under the two tests
    above: they must fail because the frame was oversized, not because the
    reader stopped delivering.
    """
    monkeypatch.setattr(events_module, "MAX_EVENT_FRAME_CHARS", 200)
    payload = json.dumps({"task_id": "task-1", "pad": "x" * 100})
    chunks: list[str | bytes] = [
        "id: evt-fits",
        "event: task.updated",
        f"data: {payload}",
        "",
    ]

    received = await _events_from_stream(monkeypatch, chunks, 1)

    assert received[0].id == "evt-fits"
    assert received[0].task_id == "task-1"


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Poll until `predicate` holds, or fail the test on the deadline."""

    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("terminator", "chunks"),
    [
        ("LF", [b'id: evt-t\nevent: task.updated\ndata: {"task_id":"task-1"}\n\n']),
        (
            "CRLF",
            [b'id: evt-t\r\nevent: task.updated\r\ndata: {"task_id":"task-1"}\r\n\r\n'],
        ),
        ("CR", [b'id: evt-t\revent: task.updated\rdata: {"task_id":"task-1"}\r\r']),
        (
            "CRLF cut in half by every chunk boundary",
            [
                b"id: evt-t\r",
                b"\nevent: task.updated\r",
                b'\ndata: {"task_id":"task-1"}\r',
                b"\n\r\n",
            ],
        ),
    ],
)
async def test_every_sse_line_terminator_is_read(
    monkeypatch: pytest.MonkeyPatch, terminator: str, chunks: list[str | bytes]
) -> None:
    """SSE terminates a line with CR, LF or CRLF, and Lens reads all three.

    Splitting bytes here rather than taking httpx's lines moved this
    responsibility onto Lens, and it is easy to move it halfway: a splitter
    that handles only LF leaves a CR-only stream connected and silent, with
    every event of it accumulating into one line that the cap then drops. So
    each terminator is a case here, plus the one the chunk boundary can cut in
    half — a CR at the end of a chunk is not a terminator until the next chunk
    proves it is not the first half of a CRLF.
    """
    received = await _events_from_stream(monkeypatch, chunks, 1)

    assert received[0].id == "evt-t", f"{terminator} terminator was not read"
    assert received[0].task_id == "task-1"


@pytest.mark.anyio
async def test_the_stream_is_requested_unencoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx advertises gzip and deflate by default; Lens must not.

    A content coding is decoded a chunk at a time BELOW the pending-line cap,
    and gzip of a repetitive line runs about 1000:1 — so accepting one hands
    the size term back to whoever chose the compressor.
    """
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        _recording_httpx_client(requests, [_reopened_frame("evt-plain")]),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    await hub.start()
    try:
        await asyncio.wait_for(subscriber.get(), timeout=2)
        await _wait_for(lambda: len(requests) >= 2)
    finally:
        await hub.stop()

    # Every connect, not just the first: a reconnect that quietly dropped the
    # header would reopen the exposure on the next attempt.
    assert [headers["Accept-Encoding"] for _, headers in requests if headers] == [
        "identity",
        "identity",
    ]


@pytest.mark.anyio
async def test_a_content_encoded_stream_is_refused_rather_than_decoded(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    metric_reader: InMemoryMetricReader,
) -> None:
    """Asking for identity is not the same as getting it.

    A proxy that compresses an event stream (which also breaks its streaming
    semantics) or a server that ignores the header would put the decode back
    below the cap, so the response is checked and refused. The body here is a
    perfectly good frame: the ONLY reason nothing reaches the board is the
    encoding.

    Refusing means the reconnect loop retries, which makes this a STANDING
    condition rather than a burst — so the rate limit matters more here than
    anywhere else in this module, not less.
    """
    monkeypatch.setattr(
        events_module,
        "REFUSED_ENCODINGS",
        RateLimitedWarning("refusing a content-encoded lithos event stream"),
    )
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        _recording_httpx_client(
            requests,
            [_reopened_frame("evt-gzipped"), _reopened_frame("evt-gzipped-again")],
            response_headers={"content-encoding": "gzip"},
        ),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    with caplog.at_level(logging.WARNING, logger=events_module.__name__):
        await hub.start()
        try:
            await _wait_for(lambda: len(requests) >= 2)
        finally:
            await hub.stop()

    assert subscriber.empty(), "a refused stream must not deliver events"
    assert "content-encoded" in caplog.text
    assert caplog.records[0].__dict__["content_encoding"] == "gzip"
    # Retried every backoff, recorded once: the log rate is Lens's, not the
    # reconnect loop's.
    assert len(caplog.records) == 1


def _mock_transport_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> type:
    """A REAL ``httpx.AsyncClient`` wired to a mock transport.

    The other fake in this file stands in for httpx entirely, which is what
    most of these tests want. This one does not: the exposure it is here for
    lives inside httpx's own content-decoding path, so the test has to run
    through the real client to mean anything.
    """

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    return _Client


@pytest.mark.anyio
async def test_a_gzipped_oversized_line_never_reaches_the_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bypass around the pending-line cap, reproduced against real httpx.

    ``aiter_bytes`` content-decodes each chunk before yielding it, and httpx
    advertises gzip and deflate by default — so a compressed upstream turns one
    small socket read into an arbitrarily large decoded chunk, materialized
    BELOW the cap and before anything here could measure it. 2 MiB of one line
    compresses to about 2 KiB: roughly 1000:1, and none of the terms are
    Lens's.

    Two things close it, and this exercises both: the request asks for
    identity, and the response is checked rather than trusted. The body is
    built before the measurement starts, so the peak is the reader's.
    """
    monkeypatch.setattr(
        events_module,
        "REFUSED_ENCODINGS",
        RateLimitedWarning("refusing a content-encoded lithos event stream"),
    )
    body = b'data: {"task_id":"task-1","pad":"' + b"x" * 2 * 1024 * 1024 + b'"}\n\n'
    compressed = gzip.compress(body)
    assert len(compressed) * 100 < len(body), "the amplification is the point"
    asked: list[str] = []

    async def lazy_body() -> AsyncIterator[bytes]:
        yield compressed

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers.get("accept-encoding", ""))
        # An async iterator, not `content=<bytes>`: constructing a Response
        # from bytes makes httpx decode them THERE, which would put 2 MiB on
        # the test's own account and measure nothing about the reader.
        return httpx.Response(
            200,
            content=lazy_body(),
            headers={
                "content-encoding": "gzip",
                "content-type": "text/event-stream",
            },
        )

    monkeypatch.setattr(
        events_module.httpx, "AsyncClient", _mock_transport_client(handler)
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe()

    tracemalloc.start()
    try:
        await hub.start()
        try:
            await _wait_for(lambda: len(asked) >= 2)
        finally:
            await hub.stop()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert asked[0] == "identity", "httpx would otherwise advertise gzip, deflate"
    assert subscriber.empty()
    assert peak < 1024 * 1024, f"peak {peak} bytes: the line was decoded after all"


@pytest.mark.anyio
async def test_the_reader_reassembles_lines_across_chunk_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Splitting bytes rather than taking httpx's lines puts three joins on Lens.

    Chunk boundaries fall wherever the network put them — mid-line, and mid
    UTF-8 sequence (the second chunk here opens with the continuation byte of
    an e-acute). And a CRLF stream must read the same as an LF one, so the
    terminator is stripped rather than left on the value.
    """
    chunks: list[str | bytes] = [
        b"id: evt-split\r\nevent: task.upda",
        b'ted\r\ndata: {"task_id":"caf\xc3',
        b'\xa9-1"}\r\n\r\n',
    ]

    received = await _events_from_stream(monkeypatch, chunks, 1)

    assert received[0].id == "evt-split"
    assert received[0].type == "task.updated"
    assert received[0].task_id == "café-1"


@pytest.mark.anyio
async def test_an_unterminated_line_is_bounded_while_it_is_still_arriving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured, because this bound has no behavioural signature.

    Every OTHER guard here is visible in what reaches the board, so a test can
    watch for it. This one is not: for any line that eventually ends, the frame
    cap would refuse the frame anyway, and for a line that never ends nothing
    is emitted either way. What changes is what the process is HOLDING while
    the line is still arriving — which is the whole exposure (`a single
    upstream data: line ... bounded only by available memory`), and the reason
    the reader splits bytes itself instead of taking httpx's lines: httpx's own
    buffer would already have grown before anything downstream could look.

    So the assertion is on peak allocation, and the margin is wide enough not
    to be a flake: 2 MiB of line against a cap of 200 characters measures 217
    KiB held with the cap (one chunk, plus its decoded copy) and 2.2 MiB
    without it, against a 1 MiB threshold. The chunks are generated lazily so
    the number measures the reader rather than the test's own input.
    """
    monkeypatch.setattr(events_module, "MAX_EVENT_FRAME_CHARS", 200)

    def never_terminated() -> Iterator[bytes]:
        yield b'data: {"task_id":"task-1","pad":"'
        for _ in range(32):
            yield b"x" * 64 * 1024  # 2 MiB, and not a newline anywhere in it

    tracemalloc.start()
    try:
        received = await _events_from_stream(monkeypatch, never_terminated(), 1)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    # Nothing was ever a frame, so the only thing the stream produced is the
    # reconnect backstop once the connection ended.
    assert received[0].type == LENS_REFRESH_EVENT
    assert peak < 1024 * 1024, f"peak {peak} bytes: the pending line accumulated"
