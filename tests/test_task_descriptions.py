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
from lithos_lens.knowledge import (
    description_preview,
    render_description,
    render_markdown,
)
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


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "src" / "lithos_lens" / "templates"
STATIC = REPO_ROOT / "src" / "lithos_lens" / "static"


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


def test_description_escapes_raw_html_as_visible_text() -> None:
    """Exactly what ``render_markdown`` does to a note body (§6.2): a
    description is agent-written too, and the board is not a place to discover
    that the second renderer was configured more loosely than the first.

    ESCAPED, character for character — not merely absent. Dropping the tag
    would satisfy "no live markup reached the browser" while losing text the
    author wrote, and a reader could not tell which had happened."""
    html = render_description(
        '<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">'
    )

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "<script" not in html
    assert "<img" not in html


# The §6.2 corpus, as the note renderer's own tests spell it: what may become
# an anchor (the three allowed schemes, plus relative destinations) and what
# may not (every other scheme, and the protocol-relative forms of issue #41).
ALLOWED_DESTINATIONS = (
    "https://example.com",
    "http://x.com",
    "mailto:a@b.com",
    "/tasks/abc",
    "docs/note.md",
    "#section",
    "./sibling.md",
)
REFUSED_DESTINATIONS = (
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "  javascript:alert(1)",
    "ftp://example.com",
    "vbscript:msgbox",
    "file:///etc/passwd",
    "tel:+15551234",
    "data:text/html;base64,PHNjcmlwdD4=",
    "//evil.example/x",
    "///evil.example/x",
    "  //evil.example/x",
)


@pytest.mark.parametrize("url", ALLOWED_DESTINATIONS + REFUSED_DESTINATIONS)
def test_a_description_link_is_judged_exactly_as_a_note_link_is(url: str) -> None:
    """The allow-list is SHARED, and this is what says so: both renderers are
    asked the same question and must give the same answer, byte for byte.

    A test that only tried ``javascript:`` would stay green with
    ``validateLink`` unwired, because markdown-it's own default validator
    rejects that one — and descriptions would quietly start emitting anchors
    for ``ftp:``, ``data:`` and ``//evil.example/…``.
    """
    markup = f"[x]({url})"

    assert render_description(markup) == render_markdown(markup)


@pytest.mark.parametrize("url", ALLOWED_DESTINATIONS)
def test_an_allow_listed_description_link_becomes_an_anchor(url: str) -> None:
    """Guard against the differential above passing because NEITHER renderer
    emits anchors any more."""
    assert f'<a href="{url}">x</a>' in render_description(f"[x]({url})")


@pytest.mark.parametrize("url", REFUSED_DESTINATIONS)
def test_a_refused_description_link_emits_no_anchor(url: str) -> None:
    html = render_description(f"[x]({url})")

    assert "<a " not in html
    assert "href" not in html


def test_a_description_image_may_not_point_off_site_either() -> None:
    """The same hole on the image path — an off-site pixel leaks the reader's
    IP, and a board row is a place an agent-written description is rendered
    unattended."""
    assert "<img" not in render_description("![x](//evil.example/pixel.png)")


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


def test_the_budget_counts_the_separators_the_author_actually_wrote() -> None:
    """The cut is a PREFIX of the source, not a rebuilt join of block texts.

    A heading and the paragraph under it need no blank line between them, so
    the two occupy exactly 200 source characters and both fit a 200 budget. A
    preview reassembled with a synthetic ``\n\n`` between every block charges
    two characters nobody wrote and drops the paragraph.
    """
    body = "# H\n" + "P" * 196 + "\n\nTail"

    preview = description_preview(body, limit=200)

    assert preview.text == "# H\n" + "P" * 196
    assert preview.truncated is True


def test_a_reference_definition_before_its_use_survives_the_cut() -> None:
    """A ``[id]: …`` line emits no block token of its own. Rebuilding the
    preview out of token texts dropped it, and the row then showed literal
    ``[link][id]`` where the full body showed a link."""
    body = "[id]: https://example.com\n\nUse [link][id].\n\n" + "T" * 400

    preview = description_preview(body, limit=200)

    assert preview.truncated is True
    assert preview.text == "[id]: https://example.com\n\nUse [link][id]."
    assert '<a href="https://example.com">link</a>' in preview.html


