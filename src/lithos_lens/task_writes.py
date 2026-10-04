"""The five Lithos task-write tools: their request shapes and their results.

Both directions of one wire, in one module. The ``*_arguments`` builders are
the single definition of what Lens sends — the real client passes them straight
to ``_call_tool`` and the in-memory fake records them in its write log, so the
fake's call log and the server's arguments cannot drift apart; the contract
suite pins the builders through the real client against
``tests/contracts/lithos_task_*.json``. The frozen records are what comes
back, normalized at the usual boundary (unknown fields dropped, absent lists
empty) so no caller reads a raw payload.

Nothing here decides WHETHER a write happens or what it means to the operator:
the pre-check, the error mapping and the receipts are the write funnel's
(T3 D4-D6). This module is the transport shape, like ``task_graph`` is for the
graph reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "TaskCancelResult",
    "TaskCompleteResult",
    "TaskCreateResult",
    "TaskEdgeUpsertResult",
    "TaskReopenResult",
    "cancel_arguments",
    "complete_arguments",
    "create_arguments",
    "edge_upsert_arguments",
    "normalize_task_cancel",
    "normalize_task_complete",
    "normalize_task_create",
    "normalize_task_edge_upsert",
    "normalize_task_reopen",
    "reopen_arguments",
]


# ── results ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskCompleteResult:
    """``lithos_task_complete``: the task closed, and who it released.

    ``unblocked`` holds the released tasks' IDS, not records — the payload
    names only the completed task — in the order upstream reported them.
    ``task_id``/``title`` are the task upstream resolved and acted on.
    """

    success: bool
    task_id: str = ""
    title: str = ""
    updated_at: str = ""
    unblocked: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskReopenResult:
    """``lithos_task_reopen``: the task back to open, and who it re-blocked.

    ``reblocked`` holds task ids. It is empty when a *cancelled* task is
    reopened — those dependents were already stranded, so reopening un-strands
    them rather than re-blocking anyone (upstream states this explicitly).
    """

    success: bool
    task_id: str = ""
    title: str = ""
    updated_at: str = ""
    reblocked: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskCancelResult:
    """``lithos_task_cancel``: the task cancelled and every claim released.

    The reason is not echoed: it reaches the log and the ``task.cancelled``
    event only, never the task row (ROADMAP ledger #6).
    """

    success: bool
    task_id: str = ""
    title: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class TaskCreateResult:
    """``lithos_task_create``: the minted id, plus the links it resolved.

    ``depends_on`` and ``parent_task_id`` are the FULL ids upstream resolved
    the request's (possibly abbreviated) ones to, and are empty when the
    request supplied none.
    """

    success: bool
    task_id: str
    title: str = ""
    updated_at: str = ""
    depends_on: tuple[str, ...] = ()
    parent_task_id: str = ""


@dataclass(frozen=True)
class TaskEdgeUpsertResult:
    """``lithos_task_edge_upsert``: the relation exists.

    Deliberately field-poor: an upsert answers an insert and a metadata
    replacement with the same ``{"success": true}``, so nothing in the payload
    says which happened. Whether Lens's own call inserted the edge is settled
    by reading the edge back (T3 D11), not from here — which is also why the
    resolved endpoint ids and titles the payload carries beside ``success``
    are not modeled (see the contract's ``observed_divergences``).
    """

    success: bool


# ── requests ────────────────────────────────────────────────────────────


def complete_arguments(
    task_id: str, *, agent: str, outcome: str = ""
) -> dict[str, Any]:
    """Arguments for ``lithos_task_complete``.

    ``cited_nodes`` / ``misleading_nodes`` / ``receipt_id`` are accepted
    upstream and never sent: Lens has no node-feedback surface on this path.
    """
    arguments: dict[str, Any] = {"task_id": task_id, "agent": agent}
    if outcome:
        arguments["outcome"] = outcome
    return arguments


def reopen_arguments(task_id: str, *, agent: str) -> dict[str, Any]:
    """Arguments for ``lithos_task_reopen`` (the whole surface it takes)."""
    return {"task_id": task_id, "agent": agent}


def cancel_arguments(task_id: str, *, agent: str, reason: str = "") -> dict[str, Any]:
    """Arguments for ``lithos_task_cancel``; ``reason`` is optional upstream."""
    arguments: dict[str, Any] = {"task_id": task_id, "agent": agent}
    if reason:
        arguments["reason"] = reason
    return arguments


def create_arguments(
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
    """Arguments for ``lithos_task_create``.

    ``task_type`` is ALWAYS sent even when it equals the upstream default, the
    same discipline the read side applies to ``with_claims``: an upstream
    default flip must not silently re-type every task Lens creates.
    """
    arguments: dict[str, Any] = {
        "title": title,
        "agent": agent,
        "task_type": task_type,
    }
    if description:
        arguments["description"] = description
    if tags:
        arguments["tags"] = list(tags)
    if metadata:
        arguments["metadata"] = dict(metadata)
    if depends_on:
        arguments["depends_on"] = list(depends_on)
    if parent_task_id:
        arguments["parent_task_id"] = parent_task_id
    return arguments


def edge_upsert_arguments(
    *,
    from_task_id: str,
    to_task_id: str,
    edge_type: str,
    agent: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Arguments for ``lithos_task_edge_upsert``.

    The edge type travels as ``type`` on the wire; it is ``edge_type`` in
    Python so no caller shadows the builtin.
    """
    arguments: dict[str, Any] = {
        "from_task_id": from_task_id,
        "to_task_id": to_task_id,
        "type": edge_type,
        "agent": agent,
    }
    if metadata:
        arguments["metadata"] = dict(metadata)
    return arguments


# ── normalizers ─────────────────────────────────────────────────────────


def normalize_task_complete(payload: dict[str, Any]) -> TaskCompleteResult:
    return TaskCompleteResult(
        success=bool(payload.get("success")),
        task_id=str(payload.get("task_id") or ""),
        title=str(payload.get("title") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        unblocked=_id_tuple(payload.get("unblocked")),
    )


def normalize_task_reopen(payload: dict[str, Any]) -> TaskReopenResult:
    return TaskReopenResult(
        success=bool(payload.get("success")),
        task_id=str(payload.get("task_id") or ""),
        title=str(payload.get("title") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        reblocked=_id_tuple(payload.get("reblocked")),
    )


def normalize_task_cancel(payload: dict[str, Any]) -> TaskCancelResult:
    return TaskCancelResult(
        success=bool(payload.get("success")),
        task_id=str(payload.get("task_id") or ""),
        title=str(payload.get("title") or ""),
        updated_at=str(payload.get("updated_at") or ""),
    )


def normalize_task_create(payload: dict[str, Any]) -> TaskCreateResult:
    return TaskCreateResult(
        success=bool(payload.get("success")),
        task_id=str(payload.get("task_id") or ""),
        title=str(payload.get("title") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        depends_on=_id_tuple(payload.get("depends_on")),
        parent_task_id=str(payload.get("parent_task_id") or ""),
    )


def normalize_task_edge_upsert(payload: dict[str, Any]) -> TaskEdgeUpsertResult:
    return TaskEdgeUpsertResult(success=bool(payload.get("success")))


def _id_tuple(raw: Any) -> tuple[str, ...]:
    """An id list from a write payload: order kept, empties dropped."""
    if not isinstance(raw, list):
        return ()
    return tuple(str(entry) for entry in raw if entry)
