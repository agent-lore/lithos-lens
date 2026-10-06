"""Frontmatter-driven metadata chips + lede for the knowledge note view.

Foundation companion to ``knowledge`` (split out to keep that module under the
architecture god-module ceiling, the way ``task_graph`` sits beside ``tasks``).
A note's frontmatter drives a row of scannable chips (note_type, status,
access_scope, namespace, confidence), a ``summaries.short`` lede rendered above
the body, a ``supersedes`` back-reference, and an authorship line
(REQUIREMENTS.md §6.4). The view model is built purely from
``NoteRecord.metadata`` so the ``/note/{id}`` route stays a thin orchestrator;
every field degrades to empty when its frontmatter key is absent, so the
template renders only the chips a note actually carries.

The /knowledge landing's result cards and recent-list rows carry the same
chips, compact. Neither ``lithos_search`` rows nor ``lithos_list`` items carry
these fields (ROADMAP ledger #16), so :func:`load_list_chips` reads each row's
frontmatter with the cheap ``lithos_read(id, max_length=1)`` the related
panel's title resolution uses — capped, and once per id per request.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from lithos_lens.tasks import NoteRecord

logger = logging.getLogger(__name__)

# Anything outside this class-safe set collapses to ``-`` in a status slug.
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class NoteMetadata:
    """Frontmatter-derived chips, lede, supersedes link, and authorship (§6.4)."""

    note_type: str = ""
    status: str = ""
    confidence: str = ""
    access_scope: str = ""
    namespace: str = ""
    lede: str = ""
    supersedes: str = ""
    author: str = ""
    contributors: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    @property
    def has_chips(self) -> bool:
        return self.has_standing_chips or bool(self.supersedes)

    @property
    def has_standing_chips(self) -> bool:
        """Whether any chip a list row carries is present.

        The note's standing — type, status, scope, namespace, confidence — and
        not ``supersedes``, which is a link to another note and stays on the
        note page.
        """
        return bool(
            self.note_type
            or self.status
            or self.confidence
            or self.access_scope
            or self.namespace
        )

    @property
    def has_authorship(self) -> bool:
        return bool(
            self.author or self.contributors or self.created_at or self.updated_at
        )

    @property
    def status_slug(self) -> str:
        """A class-safe slug of ``status`` for the ``note-status-<slug>`` CSS hook.

        ``status`` is untrusted author frontmatter. The template interpolates it
        as a *class suffix*, and Jinja autoescape leaves whitespace untouched, so
        a raw value like ``open banner-warning`` would inject a second,
        attacker-chosen class token onto the chip (UI/content spoofing — e.g.
        recolouring a quarantined note to look benign). Lower-casing and
        collapsing every non ``[a-z0-9]`` run to a single ``-`` yields exactly one
        token, so no extra class can be smuggled in.
        """
        return _NON_SLUG_RE.sub("-", self.status.lower()).strip("-")


def build_note_metadata(note: NoteRecord) -> NoteMetadata:
    """Project a note's frontmatter into the §6.4 metadata view model."""
    meta = note.metadata
    return NoteMetadata(
        note_type=_meta_str(meta, "note_type"),
        status=_meta_str(meta, "status"),
        confidence=_format_confidence(meta.get("confidence")),
        access_scope=_format_scope(_meta_str(meta, "access_scope")),
        namespace=_derive_namespace(meta),
        lede=_summary_short(meta.get("summaries")),
        supersedes=_meta_str(meta, "supersedes"),
        author=_meta_str(meta, "author"),
        contributors=_str_tuple(meta.get("contributors")),
        created_at=_meta_str(meta, "created_at"),
        updated_at=_meta_str(meta, "updated_at"),
    )


def _meta_str(meta: dict[str, Any], key: str) -> str:
    """A trimmed string frontmatter value, or empty for a missing/non-string."""
    value = meta.get(key)
    return value.strip() if isinstance(value, str) else ""


