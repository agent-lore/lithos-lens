"""K2 slice 2 — the note facts cache behind every graph node (``knowledge_facts.py``).

Every node's title, type, status, namespace, confidence and lede come from a
cached ``lithos_read(id, max_length=1)``. These tests pin what that cache
promises (PRD Testing Decisions, "Note facts cache"): the reads run under the
injected gate, a render spends at most its cap and says so, a missing note
becomes a ghost, and the note events patch what they can — the title — and
mark the rest stale, so a note quarantined behind an unchanged title is
re-read, once, on the next draw. Plus the four config knobs S2 adds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from lithos_lens.config import (
    DEFAULT_KNOWLEDGE_GRAPH_DEFAULT_DEPTH,
    DEFAULT_KNOWLEDGE_GRAPH_FOCUS_MAX_NODES,
    DEFAULT_KNOWLEDGE_GRAPH_GLOBAL_MAX_NODES,
    DEFAULT_KNOWLEDGE_GRAPH_MIN_WEIGHT_DEFAULT,
    DEFAULT_KNOWLEDGE_GRAPH_NOTE_FACTS_TTL_S,
    DEFAULT_KNOWLEDGE_GRAPH_TITLE_FANOUT_CAP,
    load_config,
)
from lithos_lens.errors import ConfigError
from lithos_lens.fake_knowledge_dataset import DANGLING_NOTE_ID
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge_facts import (
    DEFAULT_NOTE_FACTS_TTL_S,
    DEFAULT_TITLE_FANOUT_CAP,
    NoteFacts,
    NoteFactsCache,
)
from lithos_lens.knowledge_graph import (
    DEFAULT_FOCUS_MAX_NODES,
    DEFAULT_GLOBAL_MAX_NODES,
)
from lithos_lens.knowledge_graph_view import DEFAULT_DEPTH, DEFAULT_MIN_WEIGHT
from lithos_lens.tasks import NoteRecord

pytestmark = pytest.mark.anyio

PLAN = "note-influx-plan"
CAPACITY = "note-influx-capacity"
ROLLBACK = "note-influx-rollback"
LEGACY = "note-influx-legacy-ingest"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Ticks:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class CountingGate(asyncio.Semaphore):
    """A semaphore that records how many holders it has had, and at most at once."""

    def __init__(self, permits: int) -> None:
        super().__init__(permits)
        self.held = 0
        self.entered = 0
        self.max_held = 0

    async def __aenter__(self) -> None:
        await super().__aenter__()
        self.held += 1
        self.entered += 1
        self.max_held = max(self.max_held, self.held)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.held -= 1
        await super().__aexit__(exc_type, exc, tb)


class Reader:
    """The injected read, bound to the fake's ``read_note(id, max_length=1)``,
    counting calls per id and checking each one runs inside the gate."""

    def __init__(self, fake: FakeLithosClient, gate: CountingGate) -> None:
        self.fake = fake
        self.gate = gate
        self.calls: list[str] = []
        self.fail: set[str] = set()

    async def __call__(self, note_id: str) -> NoteRecord | None:
        assert self.gate.held > 0, "read outside the gate"
        self.calls.append(note_id)
        await asyncio.sleep(0)
        if note_id in self.fail:
            raise RuntimeError("lithos_read timed out")
        return await self.fake.read_note(note_id, max_length=1)


@pytest.fixture
def fake() -> FakeLithosClient:
    return FakeLithosClient()


@pytest.fixture
def gate() -> CountingGate:
    return CountingGate(8)


@pytest.fixture
def reader(fake: FakeLithosClient, gate: CountingGate) -> Reader:
    return Reader(fake, gate)


@pytest.fixture
def ticks() -> Ticks:
    return Ticks()


@pytest.fixture
def cache(reader: Reader, gate: CountingGate, ticks: Ticks) -> NoteFactsCache:
    return NoteFactsCache(reader, lambda: gate, ticks=ticks)


def quarantine(fake: FakeLithosClient, note_id: str, lede: str) -> None:
    """What misleading feedback does upstream: status quarantined, a new
    summary, the title untouched — swapped into the fake's frozen dataset."""
    note = fake.dataset.notes[note_id]
    metadata = {**note.metadata, "status": "quarantined", "summaries": {"short": lede}}
    notes = {**fake.dataset.notes, note_id: replace(note, metadata=metadata)}
    fake.dataset = replace(fake.dataset, notes=notes)


