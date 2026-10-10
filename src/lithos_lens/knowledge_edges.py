"""The knowledge edge-table snapshot every graph read is served from (K2 D2).

``lithos_edge_list`` with no filter answers the WHOLE ``edges`` table — no
limit, offset, order or total (``tests/contracts/lithos_edge_list.json``). On
2026-10-05 that was 9,442 rows and 4.9 MB, parsed in well under a second, so
Lens fetches it in one call and holds it here as an :class:`EdgeTableSnapshot`:
frozen rows plus indexes by endpoint, type and namespace, and the facet counts
the scope picker needs. No tool enumerates types or namespaces (ROADMAP ledger
#13), so those facets have no other source.

The holder (:class:`EdgeTable`) adds:

- **TTL** (``[knowledge].graph_edge_table_ttl_s``, 300 s) — the staleness
  bound the page states. Two clocks, as in :mod:`lithos_lens.graph_cache`: the
  wall clock stamps ``as_of`` for the reader, the monotonic one alone decides
  expiry, so a clock step cannot stretch the bound.
- **Single-flight** — concurrent reads during a fetch await the one in flight.
- **The bound** (``[knowledge].graph_edge_table_max_edges``, 50,000). A table
  over it is not held: the rows are discarded and an :class:`EdgeTableRefusal`
  naming the count is kept for one TTL instead, so an over-bound table costs
  one full fetch per TTL rather than one per request. Only
  :meth:`EdgeTable.filtered` reads (``type=`` and/or ``namespace=``) are
  served then, each straight through to Lithos, uncached, with no facets.
- **Stale-but-served** — a failed fetch is never cached. With a previous
  snapshot, readers get it back marked ``stale`` with its old ``as_of``, and
  the next read tries again; with none, the failure reaches every waiter.
- **Patching** — ``edge.upserted`` carries ``{edge_id, from_id, to_id, type,
  namespace, conflict_state}``: enough to insert or re-identify a row and set
  its conflict state, not its weight, provenance or evidence. A patched row is
  marked ``partial`` until the next full fetch replaces it. Changes that emit
  no event (reinforcement, projection, weight decay — ledger #15) converge on
  the TTL.

Foundation module: it holds no client. The caller passes the read as one
callable ``(type, namespace) -> rows``; the unfiltered fetch passes
``(None, None)``. The client normalises rows with
:func:`normalize_edge_list`, the way ``list_tags`` uses
``knowledge_tags.normalize_tag_counts``.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal

from lithos_lens import metrics
from lithos_lens.knowledge_edge_evidence import number_or_none
from lithos_lens.knowledge_edge_types import is_conflict_resolved
from lithos_lens.telemetry import get_tracer

# Mirror the ``[lithos-lens.knowledge]`` config defaults so a caller with no
# tuning to do (most tests) gets the shipped behaviour; Foundation may not
# reach for Config's constants from here without a new component edge, and
# ``tests/test_knowledge_edges.py`` pins the two together.
DEFAULT_EDGE_TABLE_TTL_S = 300
DEFAULT_EDGE_TABLE_MAX_EDGES = 50_000

#: The one knowledge event that carries an edge.
EDGE_UPSERTED = "edge.upserted"

#: The fetch outcomes the ``lens.knowledge.edge_table`` span reports.
FetchOutcome = Literal["ok", "refused", "failed", "stale"]

#: How the table reads Lithos: ``(type, namespace)`` filters, ``None`` for
#: "not filtered". Bound by the caller to a client's ``edge_list``.
EdgeListFetch = Callable[[str | None, str | None], Awaitable[Sequence["KnowledgeEdge"]]]

#: Injectable WALL clock; it stamps ``as_of``, which the page shows.
Clock = Callable[[], datetime]

#: Injectable MONOTONIC clock, in seconds; it alone decides expiry and age.
Ticks = Callable[[], float]

_IDENTITY_KEYS = ("edge_id", "from_id", "to_id", "type", "namespace")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class KnowledgeEdge:
    """One row of the Lithos ``edges`` table, its twelve columns as sent.

    NULL columns stay ``None`` rather than becoming ``""``, and
    ``conflict_state`` is kept as stored: a contradiction is resolved only
    by one of the four ``lithos_conflict_resolve`` values
    (:func:`is_unresolved_contradiction`), so NULL and any caller-authored
    marker both read unresolved. A ``None`` ``evidence`` is told apart from
    an empty one.
    ``evidence`` is kept as the raw JSON string; the panel that shows it
    parses it (``knowledge_edge_evidence.parse_edge_evidence``).

    ``partial`` is Lens's, not Lithos's: the row was inserted or re-identified
    from an ``edge.upserted`` payload, so its weight, provenance and evidence
    are last-known (or unknown, ``None``) until the next full fetch.
    """

    edge_id: str
    from_id: str
    to_id: str
    type: str
    weight: float | None
    namespace: str
    created_at: str | None = None
    updated_at: str | None = None
    provenance_actor: str | None = None
    provenance_type: str | None = None
    evidence: str | None = None
    conflict_state: str | None = None
    partial: bool = False

    @property
    def endpoints(self) -> tuple[str, str]:
        return (self.from_id, self.to_id)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def normalize_knowledge_edge(raw: Mapping[str, Any]) -> KnowledgeEdge | None:
    """One ``lithos_edge_list`` row as a :class:`KnowledgeEdge`.

    ``None`` for a row without its identity — a non-string ``edge_id``,
    endpoint, ``type`` or ``namespace``, or an empty ``edge_id`` (the primary
    key the patches match on) — so one odd row is dropped rather than
    guessed at. The other seven columns are nullable or typed loosely
    upstream and are kept when they have the right type, ``None`` otherwise.
    """
    identity = [raw.get(key) for key in _IDENTITY_KEYS]
    if not all(isinstance(value, str) for value in identity) or not raw["edge_id"]:
        return None
    return KnowledgeEdge(
        edge_id=raw["edge_id"],
        from_id=raw["from_id"],
        to_id=raw["to_id"],
        type=raw["type"],
        weight=number_or_none(raw.get("weight")),
        namespace=raw["namespace"],
        created_at=_str_or_none(raw.get("created_at")),
        updated_at=_str_or_none(raw.get("updated_at")),
        provenance_actor=_str_or_none(raw.get("provenance_actor")),
        provenance_type=_str_or_none(raw.get("provenance_type")),
        evidence=_str_or_none(raw.get("evidence")),
        conflict_state=_str_or_none(raw.get("conflict_state")),
    )


def normalize_edge_list(payload: Mapping[str, Any]) -> tuple[KnowledgeEdge, ...]:
    """``lithos_edge_list``'s ``{"results": [...]}`` as rows, in the order sent."""
    rows = payload.get("results")
    if not isinstance(rows, list):
        return ()
    edges = (normalize_knowledge_edge(row) for row in rows if isinstance(row, Mapping))
    return tuple(edge for edge in edges if edge is not None)


