"""Task descriptions render as Markdown, and are cut at a block boundary.

Agent-written descriptions ARE Markdown — loom's, and the tasks the operator
files through Claude Code: a lead-in paragraph, bold labels, numbered lists,
code spans, acceptance bullets. Every surface used to escape one into a single
``<p>``, which collapsed every newline and showed the markup as literal ``**``
and ``1.``; a board with several long tasks open was unreadable.

Two halves are pinned here:

- the RENDER — the same safety properties the note body has (raw HTML escaped,
  the §6.2 link-scheme allow-list, escaped plaintext when the parse raises),
  plus soft breaks kept, minus the note-only ``[[wiki-link]]`` splicing;
- the CUT — whole top-level blocks while the budget lasts, so a preview never
  ends inside a list, a fence or a table, and the first block is always shown.

The surfaces that use them (row, gate row, side panel, detail page) are pinned
below through the real app, because "everywhere Lens shows a description" is
the claim, and a renderer nothing calls satisfies none of it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import pytest

from lithos_lens import knowledge
from lithos_lens.config import load_config
from lithos_lens.knowledge import description_preview, render_description
from tests.test_task_detail import _client
from tests.test_tasks_mvp import TaskFakeLithosClient, _add_gate

# One description carrying every element the acceptance criteria name: two
# paragraphs, bold, a `-` list, a `1.` list, a code span and a fenced block.
MARKDOWN_DESCRIPTION = dedent(
    """\
    A **bold** lead-in with a `code span`.

    - first bullet
    - second bullet

    1. first step
    2. second step

    ```python
    print("hello")
    ```

    The closing paragraph.
    """
)


def _markdown_body(html: str, *, index: int = 0) -> str:
    """The inner HTML of the ``index``-th ``.markdown-body`` container."""
    bodies = re.findall(
        r'<div class="task-description markdown-body"[^>]*>(.*?)</div>', html, re.S
    )
    assert len(bodies) > index, html
    return bodies[index]


# ── The renderer ───────────────────────────────────────────────────────


def test_description_renders_every_markdown_element() -> None:
    html = render_description(MARKDOWN_DESCRIPTION)

    assert "<strong>bold</strong>" in html
    assert "<code>code span</code>" in html
    assert html.count("<ul>") == 1
    assert html.count("<ol>") == 1
    assert "<pre><code" in html
    # The lead-in and the closing paragraph, as two paragraphs.
    assert "<p>A <strong>bold</strong>" in html
    assert "<p>The closing paragraph.</p>" in html


def test_description_escapes_raw_html_and_neutralises_hostile_links() -> None:
    """Exactly what ``render_markdown`` does to a note body (§6.2): a
    description is agent-written too, and the board is not a place to discover
    that the second renderer was configured more loosely than the first."""
    html = render_description(
        "<script>alert(1)</script>\n\n"
        '<img src=x onerror="alert(1)">\n\n'
        "[click](javascript:alert(1))"
    )

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "onerror" not in html or "&lt;img" in html
    assert "<img" not in html
    # The anchor is refused outright rather than emitted with a live scheme.
    assert "javascript:" not in html.replace("javascript:alert(1)", "")
    assert "<a " not in html


def test_description_falls_back_to_escaped_plaintext_when_the_parser_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parse that raises degrades to escaped plaintext — never to raw
    passthrough, which is the one failure mode that would matter."""

    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(knowledge.DESCRIPTION_MARKDOWN, "render", boom)

    html = render_description("<b>not html</b>")

    assert html == ('<pre class="markdown-fallback">&lt;b&gt;not html&lt;/b&gt;</pre>')


def test_description_leaves_wiki_syntax_literal() -> None:
    """``[[…]]`` is a NOTE concept (§6.3): the resolver cross-checks the source
    note's own outgoing links, and a task has none."""
    html = render_description("see [[some-note]] for context")

    assert "[[some-note]]" in html
    assert "/knowledge/resolve" not in html


def test_description_keeps_single_newlines_as_line_breaks() -> None:
    """``breaks`` is on: a hand-typed description is lines, not a document, and
    CommonMark's soft break would run them together."""
    html = render_description("first line\nsecond line")

    assert html.count("<p>") == 1
    assert "<br" in html


def test_a_description_with_no_markdown_is_the_prose_it_always_was() -> None:
    html = render_description("Work in progress")

    assert html.strip() == "<p>Work in progress</p>"


# ── The cut ────────────────────────────────────────────────────────────


