"""The note page's source link and Influx archive notice.

A note's ``source_url`` is the only way back to the article an ingested note
summarises, and for a note Influx never archived (``influx:archive-terminal``)
it is the only copy of the article there is: the note's ``## Archive`` section
is empty and Influx will not try again. The page links the source and says
why the body is missing, rather than leaving an empty heading unexplained.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.knowledge_metadata import build_note_metadata
from lithos_lens.normalizers import normalize_note
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app
from tests.conftest import load_contract
from tests.test_tasks_mvp import TaskFakeLithosClient

_CSS = Path(__file__).parent.parent / "src" / "lithos_lens" / "static" / "lens.css"


def _note(*tags: str, **metadata: object) -> NoteRecord:
    return NoteRecord(
        id="n", title="T", content="body", tags=tags, metadata=dict(metadata)
    )


def _influx_payload(**metadata: Any) -> dict[str, Any]:
    """The vendored Influx note, with frontmatter keys overridden."""
    payload = load_contract("lithos_read")["responses"]["variants"][
        "influx_archive_terminal"
    ]
    return {**payload, "metadata": {**payload["metadata"], **metadata}}


def _get(config_path: Path, record: NoteRecord) -> str:
    fake = TaskFakeLithosClient()
    fake.notes[record.id] = record
    app = create_app(load_config(config_path), lithos_client_factory=lambda _: fake)
    with TestClient(app) as client:
        response = client.get(f"/note/{record.id}")
    assert response.status_code == 200
    return response.text


# ── source_url ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://openai.com/index/x", "https://openai.com/index/x"),
        ("http://example.com/a?b=c#d", "http://example.com/a?b=c#d"),
        # Trimmed, like every other frontmatter string the view model reads.
        ("  https://example.com/x \n", "https://example.com/x"),
        # urlsplit lower-cases the scheme, so an upper-case one still links.
        ("HTTPS://Example.com/x", "HTTPS://Example.com/x"),
    ],
)
def test_source_url_links_an_absolute_http_url(value: str, expected: str) -> None:
    meta = build_note_metadata(_note(source_url=value))
    assert meta.source_url == expected
    assert meta.source_text == ""


@pytest.mark.parametrize(
    "value",
    [
        # Frontmatter is peer-written: nothing but an absolute http(s) url is
        # ever made clickable.
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/x",
        "file:///etc/passwd",
        # Protocol-relative: no scheme, but the browser navigates off-site.
        "//evil.example/x",
        # A scheme with no host is not a link to anywhere.
        "https://",
        "doi:10.1000/182",
        "see the feed",
        # urlsplit raises on an unbalanced IPv6 bracket.
        "https://[::1/x",
    ],
)
def test_source_url_that_is_not_an_http_link_shows_as_text(value: str) -> None:
    meta = build_note_metadata(_note(source_url=value))
    assert meta.source_url == ""
    assert meta.source_text == value


def test_source_text_is_bounded() -> None:
    meta = build_note_metadata(_note(source_url="x" * 500))
    assert len(meta.source_text) == 200
    assert meta.source_text.endswith("…")


@pytest.mark.parametrize("value", [None, "", "   ", 5, ["https://example.com"]])
def test_absent_or_non_string_source_url_shows_nothing(value: object) -> None:
    meta = build_note_metadata(_note(source_url=value))
    assert meta.source_url == ""
    assert meta.source_text == ""


# ── archive notice ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reason_tag", "reason"),
    [
        (
            "influx:archive-unsupported",
            "Influx does not archive articles from this site",
        ),
        ("influx:archive-blocked", "the site blocked Influx's fetch"),
        ("influx:archive-skipped-by-policy", "Influx's archive policy skips this site"),
        ("influx:archive-non-html-source", "the source is not an HTML page"),
    ],
)
def test_archive_notice_names_the_reason_influx_tagged(
    reason_tag: str, reason: str
) -> None:
    meta = build_note_metadata(_note(reason_tag, "influx:archive-terminal"))
    assert meta.archive_notice == (
        f"The article body was not archived: {reason}. Influx will not retry."
    )


def test_archive_notice_without_a_reason_tag_is_the_repair_cap() -> None:
    """The repair sweep marks a note terminal after its cap of failed fetches,
    leaving ``influx:archive-missing`` beside it and no policy reason."""
    meta = build_note_metadata(
        _note("influx:archive-missing", "influx:archive-terminal")
    )
    assert meta.archive_notice == (
        "The article body was not archived: Influx gave up after repeated "
        "failed fetches. Influx will not retry."
    )


@pytest.mark.parametrize(
    "tags",
    [
        (),
        # Not terminal: Influx will retry these, so there is no settled
        # outcome to report.
        ("influx:archive-missing",),
        ("influx:archive-missing", "influx:archive-rate-limited"),
        # A reason tag on its own is not the terminal marker.
        ("influx:archive-unsupported",),
    ],
)
def test_no_archive_notice_unless_the_note_is_archive_terminal(
    tags: tuple[str, ...],
) -> None:
    assert build_note_metadata(_note(*tags)).archive_notice == ""


@pytest.mark.parametrize(
    "archive",
    [
        "## Archive\n\npath: archive/2026/09/x.html\n",
        "## Archive\npath:archive/x.pdf\n",
        "## Archive\r\n\r\npath: archive/x.html\r\n",
        # Influx strips the section body before matching ``path:``, so an
        # indented line is a stored path too (influx ``canonical_note.py``
        # ``_PATH_LINE_RE`` allows the same leading spaces and tabs).
        "## Archive\n  path: archive/x.html\n",
        "## Archive\n\n\tpath: archive/x.html\n",
        "## Archive\r\n \t\r\n  path: archive/x.html\r\n",
        # Influx rejects a second line as malformed, but the section is not
        # empty, so there is no missing body to explain.
        "## Archive\npath: archive/x.html\nstray\n",
    ],
)
def test_no_archive_notice_when_the_archive_section_holds_a_path(
    archive: str,
) -> None:
    """Influx treats a terminal note WITH a stored ``path:`` as a real state
    (``repair.py``: ``archive_ok = archive_path is not None or
    is_archive_terminal``): the tag stops retries, it does not unsay the
    archive. The notice must not contradict a populated ``## Archive``."""
    note = NoteRecord(
        id="n",
        title="T",
        content=f"# T\n\n{archive}\n## Summary\nGist.\n",
        tags=("influx:archive-missing", "influx:archive-terminal"),
    )
    assert build_note_metadata(note).archive_notice == ""