# ── defaults ───────────────────────────────────────────────────────────


def test_module_defaults_mirror_the_config_defaults() -> None:
    assert DEFAULT_NOTE_FACTS_TTL_S == DEFAULT_KNOWLEDGE_GRAPH_NOTE_FACTS_TTL_S == 3600
    assert DEFAULT_TITLE_FANOUT_CAP == DEFAULT_KNOWLEDGE_GRAPH_TITLE_FANOUT_CAP == 300
    assert DEFAULT_FOCUS_MAX_NODES == DEFAULT_KNOWLEDGE_GRAPH_FOCUS_MAX_NODES == 250
    assert DEFAULT_DEPTH == DEFAULT_KNOWLEDGE_GRAPH_DEFAULT_DEPTH == 1
    assert DEFAULT_GLOBAL_MAX_NODES == DEFAULT_KNOWLEDGE_GRAPH_GLOBAL_MAX_NODES == 500
    assert DEFAULT_MIN_WEIGHT == DEFAULT_KNOWLEDGE_GRAPH_MIN_WEIGHT_DEFAULT == 0.1


# ── reads ──────────────────────────────────────────────────────────────


async def test_reads_carry_the_frontmatter_facts_through_build_note_metadata(
    cache: NoteFactsCache, reader: Reader
) -> None:
    batch = await cache.lookup([PLAN])

    answer = batch.for_id(PLAN)
    assert answer.state == "ok"
    assert answer.facts == NoteFacts(
        title="Influx migration plan",
        note_type="summary",
        status="active",
        namespace="plans",
        confidence="90%",
        lede="Cut ingest over first, backfill after; abort via the feature gate.",
    )
    assert answer.label == "Influx migration plan"
    assert reader.calls == [PLAN]


async def test_the_fan_out_runs_under_the_injected_gate(
    fake: FakeLithosClient, ticks: Ticks
) -> None:
    gate = CountingGate(2)
    reader = Reader(fake, gate)
    cache = NoteFactsCache(reader, lambda: gate, ticks=ticks)

    batch = await cache.lookup([PLAN, CAPACITY, ROLLBACK, LEGACY])

    assert sorted(reader.calls) == sorted([PLAN, CAPACITY, ROLLBACK, LEGACY])
    # Every read entered the gate (the reader asserts it held a permit), and
    # never more at once than the gate's two permits.
    assert gate.entered == 4
    assert gate.max_held == 2
    assert batch.tally.reads == 4


async def test_a_cache_hit_costs_no_read_and_the_ttl_expires_it(
    cache: NoteFactsCache, reader: Reader, ticks: Ticks
) -> None:
    await cache.lookup([PLAN])
    batch = await cache.lookup([PLAN])
    assert reader.calls == [PLAN]
    assert (batch.tally.hits, batch.tally.reads) == (1, 0)

    ticks.now += DEFAULT_NOTE_FACTS_TTL_S
    batch = await cache.lookup([PLAN])
    assert reader.calls == [PLAN, PLAN]
    assert batch.for_id(PLAN).state == "ok"


async def test_past_the_cap_nodes_are_id_labelled_and_the_batch_says_so(
    cache: NoteFactsCache, reader: Reader
) -> None:
    ids = [PLAN, CAPACITY, ROLLBACK, LEGACY]
    batch = await cache.lookup(ids, cap=2)

    assert reader.calls == [PLAN, CAPACITY]  # the front of the ranked list
    assert batch.capped_at == 2
    assert batch.tally.capped == 2
    for node_id in (ROLLBACK, LEGACY):
        answer = batch.for_id(node_id)
        assert (answer.state, answer.facts, answer.label) == ("unread", None, node_id)
    # Under the cap nothing is said.
    assert (await cache.lookup([PLAN, CAPACITY], cap=2)).capped_at == 0


