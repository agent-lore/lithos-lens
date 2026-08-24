"""Raw Lithos payloads to frozen records: the normalizer boundary.

Split out of ``tasks.py`` when T1-S5 landed on the S9/S10/S12 slices and pushed
it past the 800-line god-module ceiling. The seam is a real one rather than a
line-count convenience: everything here reads UNTRUSTED upstream JSON and
answers with a validated record, which is the one place in Foundation that
must never assume a shape (AGENTS.md: contracts in ``tests/contracts``).
``tasks.py`` keeps the records themselves and the view models built from them.
"""

from __future__ import annotations

from typing import Any

from lithos_lens.tasks import (
    TASK_STATUSES,
    AgentRecord,
    ClaimRecord,
    FindingRecord,
    NoteRecord,
    NoteSummary,
    TaskRecord,
    TaskStatusName,
    TaskStatusRecord,
)


def normalize_task(raw: dict[str, Any]) -> TaskRecord:
    status_raw = str(raw.get("status") or "open")
    status: TaskStatusName = status_raw if status_raw in TASK_STATUSES else "open"  # type: ignore[assignment]
    # Boundary robustness, NOT version compatibility: task_type is required
    # by the 0.4 contract (tests/contracts/lithos_task_list.json), so a payload
    # without it is malformed. It still defaults to "task" because the workable
    # filter in ``frontier_join`` drops every other type — an empty string would
    # silently delete the row rather than degrade it. An unknown EXPLICIT value
    # survives round-trip untouched.
    task_type = str(raw.get("task_type") or "task")
    claims: tuple[ClaimRecord, ...] | None = None
    if "claims" in raw and raw["claims"] is not None:
        claims = tuple(
            ClaimRecord(
                agent=str(claim.get("agent") or ""),
                aspect=str(claim.get("aspect") or ""),
                expires_at=str(claim.get("expires_at") or ""),
            )
            for claim in raw["claims"]
            if isinstance(claim, dict)
        )
    return TaskRecord(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or "Untitled task"),
        description=str(raw.get("description") or ""),
        status=status,
        created_by=str(raw.get("created_by") or raw.get("agent") or ""),
        created_at=str(raw.get("created_at") or ""),
        tags=tuple(str(tag) for tag in raw.get("tags") or []),
        metadata=dict(raw.get("metadata") or {}),
        outcome=str(raw.get("outcome") or ""),
        completed_at=str(raw.get("completed_at") or ""),
        task_type=task_type,
        resolved_at=str(raw.get("resolved_at") or ""),
        claims=claims,
    )


def normalize_task_status(raw: dict[str, Any]) -> TaskStatusRecord:
    return TaskStatusRecord(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        status=str(raw.get("status") or ""),
        claims=tuple(
            ClaimRecord(
                agent=str(claim.get("agent") or ""),
                aspect=str(claim.get("aspect") or ""),
                expires_at=str(claim.get("expires_at") or ""),
            )
            for claim in raw.get("claims") or []
            if isinstance(claim, dict)
        ),
        metadata=dict(raw.get("metadata") or {}),
    )


def normalize_finding(raw: dict[str, Any], task_id: str) -> FindingRecord:
    return FindingRecord(
        id=str(raw.get("id") or ""),
        task_id=str(raw.get("task_id") or task_id),
        agent=str(raw.get("agent") or ""),
        summary=str(raw.get("summary") or ""),
        knowledge_id=str(raw.get("knowledge_id") or ""),
        created_at=str(raw.get("created_at") or ""),
    )


def normalize_agent(raw: dict[str, Any]) -> AgentRecord:
    return AgentRecord(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        type=str(raw.get("type") or ""),
        last_seen_at=str(raw.get("last_seen_at") or ""),
    )


def normalize_note(raw: dict[str, Any]) -> NoteRecord:
    metadata = dict(raw.get("metadata") or {})
    tags = raw.get("tags") or metadata.get("tags") or []
    return NoteRecord(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or "Untitled document"),
        content=str(raw.get("content") or ""),
        tags=tuple(str(tag) for tag in tags),
        metadata=metadata,
    )


def normalize_note_summary(raw: dict[str, Any]) -> NoteSummary:
    metadata = dict(raw.get("metadata") or {})
    tags = raw.get("tags") or metadata.get("tags") or []
    return NoteSummary(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        path=str(raw.get("path") or ""),
        updated=str(raw.get("updated") or raw.get("updated_at") or ""),
        tags=tuple(str(tag) for tag in tags),
    )
