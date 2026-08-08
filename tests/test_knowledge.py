"""K1 slice 1 — server-side markdown rendering for knowledge notes."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

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
