"""The note facts every graph node draws with, and the missing-note ghost (K2 D4).

Edges carry ids, not titles, and ``lithos_list`` cannot select by id, so each
node is read with ``lithos_read(id, max_length=1)``, which still answers the
complete frontmatter. :class:`NoteFactsCache` holds what those reads said —
title, ``note_type``, ``status``, ``namespace``, ``confidence`` and lede, as
:class:`NoteFacts` — for every graph render:

- **Gated fan-out.** Each read runs inside ``async with gate():``. The gate is
  injected (the graph page passes ``graph_cache.graph_fanout_gate``, T2's
  process-wide share), so this module adds no component edge.
- **TTL** (``[knowledge].graph_note_facts_ttl_s``, 3600 s) on an injectable
  monotonic clock, as :class:`~lithos_lens.knowledge_edges.EdgeTable` keeps.
- **Per-render cap** (``[knowledge].graph_title_fanout_cap``, 300). Ids are
  read in the order the caller ranks them; past the cap a node with
  last-known facts keeps them and is ``pending``, one with none is ``unread``
  and labelled by its full id. A cache hit costs nothing against the cap.
- **The ghost.** A read answering ``doc_not_found`` (or ``None``) marks the id
  ``missing`` under the same TTL: edges outlive notes, and the node is drawn
  dashed with its short id rather than dropped. Any other failure is never
  cached — the node is ``unread``, or ``pending`` on its last facts — and is
  logged once per render.
- **Event patches** (:meth:`NoteFactsCache.apply_note_event`).
  ``note.created`` / ``note.updated`` carry ``{id, title, path}`` and fire for
  every metadata change, including the misleading-feedback path that
  quarantines a note, so they patch the title and mark the rest of the entry
  stale: the next draw re-reads it (one read, under the gate and the cap).
  ``note.deleted`` marks the id missing. ``note.renamed`` changes nothing
  here, since no fact the graph draws is the path. The hub applies each
  live note event before its fan-out, and marks every entry stale on
  ``lens.refresh`` (:meth:`NoteFactsCache.mark_all_stale`).

Foundation: the read is injected as one callable ``(id) -> NoteRecord | None``
(the caller binds ``read_note(id, max_length=1)``). ``LithosToolError`` lives
in ``mcp_transport``, which Foundation may not import, so ``doc_not_found`` is
read off the exception duck-typed, as ``graph_fanout`` does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal

from lithos_lens.knowledge_metadata import build_note_metadata
from lithos_lens.tasks import NoteRecord
from lithos_lens.template_vocabulary import short_id

logger = logging.getLogger(__name__)

# Mirror the ``[lithos-lens.knowledge]`` config defaults, as knowledge_edges
# does; ``tests/test_knowledge_facts.py`` pins the two together.
DEFAULT_NOTE_FACTS_TTL_S = 3600
DEFAULT_TITLE_FANOUT_CAP = 300

#: The note events that touch the cache; ``note.renamed`` patches nothing.
NOTE_CREATED = "note.created"
NOTE_UPDATED = "note.updated"
NOTE_RENAMED = "note.renamed"
NOTE_DELETED = "note.deleted"

#: A node's facts in one render: read or cached fresh (``ok``); last-known,
#: with a re-read due but not done (``pending``); never read (``unread``);
#: the note does not exist (``missing`` — the ghost).
FactsState = Literal["ok", "pending", "unread", "missing"]

#: The injected read: ``read_note(id, max_length=1)``.
NoteFactsRead = Callable[[str], Awaitable[NoteRecord | None]]

#: The injected gate provider, called once per read.
GateProvider = Callable[[], asyncio.Semaphore]

#: Injectable MONOTONIC clock, in seconds; it alone decides expiry.
Ticks = Callable[[], float]


def is_doc_not_found(exc: BaseException) -> bool:
    """Whether a Lithos read failed because the note does not exist."""
    return getattr(exc, "code", "") == "doc_not_found"


@dataclass(frozen=True)
class NoteFacts:
    """What a graph node shows of its note: the title and five frontmatter facts.

    The five come from :func:`~lithos_lens.knowledge_metadata.build_note_metadata`,
    so ``confidence`` is the K1 chip's formatted percentage ("85%") and the
    panel's chip partial reads the same values the note page does.
    """

    title: str = ""
    note_type: str = ""
    status: str = ""
    namespace: str = ""
    confidence: str = ""
    lede: str = ""


def note_facts(note: NoteRecord) -> NoteFacts:
    """A read note's facts."""
    meta = build_note_metadata(note)
    return NoteFacts(
        title=note.title,
        note_type=meta.note_type,
        status=meta.status,
        namespace=meta.namespace,
        confidence=meta.confidence,
        lede=meta.lede,
    )


