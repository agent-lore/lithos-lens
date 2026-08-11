"""Produced-by-task chip for the note page (K1-S5).

A note may record the task that produced it in ``metadata.source`` (a task
id). The chip is a validated backlink to ``/tasks/{id}`` — it renders ONLY
when ``lithos_task_get`` confirms the source is a real task, so a stale or
malformed source id shows nothing rather than a dead link. ``task_get`` is
the one client method K1 shares with T1; K1 does not hard-depend on T1 having
landed it, so a client without the method degrades to "no chip" (§ slice 5).

Split out of ``knowledge.py`` on the same seam as ``knowledge_metadata``:
one note-page feature per Foundation module, keeping each under the
architecture line budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from lithos_lens.tasks import NoteRecord, TaskRecord


@dataclass(frozen=True)
class ProducedByTask:
    """The validated 'produced by task' chip for a note's ``metadata.source``.

    ``is_task_record`` marks a note whose ``metadata.note_type`` is
    ``task_record`` — the PRD gives those a distinct chip style, since the whole
    note *is* a task's record rather than a document a task merely produced.
    """

    task_id: str
    title: str
    is_task_record: bool = False

    @property
    def url(self) -> str:
        """Task detail URL with the id percent-encoded as one path segment.

        ``normalize_task`` keeps task ids as arbitrary non-empty strings —
        nothing excludes URL-reserved characters — so an id like ``task?42`` or
        ``a#b`` must be encoded, or the ``?``/``#`` would truncate the path and
        route to the wrong (or no) task. ``quote(..., safe="")`` encodes every
        reserved character; such ids then round-trip through the single-segment
        ``/tasks/{task_id}`` route (verified end-to-end in the tests).

        The one character that route cannot carry is a literal ``/``: ASGI
        percent-decodes ``%2F`` back to a separator before routing, so a task id
        containing ``/`` is not addressable by ``/tasks/{task_id}``.
        ``load_produced_by`` therefore never builds a chip for such an id — a
        ``ProducedByTask`` only ever carries a routable ``task_id``.
        """
        return f"/tasks/{quote(self.task_id, safe='')}"


@runtime_checkable
class ProducedByClientProtocol(Protocol):
    """The ``task_get`` capability the chip needs — checked at runtime.

    Declared ``@runtime_checkable`` so ``load_produced_by`` can probe an
    arbitrary client for the method with ``isinstance``: K1 does not require T1
    to have landed ``task_get``, and a client lacking it degrades to no chip.
    """

    async def task_get(self, task_id: str) -> TaskRecord: ...


async def load_produced_by(
    lithos: object,
    note: NoteRecord,
) -> ProducedByTask | None:
    """Validate a note's ``metadata.source`` into a produced-by-task chip.

    Returns ``None`` (render no chip) when there is no ``source``, when the
    client cannot answer ``lithos_task_get`` (the method is absent — K1 tolerates
    T1 not having landed it, so ``lithos`` is typed ``object`` and probed for the
    capability), or when validation fails (an unknown/invalid source id raises).
    Only a successful ``task_get`` yields a chip, so the note view never links to
    a task that isn't there.
    """
    source = note.metadata.get("source")
    if not isinstance(source, str) or not source.strip():
        return None
    if not isinstance(lithos, ProducedByClientProtocol):
        return None
    try:
        task = await lithos.task_get(source.strip())
    except Exception:
        # Unknown/invalid source (task_not_found), a broken response, or an
        # unreachable backend — all degrade to "no chip", never a dead link.
        return None
    if "/" in task.id:
        # The single-segment /tasks/{task_id} route cannot address an id
        # containing a literal "/": ASGI percent-decodes %2F back to a
        # separator before routing, so the chip's href would 404 even though
        # the task validated. No chip beats a dead link.
        return None
    return ProducedByTask(
        task_id=task.id,
        title=task.title,
        is_task_record=str(note.metadata.get("note_type") or "") == "task_record",
    )
