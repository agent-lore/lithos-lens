"""Search-result view model for the ``/knowledge`` landing (K1-S6).

Split out of ``knowledge.py`` on the same seam as ``knowledge_metadata`` and
``knowledge_produced_by``: one knowledge feature per Foundation module, keeping
each under the architecture line budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from lithos_lens.knowledge import same_title

# ``/knowledge?q=…`` renders hybrid-search result cards from ``lithos_search``.
# The snippet arrives as raw markdown (verified live, §7.1) and is rendered
# ESCAPED by the template — never fed through the markdown renderer — so markup
# in a snippet cannot break the results page. A snippet usually opens with the
# note's own ``# <title>`` line, which the card already shows as its title; that
# one line is dropped (the note page's leading-H1 rule, on snippet text).

# An ATX H1 line: ``#``, a space, the text, an optional closing ``#`` run.
_SNIPPET_H1_RE = re.compile(r"#[ \t]+(.*?)(?:[ \t]+#+)?[ \t\r]*")


@dataclass(frozen=True)
class SearchResult:
    """One ``lithos_search`` hit rendered as a result card (§7.1)."""

    id: str
    title: str = ""
    path: str = ""
    snippet: str = ""
    updated: str = ""
    score: float | None = None

    @property
    def label(self) -> str:
        """Human label: title, then path, falling back to the bare id."""
        return self.title or self.path or self.id


def normalize_search_result(raw: dict[str, Any]) -> SearchResult:
    """Normalize one ``lithos_search`` result row into a ``SearchResult``.

    ``lithos_search`` answers ``{"results": [{"id", "title", "path",
    "snippet", "updated_at", "score", ...}]}``. ``updated`` / ``updated_at``
    are both accepted for the timestamp (parity with
    :func:`~lithos_lens.tasks.normalize_note_summary`). A leading
    ``# <title>`` line is dropped from the snippet (:func:`_drop_title_line`).
    """
    title = str(raw.get("title") or "")
    raw_score = raw.get("score")
    score = (
        float(raw_score)
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool)
        else None
    )
    return SearchResult(
        id=str(raw.get("id") or ""),
        title=title,
        path=str(raw.get("path") or ""),
        snippet=_drop_title_line(str(raw.get("snippet") or ""), title),
        updated=str(raw.get("updated") or raw.get("updated_at") or ""),
        score=score,
    )


def _drop_title_line(snippet: str, title: str) -> str:
    """Drop the snippet's first line when it is ``# <title>``.

    Stays text: the snippet is never parsed or rendered (§7.1), so this looks
    only at the first line and leaves everything after it as it came.
    """
    first, _, rest = snippet.lstrip().partition("\n")
    match = _SNIPPET_H1_RE.fullmatch(first)
    if match and same_title(match.group(1), title):
        return rest.lstrip()
    return snippet
