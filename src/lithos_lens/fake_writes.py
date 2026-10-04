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
and every envelope here is the one vendored in the matching contract —
transcribed from lithos 0.5.0 @ ``a4d2d62``, with a newer live server's
differences recorded in its ``observed_divergences``.

Each write answers a :class:`FakeWriteOutcome`: the tool's success payload AND
the event the real server emits with it, which are not the same fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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

#: Length at or above which the resolver treats a value as a FULL id and stops
#: searching, handing it to the calling tool's own lookup. Probed: a 35-char
#: non-match answers the resolver's "No task matches id prefix …", a 36-char
#: one answers complete's own "Task … not found or not in an open state". A
#: Lithos task id is a UUID, hence 36.
FULL_ID_LENGTH = 36

#: How many candidates an ambiguous prefix names. Upstream caps the list, and
#: its message says "<n> or more matches" from the CAPPED count rather than the
#: true total — so the cap is part of the envelope, not a display detail.
MAX_ID_CANDIDATES = 5

#: Metadata keys ``lithos_task_create`` refuses: dependencies are first-class
#: edges, so the old metadata spelling is an error rather than a no-op.
FORBIDDEN_CREATE_METADATA_KEYS = ("depends_on", "blocked_on")


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
    endpoints answer the same too-short, no-match and ambiguous-prefix
    envelopes), so they share one here. Note where the refusal comes FROM: a
    searched prefix that matches nothing is the resolver's own
    ``task_not_found``, while a full-length id is handed through to the calling
    tool's lookup — which is how complete and cancel answer "missing OR not
    open" with that one code.
    """

    # ── writes ─────────────────────────────────────────────────────────

    def complete(
        self, task_id: str, *, agent: str, outcome: str = ""
    ) -> FakeWriteOutcome:
        """Complete an OPEN task, releasing its claims; answers ``unblocked``."""
        task = self._open_task(
            task_id, "Task '{task_id}' not found or not in an open state."
        )
        stamp = self._commit_stamp(task)
        self.overlay.statuses[task.id] = "completed"
        self.overlay.outcomes[task.id] = outcome
        self.overlay.resolved_at[task.id] = stamp
        self.overlay.released_claims.add(task.id)
        # Upstream's `newly_unblocked_by`: every task this one blocks or gates
        # that is ready NOW — not a before/after difference. The two differ only
        # where a waiter was already ready before the write, which is a waiter
        # on a timer gate whose `ready_at` has passed; upstream reports it, so
        # the fake does too.
        unblocked = [
            dependent
            for dependent in self._dependents(task.id)
            if self.is_ready(dependent)
        ]
        return FakeWriteOutcome(
            payload=self._resolved(task, unblocked=unblocked),
            event_type="task.completed",
            # The node-feedback arguments ride the event even when nothing
            # sent them — which is the state Lens always leaves them in, having
            # no node-feedback surface on this path — and they ride it
            # PRE-SERIALIZED: upstream puts `json.dumps(value)` on the event, so
            # an absent one arrives as the literal four-character string
            # "null", not as a JSON null. `normalize_lithos_event` does not
            # decode them, so a consumer sees the string. Reproduced rather
            # than tidied: a fake that sent real nulls would hide it.
            event={
                "task_id": task.id,
                "agent": agent,
                "outcome": outcome or None,
                "updated_at": stamp,
                "cited_nodes": json.dumps(None),
                "misleading_nodes": json.dumps(None),
                "receipt_id": json.dumps(None),
            },
        )

    def cancel(self, task_id: str, *, agent: str, reason: str = "") -> FakeWriteOutcome:
        """Cancel an OPEN task, releasing its claims.

        The reason is deliberately not recorded: upstream puts it in the log
        and the ``task.cancelled`` event only, never on the task row
        (ROADMAP ledger #6), so a fake that stored it would let a test assert
        a fact the real server does not keep.
        """
        # Worded differently from complete's, and without quotes (lithos a4d2d62).
        task = self._open_task(task_id, "Task {task_id} not found or already closed")
        stamp = self._commit_stamp(task)
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

        ``reblocked`` is upstream's ``newly_reblocked_by``: nobody at all
        unless the task was COMPLETED (a cancelled task's dependents were
        stranded, and reopening it un-strands them without newly blocking
        anyone); otherwise each OPEN dependent whose blockers now consist of
        this task alone.
        """
        resolved = self.resolve_id(task_id)
        task = self.task(resolved)
        if task is None:
            raise write_error("task_not_found", f"Task '{resolved}' not found.")
        if task.status == "open":
            raise write_error(
                "task_not_resolved",
                f"Task '{task.id}' is already open; nothing to reopen.",
            )
        prior_status, prior_outcome = task.status, task.outcome
        stamp = self._commit_stamp(task)
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
                summary=f"{REOPENED_FINDING_PREFIX} task reopened (was {prior_status})"
                + (f"; prior outcome: {prior_outcome}" if prior_outcome else ""),
                # `post_finding` reads the clock for itself upstream, after
                # the reopen committed — it is not the task's stamp.
                created_at=self.clock().isoformat(),
            )
        )
        reblocked = (
            [
                dependent
                for dependent in self._dependents(task.id)
                if (row := self.task(dependent)) is not None
                and row.status == "open"
                and (blockers := self.blockers(dependent))
                and all(blocker.task_id == task.id for blocker in blockers)
            ]
            if prior_status == "completed"
            else []
        )
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
        """Mint a task, resolving its predecessor and parent id prefixes.

        There is deliberately NO title rule: upstream accepts an empty title
        and stores it. Lens's own form validation refuses one before the call
        (T3 D10), but a fake that refused it here would be answering for a
        rule the server does not have — and the create slice would then be
        tested against a refusal it will never see.

        Upstream's order (a4d2d62): the tool resolves the parent and then each
        predecessor BEFORE anything else, so a bad id outranks every other
        refusal; then the metadata keys, the task type and the gate rules; and
        only then does existence come in — a full-length id the resolver
        handed through, predecessors (all missing ones in one message) before
        the parent.
        """
        resolved_parent = (
            self.resolve_id(parent_task_id, "parent_task_id") if parent_task_id else ""
        )
        resolved_depends_on = [
            self.resolve_id(entry, "depends_on") for entry in depends_on
        ]
        task_metadata = dict(metadata or {})
        # Dependencies are first-class edges now, so upstream refuses the old
        # metadata spelling outright rather than silently ignoring it — with a
        # code of its own.
        # Named in upstream's own key order, not sorted.
        forbidden = [
            key for key in FORBIDDEN_CREATE_METADATA_KEYS if key in task_metadata
        ]
        if forbidden:
            raise write_error(
                "invalid_metadata_key",
                f"metadata key(s) {forbidden} are no longer accepted: task "
                "dependencies are first-class task edges. Use depends_on on "
                "lithos_task_create, or lithos_task_edge_upsert.",
            )
        if task_type not in KNOWN_TASK_TYPES:
            # Its OWN code too, not `invalid_input`.
            raise write_error(
                "invalid_task_type",
                f"task_type '{task_type}' is not accepted in this phase "
                f"(accepted: {sorted(KNOWN_TASK_TYPES)}).",
            )
        if task_type == "gate":
            task_metadata = self._validate_gate(task_metadata)
        missing = [
            entry
            for entry in dict.fromkeys(resolved_depends_on)
            if self.task(entry) is None
        ]
        if missing:
            raise write_error(
                "task_not_found",
                f"depends_on references nonexistent task(s): {missing}",
            )
        if resolved_parent and self.task(resolved_parent) is None:
            raise write_error(
                "task_not_found",
                f"parent_task_id references nonexistent task: {resolved_parent}",
            )
        # A NEW id, as upstream's uuid4 is: the sequence is per instance and a
        # seed may already hold `fake-created-<n>` (from another fake, say), so
        # an occupied value is skipped rather than shadowed.
        self.overlay.sequence += 1
        while self.task(f"fake-created-{self.overlay.sequence}") is not None:
            self.overlay.sequence += 1
        new_id = f"fake-created-{self.overlay.sequence}"
        created_at = self.clock().isoformat()
        # `create_task` writes one stamp to both created_at and updated_at.
        self.overlay.updated_at[new_id] = created_at
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
        # Deduplicated for the INSERT only: upstream's edge table is unique on
        # (from, to, type) and it dedupes before inserting, while the response
        # still echoes the resolved request list — so a form that submitted the
        # same predecessor twice yields one edge and two echoed entries.
        for predecessor in dict.fromkeys(resolved_depends_on):
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

        Upstream's order (a4d2d62): BOTH endpoints are resolved first (from, then
        to) — so ``("x", "x", "blocks")`` is the resolver's ``invalid_input``,
        not ``self_edge`` — then the edge type, then the self-edge check on the
        RESOLVED ids, then existence, then the type-specific rules.
        """
        source_id = self.resolve_id(from_task_id, "from_task_id")
        target_id = self.resolve_id(to_task_id, "to_task_id")
        if edge_type not in KNOWN_EDGE_TYPES:
            raise write_error(
                "invalid_edge_type",
                f"edge type '{edge_type}' is not accepted in this phase "
                f"(accepted: {sorted(KNOWN_EDGE_TYPES)}).",
            )
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
                    f"task {target_id} already has a parent ({existing}); a task "
                    "may have at most one parent. Remove the existing "
                    "parent_child edge before re-parenting.",
                )
        # A cycle is refused whichever graph it closes: a dependency loop over
        # the blocking edges, or a hierarchy that contains itself over
        # parent_child. Only `discovered_from` (provenance) forms neither.
        # `path` runs target -> … -> source along existing edges; upstream
        # renders the two kinds from different ends (``_find_edge_path``): a
        # dependency cycle from the new edge's source, walking what each
        # member depends on, and a hierarchy cycle from its target, walking
        # parent -> child.
        for types, label in (
            (BLOCKING_EDGE_TYPES, "dependency"),
            (HIERARCHY_EDGE_TYPES, "hierarchy"),
        ):
            if edge_type not in types:
                continue
            path = self._edge_path(target_id, source_id, types)
            if path is not None:
                members = (
                    [*reversed(path), source_id]
                    if label == "dependency"
                    else [*path, target_id]
                )
                raise write_error(
                    "cycle",
                    f"{edge_type} edge {source_id} -> {target_id} would create a "
                    f"{label} cycle: {' -> '.join(members)}",
                )
        key = (source_id, target_id, edge_type)
        if key in self._edge_set():
            # The metadata is replaced wholesale; nothing else about the edge
            # moves.
            self.overlay.edge_metadata[key] = dict(metadata or {})
        else:
            self._insert_edge(
                source_id,
                target_id,
                edge_type,
                agent,
                dict(metadata or {}),
                self.clock().isoformat(),
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
            "updated_at": self.overlay.updated_at[task.id],
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
            # Upstream ids are UUIDs, so an exact match there is always a full
            # id; this fixture's ids are short, so the exact lookup runs first
            # or no test could name a task at all.
            return exact.id
        if len(task_id) < MIN_ID_PREFIX_LENGTH:
            raise write_error(
                "invalid_input",
                f"{field} '{task_id}' is too short: pass the full task id or a "
                "prefix of at least 6 characters.",
            )
        if len(task_id) >= FULL_ID_LENGTH:
            # A full-length id is not a prefix to search: it goes to the
            # calling tool's own lookup, which is what answers "no such task"
            # (complete: "not found or not in an open state"; the edge write:
            # "edge references nonexistent task(s)").
            return task_id
        matches = sorted(
            (task for task in tasks if task.id.startswith(task_id)),
            key=lambda task: task.id,
        )
        if not matches:
            # A searched PREFIX that matched nothing is the resolver's own
            # refusal, with its own message — distinct from the tool-level
            # not-found above, though they share the code.
            raise write_error(
                "task_not_found",
                f"No task matches id prefix '{task_id}' ({field}).",
            )
        if len(matches) > 1:
            # Records, not id strings — the mapper offers the titles as
            # choices — and capped, with the count in the message taken from
            # the capped list exactly as upstream words it.
            candidates = [
                {"id": task.id, "title": task.title}
                for task in matches[:MAX_ID_CANDIDATES]
            ]
            raise write_error(
                "ambiguous_id_prefix",
                f"Task id prefix '{task_id}' ({field}) is ambiguous: "
                f"{len(candidates)} or more matches. Retry with a longer "
                "prefix or a full id from candidates.",
                candidates=candidates,
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

    def _commit_stamp(self, task: TaskRecord) -> str:
        """The ``updated_at`` a complete, cancel or reopen of ``task`` commits.

        Upstream advances it strictly past the row's prior ``updated_at``
        (``_advance_stamp``), so it is kept per task here — a reopen clears
        ``resolved_at`` but not this. Lens's task record carries no
        ``updated_at``, so a row no write has stamped yet falls back to the
        latest stamp it does carry: ``resolved_at``, else ``created_at``.
        """
        prior = (
            self.overlay.updated_at.get(task.id) or task.resolved_at or task.created_at
        )
        stamp = _advance_stamp(self.clock(), prior)
        self.overlay.updated_at[task.id] = stamp
        return stamp

    def _open_task(self, task_id: str, not_found: str) -> TaskRecord:
        """The task, if it is open — else ``task_not_found``, as upstream does.

        Complete and cancel apply only to an ``open`` task and answer the SAME
        code for "no such task" and "not open": one code for two facts, which
        is why the write funnel re-reads the task to tell them apart (T3 D6).
        The message is each tool's own (``not_found``, formatted with the
        resolved id) — the two are worded differently upstream.
        """
        resolved = self.resolve_id(task_id)
        task = self.task(resolved)
        if task is None or task.status != "open":
            raise write_error("task_not_found", not_found.format(task_id=resolved))
        return task

    def _validate_gate(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """The gate's metadata as upstream stores it, or ``invalid_input``.

        Validation is also normalization for a ``timer`` gate: upstream parses
        ``ready_at`` and stores it rewritten to UTC at second precision (a
        naive value read as UTC), so a later read shows the rewritten value,
        never the caller's spelling of it.
        """
        gate_type = metadata.get("gate_type")
        if gate_type not in KNOWN_GATE_TYPES:
            raise write_error(
                "invalid_input",
                "a gate task requires metadata.gate_type in "
                f"{sorted(KNOWN_GATE_TYPES)}, got {gate_type!r}.",
            )
        if gate_type != "timer":
            return metadata
        raw_ready_at = metadata.get("ready_at")
        # Upstream's parser takes a STRING only (anything else is "could not
        # interpret"), reading a trailing `Z` as UTC.
        try:
            if not isinstance(raw_ready_at, str):
                raise ValueError(raw_ready_at)
            ready_at = datetime.fromisoformat(raw_ready_at.replace("Z", "+00:00"))
        except ValueError:
            raise write_error(
                "invalid_input",
                "a 'timer' gate requires a parseable metadata.ready_at (ISO "
                f"datetime), got {raw_ready_at!r}.",
            ) from None
        if ready_at.tzinfo is None:
            ready_at = ready_at.replace(tzinfo=UTC)
        normalized = ready_at.astimezone(UTC).replace(microsecond=0).isoformat()
        return {**metadata, "ready_at": normalized}


def _advance_stamp(now: datetime, prior_raw: str) -> str:
    """Upstream's ``_advance_stamp``: the wall clock, but strictly after the
    row's prior stamp — ``max(now, prior + 1µs)`` — so a write never reuses
    the stamp it replaces when the clock repeats or runs backward. A prior that
    does not parse is ignored; a naive one is read as UTC for the comparison.
    """
    try:
        prior: datetime | None = datetime.fromisoformat(
            prior_raw.replace("Z", "+00:00")
        )
    except ValueError:
        prior = None
    if prior is not None:
        if prior.tzinfo is None:
            prior = prior.replace(tzinfo=UTC)
        now_cmp = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        bumped = prior + timedelta(microseconds=1)
        if bumped > now_cmp:
            return bumped.isoformat()
    return now.isoformat()
