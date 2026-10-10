"""Knowledge events (K2 S7, PRD D14): the knowledge scope at the hub, the two
browser streams it fans out to, the knowledge caches patched from live
frames, and ``GET /knowledge/events``.

Every frame here goes through ``parse_lithos_sse_frame`` — the path an
upstream frame takes — with the payload Lithos 0.6.0 sends (@ d2c49bb,
``src/lithos/events.py``; SPECIFICATION §5.8).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import MutableMapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.util.http import parse_excluded_urls

from lithos_lens import event_streams, events, web
from lithos_lens.config import EventsConfig, LithosConfig, load_config
from lithos_lens.errors import EventSubscriberLimit
from lithos_lens.events import (
    LENS_REFRESH_EVENT,
    EventHub,
    LensEvent,
    parse_lithos_sse_frame,
)
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge_edges import EdgeTable, EdgeTableSnapshot, KnowledgeEdge
from lithos_lens.knowledge_facts import NoteFactsCache
from lithos_lens.tasks import NoteRecord
from lithos_lens.telemetry import TRACE_EXCLUDED_URLS
from lithos_lens.web import create_app
from tests.conftest import metric_value

PLAN = "note-influx-plan"
ROLLBACK = "note-influx-rollback"
CAPACITY = "note-influx-capacity"
ROUTE = "/knowledge/graph"

#: One payload per knowledge type, as Lithos sends it.
KNOWLEDGE_FRAMES: dict[str, dict[str, Any]] = {
    "note.created": {"id": "note-1", "title": "A note", "path": "a-note.md"},
    "note.updated": {"id": "note-1", "title": "A note", "path": "a-note.md"},
    "note.deleted": {"id": "note-1", "path": "a-note.md"},
    "note.renamed": {"id": "note-1", "src_path": "a.md", "dest_path": "b.md"},
    "edge.upserted": {
        "edge_id": "edge_0123456789ab",
        "from_id": "note-1",
        "to_id": "note-2",
        "type": "supports",
        "namespace": "influx",
        "conflict_state": None,
    },
}

_PAYLOAD = re.compile(
    r'<script type="application/json" data-knowledge-graph-payload>(.*?)</script>',
    re.S,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _frame(event_type: str, payload: dict[str, Any], event_id: str = "") -> LensEvent:
    lines = [f"event: {event_type}", f"data: {json.dumps(payload)}"]
    event = parse_lithos_sse_frame([f"id: {event_id}", *lines] if event_id else lines)
    assert event is not None
    return event


def _knowledge_frames() -> list[LensEvent]:
    return [
        _frame(event_type, payload, f"evt-{event_type}")
        for event_type, payload in KNOWLEDGE_FRAMES.items()
    ]


def _task_frame() -> LensEvent:
    return _frame("task.created", {"task_id": "task-1"}, "evt-task")


def _agent_frame() -> LensEvent:
    return _frame("agent.registered", {"agent_id": "agent-a"}, "evt-agent")


def _refresh() -> LensEvent:
    return LensEvent(id="lens.refresh:1", type=LENS_REFRESH_EVENT, task_id="")


def _hub() -> EventHub:
    return EventHub(EventsConfig(enabled=False), LithosConfig())


def _drain(queue: asyncio.Queue[LensEvent]) -> list[str]:
    return [queue.get_nowait().type for _ in range(queue.qsize())]


# ── The knowledge scope at the hub ─────────────────────────────────────


@pytest.mark.anyio
async def test_each_stream_gets_its_own_scope_and_both_get_the_refresh() -> None:
    hub = _hub()
    tasks = hub.subscribe()
    knowledge = hub.subscribe(stream="knowledge")

    for event in [_task_frame(), _agent_frame(), *_knowledge_frames(), _refresh()]:
        await hub.publish(event)

    # The dashboard's stream is unchanged: no knowledge frame reaches it.
    assert _drain(tasks) == ["task.created", "agent.registered", LENS_REFRESH_EVENT]
    # The graph page's stream: all five, and the backstop — never a task or
    # agent frame.
    assert _drain(knowledge) == [*KNOWLEDGE_FRAMES, LENS_REFRESH_EVENT]


@pytest.mark.anyio
@pytest.mark.parametrize("stream", ["knowledge", "tasks"])
async def test_an_overflowing_queue_is_told_to_refresh_whatever_it_dropped(
    stream: events.EventStream,
) -> None:
    """A dropped frame leaves the browser's connection healthy, so nothing
    but the hub can tell it what it missed. One chunk of 100 frames naming
    nothing drawn, then more, overflowing twice — with a consumer waiting the
    whole time: the chunk is published without yielding to it."""
    hub = _hub()
    queue = hub.subscribe(stream=stream)
    received: list[LensEvent] = []

    async def consume() -> None:
        while True:
            received.append(await queue.get())

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    kind, key = (
        ("note.updated", "id") if stream == "knowledge" else ("task.updated", "task_id")
    )
    # 250 frames into a 100-entry queue overflow it twice — at frame 100 and
    # again at 200, when the first overflow's refresh is queued ahead of 99
    # frames: what survives is one refresh and the 49 frames after it.
    for index in range(250):
        await hub.publish(_frame(kind, {key: f"other-{index}"}, f"evt-{index}"))
    await asyncio.sleep(0)
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer

    types = [event.type for event in received]
    assert types == [LENS_REFRESH_EVENT] + [kind] * 49
    assert received[0].payload == {"reason": "overflow"}
    assert received[0].id == f"{LENS_REFRESH_EVENT}:overflow:2"
    assert [event.id for event in received[1:]] == [f"evt-{i}" for i in range(201, 250)]


@pytest.mark.anyio
async def test_the_knowledge_types_never_ask_the_dashboard_to_refresh() -> None:
    for event in _knowledge_frames():
        assert (event.scope, event.task_id, event.requires_refresh) == (
            "knowledge",
            "",
            False,
        )


def test_both_streams_share_one_subscriber_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap bounds queues and publish cost, whichever stream they serve."""
    monkeypatch.setattr(events, "MAX_EVENT_SUBSCRIBERS", 2)
    hub = _hub()
    hub.subscribe()
    knowledge = hub.subscribe(stream="knowledge")

    with pytest.raises(EventSubscriberLimit):
        hub.subscribe(stream="knowledge")
    with pytest.raises(EventSubscriberLimit):
        hub.subscribe()

    hub.unsubscribe(knowledge)
    hub.subscribe(stream="knowledge")


