"""K2 slice 1 — the knowledge edge-table snapshot (``knowledge_edges.py``).

Every graph read in K2 is served from one snapshot of the whole Lithos edge
table, so these tests pin what that snapshot promises: its indexes and facets
say what its rows say, concurrent readers share one fetch, the TTL is the
staleness bound, ``edge.upserted`` patches it, a table over the bound is
refused while filtered reads still work, and a failed refetch keeps serving
the last good table marked stale. Plus the fake's knowledge edge dataset and
the two config knobs the snapshot reads.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from lithos_lens.config import (
    DEFAULT_KNOWLEDGE_GRAPH_EDGE_TABLE_MAX_EDGES,
    DEFAULT_KNOWLEDGE_GRAPH_EDGE_TABLE_TTL_S,
    load_config,
)
from lithos_lens.errors import ConfigError
from lithos_lens.fake_dataset import FakeLithosDataset, demo_dataset
from lithos_lens.fake_knowledge_dataset import DANGLING_NOTE_ID, knowledge_edge_rows
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge_edge_types import (
    KNOWN_KNOWLEDGE_EDGE_TYPES,
    EdgeDirection,
    direction_of,
)
from lithos_lens.knowledge_edges import (
    DEFAULT_EDGE_TABLE_MAX_EDGES,
    DEFAULT_EDGE_TABLE_TTL_S,
    EdgeTable,
    EdgeTableRefusal,
    EdgeTableSnapshot,
    KnowledgeEdge,
    normalize_edge_list,
    normalize_knowledge_edge,
)
from tests.conftest import load_contract, metric_snapshot, snapshot_value

pytestmark = pytest.mark.anyio

_T0 = datetime(2026, 10, 7, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class StepClock:
    """The table's two clocks, moved by hand: ``advance`` moves both, as real
    time does; ``skew`` moves only the wall clock (an NTP step)."""

    def __init__(self) -> None:
        self.now = _T0
        self.ticks_now = 500.0

    def __call__(self) -> datetime:
        return self.now

    def ticks(self) -> float:
        return self.ticks_now

    def advance(self, **delta: float) -> None:
        step = timedelta(**delta)
        self.now += step
        self.ticks_now += step.total_seconds()

    def skew(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def edge(
    edge_id: str,
    from_id: str,
    to_id: str,
    edge_type: str = "supports",
    *,
    namespace: str = "influx",
    conflict_state: str | None = None,
    weight: float | None = 0.8,
) -> KnowledgeEdge:
    return KnowledgeEdge(
        edge_id=edge_id,
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        weight=weight,
        namespace=namespace,
        created_at="2026-10-01T00:00:00+00:00",
        updated_at="2026-10-01T00:00:00+00:00",
        provenance_actor="lithos-enrich",
        provenance_type="inferred",
        evidence='{"rationale": "r", "model": "m", "confidence": 0.8}',
        conflict_state=conflict_state,
    )


class Fetcher:
    """A recorded ``(type, namespace)`` read, optionally gated or failing."""

    def __init__(
        self,
        rows: Sequence[KnowledgeEdge] = (),
        *,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.rows = list(rows)
        self.gate = gate
        self.fail = False
        self.calls: list[tuple[str | None, str | None]] = []

    async def __call__(
        self, edge_type: str | None, namespace: str | None
    ) -> list[KnowledgeEdge]:
        self.calls.append((edge_type, namespace))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("lithos_edge_list failed")
        return [
            row
            for row in self.rows
            if (edge_type is None or row.type == edge_type)
            and (namespace is None or row.namespace == namespace)
        ]


def _demo_rows() -> tuple[KnowledgeEdge, ...]:
    return normalize_edge_list({"results": list(knowledge_edge_rows())})


def _table(fetcher: Fetcher, clock: StepClock, **kwargs: Any) -> EdgeTable:
    return EdgeTable(fetcher, clock=clock, ticks=clock.ticks, **kwargs)


async def _snapshot(table: EdgeTable) -> EdgeTableSnapshot:
    state = await table.read()
    assert isinstance(state, EdgeTableSnapshot)
    return state


# ── defaults and normalization ─────────────────────────────────────────


def test_module_defaults_mirror_the_config_defaults() -> None:
    """One decision each, stated in two places so Foundation need not import
    Config; pinned so they cannot drift apart."""
    assert DEFAULT_EDGE_TABLE_TTL_S == DEFAULT_KNOWLEDGE_GRAPH_EDGE_TABLE_TTL_S == 300
    assert (
        DEFAULT_EDGE_TABLE_MAX_EDGES
        == DEFAULT_KNOWLEDGE_GRAPH_EDGE_TABLE_MAX_EDGES
        == 50_000
    )


def test_nulls_stay_none_and_evidence_stays_the_raw_string() -> None:
    raw = load_contract("lithos_edge_list")["responses"]["success"]["results"]
    rows = normalize_edge_list({"results": raw})

    assert [row.edge_id for row in rows] == [row["edge_id"] for row in raw]
    unresolved = next(
        row for row in rows if row.type == "contradicts" and not row.conflict_state
    )
    assert unresolved.conflict_state is None
    consolidation = next(row for row in rows if row.provenance_type == "consolidation")
    assert consolidation.provenance_actor is None
    assert consolidation.evidence is None
    inferred = next(row for row in rows if row.provenance_type == "inferred")
    assert inferred.evidence == raw[0]["evidence"]
    assert not any(row.partial for row in rows)


def test_a_row_without_its_identity_is_dropped_not_guessed() -> None:
    good = dict(load_contract("lithos_edge_list")["responses"]["success"]["results"][0])
    assert normalize_knowledge_edge({**good, "edge_id": ""}) is None
    assert normalize_knowledge_edge({**good, "to_id": None}) is None
    assert normalize_knowledge_edge({**good, "namespace": 7}) is None
    odd = normalize_knowledge_edge({**good, "weight": "heavy", "evidence": 3})
    assert odd is not None and (odd.weight, odd.evidence) == (None, None)
    assert normalize_edge_list({"results": [good, "junk", {**good, "type": None}]}) == (
        normalize_knowledge_edge(good),
    )
    assert normalize_edge_list({}) == ()


# ── indexes and facets ─────────────────────────────────────────────────


def test_indexes_agree_with_the_rows() -> None:
    rows = _demo_rows() + (edge("edge_selfloop0001", "n-1", "n-1"),)
    snapshot = EdgeTableSnapshot(rows=rows, as_of=_T0)

    for row in rows:
        assert row in snapshot.edges_of(row.from_id)
        assert row in snapshot.edges_of(row.to_id)
        assert row in snapshot.of_type(row.type)
        assert row in snapshot.in_namespace(row.namespace)
    for node_id, indexed in snapshot.by_endpoint.items():
        assert all(node_id in row.endpoints for row in indexed)
    for edge_type, indexed in snapshot.by_type.items():
        assert all(row.type == edge_type for row in indexed)
    for namespace, indexed in snapshot.by_namespace.items():
        assert all(row.namespace == namespace for row in indexed)
    # Each row once per index (a self-loop is not listed twice under its node).
    assert sum(len(group) for group in snapshot.by_type.values()) == len(rows)
    assert sum(len(group) for group in snapshot.by_namespace.values()) == len(rows)
    assert snapshot.edges_of("n-1") == (rows[-1],)
    assert snapshot.edges_of("no-such-note") == ()
    assert snapshot.edges_of(DANGLING_NOTE_ID), "the dangling endpoint is indexed"


def test_a_symmetric_row_stored_reversed_is_held_as_stored() -> None:
    """``lithos_edge_upsert`` stores a caller's endpoints as given, so a
    valid symmetric row can arrive ``from_id > to_id``: the snapshot keeps it
    as stored, indexes it under both endpoints, counts it, and the type alone
    says it has no arrowhead."""
    reversed_related = edge("edge_rev0related", "note-z", "note-a", "related_to")
    reversed_contradiction = edge(
        "edge_rev0contra1", "note-z", "note-a", "contradicts", conflict_state=None
    )
    rows = (reversed_related, reversed_contradiction)
    snapshot = EdgeTableSnapshot(rows=rows, as_of=_T0)

    assert snapshot.rows == rows
    assert all((row.from_id, row.to_id) == ("note-z", "note-a") for row in rows)
    assert snapshot.edges_of("note-a") == snapshot.edges_of("note-z") == rows
    assert snapshot.facets.unresolved_contradictions == 1
    assert all(direction_of(row) is EdgeDirection.SYMMETRIC for row in rows)
    assert not any(direction_of(row).has_arrowhead for row in rows)


def test_facets_count_what_the_rows_contain() -> None:
    rows = _demo_rows()
    facets = EdgeTableSnapshot(rows=rows, as_of=_T0).facets

    assert dict(facets.types) == Counter(row.type for row in rows)
    assert dict(facets.namespaces) == Counter(row.namespace for row in rows)
    # Most rows first, ties by name: the picker's order.
    assert list(facets.types) == sorted(
        facets.types, key=lambda name: (-facets.types[name], name)
    )
    # Two contradictions are unresolved (NULL); the superseded one is not.
    assert facets.unresolved_contradictions == 2
    assert facets.types["contradicts"] == 3


def test_every_resolution_counts_as_resolved() -> None:
    rows = tuple(
        edge(f"edge_c{index}", "a", "b", "contradicts", conflict_state=state)
        for index, state in enumerate(
            (None, "accepted_dual", "superseded", "refuted", "merged")
        )
    ) + (edge("edge_s", "a", "b", "supports"),)
    facets = EdgeTableSnapshot(rows=rows, as_of=_T0).facets
    assert facets.unresolved_contradictions == 1


# ── single-flight and TTL ──────────────────────────────────────────────


async def test_two_concurrent_reads_make_one_upstream_call() -> None:
    gate = asyncio.Event()
    clock = StepClock()
    fetcher = Fetcher(_demo_rows(), gate=gate)
    table = _table(fetcher, clock)

    readers = [asyncio.create_task(table.read()) for _ in range(2)]
    await asyncio.sleep(0)
    gate.set()
    first, second = await asyncio.gather(*readers)

    assert fetcher.calls == [(None, None)]
    assert first is second
    assert isinstance(first, EdgeTableSnapshot) and first.as_of == _T0
    assert table.fetches == 1


async def test_a_live_snapshot_is_served_and_ttl_expiry_refetches() -> None:
    clock = StepClock()
    fetcher = Fetcher(_demo_rows())
    table = _table(fetcher, clock, ttl_s=300)

    first = await _snapshot(table)
    clock.advance(seconds=299)
    assert await table.read() is first
    assert len(fetcher.calls) == 1

    fetcher.rows.append(edge("edge_new000000001", "a", "b"))
    clock.advance(seconds=1)
    second = await _snapshot(table)

    assert len(fetcher.calls) == 2
    assert second.as_of == _T0 + timedelta(seconds=300)
    assert len(second.rows) == len(first.rows) + 1


async def test_a_wall_clock_step_back_does_not_extend_the_ttl() -> None:
    clock = StepClock()
    fetcher = Fetcher(_demo_rows())
    table = _table(fetcher, clock, ttl_s=300)

    await table.read()
    clock.skew(hours=-1)
    clock.advance(seconds=301)
    await table.read()

    assert len(fetcher.calls) == 2


async def test_age_reads_the_monotonic_clock_from_the_last_fetch() -> None:
    clock = StepClock()
    table = _table(Fetcher(_demo_rows()), clock)
    assert table.age_seconds() == 0.0

    await table.read()
    clock.advance(seconds=42)
    clock.skew(hours=-3)
    assert table.age_seconds() == 42.0


# ── patching from edge.upserted ────────────────────────────────────────


def _upsert(**fields: Any) -> dict[str, Any]:
    payload = {
        "edge_id": "edge_patched00001",
        "from_id": "note-influx-plan",
        "to_id": "note-influx-rollback",
        "type": "supports",
        "namespace": "influx",
        "conflict_state": None,
    }
    payload.update(fields)
    return payload


async def test_an_upsert_inserts_a_new_row_as_partial() -> None:
    clock = StepClock()
    table = _table(Fetcher(_demo_rows()), clock)
    before = await _snapshot(table)

    assert table.apply_upsert(_upsert()) is True
    after = table.current
    assert isinstance(after, EdgeTableSnapshot)

    assert len(after.rows) == len(before.rows) + 1
    inserted = after.rows[-1]
    assert inserted == KnowledgeEdge(
        edge_id="edge_patched00001",
        from_id="note-influx-plan",
        to_id="note-influx-rollback",
        type="supports",
        weight=None,
        namespace="influx",
        partial=True,
    )
    # The indexes and facets are rebuilt with it.
    assert inserted in after.edges_of("note-influx-rollback")
    assert after.facets.types["supports"] == before.facets.types["supports"] + 1
    assert after.as_of == before.as_of and after.partial_rows == 1
    # A patch is not a fetch, and the live snapshot stays live.
    assert await table.read() is after


async def test_an_upsert_replaces_conflict_state_on_an_existing_row() -> None:
    clock = StepClock()
    table = _table(Fetcher(_demo_rows()), clock)
    before = await _snapshot(table)
    target = next(
        row
        for row in before.rows
        if row.type == "contradicts" and row.conflict_state is None
    )

    table.apply_upsert(
        _upsert(
            edge_id=target.edge_id,
            from_id=target.from_id,
            to_id=target.to_id,
            type=target.type,
            namespace=target.namespace,
            conflict_state="refuted",
        )
    )
    after = table.current
    assert isinstance(after, EdgeTableSnapshot)

    replaced = next(row for row in after.rows if row.edge_id == target.edge_id)
    assert replaced.conflict_state == "refuted"
    assert replaced.partial is True
    # Everything the payload does not carry is kept as last known.
    assert (replaced.weight, replaced.evidence, replaced.provenance_type) == (
        target.weight,
        target.evidence,
        target.provenance_type,
    )
    assert len(after.rows) == len(before.rows)
    assert (
        after.facets.unresolved_contradictions
        == before.facets.unresolved_contradictions - 1
    )


async def test_an_upsert_re_identifies_an_existing_row() -> None:
    """Every identity field the payload carries replaces the row's own —
    both endpoints, type, namespace, conflict state — and nothing else does:
    weight, timestamps, provenance and evidence are kept as last known. The
    indexes and facets move with the row."""
    clock = StepClock()
    moved = edge("edge_move00000001", "a", "b", "supports", conflict_state="refuted")
    kept = edge("edge_keep00000001", "a", "z", "supports")
    table = _table(Fetcher([moved, kept]), clock)
    await table.read()

    table.apply_upsert(
        _upsert(
            edge_id="edge_move00000001",
            from_id="x",
            to_id="y",
            type="refines",
            namespace="ns2",
            conflict_state=None,
        )
    )
    after = table.current
    assert isinstance(after, EdgeTableSnapshot)

    expected = KnowledgeEdge(
        edge_id="edge_move00000001",
        from_id="x",
        to_id="y",
        type="refines",
        weight=moved.weight,
        namespace="ns2",
        created_at=moved.created_at,
        updated_at=moved.updated_at,
        provenance_actor=moved.provenance_actor,
        provenance_type=moved.provenance_type,
        evidence=moved.evidence,
        conflict_state=None,
        partial=True,
    )
    assert after.rows == (expected, kept)
    # Gone from every old identity's index ...
    assert after.edges_of("a") == (kept,)
    assert after.edges_of("b") == ()
    assert after.of_type("supports") == (kept,)
    assert after.in_namespace("influx") == (kept,)
    # ... and present under every new one.
    assert after.edges_of("x") == after.edges_of("y") == (expected,)
    assert after.of_type("refines") == (expected,)
    assert after.in_namespace("ns2") == (expected,)
    assert dict(after.facets.types) == {"refines": 1, "supports": 1}
    assert dict(after.facets.namespaces) == {"influx": 1, "ns2": 1}


async def test_an_upsert_is_a_no_op_with_nothing_held_or_a_bad_payload() -> None:
    clock = StepClock()
    table = _table(Fetcher(_demo_rows()), clock)

    assert table.apply_upsert(_upsert()) is False
    assert table.current is None

    before = await _snapshot(table)
    assert table.apply_upsert(_upsert(edge_id="")) is False
    assert table.apply_upsert(_upsert(to_id=None)) is False
    assert table.apply_upsert(_upsert(conflict_state=3)) is False
    assert table.current is before


async def test_a_full_fetch_replaces_partial_rows() -> None:
    clock = StepClock()
    table = _table(Fetcher(_demo_rows()), clock, ttl_s=10)
    await table.read()
    table.apply_upsert(_upsert())

    clock.advance(seconds=10)
    refreshed = await _snapshot(table)
    assert refreshed.partial_rows == 0
    assert len(refreshed.rows) == len(_demo_rows())


# ── the bound ──────────────────────────────────────────────────────────


async def test_a_table_over_the_bound_is_refused_and_filtered_reads_still_work() -> (
    None
):
    clock = StepClock()
    rows = _demo_rows()
    fetcher = Fetcher(rows)
    table = _table(fetcher, clock, max_edges=len(rows) - 1)

    state = await table.read()
    assert state == EdgeTableRefusal(
        row_count=len(rows), max_edges=len(rows) - 1, as_of=_T0
    )
    # Refused for one TTL: no refetch per request.
    assert await table.read() is state
    assert fetcher.calls == [(None, None)]

    contradictions = await table.filtered(type="contradicts", namespace="influx")
    assert {row.type for row in contradictions} == {"contradicts"}
    assert len(contradictions) == 3
    # Uncached: each filtered read goes upstream.
    await table.filtered(type="contradicts", namespace="influx")
    assert fetcher.calls[1:] == [("contradicts", "influx")] * 2
    assert await table.filtered(namespace="runbooks") == tuple(
        row for row in rows if row.namespace == "runbooks"
    )

    # Nothing held, so nothing to patch.
    assert table.apply_upsert(_upsert()) is False
    assert table.current is state


async def test_a_table_exactly_at_the_bound_is_held() -> None:
    clock = StepClock()
    rows = _demo_rows()
    table = _table(Fetcher(rows), clock, max_edges=len(rows))
    assert isinstance(await table.read(), EdgeTableSnapshot)


async def test_a_filtered_read_needs_a_filter() -> None:
    table = _table(Fetcher(_demo_rows()), StepClock())
    with pytest.raises(ValueError, match="type= and/or namespace="):
        await table.filtered()


async def test_a_refusal_lasts_exactly_one_ttl_then_the_table_is_recounted() -> None:
    """The refusal is cached for one TTL, not forever: at expiry the next
    read re-fetches, refreshes the count and ``as_of`` while still over, and
    holds a snapshot again once Lithos's table is back within the bound."""
    clock = StepClock()
    rows = list(_demo_rows())
    fetcher = Fetcher(rows)
    table = _table(fetcher, clock, ttl_s=300, max_edges=len(rows) - 1)

    first = await table.read()
    assert isinstance(first, EdgeTableRefusal)
    clock.advance(seconds=299)
    assert await table.read() is first
    assert len(fetcher.calls) == 1

    fetcher.rows.append(edge("edge_grown0000001", "a", "b"))
    clock.advance(seconds=1)
    second = await table.read()
    assert len(fetcher.calls) == 2
    assert second == EdgeTableRefusal(
        row_count=len(rows) + 1,
        max_edges=len(rows) - 1,
        as_of=_T0 + timedelta(seconds=300),
    )

    fetcher.rows = rows[:3]
    clock.advance(seconds=300)
    recovered = await _snapshot(table)
    assert len(fetcher.calls) == 3
    assert recovered.rows == tuple(rows[:3])


