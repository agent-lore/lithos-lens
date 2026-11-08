"""The ``lithos_note_update`` wire: the request Lens sends and what comes back.

The note counterpart of :mod:`lithos_lens.task_writes`, on the same rule: the
argument builder is the single definition of what Lens sends — the real client
passes it to ``_call_tool`` and the in-memory fake records it in its write log
— and the result is normalized at the usual boundary, so no caller reads a raw
payload. Shapes from ``tests/contracts/lithos_note_update.json``.

Lens patches TAGS only (REQUIREMENTS §10.2, relevance feedback), always with
``expected_version``: the tag list replaces the note's whole list, so the write
must refuse to land on a note that changed since Lens read the list it edited.

``version_conflict`` is NOT an error envelope upstream: it is an actionable
outcome with its own top-level ``status`` (and the ``current_version``), which
a read-merge-write loop branches on. So it comes back as a result, not as a
raised error; only ``{"status": "error", …}`` envelopes raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "NOTE_UPDATED",
    "VERSION_CONFLICT",
    "NoteUpdateResult",
    "normalize_note_update",
    "note_update_arguments",
]

#: The success ``status`` of a note patch.
NOTE_UPDATED = "updated"

#: The ``status`` a patch carrying a stale ``expected_version`` answers with.
VERSION_CONFLICT = "version_conflict"


@dataclass(frozen=True)
class NoteUpdateResult:
    """``lithos_note_update``: the patch applied, or the version it lost to.

    ``status`` is upstream's top-level status — ``updated`` on success,
    ``version_conflict`` when ``expected_version`` was stale (then
    ``current_version`` is the note's version now, and ``version`` is unset).
    ``note_id`` / ``title`` / ``path`` / ``version`` describe the note as the
    patch left it, on success only.
    """

    status: str
    note_id: str = ""
    title: str = ""
    path: str = ""
    version: int | None = None
    current_version: int | None = None
    message: str = ""

    @property
    def updated(self) -> bool:
        return self.status == NOTE_UPDATED

    @property
    def conflicted(self) -> bool:
        return self.status == VERSION_CONFLICT


def note_update_arguments(
    note_id: str,
    *,
    agent: str,
    tags: tuple[str, ...] | list[str],
    expected_version: int,
) -> dict[str, Any]:
    """The tag patch, as sent: the FULL new list and the version it was read at."""
    return {
        "id": note_id,
        "agent": agent,
        "tags": list(tags),
        "expected_version": expected_version,
    }


def normalize_note_update(payload: dict[str, Any]) -> NoteUpdateResult:
    """A non-error ``lithos_note_update`` answer as a :class:`NoteUpdateResult`."""
    return NoteUpdateResult(
        status=str(payload.get("status") or ""),
        note_id=str(payload.get("id") or ""),
        title=str(payload.get("title") or ""),
        path=str(payload.get("path") or ""),
        version=_version(payload.get("version")),
        current_version=_version(payload.get("current_version")),
        message=str(payload.get("message") or ""),
    )


def _version(raw: Any) -> int | None:
    """A version number, or None — never a bool passed off as one."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None