def test_the_knowledge_subscriber_gauge_counts_its_own_stream(
    metric_reader: InMemoryMetricReader,
) -> None:
    hub = _hub()
    hub.subscribe()
    first = hub.subscribe(stream="knowledge")
    hub.subscribe(stream="knowledge")

    assert metric_value(metric_reader, "lens_event_subscribers").value == 3
    assert metric_value(metric_reader, "lens_knowledge_event_subscribers").value == 2

    hub.unsubscribe(first)
    assert metric_value(metric_reader, "lens_knowledge_event_subscribers").value == 1


# ── The caches the hub patches before fan-out ──────────────────────────


def _row(edge_id: str = "edge_held0000001") -> KnowledgeEdge:
    return KnowledgeEdge(
        edge_id=edge_id,
        from_id="note-1",
        to_id="note-3",
        type="related_to",
        weight=0.5,
        namespace="influx",
    )


class _Fetches:
    """The edge table's fetch: one held row, failing on demand."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def __call__(self, _type: str | None, _namespace: str | None) -> Any:
        self.calls += 1
        if self.fail:
            raise RuntimeError("lithos is down")
        return [_row()]


class _Reads:
    """The facts cache's read: every id is a note, every read recorded."""

    def __init__(self) -> None:
        self.ids: list[str] = []

    async def __call__(self, note_id: str) -> NoteRecord:
        self.ids.append(note_id)
        return NoteRecord(id=note_id, title=f"read {note_id}", content="")


