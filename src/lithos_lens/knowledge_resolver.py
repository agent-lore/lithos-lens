"""Wiki-link resolution (§6.3).

Extracted from :mod:`lithos_lens.knowledge` when that module reached the 800
line ceiling: the repo's convention is extraction over budget-raising
(precedent: ``knowledge_produced_by.py`` in #40, ``request_filters.py``,
``mcp_transport.py``). The seam is clean — resolution shares no state with note
rendering or the related panel, and reads a narrower slice of the client.

Stays in the ``Knowledge`` component, so this costs no new component edge.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from lithos_lens.knowledge import RelatedNeighborhood
from lithos_lens.tasks import NoteRecord, NoteSummary

# ── Wiki-link resolver (K1-S2) ─────────────────────────────────────────
#
# No MCP response maps an inline ``[[target]]`` to a note id, so resolution is
# per-click in ``GET /knowledge/resolve`` (§6.3): a UUID target is a direct
# redirect, a ``target + ".md"`` path probe covers the dominant
# ``[[folder/note]]`` convention, and otherwise candidates are gathered from the
# source note's outgoing links and a title search — one confident candidate
# redirects, several disambiguate, none is reported as an unresolved link.

# Upper bound on candidates gathered for the title-disambiguation step, so a
# broad ``title_contains`` match can't inflate the disambiguation page.
RESOLVE_CANDIDATE_LIMIT = 10


@dataclass(frozen=True)
class ResolveCandidate:
    """One plausible target for a wiki-link, shown on the disambiguation page."""

    id: str
    title: str = ""
    path: str = ""

    @property
    def label(self) -> str:
        """Human label: title, then path, falling back to the bare id."""
        return self.title or self.path or self.id


@dataclass(frozen=True)
class ResolveOutcome:
    """The result of resolving one wiki-link click.

    ``kind`` is ``redirect`` (follow ``target_id``), ``disambiguation`` (show
    ``candidates``), or ``unresolved`` (offer a search for ``search_query``).

    ``via`` names WHICH arm decided, which ``kind`` cannot: a uuid target, a
    path probe and a single title match all redirect, so all three collapse
    into ``redirect``. The K1 PRD asks for that distinction and it was
    previously discarded — "resolution works" and "resolution works only
    because every link happens to be a uuid" look identical without it.
    """

    kind: str
    #: `uuid` | `path` | `title` | `disambiguated` | `unresolved` | `empty`.
    via: str = ""
    #: Candidates FOUND, which is not `len(candidates)`. The single-candidate
    #: arm redirects and carries only `target_id`, so the candidate that
    #: decided it is dropped from the tuple — reporting the tuple's length
    #: would say "0 candidates" about a resolution exactly one candidate made.
    #: `candidates` is what the disambiguation page renders; this is how much
    #: the resolver found, which is the measure of how ambiguous the corpus is.
    candidate_count: int = 0
    target: str = ""
    target_id: str = ""
    candidates: tuple[ResolveCandidate, ...] = ()
    search_query: str = ""


class WikiResolverClientProtocol(Protocol):
    """Subset of Lithos operations required by the wiki-link resolver."""

    async def read_note_by_path(self, path: str) -> NoteRecord | None: ...

    async def related(self, knowledge_id: str) -> RelatedNeighborhood: ...

    async def list_notes(
        self,
        *,
        title_contains: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]: ...


async def resolve_wiki_link(
    lithos: WikiResolverClientProtocol,
    target: str,
    from_id: str,
    *,
    candidate_limit: int = RESOLVE_CANDIDATE_LIMIT,
) -> ResolveOutcome:
    """Resolve a wiki-link ``target`` clicked from note ``from_id`` (§6.3)."""
    target = target.strip()
    if not target:
        return ResolveOutcome(kind="unresolved", via="empty", target=target)

    if _is_uuid(target):
        return ResolveOutcome(
            kind="redirect", via="uuid", target=target, target_id=target
        )

    note = await _probe_path(lithos, target)
    if note is not None and note.id:
        return ResolveOutcome(
            kind="redirect", via="path", target=target, target_id=note.id
        )

    candidates = await _gather_candidates(
        lithos, target, from_id, limit=candidate_limit
    )
    if len(candidates) == 1:
        return ResolveOutcome(
            kind="redirect",
            via="title",
            candidate_count=1,
            target=target,
            target_id=candidates[0].id,
        )
    if candidates:
        return ResolveOutcome(
            kind="disambiguation",
            via="disambiguated",
            candidate_count=len(candidates),
            target=target,
            candidates=candidates,
        )
    return ResolveOutcome(
        kind="unresolved", via="unresolved", target=target, search_query=target
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _last_component(target: str) -> str:
    """The last path component of a ``folder/note`` target (the title search key)."""
    return target.rstrip("/").split("/")[-1] or target


def _is_unsafe_probe_target(target: str) -> bool:
    """Whether ``target`` must not be forwarded to a path-addressed read.

    ``target`` is an unauthenticated query param, so a raw ``target + ".md"``
    probe would turn ``GET /knowledge/resolve`` into a path-traversal existence
    oracle against ``lithos_read(path=…)`` — and a hit escalates to full-content
    disclosure via the ``302 /note/{id}`` redirect (CWE-22 / CWE-639). Reject
    traversal (``..`` path segments), absolute paths (leading ``/`` or a
    backslash), and NUL; such a target simply misses the probe and falls through
    to the title search, which only ever passes the last path component.
    """
    if "\x00" in target or "\\" in target:
        return True
    if target.startswith("/"):
        return True
    return ".." in target.split("/")


async def _probe_path(
    lithos: WikiResolverClientProtocol, target: str
) -> NoteRecord | None:
    """Cheap ``lithos_read(path=target + ".md", max_length=1)`` existence probe.

    Traversal/absolute targets are refused before the probe (treated as a miss).
    A miss (the common case for a title-style target) is not an error here: any
    failure falls through to the candidate-gathering step, so it degrades to
    ``None`` rather than propagating.
    """
    if _is_unsafe_probe_target(target):
        return None
    path = target if target.endswith(".md") else f"{target}.md"
    try:
        return await lithos.read_note_by_path(path)
    except Exception:
        return None


async def _gather_candidates(
    lithos: WikiResolverClientProtocol,
    target: str,
    from_id: str,
    *,
    limit: int,
) -> tuple[ResolveCandidate, ...]:
    """Candidate targets from the source note's links plus a title search.

    A confident match from the source note's own outgoing links (its title
    equals the target or the target's last path component) ranks first; the
    ``lithos_list(title_contains=…)`` matches follow. Duplicates merge by id —
    first-seen ranking wins, missing title/path fill from later sources — and
    the result is capped at ``limit`` so a broad title match can't inflate the
    page.
    """
    last = _last_component(target)
    candidates: dict[str, ResolveCandidate] = {}

    if from_id:
        try:
            neighborhood = await lithos.related(from_id)
        except Exception:
            neighborhood = None
        if neighborhood is not None:
            for ref in neighborhood.links:
                if ref.id and _title_matches(ref.title, target, last):
                    candidates.setdefault(
                        ref.id, ResolveCandidate(id=ref.id, title=ref.title)
                    )

    try:
        matches = await lithos.list_notes(title_contains=last, limit=limit)
    except Exception:
        matches = []
    for note in matches:
        if not note.id:
            continue
        existing = candidates.get(note.id)
        if existing is None:
            candidates[note.id] = ResolveCandidate(
                id=note.id, title=note.title, path=note.path
            )
        else:
            # Merge, don't drop: the outgoing-link candidate ranked first but
            # arrived pathless; the title-search row carries the path §6.3
            # wants on the disambiguation page. Ranking (insertion order) is
            # preserved; missing fields fill from the later source.
            candidates[note.id] = ResolveCandidate(
                id=note.id,
                title=existing.title or note.title,
                path=existing.path or note.path,
            )

    return tuple(candidates.values())[:limit]


def _title_matches(title: str, target: str, last: str) -> bool:
    """Whether an outgoing link's ``title`` is a confident match for the target."""
    if not title:
        return False
    normalized = title.strip().lower()
    return normalized in {target.strip().lower(), last.strip().lower()}
