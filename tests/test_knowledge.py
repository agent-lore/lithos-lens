"""K1 slice 1 — server-side markdown rendering for knowledge notes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lithos_lens import knowledge, knowledge_metadata
from lithos_lens.config import load_config
from lithos_lens.knowledge import render_markdown
from lithos_lens.knowledge_metadata import build_note_metadata
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
    [
        "ftp://example.com",
        "vbscript:msgbox",
        "file:///etc/passwd",
        "tel:+15551234",
        "data:text/html;base64,PHNjcmlwdD4=",
        "JaVaScRiPt:alert(1)",
        "  javascript:alert(1)",
    ],
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
    passthrough.

    Patches ``MARKDOWN.parse`` — the first call in ``render_markdown``'s try
    block since the wiki-link splice moved rendering to ``parse`` +
    ``renderer.render`` (patching the old ``.render`` seam no longer fires).
    The ``markdown-fallback`` marker assertion pins that the except path
    actually ran: without it this test passes vacuously through the normal
    path, because the renderer escapes raw HTML anyway.
    """

    def boom(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(knowledge.MARKDOWN, "parse", boom)
    html = render_markdown("# Title\n\n<script>alert(1)</script>")
    assert 'class="markdown-fallback"' in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_render_markdown_renders_large_notes_in_full() -> None:
    """/note/{id} is the canonical document page: no silent truncation. A
    render-cost bound, if ever needed, is an explicit product decision — not a
    hidden cap (PR #22 review)."""
    paragraphs = [f"paragraph {i} " + "word " * 200 for i in range(300)]
    html = render_markdown("\n\n".join(paragraphs))
    assert "paragraph 0" in html
    assert "paragraph 299" in html
    assert "truncated" not in html


def test_render_markdown_rejects_unsafe_image_destinations() -> None:
    html = render_markdown("![x](javascript:alert(1))")
    assert "<img" not in html


def test_render_markdown_keeps_safe_image_destinations() -> None:
    html = render_markdown("![alt text](https://example.com/pic.png)")
    assert '<img src="https://example.com/pic.png" alt="alt text"' in html


def test_render_markdown_covers_corpus_inline_and_block_elements() -> None:
    html = render_markdown(
        "- item one\n- item two\n\n"
        "```python\nprint('hi')\n```\n\n"
        "some `inline code` and ~~struck~~ text"
    )
    assert "<ul>" in html and "<li>item one</li>" in html
    assert '<pre><code class="language-python">' in html
    assert "<code>inline code</code>" in html
    assert "<s>struck</s>" in html


def test_render_markdown_soft_break_stays_inside_one_paragraph() -> None:
    """CommonMark: a single newline is a soft break inside one paragraph. The
    browser must show it as a space — which requires the stylesheet NOT to
    apply ``pre-wrap`` to rendered output (see the stylesheet test below)."""
    html = render_markdown("first line\nsecond line")
    assert html.count("<p>") == 1


def test_markdown_body_stylesheet_does_not_preserve_newlines() -> None:
    """Regression pin for the review finding: ``.markdown-body`` once carried
    ``white-space: pre-wrap`` (from its plaintext ``<pre>`` days), which turns
    CommonMark soft breaks into visual line breaks in the browser. Whitespace
    preservation belongs only to ``pre`` descendants (code blocks and the
    escaped-plaintext fallback)."""
    css = (
        Path(__file__).parent.parent / "src" / "lithos_lens" / "static" / "lens.css"
    ).read_text()
    body_rule = re.search(r"\.markdown-body \{([^}]*)\}", css)
    assert body_rule is not None
    assert "white-space" not in body_rule.group(1)
    pre_rule = re.search(r"\.markdown-body pre[^{]*\{([^}]*)\}", css)
    assert pre_rule is not None
    assert "pre-wrap" in pre_rule.group(1)


def test_render_markdown_does_not_autolink_bare_urls() -> None:
    """Defense-in-depth: ``linkify`` stays off, so a bare URL is inert text —
    if it flipped on, the XSS boundary would silently widen."""
    html = render_markdown("see https://example.com here")
    assert "<a " not in html


def test_markdown_parser_keeps_raw_html_and_linkify_disabled() -> None:
    """Defense-in-depth guard on the parser config itself."""
    assert knowledge.MARKDOWN.options["html"] is False
    assert knowledge.MARKDOWN.options["linkify"] is False


# ── Metadata chips + lede (K1-S3) ──────────────────────────────────────


def _note(**metadata: object) -> NoteRecord:
    return NoteRecord(id="n", title="T", content="body", metadata=dict(metadata))


def test_build_note_metadata_projects_frontmatter_fields() -> None:
    meta = build_note_metadata(
        _note(
            note_type="observation",
            status="active",
            confidence=0.85,
            access_scope="shared",
            namespace="runbooks",
            supersedes="old-note-id",
            author="agent-7",
            contributors=["reviewer-a", "reviewer-b"],
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-02-01T00:00:00+00:00",
            summaries={"short": "One-line lede."},
        )
    )
    assert meta.note_type == "observation"
    assert meta.status == "active"
    assert meta.confidence == "85%"
    assert meta.access_scope == "shared"
    assert meta.namespace == "runbooks"
    assert meta.supersedes == "old-note-id"
    assert meta.author == "agent-7"
    assert meta.contributors == ("reviewer-a", "reviewer-b")
    assert meta.created_at == "2026-01-01T00:00:00+00:00"
    assert meta.updated_at == "2026-02-01T00:00:00+00:00"
    assert meta.lede == "One-line lede."
    assert meta.has_chips
    assert meta.has_authorship


def test_build_note_metadata_empty_when_frontmatter_absent() -> None:
    meta = build_note_metadata(_note())
    assert meta == knowledge_metadata.NoteMetadata()
    assert not meta.has_chips
    assert not meta.has_authorship
    assert meta.lede == ""


def test_build_note_metadata_lede_only_from_summaries_short() -> None:
    assert build_note_metadata(_note(summaries={"long": "not this"})).lede == ""
    assert build_note_metadata(_note(summaries="oops")).lede == ""
    trimmed = build_note_metadata(_note(summaries={"short": " trim me "}))
    assert trimmed.lede == "trim me"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.85, "85%"),
        (0.0, "0%"),
        (1, "100%"),
        (0.333, "33%"),
        (True, ""),
        ("0.85", ""),
        (None, ""),
    ],
)
def test_build_note_metadata_confidence_formatting(
    value: object, expected: str
) -> None:
    assert build_note_metadata(_note(confidence=value)).confidence == expected