def test_a_reference_definition_after_its_use_still_renders_the_link() -> None:
    """CommonMark lets the definition come last, so a cut before it leaves the
    retained paragraph with no way to resolve its own link. The preview is
    rendered against the WHOLE description's definitions, so the row shows the
    anchor the detail page shows rather than markup the author did not write."""
    body = "Use [link][id].\n\n" + "T" * 400 + "\n\n[id]: https://example.com"

    preview = description_preview(body, limit=200)

    assert preview.truncated is True
    assert preview.text == "Use [link][id]."
    assert '<a href="https://example.com">link</a>' in preview.html
    assert "[link][id]" not in preview.html


def test_the_preview_of_an_untruncated_description_needs_no_link_context() -> None:
    """``source`` is empty unless a cut was made, and an empty context must not
    silently blank the body it renders."""
    preview = description_preview("Use [link][id].\n\n[id]: https://x.test", limit=0)

    assert preview.truncated is False
    assert '<a href="https://x.test">link</a>' in preview.html


@pytest.mark.parametrize(
    "separator",
    [
        # Everything `str.splitlines` breaks on and markdown-it does NOT: the
        # parser's line boundaries are `\n`, `\r\n` and `\r`, full stop. Each of
        # these is ordinary CONTENT inside a paragraph.
        "\u2028",  # LINE SEPARATOR
        "\u2029",  # PARAGRAPH SEPARATOR
        "\x0b",  # VERTICAL TAB
        "\x0c",  # FORM FEED
        "\x85",  # NEXT LINE
        "\x1c",  # FILE SEPARATOR
        "\x1d",  # GROUP SEPARATOR
        "\x1e",  # RECORD SEPARATOR
    ],
)
def test_a_unicode_separator_inside_a_paragraph_is_not_a_block_boundary(
    separator: str,
) -> None:
    """Mapping a token's line numbers back onto the source has to use the
    PARSER's idea of a line.

    `str.splitlines` breaks on seven characters markdown-it does not, so every
    offset after one of them was out of step with the block it bounded — and
    the cut landed inside the first paragraph, against the one guarantee the
    budget never overrides: the first block is always shown whole.
    """
    first = f"A{separator}B"
    body = f"{first}\n\nC\n\n" + "D" * 100

    preview = description_preview(body, limit=1)

    assert preview.text == first
    assert preview.truncated is True


def test_an_indented_code_blocks_trailing_spaces_survive_the_cut() -> None:
    """A block the cut decided to keep WHOLE must reach the row whole.

    Trailing spaces on the last line are ignorable after a paragraph and are
    CONTENT in an indented code block, so trimming them reflowed the very block
    the boundary rule exists to protect. The proof is the rendering: the
    ``<pre>`` on the row is the one the full body shows, character for
    character.
    """
    first = "    code  "
    body = f"{first}\n\n" + "T" * 400

    preview = description_preview(body, limit=20)

    assert preview.truncated is True
    assert preview.text == first
    assert "<pre><code>code  \n</code></pre>" in preview.html
    assert "<pre><code>code  \n</code></pre>" in preview.full_html


def test_a_blank_line_a_block_swallowed_is_still_a_separator() -> None:
    """The other side of the same trim: a bullet list's map ends AFTER the
    blank line that closed it, and that line is separator, not content. Leaving
    it in would spend budget on a line the preview does not show and end the
    preview source on a blank line."""
    body = "- a\n- b\n\n" + "T" * 400

    preview = description_preview(body, limit=20)

    assert preview.text == "- a\n- b"
    assert preview.truncated is True