async def test_doc_not_found_is_a_ghost_cached_under_the_ttl(
    cache: NoteFactsCache, reader: Reader, ticks: Ticks
) -> None:
    batch = await cache.lookup([DANGLING_NOTE_ID])

    ghost = batch.for_id(DANGLING_NOTE_ID)
    assert ghost.state == "missing"
    assert ghost.label == DANGLING_NOTE_ID[:8]
    assert batch.tally.missing == 1
    assert batch.tally.failed == 0

    again = await cache.lookup([DANGLING_NOTE_ID])
    assert again.for_id(DANGLING_NOTE_ID).state == "missing"
    assert reader.calls == [DANGLING_NOTE_ID]

    ticks.now += DEFAULT_NOTE_FACTS_TTL_S
    await cache.lookup([DANGLING_NOTE_ID])
    assert reader.calls == [DANGLING_NOTE_ID, DANGLING_NOTE_ID]


async def test_a_none_read_is_missing_too(gate: CountingGate, ticks: Ticks) -> None:
    async def read(note_id: str) -> NoteRecord | None:
        return None

    cache = NoteFactsCache(read, lambda: gate, ticks=ticks)
    assert (await cache.lookup(["x"])).for_id("x").state == "missing"


async def test_a_failed_read_is_never_cached(
    cache: NoteFactsCache,
    reader: Reader,
    ticks: Ticks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reader.fail = {PLAN, CAPACITY}
    batch = await cache.lookup([PLAN, CAPACITY, ROLLBACK])

    assert batch.for_id(PLAN).state == "unread"
    assert batch.tally.failed == 2
    # One aggregate warning for the render, not one per id.
    failures = [r for r in caplog.records if "facts read failed" in r.getMessage()]
    assert len(failures) == 1
    assert "2 of 3" in failures[0].getMessage()

    reader.fail = set()
    batch = await cache.lookup([PLAN])
    assert batch.for_id(PLAN).state == "ok"
    assert reader.calls.count(PLAN) == 2

    # With last facts held, a failed re-read keeps them as pending.
    ticks.now += DEFAULT_NOTE_FACTS_TTL_S
    reader.fail = {PLAN}
    answer = (await cache.lookup([PLAN])).for_id(PLAN)
    assert answer.state == "pending"
    assert answer.facts is not None and answer.facts.title == "Influx migration plan"


# ── event patches ──────────────────────────────────────────────────────


async def test_note_updated_patches_the_title_without_a_read_and_marks_the_rest_stale(
    cache: NoteFactsCache, reader: Reader
) -> None:
    await cache.lookup([PLAN])

    assert cache.apply_note_event(
        "note.updated", {"id": PLAN, "title": "Influx plan v2", "path": "p.md"}
    )
    assert reader.calls == [PLAN]

    # Past the cap (0 reads left), the patched title shows with the last
    # facts, marked pending — no read was needed for the title.
    batch = await cache.lookup([PLAN], cap=0)
    answer = batch.for_id(PLAN)
    assert answer.state == "pending"
    assert answer.facts is not None
    assert answer.facts.title == "Influx plan v2"
    assert answer.facts.status == "active"
    assert answer.label == "Influx plan v2"
    assert (batch.tally.capped, batch.capped_at) == (1, 0)
    assert reader.calls == [PLAN]

    # Stale, not a hit: the next draw under the cap re-reads it.
    batch = await cache.lookup([PLAN])
    assert reader.calls == [PLAN, PLAN]
    assert (batch.tally.hits, batch.tally.reads) == (0, 1)
    assert batch.for_id(PLAN).state == "ok"


async def test_quarantine_behind_an_unchanged_title_is_re_read_once_on_the_next_draw(
    cache: NoteFactsCache, reader: Reader, fake: FakeLithosClient
) -> None:
    await cache.lookup([PLAN, CAPACITY])
    quarantine(fake, PLAN, "Quarantined: misleading feedback on the cutover window.")

    # Without an event the cache keeps serving what it read.
    before = (await cache.lookup([PLAN])).for_id(PLAN)
    assert before.facts is not None and before.facts.status == "active"

    title = fake.dataset.notes[PLAN].title
    assert cache.apply_note_event("note.updated", {"id": PLAN, "title": title})
    reader.calls.clear()

    batch = await cache.lookup([PLAN, CAPACITY])

    assert reader.calls == [PLAN]  # exactly one read, for that node only
    answer = batch.for_id(PLAN)
    assert answer.state == "ok"
    assert answer.facts is not None
    assert answer.facts.status == "quarantined"
    assert (
        answer.facts.lede == "Quarantined: misleading feedback on the cutover window."
    )
    assert answer.facts.title == title


async def test_note_renamed_leaves_facts_and_freshness_unchanged(
    cache: NoteFactsCache, reader: Reader
) -> None:
    first = (await cache.lookup([PLAN])).for_id(PLAN)

    renamed = {"id": PLAN, "src_path": "plans/a.md", "dest_path": "plans/b.md"}
    assert cache.apply_note_event("note.renamed", renamed) is False

    batch = await cache.lookup([PLAN])
    assert batch.for_id(PLAN) == first
    assert batch.tally.hits == 1
    assert reader.calls == [PLAN]


async def test_note_deleted_is_a_ghost_on_the_next_draw_without_a_read(
    cache: NoteFactsCache, reader: Reader
) -> None:
    await cache.lookup([PLAN])
    assert cache.apply_note_event("note.deleted", {"id": PLAN})

    answer = (await cache.lookup([PLAN])).for_id(PLAN)
    assert (answer.state, answer.label) == ("missing", PLAN[:8])
    assert reader.calls == [PLAN]

    # Deleted marks an id missing whether or not it was cached.
    assert cache.apply_note_event("note.deleted", {"id": ROLLBACK})
    assert (await cache.lookup([ROLLBACK])).for_id(ROLLBACK).state == "missing"
    assert ROLLBACK not in reader.calls


async def test_created_on_a_missing_id_clears_the_ghost(
    cache: NoteFactsCache, reader: Reader
) -> None:
    await cache.lookup([DANGLING_NOTE_ID])
    assert cache.apply_note_event(
        "note.created", {"id": DANGLING_NOTE_ID, "title": "Archived sizing"}
    )

    answer = (await cache.lookup([DANGLING_NOTE_ID], cap=0)).for_id(DANGLING_NOTE_ID)
    assert answer.state == "pending"
    assert answer.label == "Archived sizing"


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("note.updated", {"id": PLAN, "title": "never read"}),  # not cached
        ("note.created", {"id": PLAN, "title": "never read"}),
        ("note.updated", {"path": "plans/influx-migration.md"}),  # watcher, no id
        ("note.deleted", {"id": ""}),
        ("note.deleted", {"id": 7}),
        ("edge.upserted", {"id": PLAN}),
    ],
)
async def test_events_that_patch_nothing_answer_false(
    cache: NoteFactsCache, reader: Reader, event_type: str, payload: dict[str, Any]
) -> None:
    assert cache.apply_note_event(event_type, payload) is False
    assert (await cache.lookup([PLAN])).for_id(PLAN).state == "ok"
    assert reader.calls == [PLAN]