async def test_an_insertion_that_crosses_the_bound_refuses_the_table() -> None:
    """The bound holds however the rows arrive. At the bound a replacement
    patches as usual; one insertion more refuses the table — rows dropped,
    count named — for the rest of the TTL, with filtered reads still served,
    and the next fetch re-counts it upstream."""
    clock = StepClock()
    rows = list(_demo_rows())
    fetcher = Fetcher(rows)
    table = _table(fetcher, clock, ttl_s=300, max_edges=len(rows))
    held = await _snapshot(table)

    assert table.apply_upsert(_upsert(edge_id=rows[0].edge_id, conflict_state="merged"))
    replaced = table.current
    assert isinstance(replaced, EdgeTableSnapshot)
    assert len(replaced.rows) == len(rows)

    assert table.apply_upsert(_upsert(edge_id="edge_onetoomany01")) is True
    refused = table.current
    assert refused == EdgeTableRefusal(
        row_count=len(rows) + 1, max_edges=len(rows), as_of=held.as_of
    )
    # Later reads within the TTL serve the refusal, without a fetch ...
    clock.advance(seconds=299)
    assert await table.read() is refused
    assert len(fetcher.calls) == 1
    # ... filtered reads still work, and further patches are no-ops.
    assert len(await table.filtered(type="contradicts")) == 3
    assert table.apply_upsert(_upsert(edge_id="edge_another00001")) is False
    assert table.current is refused

    clock.advance(seconds=1)
    recounted = await _snapshot(table)
    assert recounted.rows == tuple(rows)