def test_build_note_metadata_namespace_falls_back_to_path_directory() -> None:
    # Explicit namespace wins over the path.
    assert (
        build_note_metadata(_note(namespace="explicit", path="runbooks/x.md")).namespace
        == "explicit"
    )
    # Otherwise the path's directory stands in as the namespace.
    assert (
        build_note_metadata(_note(path="runbooks/influx/x.md")).namespace
        == "runbooks/influx"
    )
    # A bare (directory-less) path derives no namespace.
    assert build_note_metadata(_note(path="x.md")).namespace == ""


def test_build_note_metadata_contributors_accepts_scalar_and_drops_blanks() -> None:
    assert build_note_metadata(_note(contributors="solo")).contributors == ("solo",)
    assert build_note_metadata(
        _note(contributors=["a", "", "  ", "b", 5])
    ).contributors == ("a", "b")


@pytest.mark.parametrize(
    ("status", "expected_slug"),
    [
        ("quarantined", "quarantined"),
        ("active", "active"),
        ("archived", "archived"),
        # The known states pass through unchanged, so their stylesheet hooks
        # still match. A hostile status collapses whitespace/punctuation to a
        # single token — no second CSS class can be smuggled in via the suffix.
        ("open banner-warning", "open-banner-warning"),
        ("a\tb\nc", "a-b-c"),
        ("UPPER Case", "upper-case"),
        ("--edge--", "edge"),
    ],
)
def test_build_note_metadata_status_slug_is_a_single_class_token(
    status: str, expected_slug: str
) -> None:
    slug = build_note_metadata(_note(status=status)).status_slug
    assert slug == expected_slug
    # The whole point: the slug is one whitespace-free class token.
    assert " " not in slug and "\t" not in slug and "\n" not in slug


