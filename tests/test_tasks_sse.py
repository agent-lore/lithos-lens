"""Task event normalization, scope rules, and reconnect replay tests."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from lithos_lens import events as events_module
from lithos_lens import web
from lithos_lens.config import EventsConfig, LithosConfig, load_config
from lithos_lens.events import (
    CONSUMED_EVENT_TYPES,
    EVENT_ID_MAX_LENGTH,
    LENS_REFRESH_EVENT,
    DroppedEventLog,
    EventHub,
    LensEvent,
    is_replay_cursor,
    parse_lithos_sse_frame,
)
from lithos_lens.web import create_app
from tests.conftest import stream_frames


@pytest.fixture
def fresh_drop_log(monkeypatch: pytest.MonkeyPatch) -> DroppedEventLog:
    """Give a test its own drop-log counter (the module instance is process-wide)."""

    drop_log = DroppedEventLog()
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


def test_a_system_event_forwards_only_the_fields_lens_contracts_for() -> None:
    """Nothing consumes an `agent.registered` payload — `tasks.js` registers no
    listener and `requires_refresh=False` drives no reconcile — yet every frame
    goes to every subscriber of the unauthenticated `/tasks/events`. So the
    payload is projected to the two fields REQUIREMENTS.md documents rather
    than passed through: `lithos_agent_register` takes caller-supplied
    `metadata`, and any field Lithos adds later would otherwise reach every
    connected browser with no change here.
    """
    event = parse_lithos_sse_frame(
        [
            "id: evt-sys-2",
            "event: agent.registered",
            'data: {"agent_id":"agent-a","name":"Agent A",'
            '"metadata":{"token":"s3cret","host":"10.0.0.4"},'
            '"endpoint":"http://10.0.0.4:9000"}',
        ]
    )

    assert event is not None
    assert event.payload == {"agent_id": "agent-a", "name": "Agent A"}


def test_a_non_finite_number_never_enters_an_event_payload() -> None:
    """`json.loads` accepts the bare `NaN`/`Infinity` tokens and `json.dumps`
    re-emits them, so without this guard a non-finite float upstream would ride
    into `as_sse`'s `data:` line — which `JSON.parse` rejects.

    That is worse than losing one event: `tasks.js` commits the dedupe key
    before parsing, so a Lithos replay of the same frame is discarded at the
    guard instead of retried, and the reconcile never runs on a stream that
    looks healthy throughout. Refused at ingest, the frame is simply unusable
    and the task-scoped event is dropped like any other without a `task_id`.
    """
    for token in ("NaN", "Infinity", "-Infinity"):
        assert (
            parse_lithos_sse_frame(
                [
                    "id: evt-nan",
                    "event: task.updated",
                    f'data: {{"task_id":"task-1","score": {token}}}',
                ]
            )
            is None
        ), token

    # And a system-scoped frame, which survives having no `task_id`, still
    # cannot carry the value onto the wire.
    event = parse_lithos_sse_frame(
        [
            "id: evt-nan-sys",
            "event: agent.registered",
            'data: {"agent_id":"agent-a","name": Infinity}',
        ]
    )

    assert event is not None
    assert event.payload == {}
    assert "Infinity" not in event.as_sse()


def test_task_scoped_event_without_task_id_is_dropped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
    fresh_drop_log: DroppedEventLog,
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
    # The dropped id is upstream-controlled, so the log sink must stay bounded
    # no matter how many malformed events arrive.
    monkeypatch.setattr(events_module, "DROPPED_EVENTS", DroppedEventLog(interval=10))
    with caplog.at_level(logging.WARNING, logger=events_module.__name__):
        for index in range(25):
            assert (
                parse_lithos_sse_frame(
                    [
                        f"id: evt-drop-{index}",
                        "event: task.updated",
                        "data: {}",
                    ]
                )
                is None
            )

    # First, tenth and twentieth only — each carrying the running total.
    assert len(caplog.records) == 3
    totals = [record.__dict__["dropped_total"] for record in caplog.records]
    assert totals == [1, 10, 20]


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


class _FakeStreamResponse:
    def __init__(self, lines: list[str], *, hold: bool) -> None:
        self._lines = lines
        self._hold = hold

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line
        if self._hold:
            # Keep the connection open so the hub stays on this attempt until
            # the test stops it; otherwise it would reconnect in a tight loop.
            await asyncio.Event().wait()


class _FakeStreamContext:
    def __init__(self, lines: list[str], *, hold: bool) -> None:
        self._response = _FakeStreamResponse(lines, hold=hold)

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _recording_httpx_client(
    requests: list[tuple[str, dict[str, str] | None]],
    connections: list[list[str] | Exception],
) -> type:
    """An ``httpx.AsyncClient`` stand-in for the upstream ``/events`` stream.

    Records every outbound GET and serves one prepared connection per attempt:
    a frame batch to replay, or an exception to raise instead of connecting (as
    httpx does for a header it refuses to send). Once the entries run out the
    connection is held open.
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
            return _FakeStreamContext(entry or [], hold=entry is None)

    return _Client