async def test_a_cancelled_reader_does_not_cancel_the_shared_fetch() -> None:
    """A browser that goes away mid-read must not take the fetch the other
    readers are waiting on down with it."""
    gate = asyncio.Event()
    clock = StepClock()
    fetcher = Fetcher(_demo_rows(), gate=gate)
    table = _table(fetcher, clock)

    leaving = asyncio.create_task(table.read())
    staying = asyncio.create_task(table.read())
    while not fetcher.calls:
        await asyncio.sleep(0)
    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving
    gate.set()
    result = await staying

    assert isinstance(result, EdgeTableSnapshot)
    assert fetcher.calls == [(None, None)]
    assert table.current is result
    assert await table.read() is result
    assert len(fetcher.calls) == 1


# ── failure ────────────────────────────────────────────────────────────


async def test_a_failed_refetch_serves_the_previous_snapshot_marked_stale() -> None:
    clock = StepClock()
    fetcher = Fetcher(_demo_rows())
    table = _table(fetcher, clock, ttl_s=300)
    good = await _snapshot(table)

    clock.advance(seconds=300)
    fetcher.fail = True
    stale = await _snapshot(table)

    assert stale.stale is True
    assert stale.as_of == good.as_of
    assert stale.rows == good.rows
    assert table.age_seconds() == 300.0

    # Not cached: the next read tries again, and a success clears the mark.
    clock.advance(seconds=1)
    await table.read()
    assert len(fetcher.calls) == 3
    fetcher.fail = False
    recovered = await _snapshot(table)
    assert recovered.stale is False
    assert recovered.as_of == _T0 + timedelta(seconds=301)
    assert len(fetcher.calls) == 4