def test_a_paragraphs_own_trailing_whitespace_is_not_trimmed_away() -> None:
    """Only the inter-block SEPARATOR is trimmed — the newline and the blank
    lines after a block, blank meaning spaces and tabs. A non-breaking space is
    text the author wrote, and an unrestricted ``rstrip()`` ate it."""
    first = "Para\u00a0"
    body = f"{first}\n\n" + "T" * 400

    preview = description_preview(body, limit=200)

    assert preview.text == first
    assert preview.truncated is True


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_every_real_line_ending_cuts_at_the_same_block_boundary(
    newline: str,
) -> None:
    """The other half of the alphabet: these three ARE line endings, and each
    is ONE of them — ``normalize`` rewrites ``\r\n`` and a lone ``\r`` to
    ``\n`` before the parser counts lines. A description authored on Windows
    (or by something emitting bare CR) must therefore number its lines exactly
    as an LF one does, while the offsets stay those of the original text.
    """
    body = f"First para{newline}{newline}Second para{newline}{newline}" + "T" * 400

    preview = description_preview(body, limit=200)

    assert preview.text == f"First para{newline}{newline}Second para"
    assert preview.truncated is True
    assert body.startswith(preview.text)


@pytest.mark.parametrize("limit", [1, 20, 45, 60, 90, 120, 200])
def test_a_preview_is_always_a_prefix_of_the_description(limit: int) -> None:
    """The structural form of "the cut is made on the source": whatever the
    budget, what the row shows is the description's own opening characters —
    never a reassembly of them, which is where a changed separator or a
    dropped reference definition can only come from."""
    preview = description_preview(MARKDOWN_DESCRIPTION, limit=limit)

    assert MARKDOWN_DESCRIPTION.startswith(preview.text)


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


def test_preview_never_cuts_inside_a_fenced_block() -> None:
    """A fence is one block however many blank lines it contains: half of one
    is not even valid markup — the closing ``` would be missing."""
    fence = "```python\n" + "print('x')\n" * 30 + "```"
    body = f"Lead-in.\n\n{fence}\n\nTail paragraph."

    preview = description_preview(body, limit=200)

    assert preview.text == "Lead-in."
    assert preview.truncated is True
    assert "```" not in preview.text


def test_preview_never_cuts_inside_a_table() -> None:
    """The table gets the second-block position of its own, not a place behind
    an oversized fence: behind one, selection stops before the table is ever
    considered and the test passes whether or not the parser knows what a table
    is. A table cut mid-body renders as a headerless run of rows."""
    table = "| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 25
    assert len(table) > 200
    body = f"Lead-in.\n\n{table}\nTail paragraph."

    preview = description_preview(body, limit=200)

    assert preview.text == "Lead-in."
    assert preview.truncated is True
    assert "|" not in preview.text
    # …and the table this refused to split IS a table when it is rendered, so
    # the block it was kept whole as is the block the reader gets.
    assert "<table>" in preview.full_html
    assert preview.full_html.count("<tr>") == 26


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
    # The full body rides along HIDDEN, under the hook tasks.js swaps it in by,
    # inside the group element it scopes that swap to. Spelled out rather than
    # asserted as "a second markdown-body somewhere with the word hidden in the
    # row": the exact attributes ARE the contract with the script, and a test
    # that does not name them stays green while a renamed hook breaks the
    # click (test-quality/f-002).
    assert "<div data-description>" in markup
    assert (
        '<div class="task-description markdown-body" data-task-description>' in markup
    )
    full = re.search(
        r'<div class="task-description markdown-body" data-task-description-full '
        r"hidden>(.*?)</div>",
        markup,
        re.S,
    )
    assert full is not None, markup
    assert "D" * 60 in full.group(1)


def test_the_markup_carries_exactly_the_hooks_the_script_reaches_for() -> None:
    """The two halves of the progressive enhancement, checked against each
    other. Both sides of this contract are otherwise mocked — the Node harness
    fabricates the elements, and the server test reads the markup — so a hook
    renamed on ONE side leaves every test green and the real click navigating
    away instead of expanding (test-quality/f-002)."""
    template = (TEMPLATES / "tasks" / "description.html").read_text()
    script = (STATIC / "tasks.js").read_text()

    for hook in (
        "data-description",
        "data-task-description",
        "data-task-description-full",
        "data-description-toggle",
    ):
        # In the script as the selector it queries by…
        assert f'"[{hook}]"' in script, hook
        # …and in the template as an attribute of its own, not as the prefix
        # of a longer one (`data-description` vs `data-description-toggle`).
        assert re.search(rf"(?<![\w-]){hook}(?=[\s=>])", template), hook


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