@dataclass(frozen=True)
class NoteFactsAnswer:
    """One node's facts in one render, and the state they are in."""

    id: str
    state: FactsState = "unread"
    #: ``None`` when ``unread`` or ``missing``.
    facts: NoteFacts | None = None

    @property
    def is_missing(self) -> bool:
        return self.state == "missing"

    @property
    def label(self) -> str:
        """The node's label: its title; a ghost's short id; else its full id."""
        if self.facts is not None and self.facts.title:
            return self.facts.title
        return short_id(self.id) if self.is_missing else self.id


@dataclass(frozen=True)
class NoteFactsTally:
    """What one render's lookup cost and found (the request span reports it)."""

    #: Served from a fresh cache entry, no read.
    hits: int = 0
    #: ``lithos_read`` calls spent; at most the cap.
    reads: int = 0
    #: Nodes answered ``missing`` (from a read or the cache).
    missing: int = 0
    #: Nodes that needed a read and were past the cap.
    capped: int = 0
    #: Of ``capped``, those with no last-known facts: ``unread``, labelled
    #: by their full id.
    capped_unread: int = 0
    #: Of ``capped``, those drawn on their last-known facts: ``pending``. The
    #: remainder are last known ``missing`` and stay ghosts.
    capped_pending: int = 0
    #: Reads that failed for a reason other than ``doc_not_found``.
    failed: int = 0


@dataclass(frozen=True)
class NoteFactsBatch:
    """Every asked-for node's answer, and what the lookup cost."""

    answers: Mapping[str, NoteFactsAnswer] = field(
        default_factory=lambda: MappingProxyType({})
    )
    tally: NoteFactsTally = NoteFactsTally()
    #: The cap, set only when nodes went unread for it, so the page can say so.
    capped_at: int = 0

    def for_id(self, node_id: str) -> NoteFactsAnswer:
        """The node's answer; ``unread`` for an id the lookup was not asked for."""
        return self.answers.get(node_id) or NoteFactsAnswer(node_id)


@dataclass
class _Entry:
    facts: NoteFacts | None
    expires_at: float
    missing: bool = False
    #: An event changed the note after this was read; re-read on next draw.
    stale: bool = False