async def test_a_first_fetch_failure_reaches_every_waiter() -> None:
    gate = asyncio.Event()
    fetcher = Fetcher(gate=gate)
    fetcher.fail = True
    table = _table(fetcher, StepClock())

    waiters = [asyncio.create_task(table.read()) for _ in range(2)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(result, RuntimeError) for result in results)
    assert fetcher.calls == [(None, None)]
    assert table.current is None

    # Not cached: an immediate second read asks Lithos again (no TTL passed).
    with pytest.raises(RuntimeError):
        await table.read()
    assert len(fetcher.calls) == 2

    # Lithos recovers: two concurrent readers share ONE new fetch.
    fetcher.fail = False
    fetcher.gate = asyncio.Event()
    fetcher.rows = list(_demo_rows())
    readers = [asyncio.create_task(table.read()) for _ in range(2)]
    await asyncio.sleep(0)
    fetcher.gate.set()
    first, second = await asyncio.gather(*readers)
    assert isinstance(first, EdgeTableSnapshot) and first is second
    assert len(fetcher.calls) == 3
    assert table.current is first and first.stale is False


# ── telemetry ──────────────────────────────────────────────────────────


def _edge_table_spans(spans: InMemorySpanExporter) -> list[dict[str, Any]]:
    return [
        dict(span.attributes or {})
        for span in spans.get_finished_spans()
        if span.name == "lens.knowledge.edge_table"
    ]


