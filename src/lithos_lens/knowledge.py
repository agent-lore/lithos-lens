"""Knowledge note rendering and normalization.

Foundation module for the knowledge browser surface (mirrors ``tasks.py``).
K1 slice 1 introduces server-side markdown rendering; later slices add the
wiki-link tokenizer, metadata chips, related panel, and search view models.
"""

from __future__ import annotations

import logging
from html import escape
from urllib.parse import urlparse

from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

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