def _format_scope(value: str) -> str:
    """The access-scope chip value — empty for the ``shared`` default (§6.4).

    The PRD user story is explicit: "access scope WHEN NOT SHARED". ``shared``
    is the default visibility, so a chip for it is noise; ``task`` /
    ``agent_private`` (and any other non-shared value — odd variants stay
    visible, this is a knowledge-hygiene surface) are the signal worth a chip.
    Only the exact lowercase ``shared`` is the quiet default.
    """
    return "" if value == "shared" else value


def _format_confidence(value: Any) -> str:
    """Render a ``0..1`` confidence fraction as a whole percentage (§6.4).

    Lithos stores confidence as a fraction; frontmatter is untrusted input, so
    anything non-numeric (or a bool, which is an ``int`` subclass) and any
    out-of-range value renders no chip rather than a nonsense/misleading one
    (``2`` must not become a "200%" chip). The range check also rejects
    NaN/inf, which would otherwise make ``round()`` raise mid-render.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if not 0 <= value <= 1:
        return ""
    return f"{round(value * 100)}%"


def _summary_short(summaries: Any) -> str:
    """The ``summaries.short`` lede text, or empty when absent."""
    if isinstance(summaries, dict):
        short = summaries.get("short")
        if isinstance(short, str):
            return short.strip()
    return ""


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Normalize a ``contributors``-style value into a tuple of trimmed names."""
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    return ()


def _derive_namespace(meta: dict[str, Any]) -> str:
    """Explicit ``namespace``, else the directory of a ``path`` frontmatter value."""
    explicit = _meta_str(meta, "namespace")
    if explicit:
        return explicit
    path = _meta_str(meta, "path")
    return path.rsplit("/", 1)[0] if "/" in path else ""


# ── Landing list chips ─────────────────────────────────────────────────


class NoteHeadReader(Protocol):
    """The one Lithos read the landing's chip fan-out needs."""

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None: ...


@dataclass(frozen=True)
class ListChips:
    """The landing rows' chips, keyed by note id, and what reading them cost."""

    by_id: Mapping[str, NoteMetadata] = field(default_factory=dict)
    #: ``lithos_read`` calls spent — one per distinct id, at most the cap.
    fanout: int = 0
    #: The cap, set only when the list named more distinct ids than it, so
    #: the page can say which rows are chipless and why.
    capped_at: int = 0

    def for_note(self, note_id: str) -> NoteMetadata | None:
        """The row's chips, or ``None`` for a row past the cap, a failed read,
        or a note whose frontmatter carries none of them."""
        meta = self.by_id.get(note_id)
        return meta if meta is not None and meta.has_standing_chips else None


async def load_list_chips(
    lithos: NoteHeadReader, ids: Iterable[str], *, cap: int
) -> ListChips:
    """Read the chips for the first ``cap`` distinct ids, once each.

    ``ids`` is every row the page shows, in page order, across all its
    sections; deduping here is the per-request cache, so an id two rows (or two
    sections) share is read once. Each read is the related panel's cheap
    ``max_length=1`` read, which still returns complete frontmatter, and runs
    under the same process-wide call gate (``mcp_transport``) — the cap bounds
    the call count, the gate how many run at once. A failed read leaves only
    its own row chipless; it never fails the list.
    """
    distinct = list(dict.fromkeys(note_id for note_id in ids if note_id))
    to_read = distinct[: max(cap, 0)]

    async def fetch(note_id: str) -> tuple[NoteRecord | None, bool]:
        try:
            return await lithos.read_note(note_id, max_length=1), False
        except Exception:
            return None, True

    results = await asyncio.gather(*(fetch(note_id) for note_id in to_read))
    failures = sum(1 for _, failed in results if failed)
    if failures:
        # One aggregate line, as the related panel's title lookup logs: a list
        # of 200 rows against a flaky backend must not spam the log.
        logger.warning(
            "knowledge list chip read failed for %d of %d ids", failures, len(to_read)
        )
    return ListChips(
        by_id={
            note_id: build_note_metadata(note)
            for note_id, (note, _) in zip(to_read, results, strict=True)
            if note is not None
        },
        fanout=len(to_read),
        capped_at=cap if len(distinct) > len(to_read) else 0,
    )
