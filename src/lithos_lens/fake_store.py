"""The fake's effective store: a frozen seed under a mutable overlay.

:class:`~lithos_lens.fake_dataset.FakeLithosDataset` is immutable by
construction and stays that way — it is the demo artifact, and two fakes built
from ``demo_dataset()`` must be comparable. So a write does not touch it.
:class:`FakeStoreView` pairs it with a per-instance :class:`FakeWriteOverlay`
of everything the writes changed (statuses, outcomes, resolved stamps, minted
tasks, inserted or re-metadata'd edges, released claims, posted findings) and
answers every read from **seed plus overlay**. One overlay per
:class:`~lithos_lens.fake_lithos.FakeLithosClient`, so two fakes never share
one.

The readiness oracle is the interesting part, and it is why this is a module of
its own rather than more of :mod:`lithos_lens.fake_writes`: the writes next
door mutate the overlay and own upstream's refusal vocabulary, while everything
here ANSWERS — what the task store now looks like, and who is ready in it. The
writes read this to compute their ``unblocked`` / ``reblocked``; nothing here
knows a write happened.

The seed's ``ready_ids`` / ``blocked`` are a fixture oracle, not derived state —
Lens never re-derives readiness, so the fixtures are free to state verdicts (a
cycle, say) that no edge walk would reproduce. The rule here keeps that intact:
a task **nothing touched** keeps its seed verdict verbatim; a task whose own
status changed, or one of whose blockers' did, or one that gained an edge, is
recomputed from the effective blocking edges — preserving each seed blocker
record whose blocker did not move, so the fixture's own kinds and messages
survive. Whatever the verdict, only an open ``task``-typed row is ever ready or
listed blocked: upstream keeps gates and epics off both frontiers. That is
what makes ``unblocked`` / ``reblocked`` real: the writes next door apply
upstream's own rules to this oracle, rather than return a list the fake was
told to.

One blocker's answer is the clock's rather than a status's — an open ``timer``
gate stops blocking once its ``ready_at`` has passed, exactly as upstream
resolves one, and that passage alone counts as touching its waiters: upstream
evaluates the timer on every read, so no write is needed to release them.
Blocker-record text comes from the vendored ``lithos_task_blocked`` contract's
``blocker_kinds`` variants, so a recomputed row reads like a real one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.task_graph import BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    ClaimRecord,
    FindingRecord,
    TaskRecord,
    TaskStatusName,
)

__all__ = [
    "BLOCKING_EDGE_TYPES",
    "HIERARCHY_EDGE_TYPES",
    "NON_WORKABLE_TASK_TYPES",
    "FakeStoreView",
    "FakeWriteOverlay",
]

#: Edge types that gate readiness, and so the ones a DEPENDENCY cycle forms
#: over. ``parent_child`` forms a cycle of its own kind — a hierarchy that
#: contains itself — and is checked separately; ``discovered_from`` is
#: provenance and is not cycle-checked at all.
BLOCKING_EDGE_TYPES = frozenset({"blocks", "waits_on_gate"})
HIERARCHY_EDGE_TYPES = frozenset({"parent_child"})

#: Blocker messages, transcribed from tests/contracts/lithos_task_blocked.json
#: (``responses.variants.blocker_kinds``).
_PREDECESSOR_MESSAGE = "Waiting on predecessor {task_id} to complete."
_GATE_MESSAGE = "Waiting on {gate_type} gate {task_id}."
_TIMER_GATE_MESSAGE = "Waiting on timer gate {task_id} (ready_at={ready_at})."
_UNSATISFIABLE_MESSAGE = (
    "Blocking predecessor {task_id} was cancelled; this task can never become "
    "ready without intervention (complete/re-open the predecessor, re-route, "
    "or cancel this subtree)."
)
#: The same, for a cancelled GATE — upstream words the two differently
#: (``blocker_unsatisfiable_gate`` in the same contract).
_UNSATISFIABLE_GATE_MESSAGE = (
    "Gate {task_id} was cancelled; this task can never become ready without "
    "intervention (complete/re-open the gate, re-route, or cancel this subtree)."
)

#: Task types upstream keeps off BOTH frontiers: an ``epic`` is a roll-up
#: container and a ``gate`` an external wait, so neither is ever "ready" nor
#: listed "blocked", whatever its edges say (lithos ``coordination.py``
#: ``NON_WORKABLE_TASK_TYPES``).
NON_WORKABLE_TASK_TYPES = frozenset({"gate", "epic"})


def _unsatisfiable_message(edge_type: str, task_id: str) -> str:
    template = (
        _UNSATISFIABLE_GATE_MESSAGE
        if edge_type == "waits_on_gate"
        else _UNSATISFIABLE_MESSAGE
    )
    return template.format(task_id=task_id)


_EdgeKey = tuple[str, str, str]


@dataclass
class FakeWriteOverlay:
    """Everything a fake's writes changed, keyed by what they changed it on.

    Mutable on purpose and per-instance: the seed dataset stays frozen, and
    every field here starts empty, so an untouched fake reads exactly as it did
    before writes existed.
    """

    #: Task id -> status, for any task a write resolved or reopened.
    statuses: dict[str, TaskStatusName] = field(default_factory=dict)
    #: Task id -> outcome; a reopen writes "" (upstream clears it).
    outcomes: dict[str, str] = field(default_factory=dict)
    #: Task id -> resolved_at; a reopen writes "" (upstream clears it).
    resolved_at: dict[str, str] = field(default_factory=dict)
    #: Task id -> the ``updated_at`` its last write committed (also a minted
    #: task's creation stamp). Survives a reopen, which clears ``resolved_at``.
    updated_at: dict[str, str] = field(default_factory=dict)
    #: Tasks minted by ``lithos_task_create``, in creation order.
    created: list[TaskRecord] = field(default_factory=list)
    #: Edges inserted by a create or an upsert, canonical (``direction=""``).
    edges: list[EdgeRecord] = field(default_factory=list)
    #: ``(from, to, type)`` -> replacement metadata, from an upsert onto an
    #: edge that already existed. Only the metadata moves: ``created_by`` and
    #: ``created_at`` stay as the original writer left them.
    edge_metadata: dict[_EdgeKey, dict[str, Any]] = field(default_factory=dict)
    #: Task ids whose claims a complete or a cancel released.
    released_claims: set[str] = field(default_factory=set)
    #: Task id -> findings a write posted (the ``[Reopened]`` marker).
    findings: dict[str, list[FindingRecord]] = field(default_factory=dict)
    #: Monotonic counter behind minted task and finding ids.
    sequence: int = 0


class FakeStoreView:
    """Seed plus overlay: the effective task store a fake's reads answer from.

    Reads are views, never copies of the dataset. The write surface over this
    is :class:`~lithos_lens.fake_writes.FakeWriteStore`, which is the only
    thing that puts anything INTO the overlay.
    """

    def __init__(
        self,
        dataset: FakeLithosDataset,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.dataset = dataset
        self.overlay = FakeWriteOverlay()
        #: What "now" is for a ``timer`` gate's ``ready_at`` — the one input
        #: besides the store that moves a verdict. Injectable so a test can let
        #: time pass without a write.
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # ── effective reads ────────────────────────────────────────────────

    def tasks(self) -> tuple[TaskRecord, ...]:
        """Every task, seed rows then the ones writes minted, all overlaid.

        A minted task is overlaid exactly like a seed row: it is stored as it
        was created (open), and a later complete, cancel or reopen of it lands
        in the same status / outcome / resolved-at maps as any other task's.
        """
        return tuple(
            self._overlaid(task)
            for task in (*self.dataset.tasks, *self.overlay.created)
        )

    def task(self, task_id: str) -> TaskRecord | None:
        return next((task for task in self.tasks() if task.id == task_id), None)

    def claims(self, task_id: str) -> tuple[ClaimRecord, ...]:
        """Active claims — none once a complete or a cancel released them."""
        if task_id in self.overlay.released_claims:
            return ()
        claims: tuple[ClaimRecord, ...] = self.dataset.claims.get(task_id, ())
        return claims

    def findings(self, task_id: str) -> tuple[FindingRecord, ...]:
        return tuple(self.dataset.findings.get(task_id, ())) + tuple(
            self.overlay.findings.get(task_id, ())
        )

    def edges(self, task_id: str) -> tuple[EdgeRecord, ...]:
        """One task's edges, direction relative to it, metadata overlaid."""
        rows = [
            self._with_metadata(edge) for edge in self.dataset.edges.get(task_id, ())
        ]
        for edge in self.overlay.edges:
            if edge.from_task_id == task_id:
                rows.append(replace(self._with_metadata(edge), direction="outgoing"))
            elif edge.to_task_id == task_id:
                rows.append(replace(self._with_metadata(edge), direction="incoming"))
        return tuple(rows)

    def children(self, task_id: str) -> tuple[str, ...]:
        """Child ids: the fixture's, plus any ``parent_child`` edge a write added."""
        child_ids = list(self.dataset.children.get(task_id, ()))
        for edge in self.overlay.edges:
            if (
                edge.type == "parent_child"
                and edge.from_task_id == task_id
                and edge.to_task_id not in child_ids
            ):
                child_ids.append(edge.to_task_id)
        return tuple(child_ids)

    def blockers(self, task_id: str) -> tuple[BlockerRecord, ...]:
        """Why ``task_id`` is blocked, from seed plus overlay (see module doc)."""
        seed: tuple[BlockerRecord, ...] = tuple(self.dataset.blocked.get(task_id, ()))
        if not self._touched(task_id):
            return seed
        records: list[BlockerRecord] = []
        represented: set[tuple[str, str]] = set()
        for edge in self._incoming_blocking(task_id):
            represented.add((edge.from_task_id, edge.type))
            source = self.task(edge.from_task_id)
            status = source.status if source is not None else "open"
            if status == "completed" or self._gate_has_elapsed(edge.type, source):
                continue
            prior = next(
                (
                    record
                    for record in seed
                    if record.task_id == edge.from_task_id and record.type == edge.type
                ),
                None,
            )
            records.append(self._blocker(edge, source, status, prior))
        # Seed records no effective edge accounts for — a cycle verdict the
        # fixture states, or a blocker it listed without an edge. They stand
        # unless their blocker has since been satisfied.
        for record in seed:
            if (record.task_id, record.type) in represented:
                continue
            source = self.task(record.task_id)
            if source is not None and source.status == "completed":
                continue
            if self._gate_has_elapsed(record.type, source):
                continue
            if (
                source is not None
                and source.status == "cancelled"
                and record.kind != "blocker_unsatisfiable"
            ):
                record = replace(
                    record,
                    kind="blocker_unsatisfiable",
                    status="cancelled",
                    message=_unsatisfiable_message(record.type, record.task_id),
                )
            records.append(record)
        return tuple(records)

    def is_ready(self, task_id: str) -> bool:
        """Whether the frontier reports ``task_id`` ready.

        Only an OPEN, workable task can be: upstream's readiness predicate
        excludes every other status and both non-workable types before it
        looks at a single edge. Untouched tasks then answer from the seed's
        ``ready_ids`` verbatim; once a write — or the clock — touches one, the
        oracle owns its verdict.
        """
        task = self.task(task_id)
        if (
            task is None
            or task.status != "open"
            or task.task_type in NON_WORKABLE_TASK_TYPES
        ):
            return False
        if not self._touched(task_id):
            return task_id in self.dataset.ready_ids
        return not self.blockers(task_id)

    # ── helpers ────────────────────────────────────────────────────────

    def _overlaid(self, task: TaskRecord) -> TaskRecord:
        if not (
            task.id in self.overlay.statuses
            or task.id in self.overlay.outcomes
            or task.id in self.overlay.resolved_at
        ):
            return task
        return replace(
            task,
            status=self.overlay.statuses.get(task.id, task.status),
            outcome=self.overlay.outcomes.get(task.id, task.outcome),
            resolved_at=self.overlay.resolved_at.get(task.id, task.resolved_at),
        )

    def _with_metadata(self, edge: EdgeRecord) -> EdgeRecord:
        replacement = self.overlay.edge_metadata.get(
            (edge.from_task_id, edge.to_task_id, edge.type)
        )
        return (
            edge if replacement is None else replace(edge, metadata=dict(replacement))
        )

    def _edge_set(self) -> dict[_EdgeKey, EdgeRecord]:
        """Every effective edge once, canonical, keyed by ``(from, to, type)``."""
        edges: dict[_EdgeKey, EdgeRecord] = {}
        for rows in self.dataset.edges.values():
            for edge in rows:
                edges.setdefault(
                    (edge.from_task_id, edge.to_task_id, edge.type),
                    self._with_metadata(edge),
                )
        for edge in self.overlay.edges:
            edges[(edge.from_task_id, edge.to_task_id, edge.type)] = (
                self._with_metadata(edge)
            )
        return edges

    def _incoming_blocking(self, task_id: str) -> list[EdgeRecord]:
        return [
            edge
            for edge in self._edge_set().values()
            if edge.to_task_id == task_id and edge.type in BLOCKING_EDGE_TYPES
        ]

    def _outgoing(
        self, task_id: str, types: frozenset[str] = BLOCKING_EDGE_TYPES
    ) -> list[EdgeRecord]:
        return [
            edge
            for edge in self._edge_set().values()
            if edge.from_task_id == task_id and edge.type in types
        ]

    def _touched(self, task_id: str) -> bool:
        """Whether anything can have moved ``task_id``'s readiness verdict.

        A write, mostly — and the one thing that is not a write: the clock
        passing an open ``timer`` gate's ``ready_at``. Upstream evaluates that
        on every read, so a waiter on an elapsed timer is recomputed (and the
        elapsed gate stops blocking it) even if no write ever came near it.
        """
        if task_id in self.overlay.statuses:
            return True
        if any(task.id == task_id for task in self.overlay.created):
            return True
        inserted = {
            (edge.from_task_id, edge.to_task_id, edge.type)
            for edge in self.overlay.edges
        }
        for source_id, edge_type in self._blocking_relations(task_id):
            if source_id in self.overlay.statuses:
                return True
            if (source_id, task_id, edge_type) in inserted:
                return True
            if self._gate_has_elapsed(edge_type, self.task(source_id)):
                return True
        return False

    def _blocking_relations(self, task_id: str) -> list[tuple[str, str]]:
        """Every ``(blocker id, edge type)`` that can hold ``task_id`` back.

        The effective incoming blocking edges, plus each blocker the seed's
        ``blocked`` oracle names WITHOUT an edge (a fixture may state one —
        see the module doc). Both kinds have to be watched: a seed-only
        blocker that completes, is cancelled or lapses moves the verdict just
        as an edge's would.
        """
        relations = [
            (edge.from_task_id, edge.type) for edge in self._incoming_blocking(task_id)
        ]
        for record in self.dataset.blocked.get(task_id, ()):
            relation = (record.task_id, record.type)
            if record.type in BLOCKING_EDGE_TYPES and relation not in relations:
                relations.append(relation)
        return relations

    def _blocker(
        self,
        edge: EdgeRecord,
        source: TaskRecord | None,
        status: str,
        prior: BlockerRecord | None,
    ) -> BlockerRecord:
        if status == "cancelled":
            return BlockerRecord(
                kind="blocker_unsatisfiable",
                task_id=edge.from_task_id,
                type=edge.type,
                status="cancelled",
                message=_unsatisfiable_message(edge.type, edge.from_task_id),
            )
        if prior is not None and prior.kind != "blocker_unsatisfiable":
            # This blocker did not move, so the fixture's own verdict (which
            # may be a `cycle`, with its message) is kept rather than redrawn.
            return prior
        if edge.type == "waits_on_gate":
            metadata = source.metadata if source is not None else {}
            gate_type = str(metadata.get("gate_type") or "gate")
            ready_at = str(metadata.get("ready_at") or "")
            message = (
                _TIMER_GATE_MESSAGE.format(task_id=edge.from_task_id, ready_at=ready_at)
                if gate_type == "timer"
                else _GATE_MESSAGE.format(
                    gate_type=gate_type, task_id=edge.from_task_id
                )
            )
            return BlockerRecord(
                kind="gate",
                task_id=edge.from_task_id,
                type=edge.type,
                status="open",
                message=message,
            )
        return BlockerRecord(
            kind="task",
            task_id=edge.from_task_id,
            type=edge.type,
            status="open",
            message=_PREDECESSOR_MESSAGE.format(task_id=edge.from_task_id),
        )

    def _dependents(self, task_id: str) -> list[str]:
        """Each task ``task_id`` blocks or gates, once, whatever its status.

        Upstream's candidate set for both ``unblocked`` and ``reblocked``:
        every ``to`` end of an outgoing ``blocks`` / ``waits_on_gate`` edge —
        plus, here, every task whose seed ``blocked`` record names ``task_id``
        without an edge (see :meth:`_blocking_relations`). The tools filter it
        themselves.
        """
        seed_only = [
            dependent
            for dependent, records in self.dataset.blocked.items()
            if any(
                record.task_id == task_id and record.type in BLOCKING_EDGE_TYPES
                for record in records
            )
        ]
        return list(
            dict.fromkeys(
                [edge.to_task_id for edge in self._outgoing(task_id)] + seed_only
            )
        )

    def _edge_path(
        self, start: str, goal: str, types: frozenset[str]
    ) -> list[str] | None:
        """A path ``start -> … -> goal`` over ``types``, or None if there is none.

        Used the other way round from how it reads: adding ``goal -> start``
        closes a cycle exactly when ``start`` already reaches ``goal``.
        """
        queue: list[list[str]] = [[start]]
        seen = {start}
        while queue:
            path = queue.pop(0)
            for edge in self._outgoing(path[-1], types):
                if edge.to_task_id == goal:
                    return [*path, goal]
                if edge.to_task_id in seen:
                    continue
                seen.add(edge.to_task_id)
                queue.append([*path, edge.to_task_id])
        return None

    def _gate_has_elapsed(self, edge_type: str, source: TaskRecord | None) -> bool:
        """Whether an OPEN timer gate's wait is already over.

        Upstream resolves a `timer` gate by itself once `metadata.ready_at`
        passes, so a waiter on one is satisfied without the gate ever being
        completed — the one blocker whose answer depends on the clock rather
        than on a status. Any other gate type, an unparseable `ready_at`, or a
        time still ahead leaves the waiter blocked.

        The gate must still be OPEN. A CANCELLED timer gate whose `ready_at`
        has passed does not satisfy its waiter: cancellation wins, and the
        waiter is `blocker_unsatisfiable` — which is the whole point of the
        cancel consequences (T3 D9). Reading the clock without the status would
        leave such a waiter ready and the strand invisible.
        """
        if edge_type != "waits_on_gate" or source is None:
            return False
        if source.status != "open":
            return False
        if source.task_type != "gate" or source.metadata.get("gate_type") != "timer":
            return False
        ready_at = str(source.metadata.get("ready_at") or "")
        try:
            parsed = datetime.fromisoformat(ready_at)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed <= self.clock()
