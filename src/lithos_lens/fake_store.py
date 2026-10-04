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
a task **no write touched** keeps its seed verdict verbatim; a task whose own
status changed, or one of whose blockers' did, or one that gained an edge, is
recomputed from the effective blocking edges — preserving each seed blocker
record whose blocker did not move, so the fixture's own kinds and messages
survive. That is what makes ``unblocked`` / ``reblocked`` real: they are the
difference this oracle reports across the write, not a list the fake was told
to return.

One blocker's answer is the clock's rather than a status's — an open ``timer``
gate stops blocking once its ``ready_at`` has passed, exactly as upstream
resolves one. Blocker-record text comes from the vendored
``lithos_task_blocked`` contract's ``blocker_kinds`` variants, so a recomputed
row reads like a real one.
"""

from __future__ import annotations

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

    def __init__(self, dataset: FakeLithosDataset) -> None:
        self.dataset = dataset
        self.overlay = FakeWriteOverlay()

    # ── effective reads ────────────────────────────────────────────────

    def tasks(self) -> tuple[TaskRecord, ...]:
        """Every task, seed rows overlaid, then the ones writes minted."""
        return tuple(
            [self._overlaid(task) for task in self.dataset.tasks]
            + list(self.overlay.created)
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
            if status == "completed" or self._gate_has_elapsed(edge, source):
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
            if (
                source is not None
                and source.status == "cancelled"
                and record.kind != "blocker_unsatisfiable"
            ):
                record = replace(
                    record,
                    kind="blocker_unsatisfiable",
                    status="cancelled",
                    message=_UNSATISFIABLE_MESSAGE.format(task_id=record.task_id),
                )
            records.append(record)
        return tuple(records)

    def is_ready(self, task_id: str) -> bool:
        """Whether the frontier reports ``task_id`` ready.

        Untouched tasks answer from the seed's ``ready_ids`` verbatim; once a
        write touches one, the oracle owns its verdict.
        """
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
        """Whether any write can have moved ``task_id``'s readiness verdict."""
        if task_id in self.overlay.statuses:
            return True
        if any(task.id == task_id for task in self.overlay.created):
            return True
        inserted = {
            (edge.from_task_id, edge.to_task_id, edge.type)
            for edge in self.overlay.edges
        }
        for edge in self._incoming_blocking(task_id):
            if edge.from_task_id in self.overlay.statuses:
                return True
            if (edge.from_task_id, edge.to_task_id, edge.type) in inserted:
                return True
        return False

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
                message=_UNSATISFIABLE_MESSAGE.format(task_id=edge.from_task_id),
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

    def _dependent_readiness(self, task_id: str) -> dict[str, bool]:
        """Each OPEN dependent of ``task_id`` and whether it is ready now.

        Snapshotted before a write so the difference afterwards is what
        ``unblocked`` / ``reblocked`` report.
        """
        readiness: dict[str, bool] = {}
        for edge in self._outgoing(task_id):
            dependent = self.task(edge.to_task_id)
            if dependent is None or dependent.status != "open":
                continue
            readiness[dependent.id] = self.is_ready(dependent.id)
        return readiness

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

    def _gate_has_elapsed(self, edge: EdgeRecord, source: TaskRecord | None) -> bool:
        """Whether an OPEN timer gate's wait is already over.

        Upstream resolves a `timer` gate by itself once `metadata.ready_at`
        passes, so a waiter on one is satisfied without the gate ever being
        completed — the one blocker whose answer depends on the clock rather
        than on a status. Any other gate type, an unparseable `ready_at`, or a
        time still ahead leaves the waiter blocked.
        """
        if edge.type != "waits_on_gate" or source is None:
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
        return parsed <= datetime.now(UTC)
