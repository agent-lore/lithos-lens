"""K1 slice 1 — server-side markdown rendering for knowledge notes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lithos_lens import knowledge
from lithos_lens.config import load_config
from lithos_lens.knowledge import render_markdown
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app
from tests.test_tasks_mvp import TaskFakeLithosClient


def test_render_markdown_produces_html_blocks() -> None:
    html = render_markdown("# Heading\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<h1>Heading</h1>" in html
    assert "<table>" in html
    assert "<td>1</td>" in html


def test_render_markdown_escapes_raw_html() -> None:
    html = render_markdown("Hello\n\n<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_markdown_neutralizes_dangerous_link_schemes() -> None:
    html = render_markdown("[click](javascript:alert(1))")
    # The dangerous href is rejected: no anchor is emitted (the link renders as
    # literal text instead).
    assert 'href="javascript:' not in html
    assert "<a " not in html


def test_render_markdown_keeps_safe_links() -> None:
    html = render_markdown("[ok](https://example.com)")
    assert '<a href="https://example.com">ok</a>' in html


@pytest.mark.parametrize("scheme", ["mailto:a@b.com", "http://x.com", "https://x.com"])
def test_render_markdown_allows_allowlisted_schemes(scheme: str) -> None:
    html = render_markdown(f"[link]({scheme})")
    assert f'href="{scheme}"' in html


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com", "vbscript:msgbox", "file:///etc/passwd", "tel:+15551234"],
)
def test_render_markdown_rejects_non_allowlisted_schemes(url: str) -> None:
    # REQUIREMENTS §6.2: only http/https/mailto/relative may become anchors.
    html = render_markdown(f"[x]({url})")
    assert "<a " not in html


def test_render_markdown_allows_relative_links() -> None:
    html = render_markdown("[note](/note/abc)")
    assert '<a href="/note/abc">note</a>' in html


def test_render_markdown_falls_back_to_escaped_plaintext_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQUIREMENTS §6.2: a parse failure yields escaped plaintext, never raw
    passthrough."""

    def boom(_text: str) -> str:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(knowledge.MARKDOWN, "render", boom)
    html = render_markdown("# Title\n\n<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_render_markdown_caps_oversized_input() -> None:
    extra = 5_000
    oversized = "a" * (knowledge.MAX_RENDER_CHARS + extra)
    html = render_markdown(oversized)
    # The body is truncated to the cap (the ~200k 'a's survive; the excess does
    # not), and a visible truncation note is shown.
    assert html.count("a") < knowledge.MAX_RENDER_CHARS + extra
    assert "truncated" in html


def test_render_markdown_does_not_autolink_bare_urls() -> None:
    """Defense-in-depth: ``linkify`` stays off, so a bare URL is inert text —
    if it flipped on, the XSS boundary would silently widen."""
    html = render_markdown("see https://example.com here")
    assert "<a " not in html


def test_markdown_parser_keeps_raw_html_and_linkify_disabled() -> None:
    """Defense-in-depth guard on the parser config itself."""
    assert knowledge.MARKDOWN.options["html"] is False
    assert knowledge.MARKDOWN.options["linkify"] is False


def _client(config_path: Path, fake: TaskFakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def test_note_page_renders_markdown_body_as_html(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    fake.notes["md-note"] = NoteRecord(
        id="md-note",
        title="Markdown Note",
        content="## Section\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))",
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/md-note")

    assert response.status_code == 200
    # Markdown headings become real HTML, not raw text in a <pre>.
    assert "<h2>Section</h2>" in response.text
    # Agent-authored HTML is escaped; the dangerous link scheme is dropped.
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert 'href="javascript:' not in response.text
