"""Task event normalization, scope rules, and reconnect replay tests."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from lithos_lens import events as events_module
from lithos_lens.config import EventsConfig, LithosConfig
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
    assert headers is None
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
    assert requests[1][1] == {"Last-Event-ID": "evt-8"}
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
    assert requests[1][1] is None, description


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

    assert requests[1][1] == {"Last-Event-ID": "evt-20"}
    assert requests[2][1] == {"Last-Event-ID": "evt-21"}
    # The third attempt died before the stream opened, so the cursor goes and
    # the fourth resumes from nothing — which is exactly why that reconnect
    # MUST still deliver a refresh, even though it lands inside the cooldown.
    assert requests[3][1] is None
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