class NoteFactsCache:
    """Process-wide note facts: gated reads, TTL, per-render cap, patches.

    One per process, on ``AppState.note_facts``, built with
    ``graph_cache.graph_fanout_gate`` and the configured knobs; the hub feeds
    it the live note events.
    """

    def __init__(
        self,
        read: NoteFactsRead,
        gate: GateProvider,
        *,
        ttl_s: float = DEFAULT_NOTE_FACTS_TTL_S,
        fanout_cap: int = DEFAULT_TITLE_FANOUT_CAP,
        ticks: Ticks = time.monotonic,
    ) -> None:
        self._read = read
        self._gate = gate
        self._ttl_s = ttl_s
        self._fanout_cap = fanout_cap
        self._ticks = ticks
        self._entries: dict[str, _Entry] = {}
        # Bumped by every patch, so a read that was in flight when an event
        # landed does not overwrite the event's staleness with what it read.
        self._versions: dict[str, int] = {}
        # Reads in flight per id. An event on an id not yet cached still bumps
        # its version while one is in flight: the answer it carries may
        # predate the event, so it must not become a fresh entry.
        self._reading: Counter[str] = Counter()

    async def lookup(
        self, ids: Iterable[str], *, cap: int | None = None
    ) -> NoteFactsBatch:
        """Every id's facts, reading at most ``cap`` of them, in ``ids`` order.

        ``ids`` is ranked by the caller (focus first, then by hop and degree):
        the cap spends its reads from the front. A fresh, unpatched entry is a
        hit; a stale or expired one costs a read like an unknown id.
        """
        limit = max(self._fanout_cap if cap is None else cap, 0)
        now = self._ticks()
        answers: dict[str, NoteFactsAnswer] = {}
        to_read: list[str] = []
        hits = 0
        for node_id in dict.fromkeys(ids):
            entry = self._entries.get(node_id)
            if entry is not None and not entry.stale and now < entry.expires_at:
                hits += 1
                answers[node_id] = _answer(node_id, entry, "ok")
            else:
                to_read.append(node_id)
        reading, past_cap = to_read[:limit], to_read[limit:]
        for node_id in past_cap:
            answers[node_id] = self._last_known(node_id)
        results = await asyncio.gather(
            *(self._read_one(node_id) for node_id in reading)
        )
        failed = 0
        for node_id, (answer, ok) in zip(reading, results, strict=True):
            answers[node_id] = answer
            failed += 0 if ok else 1
        if failed:
            # One aggregate line, as the related panel's title lookup logs: a
            # 300-node render against a flaky backend must not spam the log.
            logger.warning(
                "knowledge graph facts read failed for %d of %d notes",
                failed,
                len(reading),
            )
        tally = NoteFactsTally(
            hits=hits,
            reads=len(reading),
            missing=sum(1 for answer in answers.values() if answer.is_missing),
            capped=len(past_cap),
            capped_unread=sum(
                1 for node_id in past_cap if answers[node_id].state == "unread"
            ),
            capped_pending=sum(
                1 for node_id in past_cap if answers[node_id].state == "pending"
            ),
            failed=failed,
        )
        return NoteFactsBatch(
            answers=MappingProxyType(answers),
            tally=tally,
            capped_at=limit if past_cap else 0,
        )

    async def _read_one(self, node_id: str) -> tuple[NoteFactsAnswer, bool]:
        version = self._versions.get(node_id, 0)
        self._reading[node_id] += 1
        try:
            async with self._gate():
                note = await self._read(node_id)
        except Exception as exc:
            if not is_doc_not_found(exc):
                # Never cached: the next draw tries again.
                return self._last_known(node_id), False
            note = None
        finally:
            self._reading[node_id] -= 1
            if not self._reading[node_id]:
                del self._reading[node_id]
        unpatched = self._versions.get(node_id, 0) == version
        expires_at = self._ticks() + self._ttl_s
        if note is None:
            if unpatched:
                self._entries[node_id] = _Entry(None, expires_at, missing=True)
            return NoteFactsAnswer(node_id, "missing"), True
        facts = note_facts(note)
        if unpatched:
            self._entries[node_id] = _Entry(facts, expires_at)
        return NoteFactsAnswer(node_id, "ok", facts), True

    def _last_known(self, node_id: str) -> NoteFactsAnswer:
        """A node whose re-read is due and not done: its last facts, if any."""
        entry = self._entries.get(node_id)
        if entry is None:
            return NoteFactsAnswer(node_id)
        return _answer(node_id, entry, "pending")

    def mark_missing(self, node_id: str) -> None:
        """Record that the note does not exist (under the TTL, like a read would)."""
        self._bump(node_id)
        self._entries[node_id] = _Entry(None, self._ticks() + self._ttl_s, missing=True)

    def apply_note_event(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        """Patch the cache from a note event; ``False`` when nothing changed.

        ``note.created`` / ``note.updated`` on a cached (or missing) id set its
        title, clear missing and mark the other facts stale; on an id not
        cached they change nothing visible (``False``), since its next draw
        reads it anyway — but a read of that id already in flight may carry
        an answer from before the event, so it is kept from being cached.
        ``note.deleted`` marks the id missing, cached or not. ``note.renamed``
        is a no-op: the graph draws no path. A payload without a string
        ``id`` (a watcher event naming only a path) is a no-op too.
        """
        node_id = payload.get("id")
        if not isinstance(node_id, str) or not node_id:
            return False
        if event_type == NOTE_DELETED:
            self.mark_missing(node_id)
            return True
        if event_type not in (NOTE_CREATED, NOTE_UPDATED):
            return False
        entry = self._entries.get(node_id)
        if entry is None:
            if node_id in self._reading:
                self._bump(node_id)
            return False
        title = payload.get("title")
        facts = entry.facts or NoteFacts()
        if isinstance(title, str):
            facts = replace(facts, title=title)
        self._bump(node_id)
        entry.facts = facts
        entry.missing = False
        entry.stale = True
        return True

    def mark_all_stale(self) -> None:
        """Mark every entry stale, and keep every read in flight from caching.

        The hub's ``lens.refresh`` hook: events were missed, so any entry may
        be one a missed ``note.updated`` (a quarantine, say) should have
        marked. Each is re-read on its next draw, under the gate and the cap;
        until then it keeps its last-known facts, as ``pending``.
        """
        for node_id in set(self._entries) | set(self._reading):
            self._bump(node_id)
        for entry in self._entries.values():
            entry.stale = True

    def _bump(self, node_id: str) -> None:
        self._versions[node_id] = self._versions.get(node_id, 0) + 1


def _answer(node_id: str, entry: _Entry, state: FactsState) -> NoteFactsAnswer:
    if entry.missing:
        return NoteFactsAnswer(node_id, "missing")
    return NoteFactsAnswer(node_id, state, entry.facts)