async def test_each_fetch_is_one_span_with_rows_and_outcome(
    spans: InMemorySpanExporter,
) -> None:
    clock = StepClock()
    rows = _demo_rows()
    fetcher = Fetcher(rows)
    table = _table(fetcher, clock, ttl_s=10)

    await table.read()
    clock.advance(seconds=10)
    fetcher.fail = True
    await table.read()
    over = _table(Fetcher(rows), clock, max_edges=1)
    await over.read()
    cold = _table(fetcher, clock)
    with pytest.raises(RuntimeError):
        await cold.read()

    recorded = _edge_table_spans(spans)
    assert [span["lens.edge_table.outcome"] for span in recorded] == [
        "ok",
        "stale",
        "refused",
        "failed",
    ]
    assert [span.get("lens.edge_table.rows") for span in recorded] == [
        len(rows),
        len(rows),
        len(rows),
        None,
    ]
    assert recorded[3]["error.type"] == "RuntimeError"


async def test_patches_are_counted_by_event_type_and_outcome_and_age_is_a_gauge(
    metric_reader: InMemoryMetricReader,
) -> None:
    clock = StepClock()
    rows = _demo_rows()
    table = _table(Fetcher(rows), clock)

    table.apply_upsert(_upsert())
    await table.read()
    table.apply_upsert(_upsert())
    table.apply_upsert(_upsert(edge_id=rows[0].edge_id))
    clock.advance(seconds=12)

    snapshot = metric_snapshot(metric_reader)
    name = "lens_knowledge_edge_table_patches_total"
    for outcome in ("ignored", "inserted", "replaced"):
        point = snapshot_value(
            snapshot, name, event_type="edge.upserted", outcome=outcome
        )
        assert point.value == 1
    age = snapshot_value(snapshot, "lens_knowledge_edge_table_age_seconds")
    assert age.value == 12.0