async def test_a_read_in_flight_does_not_unmark_an_event_that_landed_during_it(
    fake: FakeLithosClient, ticks: Ticks
) -> None:
    gate = CountingGate(8)
    release = asyncio.Event()
    calls: list[str] = []

    async def read(note_id: str) -> NoteRecord | None:
        calls.append(note_id)
        if len(calls) == 2:
            await release.wait()
        return await fake.read_note(note_id, max_length=1)

    cache = NoteFactsCache(read, lambda: gate, ticks=ticks)
    await cache.lookup([PLAN])
    ticks.now += DEFAULT_NOTE_FACTS_TTL_S

    pending = asyncio.create_task(cache.lookup([PLAN]))
    while len(calls) < 2:
        await asyncio.sleep(0)
    assert cache.apply_note_event("note.updated", {"id": PLAN, "title": "Renamed"})
    release.set()
    await pending

    await cache.lookup([PLAN])
    assert calls == [PLAN, PLAN, PLAN]


class HeldRead:
    """A read that fetches its answer at once and returns it only on release:
    the response Lithos sent before an event, still in flight after it."""

    def __init__(self, fake: FakeLithosClient) -> None:
        self.fake = fake
        self.calls: list[str] = []
        self.release = asyncio.Event()
        self.hold_first = True

    async def __call__(self, note_id: str) -> NoteRecord | None:
        self.calls.append(note_id)
        try:
            answer = await self.fake.read_note(note_id, max_length=1)
        except Exception as exc:
            if self.hold_first:
                self.hold_first = False
                await self.release.wait()
            raise exc
        if self.hold_first:
            self.hold_first = False
            await self.release.wait()
        return answer

    async def started(self) -> None:
        while not self.calls:
            await asyncio.sleep(0)