def _client(config_path: Path, fake: TaskFakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def test_note_page_renders_metadata_chips_lede_and_tag_links(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    fake.notes["meta-note"] = NoteRecord(
        id="meta-note",
        title="Metadata Note",
        content="Body text.",
        tags=("project:influx", "kind:plan"),
        metadata={
            "note_type": "summary",
            "status": "active",
            "confidence": 0.9,
            "access_scope": "shared",
            "namespace": "runbooks",
            "supersedes": "prior-note",
            "summaries": {"short": "The one-sentence gist."},
        },
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/meta-note")

    assert response.status_code == 200
    body = response.text
    # Chips for each frontmatter field.
    assert "summary" in body
    assert "shared" in body
    assert "runbooks" in body
    assert "90%" in body
    # Lede rendered above the body.
    assert 'class="note-lede"' in body
    assert "The one-sentence gist." in body
    # Supersedes renders a link to the superseded note.
    assert 'href="/note/prior-note"' in body
    # Tags link to the knowledge list filtered by that tag.
    assert 'href="/knowledge?tag=project%3Ainflux"' in body
    assert 'href="/knowledge?tag=kind%3Aplan"' in body


def test_note_page_marks_quarantined_note_visibly(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    fake.notes["bad-note"] = NoteRecord(
        id="bad-note",
        title="Quarantined Note",
        content="Body.",
        metadata={"status": "quarantined"},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/bad-note")

    assert response.status_code == 200
    # The status chip carries a status-specific class the stylesheet colours,
    # so a quarantined note is visibly quarantined (§6.4 acceptance).
    assert "note-status-quarantined" in response.text
    # ...and the class is not inert: the stylesheet actually styles it.
    css = (
        Path(__file__).parent.parent / "src" / "lithos_lens" / "static" / "lens.css"
    ).read_text()
    assert ".note-status-quarantined" in css


def test_note_page_status_class_cannot_inject_a_second_token(
    lithos_lens_config_env: Path,
) -> None:
    """A hostile ``status`` must not smuggle a second class onto the chip.

    Jinja autoescape stops attribute breakout but leaves whitespace intact, so
    without slugifying, ``status`` = ``quarantined banner-warning`` would render
    ``class="chip note-status note-status-quarantined banner-warning"`` — an
    attacker-chosen class (content spoofing). The slug collapses it to one token.
    """
    fake = TaskFakeLithosClient()
    fake.notes["evil-note"] = NoteRecord(
        id="evil-note",
        title="Evil Note",
        content="Body.",
        metadata={"status": "quarantined banner-warning"},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/evil-note")

    assert response.status_code == 200
    # The injected token never lands as a standalone class.
    assert "note-status-quarantined banner-warning" not in response.text
    assert (
        'class="chip note-status note-status-quarantined-banner-warning"'
        in response.text
    )
    # Display text is still the raw status (escaped as a text node).
    assert "quarantined banner-warning" in response.text


def test_note_page_omits_chips_when_no_frontmatter(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    fake.notes["plain-note"] = NoteRecord(
        id="plain-note",
        title="Plain Note",
        content="Just a body.",
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/plain-note")

    assert response.status_code == 200
    assert 'class="note-chips"' not in response.text
    assert 'class="note-lede"' not in response.text


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
