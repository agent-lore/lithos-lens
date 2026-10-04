"""The fake's five writes: what they change, answer and refuse.

The mutable half of the fake-Lithos seam. :class:`FakeWriteStore` is the write
surface over :class:`~lithos_lens.fake_store.FakeStoreView` — it puts the
deltas into that view's overlay, reads the view's readiness oracle back to
compute ``unblocked`` / ``reblocked``, and otherwise owns what the store next
door deliberately does not: **upstream's refusal vocabulary**. That is the
larger half of the fidelity here, and the part the action slices lean on —
the shared id resolver and its domain (a full id or a prefix of at least six
characters, ``{id, title}`` candidates on an ambiguous one), create's
validations, and the edge write's type / self / gate / parent / cycle checks in
upstream's order. Every refusal is a coded
:class:`~lithos_lens.mcp_transport.LithosToolError` carrying its full envelope,
and every envelope here is either probed against a live Lithos or marked as
transcribed in the matching contract's ``observed_divergences``.

Each write answers a :class:`FakeWriteOutcome`: the tool's success payload AND
the event the real server emits with it, which are not the same fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lithos_lens.fake_store import (
    BLOCKING_EDGE_TYPES,
    HIERARCHY_EDGE_TYPES,
    FakeStoreView,
)
from lithos_lens.gates import KNOWN_GATE_TYPES
from lithos_lens.mcp_transport import LithosToolError
from lithos_lens.task_graph import KNOWN_EDGE_TYPES, EdgeRecord
from lithos_lens.tasks import (
    KNOWN_TASK_TYPES,
    REOPENED_FINDING_PREFIX,
    FindingRecord,
    TaskRecord,
)

__all__ = ["FakeWriteOutcome", "FakeWriteStore", "write_error"]

#: Shortest id PREFIX the upstream resolver will look up; anything shorter
#: that is not a full id is ``invalid_input``. Probed against a live Lithos:
#: "task_id 'zqxj' is too short: pass the full task id or a prefix of at least
#: 6 characters."
MIN_ID_PREFIX_LENGTH = 6

#: Metadata keys ``lithos_task_create`` refuses: dependencies are first-class
#: edges, so the old metadata spelling is an error rather than a no-op.
FORBIDDEN_CREATE_METADATA_KEYS = frozenset({"depends_on", "blocked_on"})


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


@dataclass(frozen=True)
class FakeWriteOutcome:
    """What one write answers: the tool's payload AND the event it emits.

    Two bodies because upstream sends two, and they do not carry the same
    fields: a reopen's event names the ``prior_status`` and ``prior_outcome``
    its own return value has just cleared, and a create's event does not name
    the agent its request did. ``event_type`` is empty for an edge write —
    upstream emits no event for one, and only the hub mints the synthetic
    ``lens.edge_upserted`` (T3 D11).
    """

    payload: dict[str, Any]
    event_type: str = ""
    event: dict[str, Any] = field(default_factory=dict)


class FakeWriteStore(FakeStoreView):
    """The five Lithos writes over the effective store.

    A write mutates the overlay and returns the canonical upstream payload (the
    fake's client normalizes it with the same normalizer the real client uses,
    so both legs agree by construction).

    Every id a write carries goes through :meth:`resolve_id` — all five tools
    share one resolver upstream (probed: complete, cancel, reopen and both edge
    endpoints answer the same too-short and ambiguous-prefix envelopes), so
    they share one here. Note what the resolver does NOT do: an id it cannot
    match comes back unchanged rather than refused, because upstream leaves "no
    such task" to each tool's own lookup — which is how complete and cancel
    answer ``task_not_found`` for "missing OR not open" with one code.
    """

    # ── writes ─────────────────────────────────────────────────────────

    def complete(
        self, task_id: str, *, agent: str, outcome: str = ""
    ) -> FakeWriteOutcome:
        """Complete an OPEN task, releasing its claims; answers ``unblocked``."""
        task = self._open_task(task_id)
        before = self._dependent_readiness(task.id)
        stamp = _now()
        self.overlay.statuses[task.id] = "completed"
        self.overlay.outcomes[task.id] = outcome
        self.overlay.resolved_at[task.id] = stamp
        self.overlay.released_claims.add(task.id)
        unblocked = [
            dependent
            for dependent, was_ready in before.items()
            if not was_ready and self.is_ready(dependent)
        ]
        return FakeWriteOutcome(
            payload=self._resolved(task, unblocked=unblocked),
            event_type="task.completed",
            # The node-feedback arguments ride the event even when nothing
            # sent them, which is the state Lens always leaves them in: it has
            # no node-feedback surface on this path.
            event={
                "task_id": task.id,
                "agent": agent,
                "outcome": outcome or None,
                "updated_at": stamp,
                "cited_nodes": None,
                "misleading_nodes": None,
                "receipt_id": None,
            },
        )

    def cancel(self, task_id: str, *, agent: str, reason: str = "") -> FakeWriteOutcome:
        """Cancel an OPEN task, releasing its claims.

        The reason is deliberately not recorded: upstream puts it in the log
        and the ``task.cancelled`` event only, never on the task row
        (ROADMAP ledger #6), so a fake that stored it would let a test assert
        a fact the real server does not keep.
        """
        task = self._open_task(task_id)
        stamp = _now()
        self.overlay.statuses[task.id] = "cancelled"
        self.overlay.resolved_at[task.id] = stamp
        self.overlay.released_claims.add(task.id)
        return FakeWriteOutcome(
            payload=self._resolved(task),
            event_type="task.cancelled",
            # The one place the reason survives, besides upstream's log.
            event={
                "task_id": task.id,
                "agent": agent,
                "reason": reason or None,
                "updated_at": stamp,
            },
        )

    def reopen(self, task_id: str, *, agent: str) -> FakeWriteOutcome:
        """Return a terminal task to open; answers ``reblocked``.

        ``reblocked`` is computed, not special-cased, and that is why a
        cancelled task's reopen reports nobody: its dependents were stranded
        (``blocker_unsatisfiable``), so they were not ready before the write
        either.
        """
        resolved = self.resolve_id(task_id)
        task = self.task(resolved)
        if task is None:
            raise write_error("task_not_found", f"Task '{resolved}' not found.")
        if task.status == "open":
            raise write_error(
                "task_not_resolved",
                f"Task '{task.id}' is not resolved (status: open).",
            )
        before = self._dependent_readiness(task.id)
        prior_status, prior_outcome = task.status, task.outcome
        stamp = _now()
        self.overlay.statuses[task.id] = "open"
        # Upstream clears both and records the reopen as a finding, which is
        # then the only surviving evidence of the prior outcome — so the event
        # carries both of them too.
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
        return FakeWriteOutcome(
            payload=self._resolved(task, reblocked=reblocked, updated_at=stamp),
            event_type="task.reopened",
            event={
                "task_id": task.id,
                "agent": agent,
                "prior_status": prior_status,
                "prior_outcome": prior_outcome or None,
                "updated_at": stamp,
            },
        )

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
    ) -> FakeWriteOutcome:
        """Mint a task, resolving its predecessor and parent id prefixes."""
        if not title.strip():
            raise write_error("invalid_input", "title must not be empty.")
        if task_type not in KNOWN_TASK_TYPES:
            raise write_error(
                "invalid_input",
                f"task_type must be one of task/epic/gate, got {task_type!r}.",
            )
        task_metadata = dict(metadata or {})
        # Dependencies are first-class edges now, so upstream refuses the old
        # metadata spelling outright rather than silently ignoring it.
        forbidden = sorted(FORBIDDEN_CREATE_METADATA_KEYS & set(task_metadata))
        if forbidden:
            raise write_error(
                "invalid_input",
                f"metadata must not contain {'/'.join(forbidden)} — pass "
                "depends_on instead.",
            )
        if task_type == "gate":
            self._validate_gate(task_metadata)
        resolved_depends_on = [
            self._require_task(self.resolve_id(entry, "depends_on"))
            for entry in depends_on
        ]
        resolved_parent = (
            self._require_task(self.resolve_id(parent_task_id, "parent_task_id"))
            if parent_task_id
            else ""
        )
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
        # No `success` key: create's payload does not carry one (the minted id
        # is the signal) — see the contract's observed_divergences.
        payload: dict[str, Any] = {
            "task_id": new_id,
            "title": title,
            "updated_at": created_at,
        }
        if resolved_depends_on:
            payload["depends_on"] = resolved_depends_on
        if resolved_parent:
            payload["parent_task_id"] = resolved_parent
        return FakeWriteOutcome(
            payload=payload,
            event_type="task.created",
            # No `agent`: create's event does not name the creator, unlike the
            # other three.
            event={"task_id": new_id, "title": title, "updated_at": created_at},
        )

    def edge_upsert(
        self,
        *,
        from_task_id: str,
        to_task_id: str,
        edge_type: str,
        agent: str,
        metadata: dict[str, Any] | None = None,
    ) -> FakeWriteOutcome:
        """Insert a relation, or replace an existing one's metadata.

        An existing ``(from, to, type)`` answers the SAME success payload as an
        insert and keeps its ``created_by`` / ``created_at`` — which is why
        those fields are evidence of who inserted an edge (T3 D11) and this
        method is careful not to touch them.
        """
        if edge_type not in KNOWN_EDGE_TYPES:
            raise write_error(
                "invalid_edge_type",
                f"edge type {edge_type!r} is not accepted in this phase "
                f"(accepted: {sorted(KNOWN_EDGE_TYPES)}).",
            )
        # Checked on the raw arguments first: a self-edge needs no lookup, so
        # it is refused before either id is resolved.
        if from_task_id == to_task_id:
            raise write_error("self_edge", "An edge cannot connect a task to itself.")
        source_id = self.resolve_id(from_task_id, "from_task_id")
        target_id = self.resolve_id(to_task_id, "to_task_id")
        if source_id == target_id:
            raise write_error("self_edge", "An edge cannot connect a task to itself.")
        missing = [
            endpoint
            for endpoint in (source_id, target_id)
            if self.task(endpoint) is None
        ]
        if missing:
            raise write_error(
                "task_not_found", f"edge references nonexistent task(s): {missing}"
            )
        if edge_type == "waits_on_gate":
            source = self.task(source_id)
            if source is None or source.task_type != "gate":
                raise write_error(
                    "not_a_gate",
                    f"a waits_on_gate edge requires the from_task ({source_id}) "
                    f"to be a 'gate' task, got task_type="
                    f"{(source.task_type if source is not None else 'unknown')!r}.",
                )
        if edge_type == "parent_child":
            existing = self._existing_parent(target_id, source_id)
            if existing:
                raise write_error(
                    "parent_exists",
                    f"Task '{target_id}' already has parent '{existing}'.",
                )
        # A cycle is refused whichever graph it closes: a dependency loop over
        # the blocking edges, or a hierarchy that contains itself over
        # parent_child. Only `discovered_from` (provenance) forms neither.
        for types, label in (
            (BLOCKING_EDGE_TYPES, "dependency"),
            (HIERARCHY_EDGE_TYPES, "hierarchy"),
        ):
            if edge_type not in types:
                continue
            path = self._edge_path(target_id, source_id, types)
            if path is not None:
                members = " -> ".join([*path, target_id])
                raise write_error("cycle", f"{label} cycle: {members}")
        key = (source_id, target_id, edge_type)
        if key in self._edge_set():
            # The metadata is replaced wholesale; nothing else about the edge
            # moves.
            self.overlay.edge_metadata[key] = dict(metadata or {})
        else:
            self._insert_edge(
                source_id, target_id, edge_type, agent, dict(metadata or {}), _now()
            )
        source = self.task(source_id)
        target = self.task(target_id)
        # Same payload either way — an insert and a metadata replacement are
        # indistinguishable from it — and NO event: upstream emits none.
        return FakeWriteOutcome(
            payload={
                "success": True,
                "from_task_id": source_id,
                "from_title": source.title if source is not None else "",
                "to_task_id": target_id,
                "to_title": target.title if target is not None else "",
            }
        )

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

    def resolve_id(self, task_id: str, field: str = "task_id") -> str:
        """Resolve a full id or an id prefix, the way every write tool does.

        The id domain is upstream's, not a convenience: a value that is not a
        full id and is shorter than :data:`MIN_ID_PREFIX_LENGTH` is refused
        with ``invalid_input`` rather than looked up, and an ambiguous prefix
        answers ``ambiguous_id_prefix`` whose ``candidates`` are
        ``{id, title}`` RECORDS. Both envelopes, and the ``field``-naming in
        their messages, are probed from a live Lithos. A fake that accepted a
        three-character prefix would let the create and edge slices pass
        requests the real server refuses. Resolution is not existence: see the
        class docstring.
        """
        tasks = self.tasks()
        exact = next((task for task in tasks if task.id == task_id), None)
        if exact is not None:
            return exact.id
        if len(task_id) < MIN_ID_PREFIX_LENGTH:
            raise write_error(
                "invalid_input",
                f"{field} '{task_id}' is too short: pass the full task id or a "
                "prefix of at least 6 characters.",
            )
        matches = sorted(
            (task for task in tasks if task.id.startswith(task_id)),
            key=lambda task: task.id,
        )
        if not matches:
            # NOT this seam's refusal: upstream's resolver hands an unmatched
            # value back and each tool's own lookup answers `task_not_found`
            # (probed — complete says "not found or not in an open state", the
            # edge write "edge references nonexistent task(s)").
            return task_id
        if len(matches) > 1:
            # Records, not id strings — the error mapper offers the titles as
            # choices. Upstream caps the list ("2 or more matches", never a
            # count); no fixture here comes near any plausible cap, so the
            # fake returns every match.
            raise write_error(
                "ambiguous_id_prefix",
                f"Task id prefix '{task_id}' ({field}) is ambiguous: 2 or more "
                "matches. Retry with a longer prefix or a full id from "
                "candidates.",
                candidates=[{"id": task.id, "title": task.title} for task in matches],
            )
        return matches[0].id

    # ── helpers ────────────────────────────────────────────────────────

    def _existing_parent(self, task_id: str, ignoring: str) -> str:
        for edge in self._edge_set().values():
            if (
                edge.type == "parent_child"
                and edge.to_task_id == task_id
                and edge.from_task_id != ignoring
            ):
                return edge.from_task_id
        return ""

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

    def _require_task(self, task_id: str) -> str:
        """``task_id`` back, or ``task_not_found`` — the caller's own lookup."""
        if self.task(task_id) is None:
            raise write_error("task_not_found", f"Task '{task_id}' not found.")
        return task_id

    def _open_task(self, task_id: str) -> TaskRecord:
        """The task, if it is open — else ``task_not_found``, as upstream does.

        Complete and cancel apply only to an ``open`` task and answer the SAME
        code for "no such task" and "not open": one code for two facts, which
        is why the write funnel re-reads the task to tell them apart (T3 D6).
        """
        resolved = self.resolve_id(task_id)
        task = self.task(resolved)
        if task is None or task.status != "open":
            raise write_error(
                "task_not_found", f"Task '{resolved}' not found or not open."
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
