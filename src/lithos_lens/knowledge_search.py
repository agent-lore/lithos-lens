"""Search-result view model for the ``/knowledge`` landing (K1-S6).

Split out of ``knowledge.py`` on the same seam as ``knowledge_metadata`` and
``knowledge_produced_by``: one knowledge feature per Foundation module, keeping
each under the architecture line budget.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from lithos_lens.knowledge import MARKDOWN, is_title_heading

logger = logging.getLogger(__name__)

# ``/knowledge?q=…`` renders hybrid-search result cards from ``lithos_search``.
# The snippet arrives as raw markdown (verified live, §7.1) and is rendered
# ESCAPED by the template — never fed through the markdown renderer — so markup
# in a snippet cannot break the results page. A snippet usually opens with the
# note's own ``# <title>`` line, which the card already shows as its title; that
# line is dropped by the note page's own leading-H1 rule (``is_title_heading``).

# Line boundaries as the markdown parser counts them: CRLF, CR or LF.
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
# Complete blank lines only, so the first kept line keeps its indentation.
_BLANK_LINES_RE = re.compile(r"\A(?:[ \t]*(?:\r\n|\r|\n))*")


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
    """Drop the snippet's leading ``# <title>`` heading when it repeats ``title``.

    The snippet is PARSED to find that heading, never rendered (§7.1): what is
    kept is the raw text after the heading's lines, exactly as it came, less
    the blank lines separating it from the heading.
    """
    try:
        tokens = MARKDOWN.parse(snippet)
    except Exception:
        logger.warning("snippet parse failed; keeping it whole", exc_info=True)
        return snippet
    if not is_title_heading(tokens, title) or tokens[0].map is None:
        return snippet
    heading_lines = tokens[0].map[1]
    breaks = list(_LINE_BREAK_RE.finditer(snippet))
    if len(breaks) < heading_lines:
        return ""
    rest = snippet[breaks[heading_lines - 1].end() :]
    rest = _BLANK_LINES_RE.sub("", rest, count=1)
    return rest if rest.strip() else ""