# ── the fake's knowledge edge dataset ──────────────────────────────────


def test_the_demo_edge_rows_are_contract_shaped() -> None:
    contract_keys = set(
        load_contract("lithos_edge_list")["responses"]["success"]["results"][0]
    )
    raw = knowledge_edge_rows()
    assert all(set(row) == contract_keys for row in raw)
    assert len(_demo_rows()) == len(raw), "every demo row normalizes"
    assert len({row["edge_id"] for row in raw}) == len(raw)


def test_the_demo_edge_table_carries_every_case_the_graph_draws() -> None:
    rows = _demo_rows()
    types = {row.type for row in rows}
    assert {known.name for known in KNOWN_KNOWLEDGE_EDGE_TYPES} <= types
    assert types - {known.name for known in KNOWN_KNOWLEDGE_EDGE_TYPES} == {"assesses"}
    notes = set(demo_dataset().notes)
    dangling = {node for row in rows for node in row.endpoints if node not in notes}
    assert dangling == {DANGLING_NOTE_ID}
    contradictions = [row.conflict_state for row in rows if row.type == "contradicts"]
    assert contradictions.count(None) == 2 and "superseded" in contradictions
    assert {row.weight for row in rows if row.provenance_type == "consolidation"} == {
        0.03,
        0.06,
    }
    for row in rows:
        if direction_of(row) is EdgeDirection.SYMMETRIC:
            assert row.from_id <= row.to_id, row