def test_preview_takes_whole_blocks_up_to_the_budget() -> None:
    """Three blocks fit, the fourth crosses: the preview is the first three and
    nothing of the fourth."""
    body = "\n\n".join(["A" * 60, "B" * 60, "C" * 60, "D" * 60])

    preview = description_preview(body, limit=200)

    assert preview.truncated is True
    assert preview.text == "\n\n".join(["A" * 60, "B" * 60, "C" * 60])


def test_preview_never_cuts_inside_a_list() -> None:
    """The headline edge: block two is a 500-character list under a 200 budget,
    so the row shows block one and the link — never half a list."""
    lead = "Lead-in paragraph."
    listing = "\n".join(f"- item {index:03d} padded out" for index in range(25))
    assert len(listing) > 500
    body = f"{lead}\n\n{listing}\n\nTail paragraph."

    preview = description_preview(body, limit=200)

    assert preview.truncated is True
    assert preview.text == lead


def test_preview_never_cuts_inside_a_fenced_block_or_a_table() -> None:
    fence = "```python\n" + "print('x')\n" * 30 + "```"
    table = "| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 20
    body = f"Lead-in.\n\n{fence}\n\n{table}"

    preview = description_preview(body, limit=200)

    assert preview.text == "Lead-in."
    assert preview.truncated is True
    # Neither a dangling fence nor a headerless table row survives the cut.
    assert "```" not in preview.text
    assert "|" not in preview.text


def test_preview_always_shows_the_first_block_even_when_it_alone_is_too_long() -> None:
    """A description whose opening paragraph is huge is previewed BY that
    paragraph. Previewing it by nothing would be the worse answer."""
    first = "A" * 500
    body = f"{first}\n\nsecond block"

    preview = description_preview(body, limit=200)

    assert preview.text == first
    assert preview.truncated is True


def test_a_single_oversized_block_is_shown_whole_and_is_not_truncated() -> None:
    """Nothing was dropped, so nothing says "see more": the row already shows
    the entire description."""
    body = "A" * 500

    preview = description_preview(body, limit=200)

    assert preview.text == body
    assert preview.truncated is False


def test_zero_never_truncates() -> None:
    body = "\n\n".join(f"block {index}" for index in range(50))

    preview = description_preview(body, limit=0)

    assert preview.text == body
    assert preview.truncated is False


def test_a_description_inside_the_budget_is_untouched() -> None:
    preview = description_preview(MARKDOWN_DESCRIPTION, limit=10_000)

    assert preview.text == MARKDOWN_DESCRIPTION
    assert preview.truncated is False


# ── The surfaces ───────────────────────────────────────────────────────


def _described_fixture(description: str) -> TaskFakeLithosClient:
    """The board's in-progress row (``open-claimed``), carrying ``description``."""
    fake = TaskFakeLithosClient()
    fake.tasks[0] = replace(fake.tasks[0], description=description)
    return fake


