"""Task-graph transport records and normalizers (Lithos 0.4 task graph).

Transport-faithful mirrors of the graph-read payloads: blocked-task rows
(``lithos_task_blocked``) and task edges (``lithos_task_edge_list``). These
records carry the raw server strings — an unknown future kind or edge type
survives round-trip. Presentation enrichment (predecessor titles, gate display
names) is a later-slice view-model concern, not part of this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lithos_lens.tasks import TaskRecord, normalize_task

# Known blocker kinds emitted by lithos ``_compute_blockers``; transport
# preserves the raw value, so this set is documentation, not validation.
KNOWN_BLOCKER_KINDS = frozenset({"task", "gate", "blocker_unsatisfiable", "cycle"})

# Known task-graph edge types; same passthrough policy as blocker kinds.
KNOWN_EDGE_TYPES = frozenset(
    {"blocks", "parent_child", "discovered_from", "waits_on_gate"}
)


@dataclass(frozen=True)
class BlockerRecord:
    """One structured reason a task is blocked, from a lithos_task_blocked row.

    Transport-faithful mirror of a lithos ``_compute_blockers`` entry: every
    branch emits exactly ``{kind, task_id, type, status, message}``.

    - ``kind`` — see KNOWN_BLOCKER_KINDS: ``task`` (open blocking
      predecessor), ``gate`` (waiting on a gate), ``blocker_unsatisfiable``
      (cancelled predecessor), ``cycle`` (dependency cycle). Raw server
      string; unknown kinds survive.
    - ``task_id`` — the blocking predecessor (for ``cycle`` the predecessor
      the cycle was detected through).
    - ``type`` — the unsatisfied edge's type (e.g. ``blocks``,
      ``waits_on_gate``). Raw server string.
    - ``status`` — the predecessor's status (``open``; ``cancelled`` for
      ``blocker_unsatisfiable``).
    - ``message`` — human-readable explanation (carries the gate type / cycle
      path detail).
    """

    kind: str
    task_id: str = ""
    type: str = ""
    status: str = ""
    message: str = ""


@dataclass(frozen=True)
class EdgeRecord:
    """A task-graph edge as returned by lithos_task_edge_list.

    ``type`` is the raw server string (see KNOWN_EDGE_TYPES); an unknown
    future type survives round-trip. ``direction`` is relative to the task the
    edge list was fetched for (``incoming`` / ``outgoing``); it is empty when
    the source does not report one.
    """

    from_task_id: str
    to_task_id: str
    type: str
    direction: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_by: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class BlockedTaskRecord:
    """A row from lithos_task_blocked: the task plus its structured blockers.

    lithos_task_blocked does not return claims — the master open list supplies
    those — so this pairs the task identity with just its blocker reasons.
    """

    task: TaskRecord
    blockers: tuple[BlockerRecord, ...] = ()


def normalize_blocker(raw: dict[str, Any]) -> BlockerRecord:
    # Transport-faithful passthrough of the lithos _compute_blockers shape;
    # unknown kinds/types survive round-trip.
    return BlockerRecord(
        kind=str(raw.get("kind") or ""),
        task_id=str(raw.get("task_id") or ""),
        type=str(raw.get("type") or ""),
        status=str(raw.get("status") or ""),
        message=str(raw.get("message") or ""),
    )


def normalize_blocked_task(raw: dict[str, Any]) -> BlockedTaskRecord:
    return BlockedTaskRecord(
        task=normalize_task(raw),
        blockers=tuple(
            normalize_blocker(blocker)
            for blocker in raw.get("blockers") or []
            if isinstance(blocker, dict)
        ),
    )


def normalize_edge(raw: dict[str, Any]) -> EdgeRecord:
    return EdgeRecord(
        from_task_id=str(raw.get("from_task_id") or ""),
        to_task_id=str(raw.get("to_task_id") or ""),
        # Raw passthrough: an unknown edge type survives round-trip.
        type=str(raw.get("type") or ""),
        direction=str(raw.get("direction") or ""),
        metadata=dict(raw.get("metadata") or {}),
        created_by=str(raw.get("created_by") or ""),
        created_at=str(raw.get("created_at") or ""),
    )