def _reopened_frame(event_id: str) -> list[str]:
    return [
        f"id: {event_id}",
        "event: task.reopened",
        'data: {"task_id":"task-1","agent":"agent-a","prior_status":"completed"}',
        "",
    ]


async def _first_open_backstop(subscriber: asyncio.Queue[LensEvent]) -> LensEvent:
    """Consume the refresh owed to a tab that attached before the first open.

    `start()` only SCHEDULES the upstream dial, so a subscriber attached before
    it — which every hub test below is — rendered while Lens was receiving
    nothing. That interval is a gap like any other, so the first open pays it
    off. Drained here so each test can go on asserting about the behaviour it
    is actually for.
    """

    event = await asyncio.wait_for(subscriber.get(), timeout=2)
    assert event.id == f"{LENS_REFRESH_EVENT}:1"
    return event


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
        await _first_open_backstop(subscriber)
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
    assert headers is None
    assert event.type == "task.reopened"
    assert event.task_id == "task-1"


@pytest.mark.anyio
async def test_reconnect_replays_from_last_event_id_and_broadcasts_a_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shortened because the first open has already spent one window: the
    # reconnect's refresh is deferred to the end of it, not dropped.
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
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
        await _first_open_backstop(subscriber)
        first = await asyncio.wait_for(subscriber.get(), timeout=2)
        refresh = await asyncio.wait_for(subscriber.get(), timeout=2)
    finally:
        await hub.stop()

    assert first.id == "evt-8"
    assert hub.last_event_id == "evt-8"
    assert len(requests) == 2
    assert requests[1][1] == {"Last-Event-ID": "evt-8"}
    # Replay is bounded by Lithos's ring buffer, so the reconnect also carries
    # a synthetic full-refresh backstop — unscoped, and distinct from the one
    # the first open already paid (hence `:2`).
    assert refresh.id == f"{LENS_REFRESH_EVENT}:2"
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
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
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
        await _first_open_backstop(subscriber)
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
    assert requests[1][1] is None, description