def _wired_hub() -> tuple[EventHub, EdgeTable, _Fetches, NoteFactsCache, _Reads]:
    fetches, reads = _Fetches(), _Reads()
    table = EdgeTable(fetches)
    facts = NoteFactsCache(reads, lambda: asyncio.Semaphore(4))
    hub = _hub()
    hub.edge_table = table
    hub.note_facts = facts
    return hub, table, fetches, facts, reads


@pytest.mark.anyio
async def test_a_live_edge_upserted_is_in_the_snapshot_before_any_browser_hears(
    metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, table, _, _, _ = _wired_hub()
    await table.read()
    hub.subscribe(stream="knowledge")
    patched: list[tuple[str, ...]] = []
    reaches = events.reaches

    def observed(event_type: str, stream: events.EventStream) -> bool:
        # Asked once per browser queue, at the fan-out: the snapshot it sees
        # then must already carry the patch.
        snapshot = table.current
        assert isinstance(snapshot, EdgeTableSnapshot)
        patched.append(tuple(row.edge_id for row in snapshot.rows))
        return reaches(event_type, stream)

    monkeypatch.setattr(events, "reaches", observed)

    await hub.publish(_frame("edge.upserted", KNOWLEDGE_FRAMES["edge.upserted"]))

    assert patched == [("edge_held0000001", "edge_0123456789ab")]
    snapshot = table.current
    assert isinstance(snapshot, EdgeTableSnapshot)
    inserted = snapshot.rows[-1]
    assert (inserted.from_id, inserted.to_id, inserted.partial) == (
        "note-1",
        "note-2",
        True,
    )
    assert inserted.weight is None
    # Telemetry from the live frame: consumed by type, and the S1 patch counter.
    assert (
        metric_value(
            metric_reader, "lens_events_published_total", type="edge.upserted"
        ).value
        == 1
    )
    assert (
        metric_value(
            metric_reader,
            "lens_knowledge_edge_table_patches_total",
            event_type="edge.upserted",
            outcome="inserted",
        ).value
        == 1
    )


@pytest.mark.anyio
async def test_every_knowledge_type_is_counted_by_type(
    metric_reader: InMemoryMetricReader,
) -> None:
    hub = _hub()
    for event in _knowledge_frames():
        await hub.publish(event)

    for event_type in KNOWLEDGE_FRAMES:
        assert (
            metric_value(
                metric_reader, "lens_events_published_total", type=event_type
            ).value
            == 1
        )


@pytest.mark.anyio
async def test_live_note_events_patch_the_facts_cache() -> None:
    hub, _, _, facts, reads = _wired_hub()
    ids = ["note-1", "note-2", "note-3"]
    await facts.lookup(ids)
    reads.ids.clear()

    await hub.publish(_frame("note.updated", {**KNOWLEDGE_FRAMES["note.updated"]}))
    await hub.publish(_frame("note.deleted", {"id": "note-2", "path": "n2.md"}))
    await hub.publish(
        _frame("note.renamed", {"id": "note-3", "src_path": "a", "dest_path": "b"})
    )
    batch = await facts.lookup(ids)

    # note.updated: stale, re-read on the next draw — once; note.deleted: the
    # ghost, with no read; note.renamed: nothing the graph draws changed.
    assert reads.ids == ["note-1"]
    assert batch.for_id("note-2").is_missing
    assert batch.for_id("note-3").state == "ok"
    await facts.lookup(ids)
    assert reads.ids == ["note-1"]


@pytest.mark.anyio
async def test_a_refresh_expires_the_snapshot_and_marks_every_fact_stale() -> None:
    """``lens.refresh`` means events were missed — a quarantine among them,
    perhaps — so neither cache may answer from before the gap."""
    hub, table, fetches, facts, reads = _wired_hub()
    await table.read()
    await table.read()
    await facts.lookup(["note-1", "note-2"])
    assert (fetches.calls, reads.ids) == (1, ["note-1", "note-2"])

    await hub.publish(_refresh())
    reads.ids.clear()
    await table.read()
    batch = await facts.lookup(["note-1", "note-2"])

    assert fetches.calls == 2
    assert sorted(reads.ids) == ["note-1", "note-2"]
    assert batch.for_id("note-1").state == "ok"

    # Expired again, and the refetch fails: the held rows are served stale.
    await hub.publish(_refresh())
    fetches.fail = True
    stale = await table.read()
    assert isinstance(stale, EdgeTableSnapshot) and stale.stale
    assert [row.edge_id for row in stale.rows] == ["edge_held0000001"]


@pytest.mark.anyio
async def test_a_refresh_during_an_edge_fetch_still_has_the_next_read_refetch() -> None:
    """A fetch that began before the gap carries pre-gap rows. If it lands
    after ``lens.refresh`` it may not install them as fresh: the next draw —
    the pill's reload — must see the edge the missed event was about."""
    started, release = asyncio.Event(), asyncio.Event()
    authoritative = [_row("edge_old00000001")]
    fetches = 0

    async def fetch(_type: str | None, _namespace: str | None) -> Any:
        nonlocal fetches
        fetches += 1
        rows = list(authoritative)  # read now: what this fetch will answer
        if fetches == 1:
            started.set()
            await release.wait()
        return rows

    table = EdgeTable(fetch)
    hub = _hub()
    hub.edge_table = table
    render = asyncio.create_task(table.read())  # a page render, pre-gap
    await asyncio.wait_for(started.wait(), timeout=2)
    authoritative.append(_row("edge_missed00001"))  # changed during the gap
    await hub.publish(_refresh())
    release.set()
    await render

    redraw = await table.read()
    again = await table.read()

    assert isinstance(redraw, EdgeTableSnapshot) and not redraw.stale
    assert [row.edge_id for row in redraw.rows] == [
        "edge_old00000001",
        "edge_missed00001",
    ]
    assert again is redraw
    assert fetches == 2


@pytest.mark.anyio
@pytest.mark.parametrize("held", [True, False])
async def test_a_live_edge_upserted_survives_a_fetch_that_began_before_it(
    held: bool,
) -> None:
    """Another tab's fetch captured the rows before an agent wrote the edge;
    the event lands while that fetch is out, and the pill's reload joins it.
    What the fetch installs must still carry the patch — the new edge
    inserted partial, the existing one's conflict state — whether a snapshot
    was held before (an expired TTL) or not (the first read)."""
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(_type: str | None, _namespace: str | None) -> Any:
        nonlocal calls
        calls += 1
        rows = [_row("edge_old00000001")]  # captured before the event
        if calls == (2 if held else 1):
            started.set()
            await release.wait()
        return rows

    now = [0.0]
    table = EdgeTable(fetch, ttl_s=300.0, ticks=lambda: now[0])
    if held:
        await table.read()
        now[0] = 301.0  # the held snapshot's TTL has run out
    hub = _hub()
    hub.edge_table = table
    render = asyncio.create_task(table.read())  # the other tab's draw
    await asyncio.wait_for(started.wait(), timeout=2)
    new = {**KNOWLEDGE_FRAMES["edge.upserted"], "edge_id": "edge_new00000001"}
    old = {**new, "edge_id": "edge_old00000001", "conflict_state": "contested"}
    await hub.publish(_frame("edge.upserted", new, "evt-new"))
    await hub.publish(_frame("edge.upserted", old, "evt-old"))
    reload = asyncio.create_task(table.read())  # the pill's reload joins it
    await asyncio.sleep(0)
    release.set()
    await render

    for snapshot in (await reload, await table.read()):
        assert isinstance(snapshot, EdgeTableSnapshot) and not snapshot.stale
        rows = {row.edge_id: row for row in snapshot.rows}
        assert sorted(rows) == ["edge_new00000001", "edge_old00000001"]
        assert rows["edge_new00000001"].partial
        assert rows["edge_old00000001"].conflict_state == "contested"
        assert rows["edge_old00000001"].partial
    assert calls == (2 if held else 1)


@pytest.mark.anyio
async def test_a_read_after_the_refresh_does_not_wait_on_the_pre_gap_fetch() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    answers = [["edge_old00000001"], ["edge_old00000001", "edge_missed00001"]]
    calls = 0

    async def fetch(_type: str | None, _namespace: str | None) -> Any:
        nonlocal calls
        rows = [_row(edge_id) for edge_id in answers[min(calls, 1)]]
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return rows

    table = EdgeTable(fetch)
    hub = _hub()
    hub.edge_table = table
    before = asyncio.create_task(table.read())
    await asyncio.wait_for(started.wait(), timeout=2)
    await hub.publish(_refresh())
    after = asyncio.create_task(table.read())  # the pill's reload
    release.set()
    await before
    state = await after

    assert isinstance(state, EdgeTableSnapshot)
    assert [row.edge_id for row in state.rows][-1] == "edge_missed00001"


@pytest.mark.anyio
async def test_a_refresh_keeps_a_read_in_flight_from_caching_pre_gap_facts() -> None:
    release = asyncio.Event()
    reads: list[str] = []

    async def slow(note_id: str) -> NoteRecord:
        reads.append(note_id)
        await release.wait()
        return NoteRecord(id=note_id, title="before the gap", content="")

    facts = NoteFactsCache(slow, lambda: asyncio.Semaphore(4))
    hub = _hub()
    hub.note_facts = facts
    lookup = asyncio.create_task(facts.lookup(["note-1"]))
    while not reads:  # the read is under way
        await asyncio.sleep(0)
    await hub.publish(_refresh())
    release.set()
    await lookup

    await facts.lookup(["note-1"])
    assert reads == ["note-1", "note-1"]


# ── Through the app: AppState wires the hub to its caches ──────────────


class _CountingFake(FakeLithosClient):
    """The fake, recording each facts read (``max_length=1``) by id."""

    def __init__(self) -> None:
        super().__init__()
        self.fact_reads: list[str] = []

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        if max_length == 1:
            self.fact_reads.append(knowledge_id)
        return await super().read_note(knowledge_id, max_length=max_length)


def _client(config_path: Path, fake: FakeLithosClient | None = None) -> TestClient:
    return TestClient(
        create_app(
            load_config(config_path),
            lithos_client_factory=lambda _: fake or FakeLithosClient(),
        )
    )


def _payload(client: TestClient, url: str) -> dict[str, Any]:
    response = client.get(url)
    assert response.status_code == 200
    match = _PAYLOAD.search(response.text)
    assert match is not None
    return json.loads(match.group(1))


def _publish(client: TestClient, event: LensEvent) -> None:
    hub = client.app.state.lens.events  # type: ignore[attr-defined]
    assert client.portal is not None
    client.portal.call(hub.publish, event)


def test_a_live_edge_upserted_for_a_new_edge_draws_partial_on_the_next_render(
    lithos_lens_config_env: Path,
) -> None:
    upsert = {
        "edge_id": "edge_live00000001",
        "from_id": PLAN,
        "to_id": CAPACITY,
        "type": "supports",
        "namespace": "influx",
        "conflict_state": None,
    }
    with _client(lithos_lens_config_env) as client:
        before = _payload(client, f"{ROUTE}?focus={PLAN}")
        _publish(client, _frame("edge.upserted", upsert, "evt-live-1"))
        after = _payload(client, f"{ROUTE}?focus={PLAN}")

    assert "edge_live00000001" not in {edge["id"] for edge in before["edges"]}
    (drawn,) = (edge for edge in after["edges"] if edge["id"] == "edge_live00000001")
    assert (drawn["from"], drawn["to"], drawn["partial"]) == (PLAN, CAPACITY, True)


def test_a_live_note_updated_has_the_next_render_re_read_that_note_once(
    lithos_lens_config_env: Path,
) -> None:
    fake = _CountingFake()
    with _client(lithos_lens_config_env, fake) as client:
        first = _payload(client, f"{ROUTE}?focus={PLAN}")
        assert ROLLBACK in {node["id"] for node in first["nodes"]}
        fake.fact_reads.clear()
        _payload(client, f"{ROUTE}?focus={PLAN}")
        assert fake.fact_reads == []  # every node a cache hit

        _publish(client, _frame("note.updated", {"id": ROLLBACK, "title": "r2"}))
        _payload(client, f"{ROUTE}?focus={PLAN}")
        assert fake.fact_reads == [ROLLBACK]
        _payload(client, f"{ROUTE}?focus={PLAN}")
        assert fake.fact_reads == [ROLLBACK]


class _BornLater(_CountingFake):
    """The fake, with one note that does not exist until ``born`` is set."""

    NOTE = "note-born-later"

    def __init__(self) -> None:
        super().__init__()
        self.born = False

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        if knowledge_id != self.NOTE:
            return await super().read_note(knowledge_id, max_length=max_length)
        self.fact_reads.append(knowledge_id)
        if not self.born:
            return None
        return NoteRecord(id=knowledge_id, title="Born, read", content="")


def test_a_live_note_created_clears_a_cached_ghost_and_is_re_read_once(
    lithos_lens_config_env: Path,
) -> None:
    """A note drawn missing (an edge outlived it, or arrived first) that is
    then created: the frame reaches the facts cache through the app's hub,
    clears the ghost and sets the title at once, and the next draw re-reads
    the rest — once."""
    fake = _BornLater()
    note = _BornLater.NOTE
    with _client(lithos_lens_config_env, fake) as client:
        facts = client.app.state.lens.note_facts  # type: ignore[attr-defined]
        assert client.portal is not None

        def lookup(cap: int | None = None) -> Any:
            assert client.portal is not None
            return client.portal.call(partial(facts.lookup, [note], cap=cap))

        assert lookup().for_id(note).is_missing
        assert fake.fact_reads == [note]

        fake.born = True
        _publish(
            client,
            _frame("note.created", {"id": note, "title": "Born", "path": "b.md"}),
        )
        patched = lookup(cap=0).for_id(note)  # no read allowed
        assert fake.fact_reads == [note]
        assert (patched.state, patched.label) == ("pending", "Born")

        reread = lookup().for_id(note)
        assert (reread.state, reread.label) == ("ok", "Born, read")
        assert lookup().for_id(note).state == "ok"
        assert fake.fact_reads == [note, note]


def test_the_fake_publish_seam_scopes_a_knowledge_type_by_its_type(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam builds a LensEvent straight from JSON; the scope still comes
    from the type, so it lands on the knowledge stream and nowhere else."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        hub = app.state.lens.events
        tasks = hub.subscribe()
        knowledge = hub.subscribe(stream="knowledge")
        response = client.post(
            "/tasks/events/publish",
            json={"type": "note.updated", "payload": {"id": PLAN, "title": "x"}},
        )

    assert response.status_code == 202
    assert tasks.empty()
    assert _drain(knowledge) == ["note.updated"]


# ── GET /knowledge/events ──────────────────────────────────────────────


def _http_scope(path: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"lens")],
        "client": ("127.0.0.1", 12345),
        "server": ("lens", 80),
    }