async def test_an_update_during_the_first_read_of_an_id_is_not_lost(
    fake: FakeLithosClient, gate: CountingGate, ticks: Ticks
) -> None:
    """The quarantine lands while the id's FIRST read — answered before it —
    is still in flight: that pre-quarantine answer must not become a fresh
    entry, so the next draw re-reads it once and shows the quarantine."""
    read = HeldRead(fake)
    cache = NoteFactsCache(read, lambda: gate, ticks=ticks)

    first = asyncio.create_task(cache.lookup([PLAN]))
    await read.started()
    title = fake.dataset.notes[PLAN].title
    quarantine(fake, PLAN, "Quarantined while the first read was in flight.")
    # Not cached yet: the event stays a public no-op...
    assert cache.apply_note_event("note.updated", {"id": PLAN, "title": title}) is False
    read.release.set()
    stale_answer = (await first).for_id(PLAN)
    assert stale_answer.facts is not None and stale_answer.facts.status == "active"

    # ...but the answer read before it is not cached as fresh.
    batch = await cache.lookup([PLAN])
    assert read.calls == [PLAN, PLAN]
    assert batch.tally.reads == 1
    answer = batch.for_id(PLAN)
    assert answer.facts is not None
    assert answer.facts.status == "quarantined"
    assert answer.facts.lede == "Quarantined while the first read was in flight."
    # And now it is cached: a third draw is a hit.
    assert (await cache.lookup([PLAN])).tally.hits == 1


async def test_a_create_during_the_first_missing_read_is_not_lost(
    fake: FakeLithosClient, gate: CountingGate, ticks: Ticks
) -> None:
    read = HeldRead(fake)
    cache = NoteFactsCache(read, lambda: gate, ticks=ticks)

    first = asyncio.create_task(cache.lookup([DANGLING_NOTE_ID]))
    await read.started()
    created = {"id": DANGLING_NOTE_ID, "title": "Archived sizing"}
    assert cache.apply_note_event("note.created", created) is False
    read.release.set()
    assert (await first).for_id(DANGLING_NOTE_ID).state == "missing"

    await cache.lookup([DANGLING_NOTE_ID])
    assert read.calls == [DANGLING_NOTE_ID, DANGLING_NOTE_ID]


# ── config─────────────────────────────────────────────────────────────

_KNOBS = {
    "graph_focus_max_nodes": (250, 2000),
    "graph_default_depth": (1, 2),
    "graph_note_facts_ttl_s": (3600, 86_400),
    "graph_title_fanout_cap": (300, 2000),
    "graph_global_max_nodes": (500, 2000),
}