def _ranked(counts: Counter[str]) -> Mapping[str, int]:
    """Most rows first, ties by name, frozen."""
    return MappingProxyType(
        dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    )


def _index(
    rows: Iterable[KnowledgeEdge], keys: Callable[[KnowledgeEdge], Iterable[str]]
) -> Mapping[str, tuple[KnowledgeEdge, ...]]:
    grouped: dict[str, list[KnowledgeEdge]] = {}
    for row in rows:
        for key in keys(row):
            grouped.setdefault(key, []).append(row)
    return MappingProxyType({key: tuple(group) for key, group in grouped.items()})


def is_unresolved_contradiction(edge: KnowledgeEdge) -> bool:
    """A ``contradicts`` row not yet settled by ``lithos_conflict_resolve``.

    That tool writes one of ``accepted_dual`` / ``superseded`` / ``refuted``
    / ``merged``; only those count as resolved. NULL (as inference writes
    it) and any marker a caller stored through ``lithos_edge_upsert``
    (``"unresolved"``, ``""``, ``"pending"``) are unresolved.
    """
    return edge.type == "contradicts" and not is_conflict_resolved(edge.conflict_state)


@dataclass(frozen=True)
class EdgeFacets:
    """What the scope picker offers: rows per type and per namespace (most
    first, ties by name) and how many contradictions are still unresolved."""

    types: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    namespaces: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    unresolved_contradictions: int = 0