async def test_the_fake_edge_list_honours_each_filter_and_records_the_call() -> None:
    fake = FakeLithosClient()
    everything = await fake.edge_list()
    assert everything == _demo_rows()

    plan = "note-influx-plan"
    assert await fake.edge_list(from_id=plan) == tuple(
        row for row in everything if row.from_id == plan
    )
    assert await fake.edge_list(to_id=plan) == tuple(
        row for row in everything if row.to_id == plan
    )
    assert await fake.edge_list(type="related_to") == tuple(
        row for row in everything if row.type == "related_to"
    )
    assert await fake.edge_list(namespace="runbooks") == tuple(
        row for row in everything if row.namespace == "runbooks"
    )
    both = await fake.edge_list(type="contradicts", namespace="influx")
    assert len(both) == 3
    assert await fake.edge_list(from_id=plan, type="assesses") == ()

    assert fake.tool_calls == [
        ("lithos_edge_list", {}),
        ("lithos_edge_list", {"from_id": plan}),
        ("lithos_edge_list", {"to_id": plan}),
        ("lithos_edge_list", {"type": "related_to"}),
        ("lithos_edge_list", {"namespace": "runbooks"}),
        ("lithos_edge_list", {"type": "contradicts", "namespace": "influx"}),
        ("lithos_edge_list", {"from_id": plan, "type": "assesses"}),
    ]


