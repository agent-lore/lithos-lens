"""The fake's one note write: ``lithos_note_update``'s tag patch (K4 feedback).

A mixin of :class:`~lithos_lens.fake_lithos.FakeLithosClient`, on the
``lithos_writes`` precedent: the client's note reads answer from
:meth:`FakeNoteWrites.effective_notes` — the frozen dataset under this
instance's overlay — so a patch changes what later reads, lists and tag counts
return, and two fakes never share one.

Fidelity to upstream (``tests/contracts/lithos_note_update.json``): the patch
replaces the tag list whole, bumps ``metadata.version`` and emits
``note.updated`` with ``{id, title, path}``; a stale ``expected_version``
answers ``version_conflict`` with the ``current_version`` — returned, not
raised — and an unknown id is the resolver's ``note_not_found``. Lens sends
full ids only, so the fake resolves no prefixes.
"""

from __future__ import annotations

from collections import ChainMap
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from lithos_lens import note_writes
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_writes import FakeWriteOutcome, write_error
from lithos_lens.tasks import NoteRecord

__all__ = ["FakeNoteWrites"]


class FakeNoteWrites:
    """``note_update`` and the note overlay it writes into.

    ``pending_note_writes`` stands in for another writer racing Lens: each
    ``note_update`` first applies (and drops) the next entry — a tag list some
    agent set between Lens's read and its write, bumping the version as that
    write would. One entry makes Lens's write conflict once; two, twice.
    """

    # Set by the host client's constructor.
    dataset: FakeLithosDataset
    write_calls: list[tuple[str, dict[str, Any]]]
    note_overlay: dict[str, NoteRecord]
    pending_note_writes: list[tuple[str, ...]]

    async def _publish(self, written: FakeWriteOutcome) -> None:
        """Overridden by ``FakeLithosClient``, which owns the event hub."""
        raise NotImplementedError

    def effective_notes(self) -> Mapping[str, NoteRecord]:
        """Every note as reads see it: this instance's patches over the seed."""
        return ChainMap(self.note_overlay, dict(self.dataset.notes))

    async def note_update(
        self,
        note_id: str,
        *,
        agent: str,
        tags: tuple[str, ...] | list[str],
        expected_version: int,
    ) -> note_writes.NoteUpdateResult:
        self.write_calls.append(
            (
                "lithos_note_update",
                note_writes.note_update_arguments(
                    note_id, agent=agent, tags=tags, expected_version=expected_version
                ),
            )
        )
        note = self.effective_notes().get(note_id)
        if note is None:
            raise write_error("note_not_found", f"Document not found: {note_id}")
        if self.pending_note_writes:
            note = self._patch(note, self.pending_note_writes.pop(0))
        current = note.metadata.get("version")
        if current != expected_version:
            return note_writes.normalize_note_update(
                {
                    "status": note_writes.VERSION_CONFLICT,
                    "message": (
                        f"Version conflict: expected {expected_version}, got {current}"
                    ),
                    "warnings": [],
                    "current_version": current,
                }
            )
        note = self._patch(note, tuple(tags))
        await self._publish(
            FakeWriteOutcome(
                payload={},
                event_type="note.updated",
                event={"id": note.id, "title": note.title, "path": self._path(note)},
            )
        )
        return note_writes.normalize_note_update(
            {
                "status": note_writes.NOTE_UPDATED,
                "id": note.id,
                "title": note.title,
                "path": self._path(note),
                "version": note.metadata.get("version"),
                "warnings": [],
            }
        )

    def _patch(self, note: NoteRecord, tags: tuple[str, ...]) -> NoteRecord:
        """Store ``note`` with ``tags``, one version on, as any patch leaves it."""
        version = note.metadata.get("version")
        metadata = {**note.metadata, "tags": list(tags)}
        if isinstance(version, int):
            metadata["version"] = version + 1
        patched = replace(note, tags=tags, metadata=metadata)
        self.note_overlay[note.id] = patched
        return patched

    def _path(self, note: NoteRecord) -> str:
        """The note's path, from the dataset's path index (``""`` for none)."""
        return next(
            (
                path
                for path, known in self.dataset.note_paths.items()
                if known == note.id
            ),
            "",
        )