async def _stream(
    app: FastAPI,
    path: str,
    count: int,
    publish: Sequence[LensEvent] = (),
) -> tuple[int, list[bytes]]:
    """The status and first ``count`` body frames ``path`` writes, publishing
    ``publish`` through the app's hub once the stream is open.

    Driven against the ASGI interface, as ``test_admission`` does: a test
    client would read a stream that never ends to completion.
    """
    status = 0
    frames: list[bytes] = []
    opened, enough = asyncio.Event(), asyncio.Event()

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: MutableMapping[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
        elif message["type"] == "http.response.body":
            if message.get("body"):
                frames.append(bytes(message["body"]))
            opened.set()
            if len(frames) >= count or not message.get("more_body", False):
                enough.set()

    served = asyncio.create_task(app(_http_scope(path), receive, send))
    try:
        await asyncio.wait_for(opened.wait(), timeout=5)
        for event in publish:
            await app.state.lens.events.publish(event)
        await asyncio.wait_for(enough.wait(), timeout=5)
    finally:
        served.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await served
    return status, frames


def _types(frames: list[bytes]) -> list[str]:
    return [
        line.removeprefix("event: ")
        for frame in frames
        for line in frame.decode().splitlines()
        if line.startswith("event: ")
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/knowledge/events", [*KNOWLEDGE_FRAMES, LENS_REFRESH_EVENT]),
        ("/tasks/events", ["task.created", "agent.registered", LENS_REFRESH_EVENT]),
    ],
)
async def test_each_browser_stream_carries_only_its_scope(
    lithos_lens_config_env: Path, path: str, expected: list[str]
) -> None:
    app = create_app(load_config(lithos_lens_config_env))
    published = [_task_frame(), _agent_frame(), *_knowledge_frames(), _refresh()]

    status, frames = await _stream(app, path, 1 + len(expected), published)

    assert status == 200
    assert frames[0] == b'event: lens.status\ndata: {"status":"connected"}\n\n'
    assert _types(frames[1:]) == expected