@dataclass(frozen=True)
class EdgeTableSnapshot:
    """The whole edge table at ``as_of``, with its indexes and facets.

    Only ``rows``, ``as_of`` and ``stale`` are inputs. The indexes and facets
    are DERIVED from ``rows`` at construction and cannot be passed in, so they
    agree with the rows by construction — after a patch too, since a patch
    builds a new snapshot (``dataclasses.replace`` re-derives them).
    ``by_endpoint`` lists a row under both its endpoints (once for a
    self-loop).
    """

    rows: tuple[KnowledgeEdge, ...]
    as_of: datetime
    #: The refetch after ``as_of`` failed; this is the last good table.
    stale: bool = False
    by_endpoint: Mapping[str, tuple[KnowledgeEdge, ...]] = field(init=False)
    by_type: Mapping[str, tuple[KnowledgeEdge, ...]] = field(init=False)
    by_namespace: Mapping[str, tuple[KnowledgeEdge, ...]] = field(init=False)
    facets: EdgeFacets = field(init=False)

    def __post_init__(self) -> None:
        rows = self.rows
        derived = {
            "by_endpoint": _index(rows, lambda row: dict.fromkeys(row.endpoints)),
            "by_type": _index(rows, lambda row: (row.type,)),
            "by_namespace": _index(rows, lambda row: (row.namespace,)),
            "facets": EdgeFacets(
                types=_ranked(Counter(row.type for row in rows)),
                namespaces=_ranked(Counter(row.namespace for row in rows)),
                unresolved_contradictions=sum(
                    1 for row in rows if is_unresolved_contradiction(row)
                ),
            ),
        }
        # The sanctioned frozen-dataclass escape hatch for derived fields.
        for name, value in derived.items():
            object.__setattr__(self, name, value)

    def edges_of(self, node_id: str) -> tuple[KnowledgeEdge, ...]:
        return self.by_endpoint.get(node_id, ())

    def of_type(self, edge_type: str) -> tuple[KnowledgeEdge, ...]:
        return self.by_type.get(edge_type, ())

    def in_namespace(self, namespace: str) -> tuple[KnowledgeEdge, ...]:
        return self.by_namespace.get(namespace, ())

    @property
    def partial_rows(self) -> int:
        return sum(1 for row in self.rows if row.partial)


@dataclass(frozen=True)
class EdgeTableRefusal:
    """The table was fetched, counted and NOT held: over the bound.

    The page names ``row_count`` against ``max_edges``; until ``as_of`` plus
    the TTL, only :meth:`EdgeTable.filtered` reads are served.
    """

    row_count: int
    max_edges: int
    as_of: datetime


EdgeTableState = EdgeTableSnapshot | EdgeTableRefusal


