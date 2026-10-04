"""The fake's write half: a mutable overlay over a frozen seed dataset.

:class:`~lithos_lens.fake_dataset.FakeLithosDataset` is immutable by
construction and stays that way — it is the demo artifact, and two fakes built
from ``demo_dataset()`` must be comparable. So a write does not touch it.
:class:`FakeWriteStore` holds a per-instance :class:`FakeWriteOverlay` of
everything the writes changed (statuses, outcomes, resolved stamps, minted
tasks, inserted or re-metadata'd edges, released claims, posted findings) and
answers every read from **seed plus overlay**. One store per
:class:`~lithos_lens.fake_lithos.FakeLithosClient`, so two fakes never share
one.

The readiness oracle is the interesting part. The seed's ``ready_ids`` /
``blocked`` are a fixture oracle, not derived state — Lens never re-derives
readiness, so the fixtures are free to state verdicts (a cycle, say) that no
edge walk would reproduce. The rule here keeps that intact: a task **no write
touched** keeps its seed verdict verbatim; a task whose own status changed, or
one of whose blockers' did, or one that gained an edge, is recomputed from the
effective blocking edges — preserving each seed blocker record whose blocker
did not move, so the fixture's own kinds and messages survive. That is what
makes ``unblocked`` / ``reblocked`` real: they are the difference the oracle
reports across the write, not a list the fake was told to return.

Blocker-record text comes from the vendored ``lithos_task_blocked`` contract's
``blocker_kinds`` variants, so a recomputed row reads like a real one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.gates import KNOWN_GATE_TYPES
from lithos_lens.mcp_transport import LithosToolError
from lithos_lens.task_graph import KNOWN_EDGE_TYPES, BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    KNOWN_TASK_TYPES,
    REOPENED_FINDING_PREFIX,
    ClaimRecord,
    FindingRecord,
    TaskRecord,
    TaskStatusName,
)

__all__ = ["FakeWriteOverlay", "FakeWriteStore", "write_error"]

#: Edge types that gate readiness, and so the ones a cycle can form over.
BLOCKING_EDGE_TYPES = frozenset({"blocks", "waits_on_gate"})

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


def write_error(code: str, message: str, **fields: Any) -> LithosToolError:
    """Build the coded error a Lithos write envelope would raise.

    The envelope is assembled in full — ``status``/``code``/``message`` plus
    whatever the code carries (``candidates`` for an ambiguous prefix) — and
    handed to :class:`LithosToolError`, so the fake exercises the same
    envelope-carrying path ``raise_for_error`` gives the real client.
    """
    envelope: dict[str, Any] = {"status": "error", "code": code, "message": message}
    envelope.update(fields)
    return LithosToolError(message, code=code, envelope=envelope)


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


class FakeWriteStore:
    """Seed plus overlay: the effective task store a fake's reads answer from.

    Reads are views, never copies of the dataset; writes mutate the overlay and
    return the canonical upstream payload (the fake's client normalizes it with
    the same normalizer the real client uses, so both legs agree by
    construction). Every refusal is a coded :class:`LithosToolError` carrying
    its full envelope.

    Id PREFIXES are resolved where a Lens request can carry one — create's
    predecessors and parent, and both endpoints of an edge write (hence
    ``ambiguous_id_prefix`` there). Complete, cancel and reopen look an id up
    exactly: upstream resolves a prefix for those too, but the write funnel
    always sends the full id of a task it has just read, so the prefix path is
    not a Lens behaviour to reproduce (see each contract's notes).
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
            if status == "completed":
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

    # ── writes ─────────────────────────────────────────────────────────

    def complete(self, task_id: str, *, outcome: str = "") -> dict[str, Any]:
        """Complete an OPEN task, releasing its claims; answers ``unblocked``."""
        task = self._open_task(task_id)
        before = self._dependent_readiness(task.id)
        self.overlay.statuses[task.id] = "completed"
        self.overlay.outcomes[task.id] = outcome
        self.overlay.resolved_at[task.id] = _now()
        self.overlay.released_claims.add(task.id)
        unblocked = [
            dependent
            for dependent, was_ready in before.items()
            if not was_ready and self.is_ready(dependent)
        ]
        return self._resolved(task, unblocked=unblocked)

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Cancel an OPEN task, releasing its claims.

        The reason is deliberately not recorded: upstream puts it in the log
        and the ``task.cancelled`` event only, never on the task row
        (ROADMAP ledger #6), so a fake that stored it would let a test assert
        a fact the real server does not keep.
        """
        task = self._open_task(task_id)
        self.overlay.statuses[task.id] = "cancelled"
        self.overlay.resolved_at[task.id] = _now()
        self.overlay.released_claims.add(task.id)
        return self._resolved(task)

    def reopen(self, task_id: str, *, agent: str) -> dict[str, Any]:
        """Return a terminal task to open; answers ``reblocked``.

        ``reblocked`` is computed, not special-cased, and that is why a
        cancelled task's reopen reports nobody: its dependents were stranded
        (``blocker_unsatisfiable``), so they were not ready before the write
        either.
        """
        task = self.task(task_id)
        if task is None:
            raise write_error("task_not_found", f"Task '{task_id}' not found.")
        if task.status == "open":
            raise write_error(
                "task_not_resolved",
                f"Task '{task.id}' is not resolved (status: open).",
            )
        before = self._dependent_readiness(task.id)
        self.overlay.statuses[task.id] = "open"
        # Upstream clears both and records the reopen as a finding, which is
        # then the only surviving evidence of the prior outcome.
        self.overlay.outcomes[task.id] = ""
        self.overlay.resolved_at[task.id] = ""
        self.overlay.sequence += 1
        self.overlay.findings.setdefault(task.id, []).append(
            FindingRecord(
                id=f"finding-reopen-{self.overlay.sequence}",
                task_id=task.id,
                agent=agent,
                summary=(
                    f"{REOPENED_FINDING_PREFIX} reopened from {task.status} by "
                    f"{agent} (prior outcome: {task.outcome or 'none'})"
                ),
                created_at=_now(),
            )
        )
        reblocked = [
            dependent
            for dependent, was_ready in before.items()
            if was_ready and not self.is_ready(dependent)
        ]
        return self._resolved(task, reblocked=reblocked)

    def create(
        self,
        *,
        title: str,
        agent: str,
        description: str = "",
        tags: tuple[str, ...] | list[str] = (),
        metadata: dict[str, Any] | None = None,
        task_type: str = "task",
        depends_on: tuple[str, ...] | list[str] = (),
        parent_task_id: str = "",
    ) -> dict[str, Any]:
        """Mint a task, resolving its predecessor and parent id prefixes."""
        if not title.strip():
            raise write_error("invalid_input", "title must not be empty.")
        if task_type not in KNOWN_TASK_TYPES:
            raise write_error(
                "invalid_input",
                f"task_type must be one of task/epic/gate, got {task_type!r}.",
            )
        task_metadata = dict(metadata or {})
        if task_type == "gate":
            self._validate_gate(task_metadata)
        resolved_depends_on = [self.resolve_id(entry) for entry in depends_on]
        resolved_parent = self.resolve_id(parent_task_id) if parent_task_id else ""
        self.overlay.sequence += 1
        new_id = f"fake-created-{self.overlay.sequence}"
        created_at = _now()
        self.overlay.created.append(
            TaskRecord(
                id=new_id,
                title=title,
                description=description,
                status="open",
                created_by=agent,
                created_at=created_at,
                tags=tuple(tags),
                metadata=task_metadata,
                task_type=task_type,
            )
        )
        for predecessor in resolved_depends_on:
            self._insert_edge(predecessor, new_id, "blocks", agent, {}, created_at)
        if resolved_parent:
            self._insert_edge(
                resolved_parent, new_id, "parent_child", agent, {}, created_at
            )
        payload: dict[str, Any] = {
            "success": True,
            "task_id": new_id,
            "title": title,
            "updated_at": created_at,
        }
        if resolved_depends_on:
            payload["depends_on"] = resolved_depends_on
        if resolved_parent:
            payload["parent_task_id"] = resolved_parent
        return payload

    def edge_upsert(
        self,
        *,
        from_task_id: str,
        to_task_id: str,
        edge_type: str,
        agent: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert a relation, or replace an existing one's metadata.

        An existing ``(from, to, type)`` answers the SAME success payload as an
        insert and keeps its ``created_by`` / ``created_at`` — which is why
        those fields are evidence of who inserted an edge (T3 D11) and this
        method is careful not to touch them.
        """
        if edge_type not in KNOWN_EDGE_TYPES:
            raise write_error(
                "invalid_edge_type",
                f"Edge type {edge_type!r} is not accepted; expected one of "
                f"{sorted(KNOWN_EDGE_TYPES)}.",
            )
        # Checked on the raw arguments first: a self-edge needs no lookup, so
        # it is refused before either id is resolved.
        if from_task_id == to_task_id:
            raise write_error(
                "self_edge", f"A task cannot be related to itself ({from_task_id})."
            )
        source_id = self.resolve_id(from_task_id)
        target_id = self.resolve_id(to_task_id)
        if source_id == target_id:
            raise write_error(
                "self_edge", f"A task cannot be related to itself ({source_id})."
            )
        if edge_type == "waits_on_gate":
            source = self.task(source_id)
            if source is None or source.task_type != "gate":
                raise write_error(
                    "not_a_gate",
                    f"Task '{source_id}' is not a gate; only a gate can be waited on.",
                )
        if edge_type == "parent_child":
            existing = self._existing_parent(target_id, source_id)
            if existing:
                raise write_error(
                    "parent_exists",
                    f"Task '{target_id}' already has parent '{existing}'.",
                )
        if edge_type in BLOCKING_EDGE_TYPES:
            path = self._blocking_path(target_id, source_id)
            if path is not None:
                members = " -> ".join([*path, target_id])
                raise write_error("cycle", f"dependency cycle: {members}")
        key = (source_id, target_id, edge_type)
        if key in self._edge_set():
            # The metadata is replaced wholesale; nothing else about the edge
            # moves.
            self.overlay.edge_metadata[key] = dict(metadata or {})
        else:
            self._insert_edge(
                source_id, target_id, edge_type, agent, dict(metadata or {}), _now()
            )
        return {"success": True}

    def _resolved(self, task: TaskRecord, **extra: Any) -> dict[str, Any]:
        """A write's success payload: the resolved task, plus the tool's extras.

        Complete, reopen and cancel all answer ``success`` with the task they
        resolved and acted on and the stamp they wrote, so the shape is built
        once here; ``unblocked`` / ``reblocked`` ride along per tool.
        """
        payload: dict[str, Any] = {
            "success": True,
            "task_id": task.id,
            "title": task.title,
            "updated_at": self.overlay.resolved_at.get(task.id) or _now(),
        }
        payload.update(extra)
        return payload

    def resolve_id(self, task_id: str) -> str:
        """Resolve a full id or an id prefix, the way the write tools do."""
        ids = [task.id for task in self.tasks()]
        if task_id in ids:
            return task_id
        matches = sorted(
            candidate for candidate in ids if candidate.startswith(task_id)
        )
        if not matches:
            raise write_error("task_not_found", f"Task '{task_id}' not found.")
        if len(matches) > 1:
            raise write_error(
                "ambiguous_id_prefix",
                f"Task id prefix '{task_id}' matches {len(matches)} tasks.",
                candidates=matches,
            )
        return matches[0]

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

    def _outgoing_blocking(self, task_id: str) -> list[EdgeRecord]:
        return [
            edge
            for edge in self._edge_set().values()
            if edge.from_task_id == task_id and edge.type in BLOCKING_EDGE_TYPES
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
        for edge in self._outgoing_blocking(task_id):
            dependent = self.task(edge.to_task_id)
            if dependent is None or dependent.status != "open":
                continue
            readiness[dependent.id] = self.is_ready(dependent.id)
        return readiness

    def _existing_parent(self, task_id: str, ignoring: str) -> str:
        for edge in self._edge_set().values():
            if (
                edge.type == "parent_child"
                and edge.to_task_id == task_id
                and edge.from_task_id != ignoring
            ):
                return edge.from_task_id
        return ""

    def _blocking_path(self, start: str, goal: str) -> list[str] | None:
        """A blocking path ``start -> … -> goal``, or None if there is none.

        Used the other way round from how it reads: adding ``goal -> start``
        closes a cycle exactly when ``start`` already reaches ``goal``.
        """
        queue: list[list[str]] = [[start]]
        seen = {start}
        while queue:
            path = queue.pop(0)
            for edge in self._outgoing_blocking(path[-1]):
                if edge.to_task_id == goal:
                    return [*path, goal]
                if edge.to_task_id in seen:
                    continue
                seen.add(edge.to_task_id)
                queue.append([*path, edge.to_task_id])
        return None

    def _insert_edge(
        self,
        from_task_id: str,
        to_task_id: str,
        edge_type: str,
        agent: str,
        metadata: dict[str, Any],
        created_at: str,
    ) -> None:
        self.overlay.edges.append(
            EdgeRecord(
                from_task_id=from_task_id,
                to_task_id=to_task_id,
                type=edge_type,
                metadata=metadata,
                created_by=agent,
                created_at=created_at,
            )
        )

    def _open_task(self, task_id: str) -> TaskRecord:
        """The task, if it is open — else ``task_not_found``, as upstream does.

        Complete and cancel apply only to an ``open`` task and answer the SAME
        code for "no such task" and "not open": one code for two facts, which
        is why the write funnel re-reads the task to tell them apart (T3 D6).
        """
        task = self.task(task_id)
        if task is None or task.status != "open":
            raise write_error(
                "task_not_found", f"Task '{task_id}' not found or not open."
            )
        return task

    def _validate_gate(self, metadata: dict[str, Any]) -> None:
        gate_type = str(metadata.get("gate_type") or "")
        if gate_type not in KNOWN_GATE_TYPES:
            raise write_error(
                "invalid_input",
                "a gate requires metadata.gate_type in "
                f"{sorted(KNOWN_GATE_TYPES)}, got {gate_type!r}.",
            )
        if gate_type != "timer":
            return
        ready_at = str(metadata.get("ready_at") or "")
        try:
            datetime.fromisoformat(ready_at)
        except ValueError:
            raise write_error(
                "invalid_input",
                f"a timer gate requires a parseable metadata.ready_at, got "
                f"{ready_at!r}.",
            ) from None


def _now() -> str:
    """A write stamp at the second precision every fixture timestamp uses."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()