@pytest.mark.anyio
async def test_a_knowledge_frame_reaches_the_browser_as_the_hub_has_it(
    lithos_lens_config_env: Path,
) -> None:
    app = create_app(load_config(lithos_lens_config_env))
    event = _frame("edge.upserted", KNOWLEDGE_FRAMES["edge.upserted"], "evt-9")

    _, frames = await _stream(app, "/knowledge/events", 2, [event])

    assert frames[1] == event.as_sse().encode()
    data = json.loads(frames[1].decode().split("data: ", 1)[1])
    assert data["payload"] == KNOWLEDGE_FRAMES["edge.upserted"]
    assert (data["scope"], data["requires_refresh"]) == ("knowledge", False)


@pytest.mark.anyio
async def test_the_knowledge_stream_keeps_alive_and_unsubscribes_on_the_way_out(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    metric_reader: InMemoryMetricReader,
) -> None:
    monkeypatch.setattr(event_streams, "SSE_KEEPALIVE_S", 0.05)
    app = create_app(load_config(lithos_lens_config_env))

    _, frames = await _stream(app, "/knowledge/events", 2)

    assert frames[1] == b": keepalive\n\n"
    assert metric_value(metric_reader, "lens_knowledge_event_subscribers").value == 0
    assert metric_value(metric_reader, "lens_event_subscribers").value == 0


