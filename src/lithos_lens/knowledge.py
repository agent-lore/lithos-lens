"""Knowledge note rendering and normalization.

Foundation module for the knowledge browser surface (mirrors ``tasks.py``):
frozen dataclasses, pure normalizers, a narrow client protocol, and ``load_*``
orchestrators with per-section state. K1 slice 1 introduced server-side
markdown rendering; K1-S4 adds the *related panel* — one ``lithos_related``
call turned into four scannable sections (outgoing links, back-links,
provenance, typed edges) with link/edge endpoints shown by title rather than
bare id. Later slices add the wiki-link tokenizer, metadata chips, and search
view models.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from html import escape
from typing import Any, Protocol
from urllib.parse import urlparse

from markdown_it import MarkdownIt

from lithos_lens.tasks import NoteRecord, SectionState

logger = logging.getLogger(__name__)

# Lens-side ceiling on how many items each related-panel section emits.
# Internal constant, deliberately NOT public config: the PRD only specifies
# ``[knowledge].related_title_fanout_cap`` (the backend-call bound); the render
# bound is a page-size safety net, not an operator dial.
RELATED_RENDER_CAP = 50

# Schemes an agent-authored link may use (REQUIREMENTS.md §6.2). Anything else —
# including ``javascript:``, ``data:``, ``file:``, ``vbscript:``, ``ftp:`` — is
# rejected; relative links (no scheme) are always allowed.
_ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "mailto"})


def _validate_link(url: str) -> bool:
    """Allow only the schemes in REQUIREMENTS.md §6.2 (plus relative links).

    Replaces markdown-it-py's default validator, which merely blocks a handful of
    dangerous schemes and still emits anchors for anything else (e.g. ``ftp:``).
    """
    scheme = urlparse(url.strip()).scheme.lower()
    return scheme == "" or scheme in _ALLOWED_LINK_SCHEMES


# Agent-authored note bodies must be safe to render. The CommonMark preset is
# spec-compliant, which means it passes raw HTML through (``html=True``); we
# override ``html=False`` so raw HTML is escaped instead. ``linkify`` stays off
# so bare URLs are never auto-linked. Tables and strikethrough match the corpus.
MARKDOWN = (
    MarkdownIt("commonmark", {"html": False, "linkify": False})
    .enable("table")
    .enable("strikethrough")
)
MARKDOWN.validateLink = _validate_link


def render_markdown(text: str) -> str:
    """Render a note's markdown body to safe HTML.

    Raw HTML in ``text`` is escaped and link schemes outside the §6.2
    allow-list are neutralized, so a hostile or sloppy note cannot script the
    browser. The full body is always rendered — ``/note/{id}`` is the canonical
    document page, so bounding render cost would be an explicit product
    decision, not a silent cap. If parsing fails for any reason the fallback is
    HTML-escaped plaintext — never raw passthrough.
    """
    body = text or ""
    try:
        return MARKDOWN.render(body)
    except Exception:
        logger.warning(
            "markdown render failed; falling back to escaped plaintext",
            exc_info=True,
        )
        return f'<pre class="markdown-fallback">{escape(body)}</pre>'


# ── Related panel (K1-S4) ──────────────────────────────────────────────


@dataclass(frozen=True)
class RelatedRef:
    """A raw neighbor reference from ``lithos_related``, before title lookup.

    Link/provenance entries carry a ``title`` inline in the response; typed
    edges carry endpoint ids only, so their titles are filled in by a separate
    capped ``lithos_read`` fan-out. For an edge ref, ``id`` is the OPPOSITE
    endpoint of the edge (``to_id`` for an outgoing edge, ``from_id`` for an
    incoming one) and ``direction`` / ``edge_type`` / ``weight`` /
    ``conflict_state`` are carried through from the raw edge row per
    REQUIREMENTS.md §6.5.
    """

    id: str
    title: str = ""
    edge_type: str = ""
    weight: float | None = None
    direction: str = ""
    conflict_state: str = ""


@dataclass(frozen=True)
class RelatedNeighborhood:
    """One ``lithos_related`` call's worth of a note's neighborhood (raw ids)."""

    links: tuple[RelatedRef, ...] = ()
    backlinks: tuple[RelatedRef, ...] = ()
    sources: tuple[RelatedRef, ...] = ()
    derived: tuple[RelatedRef, ...] = ()
    unresolved: tuple[str, ...] = ()
    edges: tuple[RelatedRef, ...] = ()


@dataclass(frozen=True)
class RelatedItem:
    """A neighbor rendered in the panel — by title when it could be resolved."""

    id: str
    title: str = ""
    edge_type: str = ""
    weight: float | None = None

    @property
    def label(self) -> str:
        """Human label: the resolved title, falling back to the bare id."""
        return self.title or self.id


@dataclass(frozen=True)
class RelatedSection:
    """One panel section: resolved items plus a count collapsed past the cap."""

    items: tuple[RelatedItem, ...] = ()
    overflow: int = 0


@dataclass(frozen=True)
class RelatedPanel:
    """The four related-panel sections plus loose provenance and load state."""

    links: RelatedSection = field(default_factory=RelatedSection)
    backlinks: RelatedSection = field(default_factory=RelatedSection)
    sources: RelatedSection = field(default_factory=RelatedSection)
    derived: RelatedSection = field(default_factory=RelatedSection)
    unresolved: tuple[str, ...] = ()
    edges: RelatedSection = field(default_factory=RelatedSection)
    state: SectionState = SectionState.OK

    @property
    def has_provenance(self) -> bool:
        return bool(
            self.sources.items
            or self.sources.overflow
            or self.derived.items
            or self.derived.overflow
            or self.unresolved
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.links.items
            or self.backlinks.items
            or self.has_provenance
            or self.edges.items
        )


class KnowledgeLithosClientProtocol(Protocol):
    """Subset of Lithos operations required by the knowledge note view."""

    async def related(self, knowledge_id: str) -> RelatedNeighborhood: ...

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None: ...


async def load_related_panel(
    lithos: KnowledgeLithosClientProtocol,
    knowledge_id: str,
    *,
    title_fanout_cap: int,
    render_cap: int = RELATED_RENDER_CAP,
) -> RelatedPanel:
    """Load and resolve a note's related panel from one ``lithos_related`` call.

    Two independent bounds apply. ``title_fanout_cap`` limits the ``lithos_read``
    fan-out — at most that many distinct ids are looked up to resolve titles,
    bounding backend *call* count. ``render_cap`` (internal constant
    ``RELATED_RENDER_CAP``; parameterized only for tests) limits how many
    items each section *emits*, bounding response *size* even for a hub note
    whose neighbors arrive with inline titles (which need no fan-out). Anything past
    either bound collapses into the section's ``overflow`` count. A failed
    ``lithos_related`` call degrades to ``SectionState.ERROR`` so the note body
    still renders.
    """

    try:
        neighborhood = await lithos.related(knowledge_id)
    except Exception as exc:
        # Note id + error type give the operator a scent trail; the full
        # timing/fan-out telemetry the PRD sketches is deferred.
        logger.warning(
            "related panel load failed for note %s: %s",
            knowledge_id,
            type(exc).__name__,
        )
        return RelatedPanel(state=SectionState.ERROR)

    titles = await _resolve_titles(
        lithos, neighborhood, knowledge_id=knowledge_id, cap=title_fanout_cap
    )
    return RelatedPanel(
        links=_build_section(neighborhood.links, titles, render_cap=render_cap),
        backlinks=_build_section(neighborhood.backlinks, titles, render_cap=render_cap),
        sources=_build_section(neighborhood.sources, titles, render_cap=render_cap),
        derived=_build_section(neighborhood.derived, titles, render_cap=render_cap),
        unresolved=neighborhood.unresolved,
        edges=_build_section(neighborhood.edges, titles, render_cap=render_cap),
    )


def normalize_related(raw: dict[str, Any]) -> RelatedNeighborhood:
    """Normalize a ``lithos_related`` payload into a ``RelatedNeighborhood``.

    Parses the real nested response shape (see the lithos ``lithos_related``
    tool)::

        {
          "id": ..., "included": [...],
          "links": {"outgoing": [{"id", "title"}], "incoming": [...]},
          "provenance": {"sources": [{"id", "title"}], "derived": [...],
                         "unresolved_sources": [str, ...]},
          "edges": {"outgoing": [<edge row>], "incoming": [<edge row>]},
          "related_ids": [...],
        }

    Sections omitted from the response (not requested via ``include``)
    normalize to empty. Edge rows come straight from edges.db; the ref keeps
    the OPPOSITE endpoint by direction (``to_id`` outgoing, ``from_id``
    incoming) plus ``type`` / ``weight`` / ``conflict_state``.
    """
    links = _section(raw, "links")
    provenance = _section(raw, "provenance")
    edges = _section(raw, "edges")
    return RelatedNeighborhood(
        links=_normalize_refs(links.get("outgoing")),
        backlinks=_normalize_refs(links.get("incoming")),
        sources=_normalize_refs(provenance.get("sources")),
        derived=_normalize_refs(provenance.get("derived")),
        unresolved=_normalize_unresolved(provenance.get("unresolved_sources")),
        edges=_normalize_edges(edges.get("outgoing"), direction="outgoing")
        + _normalize_edges(edges.get("incoming"), direction="incoming"),
    )


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _ordered_ids(neighborhood: RelatedNeighborhood) -> list[str]:
    """Distinct ids needing a title lookup, in section order.

    Refs whose title arrived inline in the ``lithos_related`` response are
    skipped — they already render by title and never spend fan-out budget.
    """
    seen: dict[str, None] = {}
    for refs in (
        neighborhood.links,
        neighborhood.backlinks,
        neighborhood.sources,
        neighborhood.derived,
        neighborhood.edges,
    ):
        for ref in refs:
            if ref.id and not ref.title:
                seen.setdefault(ref.id, None)
    return list(seen)


async def _resolve_titles(
    lithos: KnowledgeLithosClientProtocol,
    neighborhood: RelatedNeighborhood,
    *,
    knowledge_id: str,
    cap: int,
) -> dict[str, str]:
    ids = _ordered_ids(neighborhood)[: max(cap, 0)]
    if not ids:
        return {}

    async def fetch(note_id: str) -> tuple[str, bool]:
        try:
            note = await lithos.read_note(note_id, max_length=1)
        except Exception:
            return "", True
        return (note.title if note else ""), False

    results = await asyncio.gather(*(fetch(note_id) for note_id in ids))
    failures = sum(1 for _, failed in results if failed)
    if failures:
        # One aggregate line, not one per id — a hub note with a flaky backend
        # must not be able to spam the log.
        logger.warning(
            "related panel title lookup failed for %d of %d ids (note %s)",
            failures,
            len(ids),
            knowledge_id,
        )
    return dict(zip(ids, (title for title, _ in results), strict=True))


def _build_section(
    refs: tuple[RelatedRef, ...],
    titles: dict[str, str],
    *,
    render_cap: int,
) -> RelatedSection:
    """Render refs with a known title; collapse the rest into ``overflow``.

    A title is known when it arrived inline in the response (``ref.title``) or
    when the capped fan-out resolved it (``titles``). Two things collapse into
    ``overflow``: refs with no known title (past the fan-out cap), and
    renderable refs past ``render_cap`` — a separate ceiling on how many items a
    section emits, so a hub note with many *inline* titles cannot inflate the
    rendered page beyond a lens-side bound.
    """
    items: list[RelatedItem] = []
    overflow = 0
    for ref in refs:
        renderable = bool(ref.title or ref.id in titles)
        if renderable and len(items) < render_cap:
            items.append(
                RelatedItem(
                    id=ref.id,
                    title=ref.title or titles[ref.id],
                    edge_type=ref.edge_type,
                    weight=ref.weight,
                )
            )
        else:
            overflow += 1
    return RelatedSection(items=tuple(items), overflow=overflow)


def _normalize_refs(items: Any) -> tuple[RelatedRef, ...]:
    """Link/provenance entries: ``{"id": ..., "title": ...}`` dicts."""
    if not isinstance(items, list):
        return ()
    refs: list[RelatedRef] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ref_id = str(item.get("id") or "")
        if ref_id:
            refs.append(RelatedRef(id=ref_id, title=str(item.get("title") or "")))
    return tuple(refs)


def _normalize_edges(items: Any, *, direction: str) -> tuple[RelatedRef, ...]:
    """Edge rows from edges.db; keep the endpoint OPPOSITE the focus note."""
    if not isinstance(items, list):
        return ()
    endpoint_key = "to_id" if direction == "outgoing" else "from_id"
    refs: list[RelatedRef] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ref_id = str(item.get(endpoint_key) or "")
        if not ref_id:
            continue
        raw_weight = item.get("weight")
        weight = (
            float(raw_weight)
            if isinstance(raw_weight, (int, float)) and not isinstance(raw_weight, bool)
            else None
        )
        refs.append(
            RelatedRef(
                id=ref_id,
                edge_type=str(item.get("type") or ""),
                weight=weight,
                direction=direction,
                conflict_state=str(item.get("conflict_state") or ""),
            )
        )
    return tuple(refs)


def _normalize_unresolved(items: Any) -> tuple[str, ...]:
    """``provenance.unresolved_sources`` is a plain list of target strings."""
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, str) and item)