@pytest.mark.anyio
async def test_a_first_connect_that_had_to_retry_still_delivers_the_backstop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop is owed to a GAP, and a retry window is the widest kind.

    Lens is up and serving throughout it, so browsers attach and their
    EventSource reports `live` — the polling fallback is driven by the
    browser-to-Lens stream, not by Lens's upstream, so it never arms. Every
    Lithos change during the retry window is invisible to those tabs, and
    without a backstop on the eventual open it stays invisible until something
    unrelated triggers a reconcile.

    Keying on "have we ever connected" asks the wrong question: that is a
    property of the hub's history, and staleness is a property of the interval.
    Which is why the count below is ONE and not two: the interval before the
    first open and the retry window that follows it are the same uninterrupted
    gap, and it is paid off once, on the open that ends it.

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
async def test_a_first_open_refreshes_a_tab_that_attached_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a CLEANLY successful first connect owes a refresh to a tab already
    attached, because Lens serves dashboards while that connect is in flight.

    `EventHub.start()` schedules `_run`; it does not await the handshake. So a
    browser can fetch its snapshot, attach, and report `live` before Lens is
    receiving anything upstream. A task that changes in that window reaches
    neither the snapshot (taken before it) nor the un-cursored stream that
    opens after it, and the tab stays stale for the process lifetime with no
    signal — the polling fallback arms off the browser-to-Lens stream, which
    is healthy throughout.

    One attempt, held open, so the refresh asserted here can only be the first
    open's. Exactly one: an open is not a flap.
    """
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
        received = await asyncio.wait_for(subscriber.get(), timeout=2)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(subscriber.get(), timeout=0.5)
    finally:
        await hub.stop()

    assert received.id == f"{LENS_REFRESH_EVENT}:1"
    assert received.requires_refresh is True
    assert len(requests) == 1


@pytest.mark.anyio
async def test_a_first_open_with_nothing_attached_spends_no_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterweight to the test above, and what makes it free.

    A refresh with no subscribers reaches nobody, so it must not spend the
    cooldown window — or the sequence number — that a real recipient is owed
    moments later. At process start that is the normal case: the hub dials
    before any browser exists.

    Demonstrated by the NUMBER: the first open happens with nothing attached,
    a tab attaches during the backoff, and the refresh it receives from the
    next open is `:1`. Were the empty broadcast counted it would be `:2`, and
    it would additionally have been deferred to the end of a cooldown window
    the tab never benefited from.
    """
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        # One frame batch, then the stream ends; the next attempt holds open.
        _recording_httpx_client(requests, [_reopened_frame("evt-40")]),
    )
    hub = EventHub(
        # A backoff long enough to attach inside deterministically.
        EventsConfig(enabled=True, reconnect_backoff_ms=(400,)),
        LithosConfig(),
    )

    await hub.start()
    try:
        # Wait out the first open and the batch behind it. Nothing is
        # attached, so nothing is published — and `subscribe()` is synchronous,
        # so no reconnect can slip in between the check and the attach.
        async with asyncio.timeout(2):
            # `start()` sets "reconnecting" BEFORE scheduling `_run`, so the
            # attempt count is what distinguishes "not dialled yet" from
            # "dialled, opened, and the batch ran out".
            while not (requests and hub.status == "reconnecting"):
                await asyncio.sleep(0.005)
        subscriber = hub.subscribe()
        received = await asyncio.wait_for(subscriber.get(), timeout=2)
    finally:
        await hub.stop()

    assert received.id == f"{LENS_REFRESH_EVENT}:1"
    assert len(requests) == 2


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

    assert requests[0][1] is None
    assert requests[1][1] == {"Last-Event-ID": "evt-20"}
    assert requests[2][1] == {"Last-Event-ID": "evt-21"}
    # The third attempt died before the stream opened, so the cursor goes and
    # the fourth resumes from nothing — which is exactly why that reconnect
    # MUST still deliver a refresh, even though it lands inside the cooldown.
    assert requests[3][1] is None
    # `:1` is the first open's (the tab attached before it); every later gap —
    # including the one that gave up the cursor — coalesces onto the trailing
    # edge of the cooldown as `:2`. Given up, never dropped.
    assert [event.id for event in received] == [
        f"{LENS_REFRESH_EVENT}:1",
        "evt-20",
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

    # Four opens, two refreshes: the flap rate does not reach the tabs (each
    # refresh costs every one of them a full refetch), but neither is the
    # signal discarded — the three reconnects inside the window coalesce into
    # one broadcast delivered on the trailing edge, after the last event.
    # `:1` is the first open's, paid immediately to the tab already attached.
    assert [event.id for event in received] == [
        f"{LENS_REFRESH_EVENT}:1",
        "evt-30",
        "evt-31",
        "evt-32",
        "evt-33",
        f"{LENS_REFRESH_EVENT}:2",
    ]
    assert subscriber.empty()


@pytest.mark.anyio
async def test_the_backstop_reaches_a_subscriber_whose_queue_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 6 is about DELIVERY, not just about broadcasting.

    An ordinary event is best-effort: a subscriber whose queue is full is one
    the browser is not draining, and the refresh that follows the gap repairs
    whatever it missed. The refresh has no such backstop — it IS the backstop —
    so a browser that is merely slow (a throttled background tab) while the
    upstream flaps would otherwise lose the one signal that would have
    repaired it, and stay stale with nothing left to say so.

    Delivered by displacing the OLDEST queued event, which the refresh
    subsumes: the reconcile it triggers refetches the whole board, so that
    event's effect is re-derived rather than lost.
    """
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx,
        "AsyncClient",
        _recording_httpx_client(requests, [_reopened_frame("evt-50")]),
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )
    subscriber = hub.subscribe(maxsize=2)
    backlog = [
        LensEvent(id=f"stale-{index}", type="task.updated", task_id="task-9")
        for index in range(2)
    ]
    for event in backlog:
        subscriber.put_nowait(event)
    assert subscriber.full()

    await hub.start()
    try:
        # Drained only AFTER the open, so the backstop meets the queue while it
        # is still full — draining first would free the slot under test.
        async with asyncio.timeout(2):
            while hub.status != "live":
                await asyncio.sleep(0.005)
        # Long enough for the frame behind the backstop to be published too.
        await asyncio.sleep(0.05)
    finally:
        await hub.stop()

    delivered = []
    while not subscriber.empty():
        delivered.append(subscriber.get_nowait().id)

    # The OLDEST queued event was displaced to make room, and the refresh got
    # through. The real event behind it took the best-effort path and was
    # dropped, rather than displacing the refresh that had just been
    # guaranteed its slot.
    assert delivered == ["stale-1", f"{LENS_REFRESH_EVENT}:1"]


@pytest.mark.anyio
async def test_a_snapshot_taken_before_the_open_refreshes_when_it_finally_attaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering `_gap_since_last_open` alone cannot reach.

    A `/tasks` render reads its Lithos snapshot while the first handshake is
    still in flight; the task changes; the stream opens with NO subscriber
    (that response has not finished, so its script cannot have run yet), so the
    gap is discharged to nobody; only then does the browser attach. Keying on
    "was anyone attached at the instant of the open" treats an in-flight
    response as proof that no stale tab is being created, and it is not.

    The marker settles it without guessing at timings: the snapshot was taken
    under no stream, the attach happens under stream 1, and an open between the
    two is exactly the interval whose changes reached neither.
    """
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx, "AsyncClient", _recording_httpx_client(requests, [])
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )

    await hub.start()
    try:
        # (1) The render reads its snapshot. `start()` only scheduled the dial,
        # so no stream covers it — and nothing is attached.
        marker = hub.snapshot_marker()
        assert marker == 0
        # (2)+(3) The stream opens. The refresh it owes reaches nobody.
        async with asyncio.timeout(2):
            while hub.status != "live":
                await asyncio.sleep(0.005)
        # (4)+(5) Only now can the response finish and its EventSource attach.
        subscriber = hub.subscribe(since=marker)
    finally:
        await hub.stop()

    assert hub.snapshot_marker() == 1
    seeded = subscriber.get_nowait()
    assert seeded.type == LENS_REFRESH_EVENT
    assert seeded.requires_refresh is True
    assert subscriber.empty()