@pytest.mark.anyio
async def test_the_knowledge_stream_refuses_past_the_ceiling_like_the_task_stream(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(events, "MAX_EVENT_SUBSCRIBERS", 0)
    app = create_app(load_config(lithos_lens_config_env))

    status, frames = await _stream(app, "/knowledge/events", 1)

    assert status == 503
    assert b"poll" in frames[0].lower()


@pytest.mark.anyio
async def test_the_knowledge_stream_is_not_metered_and_not_traced(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked tab is not a render: a saturated render budget still lets the
    stream open, as it does ``/tasks/events``; nor does an hours-long span
    belong in the latency histograms."""
    monkeypatch.setattr(web, "MAX_CONCURRENT_RENDERS", 0)
    app = create_app(load_config(lithos_lens_config_env))

    status, frames = await _stream(app, "/knowledge/events", 1)

    assert status == 200 and b"lens.status" in frames[0]
    excluded = parse_excluded_urls(TRACE_EXCLUDED_URLS)
    assert excluded.url_disabled("/knowledge/events")
    assert not excluded.url_disabled("/knowledge/graph")


def test_a_drawn_graph_page_carries_a_hidden_refresh_pill_to_itself(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env) as client:
        drawn = client.get(f"{ROUTE}?focus={PLAN}&selected={ROLLBACK}").text
        picker = client.get(ROUTE).text

    (pill,) = re.findall(r"<a [^>]*data-kgraph-refresh-pill[^>]*>[^<]*</a>", drawn)
    assert "hidden" in pill and "graph changed — refresh" in pill
    href = re.search(r'href="([^"]*)"', pill)
    assert href is not None
    assert f"focus={PLAN}" in href.group(1) and f"selected={ROLLBACK}" in href.group(1)
    assert "data-kgraph-refresh-pill" not in picker
