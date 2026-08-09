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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from lithos_lens.tasks import NoteRecord

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
        return bool(
            self.note_type
            or self.status
            or self.confidence
            or self.access_scope
            or self.namespace
            or self.supersedes
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
        access_scope=_meta_str(meta, "access_scope"),
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


def _format_confidence(value: Any) -> str:
    """Render a ``0..1`` confidence fraction as a whole percentage (§6.4).

    Lithos stores confidence as a fraction; anything non-numeric (or a bool,
    which is an ``int`` subclass) renders no chip rather than a nonsense value.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
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