@pytest.mark.anyio
async def test_a_snapshot_taken_under_the_live_stream_attaches_without_a_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control, and what keeps the marker from costing a refetch
    on every page load: a snapshot read while the stream that is still open was
    already open has missed nothing, so its tab attaches silently.

    Without this, "refresh every new subscriber" would pass the test above just
    as well while doubling the fetches of every navigation.
    """
    monkeypatch.setattr(events_module, "LENS_REFRESH_MIN_INTERVAL_S", 0.3)
    requests: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        events_module.httpx, "AsyncClient", _recording_httpx_client(requests, [])
    )
    hub = EventHub(
        EventsConfig(enabled=True, reconnect_backoff_ms=(1,)), LithosConfig()
    )

    await hub.start()
    try:
        async with asyncio.timeout(2):
            while hub.status != "live":
                await asyncio.sleep(0.005)
        # Rendered under the open stream, and attaching under the same one.
        subscriber = hub.subscribe(since=hub.snapshot_marker())
    finally:
        await hub.stop()

    assert subscriber.empty()


@pytest.mark.anyio
async def test_the_event_stream_reads_the_snapshot_marker_off_the_url(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint half of the wiring: `?since=` reaches `subscribe`.

    The hub logic above is only reachable if the endpoint actually reads what
    the page stamped, so the hop is pinned rather than assumed. Driven against
    the ASGI app directly, with no lifespan: the hub never dials, so no stream
    is open and every snapshot this process rendered is stamped `0` — a browser
    handing back anything else was rendered under a stream that is gone.
    """
    monkeypatch.setattr(web, "SSE_KEEPALIVE_S", 0.05)
    app = create_app(load_config(lithos_lens_config_env))

    stale = await stream_frames(app, "/tasks/events", 2, query="since=7")
    current = await stream_frames(app, "/tasks/events", 2, query="since=0")

    # A marker from a stream that is gone: refreshed on attach, immediately
    # behind the connected frame.
    assert b"lens.status" in stale[0]
    assert b"event: lens.refresh" in stale[1]
    # The current one is owed nothing, so the next write is the keepalive.
    assert b"lens.status" in current[0]
    assert current[1] == b": keepalive\n\n"


@pytest.mark.parametrize(
    ("query", "description"),
    [
        ("since=--1", "`lstrip('-')` strips EVERY hyphen; `int()` accepts one"),
        ("since=%C2%B2", "SUPERSCRIPT TWO: Unicode `No`, `isdigit()` yes, `int()` no"),
        ("since=%E2%91%A0", "CIRCLED DIGIT ONE: same category, same divergence"),
        ("since=-%C2%B2", "and signed, which took a different branch"),
        ("since=" + "9" * 400, "past the length cap, refused before it is parsed"),
        ("since=", "present but empty"),
        ("since=fnord", "not a number at all"),
    ],
)
@pytest.mark.anyio
async def test_a_marker_lens_cannot_parse_attaches_instead_of_failing(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    description: str,
) -> None:
    """A guard written to approximate `int()` will drift from it.

    `?since=` is an unauthenticated caller's string on the one path
    deliberately exempt from admission control, so a value that satisfies the
    guard and then fails the parse raises out of the handler AFTER the response
    has begun — which uvicorn logs with a full traceback, an unbounded
    attacker-driven write into the operator's log sink. `int()` is therefore
    its own predicate now, and every one of these attaches normally: the
    handler's contract is that an unusable marker means nothing to be stale
    about, not a 500.
    """
    monkeypatch.setattr(web, "SSE_KEEPALIVE_S", 0.05)
    app = create_app(load_config(lithos_lens_config_env))

    frames = await stream_frames(app, "/tasks/events", 2, query=query)

    assert b"lens.status" in frames[0], description
    # Unusable, so treated as no snapshot at all — attached, nothing seeded.
    assert frames[1] == b": keepalive\n\n", description
