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

# Untrusted note bodies are parsed synchronously on the request path, so cap the
# input to bound per-request CPU/latency (a malicious agent can write a
# multi-megabyte note). Beyond the cap the body is truncated with a visible note
# rather than refused. Corpus notes are far smaller than this.
MAX_RENDER_CHARS = 200_000
_TRUNCATION_NOTICE = "\n\n*(note truncated for display)*"


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

    Raw HTML in ``text`` is escaped, link schemes outside the §6.2 allow-list are
    neutralized, and the input is length-capped, so a hostile or sloppy note
    cannot script the browser or exhaust the renderer. If parsing fails for any
    reason the fallback is HTML-escaped plaintext — never raw passthrough.
    """
    body = text or ""
    if len(body) > MAX_RENDER_CHARS:
        body = body[:MAX_RENDER_CHARS] + _TRUNCATION_NOTICE
    try:
        return MARKDOWN.render(body)
    except Exception:
        logger.warning("markdown render failed; falling back to escaped plaintext")
        return f'<pre class="markdown-fallback">{escape(body)}</pre>'