async def test_the_fake_serves_a_composed_dataset_and_feeds_the_snapshot() -> None:
    raw = load_contract("lithos_edge_list")["responses"]["success"]["results"]
    fake = FakeLithosClient(dataset=FakeLithosDataset(knowledge_edges=tuple(raw)))

    table = EdgeTable(
        lambda edge_type, namespace: fake.edge_list(type=edge_type, namespace=namespace)
    )
    snapshot = await _snapshot(table)

    assert snapshot.rows == normalize_edge_list({"results": raw})
    assert fake.tool_calls == [("lithos_edge_list", {})]


# ── config ─────────────────────────────────────────────────────────────


def _set_knowledge(config_path: Path, line: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace("[lithos-lens.knowledge]", f"[lithos-lens.knowledge]\n{line}", 1)
        if "[lithos-lens.knowledge]" in text
        else f"{text}\n[lithos-lens.knowledge]\n{line}\n",
        encoding="utf-8",
    )


def test_the_edge_table_knobs_default_and_read_from_toml(
    lithos_lens_config_env: Path,
) -> None:
    knowledge = load_config(lithos_lens_config_env).knowledge
    assert (knowledge.graph_edge_table_ttl_s, knowledge.graph_edge_table_max_edges) == (
        300,
        50_000,
    )

    _set_knowledge(lithos_lens_config_env, "graph_edge_table_ttl_s = 60")
    _set_knowledge(lithos_lens_config_env, "graph_edge_table_max_edges = 1000")
    knowledge = load_config(lithos_lens_config_env).knowledge
    assert (knowledge.graph_edge_table_ttl_s, knowledge.graph_edge_table_max_edges) == (
        60,
        1000,
    )


@pytest.mark.parametrize(
    "line",
    [
        "graph_edge_table_ttl_s = 0",
        "graph_edge_table_ttl_s = 3601",
        "graph_edge_table_max_edges = 0",
        "graph_edge_table_max_edges = 500001",
    ],
)
def test_the_edge_table_knobs_reject_out_of_range(
    lithos_lens_config_env: Path, line: str
) -> None:
    _set_knowledge(lithos_lens_config_env, line)
    with pytest.raises(ConfigError, match=line.split(" ")[0]):
        load_config(lithos_lens_config_env)


def test_the_edge_table_knobs_take_env_overrides(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_edge_table_ttl_s = 60")
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_EDGE_TABLE_TTL_S", "120")
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_EDGE_TABLE_MAX_EDGES", "2000")
    knowledge = load_config(lithos_lens_config_env).knowledge
    assert (knowledge.graph_edge_table_ttl_s, knowledge.graph_edge_table_max_edges) == (
        120,
        2000,
    )

    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_GRAPH_EDGE_TABLE_MAX_EDGES", "500001")
    with pytest.raises(ConfigError, match="MAX_EDGES must be <= 500000"):
        load_config(lithos_lens_config_env)
