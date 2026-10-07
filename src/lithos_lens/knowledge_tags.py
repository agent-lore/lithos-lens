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

import unicodedata
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
    """How a tag reads on a page: one label per tag, never shared by two.

    Lithos keeps tag names verbatim, and HTML would hide what sets some apart
    (``""`` shows nothing; CR, LF and NUL show as a space or not at all). So a
    name reads bare only when it is ORDINARY — non-empty, no surrounding
    whitespace, no control character, not opening with the quote mark and not
    spelling the empty tag's label. The empty tag reads "(empty tag)"; any
    other name is quoted, with control characters and the backslash itself
    backslash-escaped. The three forms cannot meet and the escaping is
    reversible, so distinct tags get distinct labels (the page renders them
    with whitespace preserved, ``.knowledge-tag-label``).
    """
    if not tag:
        return EMPTY_TAG_LABEL
    if _is_ordinary(tag):
        return tag
    return f"\u201c{''.join(map(_escaped_char, tag))}\u201d"


def _is_ordinary(tag: str) -> bool:
    return (
        tag == tag.strip()
        and not tag.startswith("\u201c")
        and tag != EMPTY_TAG_LABEL
        and not any(unicodedata.category(ch) == "Cc" for ch in tag)
    )


def _escaped_char(ch: str) -> str:
    if ch == "\\":
        return "\\\\"
    if unicodedata.category(ch) == "Cc":
        return ch.encode("unicode_escape").decode("ascii")
    return ch


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
