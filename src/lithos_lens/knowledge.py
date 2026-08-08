"""Knowledge note rendering and normalization.

Foundation module for the knowledge browser surface (mirrors ``tasks.py``).
K1 slice 1 introduces server-side markdown rendering; later slices add the
wiki-link tokenizer, metadata chips, related panel, and search view models.
"""

from __future__ import annotations

from markdown_it import MarkdownIt

# Agent-authored note bodies must be safe to render. The CommonMark preset is
# spec-compliant, which means it passes raw HTML through (``html=True``); we
# override ``html=False`` so raw HTML is escaped instead. The built-in
# ``validateLink`` still rejects ``javascript:``/``vbscript:``/``file:``/``data:``
# hrefs. Tables and strikethrough are enabled to match the corpus's markdown.
_MARKDOWN = (
    MarkdownIt("commonmark", {"html": False})
    .enable("table")
    .enable("strikethrough")
)


def render_markdown(text: str) -> str:
    """Render a note's markdown body to safe HTML.

    Raw HTML in ``text`` is escaped and dangerous link schemes are neutralized,
    so a hostile or sloppy note cannot script the browser.
    """
    return _MARKDOWN.render(text or "")
