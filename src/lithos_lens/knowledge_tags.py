"""The ``/knowledge/tags`` browse page: every tag with its note count.

``lithos_tags`` answers ``{"tags": {tag: document_count}}`` for the whole
corpus (2,057 tags on 2026-10-05). The page lists them most notes first, ties
by name, narrowed by a ``?q=`` substring and a ``?prefix=`` family facet, and
cut to ``[knowledge].tags_page_limit``. A family is a structured tag's key with
its colon (``project:``, ``ingested-by:``): derived from the tags present, never
a fixed list, so the facet row offers exactly the families the corpus carries.

Split out on the same seam as ``knowledge_landing``: one knowledge feature per
Foundation module.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# How the empty tag reads: Lithos keeps "" as a tag like any other.
EMPTY_TAG_LABEL = "(empty tag)"

# How many families the facet row offers, most tags first. A row, not a
# picker: past this the operator types ``?prefix=`` or narrows with ``?q=``.
TAG_FAMILY_FACET_LIMIT = 12


@dataclass(frozen=True)
class TagCount:
    """One tag and how many notes carry it."""

    tag: str
    count: int


@dataclass(frozen=True)
class TagFamily:
    """One entry of the family facet row: a ``key:`` prefix and its tag count."""

    prefix: str
    count: int


@dataclass(frozen=True)
class TagBrowse:
    """The tag page as rendered: the capped list and what it was cut from.

    ``matched`` counts every tag passing the filters, ``total`` every tag in
    the corpus; ``hidden`` is what the cap left off ("N more").
    ``active_family`` is the row entry the ``prefix`` filter selects, if any.
    """

    tags: tuple[TagCount, ...] = ()
    matched: int = 0
    total: int = 0
    families: tuple[TagFamily, ...] = ()
    active_family: TagFamily | None = None

    @property
    def hidden(self) -> int:
        return self.matched - len(self.tags)


def normalize_tag_counts(payload: dict[str, Any]) -> tuple[TagCount, ...]:
    """``lithos_tags``'s ``tags`` map as rows, most notes first, ties by name.

    Every string key is a tag, kept verbatim: Lithos stores tag names as given
    (no trim, no non-empty check) and its index matches them exactly, so ``""``
    and ``" x "`` are tags of their own. A count that is not an int (a bool is
    not a count) is skipped rather than guessed at.
    """
    raw = payload.get("tags")
    if not isinstance(raw, dict):
        return ()
    rows = [
        TagCount(tag, count)
        for tag, count in raw.items()
        if isinstance(tag, str)
        and isinstance(count, int)
        and not isinstance(count, bool)
    ]
    return tuple(sorted(rows, key=lambda row: (-row.count, row.tag)))


def tag_count(rows: Iterable[TagCount], tag: str) -> int | None:
    """The count of exactly ``tag`` among ``rows`` (``None`` when absent)."""
    return next((row.count for row in rows if row.tag == tag), None)


def tag_label(tag: str) -> str:
    """How a tag reads on a page, never hiding which tag it is.

    HTML shows nothing for ``""`` and collapses surrounding whitespace, yet
    each is a distinct tag upstream: the empty tag reads "(empty tag)", and a
    padded one is quoted so it cannot pass for its trimmed twin.
    """
    if not tag:
        return EMPTY_TAG_LABEL
    if tag != tag.strip():
        return f"\u201c{tag}\u201d"
    return tag


def tag_family(tag: str) -> str:
    """A tag's family, ``key:`` — or ``""`` when it has no key before a colon."""
    head, sep, _ = tag.partition(":")
    return f"{head}:" if sep and head else ""


def tag_families(
    tags: Iterable[str], *, limit: int = TAG_FAMILY_FACET_LIMIT
) -> tuple[TagFamily, ...]:
    """The families present in ``tags``, most tags first (ties by name).

    Families differing only in case are ONE entry, because the ``prefix``
    filter matches case-insensitively (as ``lithos_tags``'s own does) and so
    selects all of them: its count is theirs together, and it is shown in the
    spelling most of its tags use, ties by name.
    """
    spellings: dict[str, Counter[str]] = {}
    for family in map(tag_family, tags):
        if family:
            spellings.setdefault(family.casefold(), Counter())[family] += 1
    merged = [
        (min(seen.items(), key=lambda item: (-item[1], item[0]))[0], seen.total())
        for seen in spellings.values()
    ]
    ranked = sorted(merged, key=lambda item: (-item[1], item[0]))
    return tuple(TagFamily(prefix, count) for prefix, count in ranked[:limit])


def build_tag_browse(
    rows: Iterable[TagCount], *, query: str, prefix: str, limit: int
) -> TagBrowse:
    """Filter the ranked ``rows`` by ``prefix`` and ``query``, then cut to ``limit``.

    Both filters are case-insensitive, as ``lithos_tags``'s own ``prefix`` is:
    ``prefix`` matches from the start of the tag, ``query`` anywhere in it. The
    facet row is over EVERY tag, before either filter — it is how the operator
    moves between families.
    """
    all_rows = tuple(rows)
    needle = query.casefold()
    head = prefix.casefold()
    matched = [
        row
        for row in all_rows
        if row.tag.casefold().startswith(head) and needle in row.tag.casefold()
    ]
    families = tag_families(row.tag for row in all_rows)
    return TagBrowse(
        tags=tuple(matched[:limit]),
        matched=len(matched),
        total=len(all_rows),
        families=families,
        active_family=next(
            (f for f in families if prefix and f.prefix.casefold() == head), None
        ),
    )