def _set_knowledge(config_path: Path, line: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace("[lithos-lens.knowledge]", f"[lithos-lens.knowledge]\n{line}", 1)
        if "[lithos-lens.knowledge]" in text
        else f"{text}\n[lithos-lens.knowledge]\n{line}\n",
        encoding="utf-8",
    )


def _knobs(config_path: Path) -> dict[str, int]:
    knowledge = load_config(config_path).knowledge
    return {name: getattr(knowledge, name) for name in _KNOBS}


def test_the_graph_knobs_default_and_read_from_toml(
    lithos_lens_config_env: Path,
) -> None:
    assert _knobs(lithos_lens_config_env) == {
        name: default for name, (default, _) in _KNOBS.items()
    }
    for name, (_, ceiling) in _KNOBS.items():
        _set_knowledge(lithos_lens_config_env, f"{name} = {ceiling}")
    assert _knobs(lithos_lens_config_env) == {
        name: ceiling for name, (_, ceiling) in _KNOBS.items()
    }


def _out_of_range() -> Iterator[str]:
    for name, (_, ceiling) in _KNOBS.items():
        yield f"{name} = 0"
        yield f"{name} = {ceiling + 1}"


@pytest.mark.parametrize("line", list(_out_of_range()))
def test_the_graph_knobs_reject_out_of_range(
    lithos_lens_config_env: Path, line: str
) -> None:
    _set_knowledge(lithos_lens_config_env, line)
    with pytest.raises(ConfigError, match=line.split(" ")[0]):
        load_config(lithos_lens_config_env)


def test_the_graph_knobs_take_env_overrides(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_default_depth = 1")
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_FOCUS_MAX_NODES", "120")
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_DEFAULT_DEPTH", "2")
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_NOTE_FACTS_TTL_S", "60")
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_TITLE_FANOUT_CAP", "150")
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_GLOBAL_MAX_NODES", "90")
    assert _knobs(lithos_lens_config_env) == {
        "graph_focus_max_nodes": 120,
        "graph_default_depth": 2,
        "graph_note_facts_ttl_s": 60,
        "graph_title_fanout_cap": 150,
        "graph_global_max_nodes": 90,
    }

    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_DEFAULT_DEPTH", "3")
    with pytest.raises(ConfigError, match="DEFAULT_DEPTH must be <= 2"):
        load_config(lithos_lens_config_env)


# ── graph_min_weight_default: the one float knob ──────────────────────


def _min_weight(config_path: Path) -> float:
    return load_config(config_path).knowledge.graph_min_weight_default


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("graph_min_weight_default = 0.0", 0.0),
        ("graph_min_weight_default = 0.35", 0.35),
        ("graph_min_weight_default = 1", 1.0),
    ],
)
def test_min_weight_default_reads_a_number_from_toml(
    lithos_lens_config_env: Path, line: str, expected: float
) -> None:
    assert _min_weight(lithos_lens_config_env) == 0.1
    _set_knowledge(lithos_lens_config_env, line)
    assert _min_weight(lithos_lens_config_env) == expected


@pytest.mark.parametrize("value", ["-0.1", "1.5", "true", '"0.2"', "nan", "inf"])
def test_min_weight_default_rejects_what_is_not_a_weight(
    lithos_lens_config_env: Path, value: str
) -> None:
    _set_knowledge(lithos_lens_config_env, f"graph_min_weight_default = {value}")
    with pytest.raises(ConfigError, match="graph_min_weight_default"):
        load_config(lithos_lens_config_env)


def test_min_weight_default_takes_an_env_override(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_MIN_WEIGHT_DEFAULT", "0.25")
    assert _min_weight(lithos_lens_config_env) == 0.25

    for bad in ("1.01", "-1", "nan", "heavy"):
        monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_MIN_WEIGHT_DEFAULT", bad)
        with pytest.raises(ConfigError, match="MIN_WEIGHT_DEFAULT"):
            load_config(lithos_lens_config_env)