def _gate_fixture(description: str) -> TaskFakeLithosClient:
    """The same description on a GATE, which renders its own row template."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-described", title="Described gate")
    fake.tasks[-1] = replace(fake.tasks[-1], description=description)
    return fake


def _surface(html: str, task_id: str) -> str:
    """The one surface under test, alone: a row, the side panel, or the page.

    The panel comes first, so `?selected=` is read as the panel it opened
    rather than as the row behind it; the detail PAGE shows one task, so there
    the document is the surface.
    """
    panel = re.search(r'<aside class="task-panel".*?</aside>', html, re.S)
    if panel:
        return panel.group(0)
    row = re.search(rf'<article[^>]*data-task-id="{task_id}".*?</article>', html, re.S)
    if row:
        return row.group(0)
    assert f'data-task-detail="{task_id}"' in html, html
    return html


def _config_with_preview(config_path: Path, chars: int) -> Path:
    config_path.write_text(
        config_path.read_text()
        + f"\n[lithos-lens.tasks]\ndescription_preview_chars = {chars}\n"
    )
    return config_path


@pytest.mark.parametrize(
    ("url", "task_id"),
    [
        # The board row, the detail page, and the side panel — as a fragment
        # (what a row click swaps in) and rendered into the board by
        # `?selected=`, which is its no-JS baseline.
        ("/tasks", "open-claimed"),
        ("/tasks/open-claimed", "open-claimed"),
        ("/tasks/open-claimed?fragment=panel", "open-claimed"),
        ("/tasks?selected=open-claimed", "open-claimed"),
    ],
)
def test_every_surface_renders_the_description_as_markdown(
    lithos_lens_config_env: Path, url: str, task_id: str
) -> None:
    """The headline acceptance criterion: the same elements, on every surface
    that shows a description, inside a `.markdown-body` container."""
    fake = _described_fixture(MARKDOWN_DESCRIPTION)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(url)

    assert response.status_code == 200
    body = _markdown_body(_surface(response.text, task_id))
    assert "<strong>bold</strong>" in body
    assert "<ul>" in body
    assert "<ol>" in body
    assert "<code>code span</code>" in body
    assert "<pre><code" in body
    assert body.count("<p>") == 2


def test_the_gate_row_renders_the_same_markdown(
    lithos_lens_config_env: Path,
) -> None:
    """A gate is a task, and its row shares the same partial."""
    fake = _gate_fixture(MARKDOWN_DESCRIPTION)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    markup = _surface(response.text, "gate-described")
    assert "gate-row" in markup
    body = _markdown_body(markup)
    assert "<strong>bold</strong>" in body
    assert "<ol>" in body


@pytest.mark.parametrize("fixture", [_described_fixture, _gate_fixture])
def test_a_row_truncates_at_a_block_boundary_and_offers_see_more(
    lithos_lens_config_env: Path,
    fixture: Callable[[str], TaskFakeLithosClient],
) -> None:
    """With a 200-character budget the row shows the leading blocks that fit
    and a `see more` anchor whose href is the task's detail page — so the
    no-JavaScript path is plain navigation. Gates truncate identically."""
    body = "\n\n".join(["A" * 60, "B" * 60, "C" * 60, "D" * 60])
    fake = fixture(body)
    task_id = "open-claimed" if fixture is _described_fixture else "gate-described"
    config_path = _config_with_preview(lithos_lens_config_env, 200)

    with _client(config_path, fake) as client:
        response = client.get("/tasks")

    markup = _surface(response.text, task_id)
    preview = _markdown_body(markup)
    assert "A" * 60 in preview
    assert "C" * 60 in preview
    assert "D" * 60 not in preview
    anchor = re.search(
        r'<a class="task-description-more" href="([^"]+)"[^>]*data-description-toggle',
        markup,
    )
    assert anchor is not None, markup
    assert anchor.group(1) == f"/tasks/{task_id}"
    # The full body rides along hidden, so the click expands without a request.
    full = _markdown_body(markup, index=1)
    assert "D" * 60 in full
    assert "hidden" in markup


def test_the_side_panel_truncates_the_same_way(
    lithos_lens_config_env: Path,
) -> None:
    """It shares the partial too: the panel IS a row's answer without leaving
    the board, so it shows what a row shows."""
    body = "\n\n".join(["A" * 60, "B" * 60, "C" * 60, "D" * 60])
    fake = _described_fixture(body)
    config_path = _config_with_preview(lithos_lens_config_env, 200)

    with _client(config_path, fake) as client:
        response = client.get("/tasks/open-claimed?fragment=panel")

    preview = _markdown_body(response.text)
    assert "D" * 60 not in preview
    assert "data-description-toggle" in response.text


def test_the_detail_page_never_truncates_and_offers_no_link(
    lithos_lens_config_env: Path,
) -> None:
    """It is the canonical full view — the page `see more` leads to."""
    body = "\n\n".join(["A" * 60, "B" * 60, "C" * 60, "D" * 60])
    fake = _described_fixture(body)
    config_path = _config_with_preview(lithos_lens_config_env, 200)

    with _client(config_path, fake) as client:
        response = client.get("/tasks/open-claimed")

    assert "D" * 60 in _markdown_body(response.text)
    assert "data-description-toggle" not in response.text


def test_zero_shows_every_description_in_full_with_no_link(
    lithos_lens_config_env: Path,
) -> None:
    body = "\n\n".join(["A" * 60, "B" * 60, "C" * 60, "D" * 60])
    fake = _described_fixture(body)
    config_path = _config_with_preview(lithos_lens_config_env, 0)

    with _client(config_path, fake) as client:
        response = client.get("/tasks")

    assert "D" * 60 in _markdown_body(_surface(response.text, "open-claimed"))
    assert "data-description-toggle" not in response.text


def test_the_shipped_default_is_600(lithos_lens_config_env: Path) -> None:
    assert load_config(lithos_lens_config_env).tasks.description_preview_chars == 600


def test_the_board_survives_a_hostile_description(
    lithos_lens_config_env: Path,
) -> None:
    """End to end, on the surface that matters: nothing a peer writes into a
    description reaches the browser as markup."""
    fake = _described_fixture("<script>alert(1)</script>\n\n[x](javascript:alert(1))")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