class EdgeTable:
    """The process's edge-table snapshot: TTL, single-flight, bound, patches.

    One per process, on ``AppState.edge_table``; the hub feeds each
    ``edge.upserted`` to :meth:`apply_upsert` and expires the table on
    ``lens.refresh`` (S7).
    """

    def __init__(
        self,
        fetch: EdgeListFetch,
        *,
        ttl_s: float = DEFAULT_EDGE_TABLE_TTL_S,
        max_edges: int = DEFAULT_EDGE_TABLE_MAX_EDGES,
        clock: Clock = _utcnow,
        ticks: Ticks = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl_s = ttl_s
        self._max_edges = max_edges
        self._clock = clock
        self._ticks = ticks
        self._state: EdgeTableState | None = None
        self._fetched_tick: float | None = None
        self._expires_at = 0.0
        self._inflight: asyncio.Task[EdgeTableState] | None = None
        # Bumped by :meth:`expire`. A fetch remembers the generation it began
        # in, and one that began before an expiry is never taken as fresh.
        self._generation = 0
        self._inflight_generation = 0
        #: Unfiltered fetches issued upstream (single-flight collapses the rest).
        self.fetches = 0
        metrics.register_knowledge_edge_table_age(self.age_seconds)

    @property
    def current(self) -> EdgeTableState | None:
        """What is held now, live or not, without fetching."""
        return self._state

    def age_seconds(self) -> float:
        """Seconds since the last successful fetch; 0 before the first."""
        if self._fetched_tick is None:
            return 0.0
        return max(0.0, self._ticks() - self._fetched_tick)

    async def read(self) -> EdgeTableState:
        """The live snapshot or refusal, from one shared fetch when expired.

        A failed fetch answers the previous snapshot marked ``stale`` when
        there is one, and raises to every waiter when there is not.
        """
        while True:
            state = self._state
            if state is not None and self._ticks() < self._expires_at:
                return state
            if self._inflight is None:
                self._inflight_generation = self._generation
                self._inflight = asyncio.create_task(
                    self._load(self._generation), name="knowledge-edge-table"
                )
            inflight, generation = self._inflight, self._inflight_generation
            # Shielded: one waiter going away (a closed tab) must not cancel
            # the fetch the other waiters are on.
            answer = await asyncio.shield(inflight)
            if generation == self._generation:
                return answer
            # Overtaken by :meth:`expire`: its rows may predate the gap the
            # expiry answers, so read again rather than serve them.

    def expire(self) -> None:
        """End the held snapshot's TTL now: the next :meth:`read` refetches.

        The hub's ``lens.refresh`` hook — events were missed, so the patches
        that would have kept the table current are missing too. What is held
        is still what a failed refetch serves, marked ``stale``. A fetch
        already in flight began before the gap, so its answer is installed
        but not as fresh, and no reader takes it: each reads again.
        """
        self._generation += 1
        self._expires_at = 0.0

    async def filtered(
        self, *, type: str | None = None, namespace: str | None = None
    ) -> tuple[KnowledgeEdge, ...]:
        """A direct, uncached, filtered ``lithos_edge_list`` read.

        The over-bound path: what a refused table still serves. At least one
        filter is required, since an unfiltered read here would fetch the very
        table the bound refused to hold.
        """
        if type is None and namespace is None:
            raise ValueError("a filtered edge read needs type= and/or namespace=")
        return tuple(await self._fetch(type, namespace))

    async def _load(self, generation: int) -> EdgeTableState:
        try:
            with get_tracer().start_as_current_span(
                "lens.knowledge.edge_table"
            ) as span:
                self.fetches += 1
                try:
                    rows = tuple(await self._fetch(None, None))
                except Exception as exc:
                    previous = self._state
                    span.set_attribute("error.type", type(exc).__name__)
                    if not isinstance(previous, EdgeTableSnapshot):
                        span.set_attribute("lens.edge_table.outcome", "failed")
                        raise
                    stale = replace(previous, stale=True)
                    self._state = stale
                    span.set_attribute("lens.edge_table.outcome", "stale")
                    span.set_attribute("lens.edge_table.rows", len(stale.rows))
                    return stale
                state: EdgeTableState
                outcome: FetchOutcome
                if len(rows) > self._max_edges:
                    state = EdgeTableRefusal(len(rows), self._max_edges, self._clock())
                    outcome = "refused"
                else:
                    state = EdgeTableSnapshot(rows=rows, as_of=self._clock())
                    outcome = "ok"
                self._state = state
                self._fetched_tick = self._ticks()
                if generation == self._generation:
                    self._expires_at = self._fetched_tick + self._ttl_s
                span.set_attribute("lens.edge_table.outcome", outcome)
                span.set_attribute("lens.edge_table.rows", len(rows))
                return state
        finally:
            self._inflight = None

    def apply_upsert(self, payload: Mapping[str, Any]) -> bool:
        """Patch the held snapshot from an ``edge.upserted`` payload.

        Matched by ``edge_id``: an existing row takes the payload's endpoints,
        type, namespace and conflict state and keeps everything else; a new
        row is inserted with weight, provenance, evidence and timestamps
        unknown. Either way the row is ``partial`` until the next full fetch.
        An insertion that takes the table over the bound refuses it, as a
        fetch over the bound would: the rows are dropped and an
        :class:`EdgeTableRefusal` is held until the current TTL expires. A
        no-op (``False``) with no snapshot held, with the table refused, or
        for a payload missing an identity field. A patch landing while a fetch
        is in flight may be overwritten by it; the TTL is the stated bound.
        """
        outcome = self._patch(payload)
        metrics.knowledge_edge_table_patches().add(
            1, {"event_type": EDGE_UPSERTED, "outcome": outcome}
        )
        return outcome != "ignored"

    def _patch(self, payload: Mapping[str, Any]) -> str:
        snapshot = self._state
        if not isinstance(snapshot, EdgeTableSnapshot):
            return "ignored"
        identity = {key: payload.get(key) for key in _IDENTITY_KEYS}
        conflict_state = payload.get("conflict_state")
        if (
            not all(isinstance(value, str) for value in identity.values())
            or not identity["edge_id"]
            or not (conflict_state is None or isinstance(conflict_state, str))
        ):
            return "ignored"
        patched = KnowledgeEdge(
            edge_id=str(identity["edge_id"]),
            from_id=str(identity["from_id"]),
            to_id=str(identity["to_id"]),
            type=str(identity["type"]),
            weight=None,
            namespace=str(identity["namespace"]),
            conflict_state=conflict_state,
            partial=True,
        )
        rows = list(snapshot.rows)
        for index, row in enumerate(rows):
            if row.edge_id == patched.edge_id:
                rows[index] = replace(
                    row,
                    from_id=patched.from_id,
                    to_id=patched.to_id,
                    type=patched.type,
                    namespace=patched.namespace,
                    conflict_state=patched.conflict_state,
                    partial=True,
                )
                outcome = "replaced"
                break
        else:
            rows.append(patched)
            if len(rows) > self._max_edges:
                # The insertion crossed the bound: the table is now one the
                # bound says not to hold, whether it arrived by fetch or by
                # event. Refuse it exactly as a fetch would — rows discarded,
                # count named — until the current TTL runs out and the next
                # read re-counts the table upstream.
                self._state = EdgeTableRefusal(
                    len(rows), self._max_edges, snapshot.as_of
                )
                return "refused"
            outcome = "inserted"
        self._state = replace(snapshot, rows=tuple(rows))
        return outcome