@pytest.mark.parametrize(
    "content",
    [
        "# T\n\n## Archive\n\n## Summary\npath: in the summary is not the archive\n",
        "# T\n\n## Summary\nNo archive section at all.\n",
        # A bare ``path:`` names no file: Influx rejects it as malformed.
        "# T\n\n## Archive\n  path:\n\n## Summary\nGist.\n",
    ],
)
def test_archive_notice_when_the_archive_section_is_empty_or_absent(
    content: str,
) -> None:
    note = NoteRecord(
        id="n",
        title="T",
        content=content,
        tags=("influx:archive-unsupported", "influx:archive-terminal"),
    )
    assert build_note_metadata(note).archive_notice.startswith(
        "The article body was not archived"
    )


# ── the page ───────────────────────────────────────────────────────────


def test_note_page_links_the_source_and_explains_the_missing_archive(
    lithos_lens_config_env: Path,
) -> None:
    # Through the real read normalization, from the vendored Influx note.
    record = normalize_note(_influx_payload())
    url = "https://openai.com/index/managing-ai-investments-in-agentic-era"

    body = _get(lithos_lens_config_env, record)

    source = re.search(r'<p class="note-source">(.*?)</p>', body, re.DOTALL)
    assert source is not None
    assert f'<a href="{url}" rel="noopener noreferrer">{url}</a>' in source.group(1)
    notice = re.search(
        r'<p class="note-archive-notice" data-archive-notice>(.*?)</p>',
        body,
        re.DOTALL,
    )
    assert notice is not None
    assert " ".join(notice.group(1).split()) == (
        "The article body was not archived: Influx does not archive articles "
        "from this site. Influx will not retry. Read the original at the "
        "source link."
    )
    # Both sit in the note header, before the body they explain.
    assert body.index('class="note-archive-notice"') < body.index(
        'class="markdown-body"'
    )


def test_note_page_archive_notice_without_a_source_points_nowhere(
    lithos_lens_config_env: Path,
) -> None:
    record = normalize_note(_influx_payload(source_url=None))

    body = _get(lithos_lens_config_env, record)

    assert 'class="note-source"' not in body
    assert "data-archive-notice" in body
    assert "source link" not in body


def test_note_page_never_links_a_dangerous_source_url(
    lithos_lens_config_env: Path,
) -> None:
    record = normalize_note(_influx_payload(source_url="javascript:alert(1)"))

    body = _get(lithos_lens_config_env, record)

    assert 'href="javascript:' not in body
    source = re.search(r'<p class="note-source">(.*?)</p>', body, re.DOTALL)
    assert source is not None
    assert "<a " not in source.group(1)
    assert "javascript:alert(1)" in source.group(1)
    # With no link to point at, the notice does not send the reader to one.
    assert "source link" not in body


def test_note_page_source_url_is_escaped(lithos_lens_config_env: Path) -> None:
    record = normalize_note(
        _influx_payload(source_url='https://example.com/"><script>alert(1)</script>')
    )

    body = _get(lithos_lens_config_env, record)

    assert "<script>alert(1)</script>" not in body
    # The quote cannot close the href and open a tag.
    assert 'href="https://example.com/">' not in body
    assert "&lt;script&gt;" in body


def test_note_page_without_source_or_archive_tags_shows_neither(
    lithos_lens_config_env: Path,
) -> None:
    # The canonical read: source_url null, no Influx tags.
    record = normalize_note(load_contract("lithos_read")["responses"]["success"])

    body = _get(lithos_lens_config_env, record)

    assert 'class="note-source"' not in body
    assert "data-archive-notice" not in body


def test_note_source_line_breaks_a_long_url() -> None:
    """A url is one unbroken token to the line breaker: like the knowledge
    landing's echoed query, it must break rather than widen the page."""
    match = re.search(r"(?m)^\.note-source\s*\{([^}]*)\}", _CSS.read_text())
    assert match is not None, "lens.css has no .note-source rule"
    assert re.search(r"overflow-wrap:\s*anywhere;", match.group(1))
