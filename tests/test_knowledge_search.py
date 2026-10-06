"""K1-S6 — /knowledge hybrid search + recently-updated landing."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import ConfigError, LithosConfig, load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge_metadata import load_list_chips
from lithos_lens.knowledge_search import SearchResult, normalize_search_result
from lithos_lens.lithos_client import LithosClient, LithosToolError
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app


def _client(config_path: Path, fake: FakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def _dataset(notes: dict[str, NoteRecord]) -> FakeLithosDataset:
    return FakeLithosDataset(notes=notes)


# ── search result view model + normalizer (pure) ───────────────────────


def test_normalize_search_result_reads_the_results_row_shape() -> None:
    row = normalize_search_result(
        {
            "id": "n-1",
            "title": "Influx migration plan",
            "path": "plans/influx.md",
            "snippet": "Cut over the ingest path first.",
            "updated_at": "2026-08-01T10:00:00+00:00",
            "score": 0.87,
        }
    )

    assert row == SearchResult(
        id="n-1",
        title="Influx migration plan",
        path="plans/influx.md",
        snippet="Cut over the ingest path first.",
        updated="2026-08-01T10:00:00+00:00",
        score=0.87,
    )


def test_normalize_search_result_accepts_updated_alias_and_missing_score() -> None:
    row = normalize_search_result({"id": "n-2", "updated": "2026-08-02"})

    assert row.updated == "2026-08-02"
    assert row.score is None


@pytest.mark.parametrize(
    ("snippet", "title", "expected"),
    [
        # A leading `# <title>` line is dropped (with the blank line after it).
        (
            "# Influx plan\n\n## Cutover\nPhase one",
            "Influx plan",
            "## Cutover\nPhase one",
        ),
        # Trimming + whitespace collapsing; an ATX closing sequence is ignored.
        ("  #  Influx \t plan  \nBody", " Influx  plan", "Body"),
        ("# Influx plan ##\nBody", "Influx plan", "Body"),
        # CR-only and CRLF line endings end the heading line too.
        ("# Influx plan\r\rBody.", "Influx plan", "Body."),
        ("# Influx plan\r\n\r\nBody", "Influx plan", "Body"),
        # Only complete blank lines go: the first kept line keeps its indent.
        ("# Influx plan\n\n    code", "Influx plan", "    code"),
        ("# Influx plan\n \n\tcode", "Influx plan", "\tcode"),
        # Compared by rendered text, like the note page: the markup-wrapped
        # title matches, and a title that IS the markup source does not.
        ("# **Influx plan**\n\nBody", "Influx plan", "Body"),
        ("# **Influx plan**\nBody", "**Influx plan**", "# **Influx plan**\nBody"),
        # A setext H1 is the same heading.
        ("Influx plan\n===\nBody", "Influx plan", "Body"),
        # Case-sensitive, like the note page.
        ("# influx plan\nBody", "Influx plan", "# influx plan\nBody"),
        # A non-matching H1 is kept.
        ("# Something else\nBody", "Influx plan", "# Something else\nBody"),
        # An H2 (or `#` with no space, which is not a heading) is kept.
        ("## Influx plan\nBody", "Influx plan", "## Influx plan\nBody"),
        ("#Influx plan\nBody", "Influx plan", "#Influx plan\nBody"),
        # An H1-shaped line inside a code fence is not the first line.
        ("```\n# Influx plan\n```", "Influx plan", "```\n# Influx plan\n```"),
        # A matching H1 that is not the first line is kept.
        ("Intro\n# Influx plan", "Influx plan", "Intro\n# Influx plan"),
        # Only the first line: a second matching line right after is kept.
        ("# Influx plan\n# Influx plan\nBody", "Influx plan", "# Influx plan\nBody"),
        # The snippet is just the title line: nothing is left to show.
        ("# Influx plan", "Influx plan", ""),
        # An empty title differs from a non-empty heading line, which is kept...
        ("# Influx plan\nBody", "", "# Influx plan\nBody"),
        # ...but an empty heading repeats an empty or whitespace-only title.
        ("#\nBody", "", "Body"),
        ("#\n\nBody.", " \t ", "Body."),
        # An image counts as its rendered alt (code spans dropped), as on the
        # note page.
        ("# Influx plan ![`deco`](/i.svg)\nBody", "Influx plan", "Body"),
        (
            "# Influx plan ![`deco`](/i.svg)\nBody",
            "Influx plan deco",
            "# Influx plan ![`deco`](/i.svg)\nBody",
        ),
        # A non-empty alt does count toward the heading's text.
        ("# Influx plan ![logo](/i.svg)\nBody", "Influx plan logo", "Body"),
        (
            "# Influx plan ![logo](/i.svg)\nBody",
            "Influx plan",
            "# Influx plan ![logo](/i.svg)\nBody",
        ),
        # A wiki-link heading compares by its display text, like the note page.
        ("# [[Influx plan]]\nBody", "Influx plan", "Body"),
        ("# [[Influx plan]]\nBody", "[[Influx plan]]", "# [[Influx plan]]\nBody"),
    ],
)
def test_normalize_search_result_drops_a_leading_title_line(
    snippet: str, title: str, expected: str
) -> None:
    row = normalize_search_result({"id": "n", "title": title, "snippet": snippet})
    assert row.snippet == expected


def test_knowledge_search_card_omits_the_title_line_from_its_snippet(
    lithos_lens_config_env: Path,
) -> None:
    notes = {
        "plan": NoteRecord(
            id="plan",
            title="Influx migration plan",
            content="# Influx migration plan\n\nCut over the ingest path first.",
        )
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=ingest")

    assert response.status_code == 200
    assert "Cut over the ingest path first." in response.text
    # The card title still shows; the snippet no longer repeats it.
    assert "# Influx migration plan" not in response.text


def test_search_result_label_falls_back_to_path_then_id() -> None:
    assert SearchResult(id="x", path="p/x.md").label == "p/x.md"
    assert SearchResult(id="x").label == "x"


# ── landing page: search cards ─────────────────────────────────────────


def test_knowledge_query_renders_search_cards_with_snippet_and_updated(
    lithos_lens_config_env: Path,
) -> None:
    notes = {
        "plan": NoteRecord(
            id="plan",
            title="Influx migration plan",
            content="Cut over the ingest path first, then backfill the rest.",
            metadata={"updated": "2026-08-01T10:00:00+00:00"},
        )
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=ingest")

    assert response.status_code == 200
    assert "Results for" in response.text
    assert 'href="/note/plan?next=' in response.text
    assert "Influx migration plan" in response.text
    # The card carries a snippet window of the body...
    assert "ingest path" in response.text
    # ...and the updated date, formatted like every other Lens date (dd/mm/yyyy).
    assert "01/08/2026" in response.text


def test_knowledge_search_snippet_is_rendered_escaped(
    lithos_lens_config_env: Path,
) -> None:
    # Verified live: lithos_search snippets contain raw markdown/markup. The
    # results page MUST escape them — never feed them through the markdown
    # renderer — so a hostile snippet can't script the browser or forge a link.
    notes = {
        "evil": NoteRecord(
            id="evil",
            title="Hostile note",
            content="Danger <script>alert(1)</script> and a [[wiki|link]] inside.",
        )
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=Danger")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    # Wiki syntax in a snippet stays literal — it is not turned into a resolver
    # anchor (only rendered note bodies get wiki-link splicing).
    assert "/knowledge/resolve" not in response.text


def test_knowledge_query_with_no_matches_renders_empty_state(
    lithos_lens_config_env: Path,
) -> None:
    """A non-empty query with zero hits renders the empty state (a truly empty
    ``?q=`` is not a search at all — it falls through to the recent list,
    covered by the landing tests below)."""
    fake = FakeLithosClient(dataset=_dataset({}))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=no-such-note")

    assert response.status_code == 200
    assert "No matching notes." in response.text


def test_knowledge_query_uses_search_not_list(lithos_lens_config_env: Path) -> None:
    # A query drives lithos_search (hybrid), never lithos_list. The fake records
    # each call, so we can assert the query path went to search and carried the
    # configured limit.
    notes = {"plan": NoteRecord(id="plan", title="Influx plan", content="ingest path")}
    fake = FakeLithosClient(dataset=_dataset(notes))
    search_calls: list[dict[str, Any]] = []
    list_calls: list[dict[str, Any]] = []
    original_search = fake.search_notes
    original_list = fake.list_notes

    async def record_search(query: str, **kwargs: Any) -> list[SearchResult]:
        search_calls.append({"query": query, **kwargs})
        return await original_search(query, **kwargs)

    async def record_list(**kwargs: Any) -> Any:
        list_calls.append(kwargs)
        return await original_list(**kwargs)

    fake.search_notes = record_search  # type: ignore[method-assign]
    fake.list_notes = record_list  # type: ignore[method-assign]

    with _client(lithos_lens_config_env, fake) as client:
        client.get("/knowledge?q=ingest")

    assert [c["query"] for c in search_calls] == ["ingest"]
    assert search_calls[0]["limit"] == 20  # [knowledge].search_limit default
    assert list_calls == []


def test_knowledge_query_with_tag_filters_the_search(
    lithos_lens_config_env: Path,
) -> None:
    notes = {
        "a": NoteRecord(
            id="a", title="Match A", content="shared body", tags=("project:x",)
        ),
        "b": NoteRecord(
            id="b", title="Match B", content="shared body", tags=("project:y",)
        ),
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=shared&tag=project:x")

    assert response.status_code == 200
    assert 'href="/note/a?next=' in response.text
    assert 'href="/note/b' not in response.text


# ── landing page: recently-updated browse (no query) ───────────────────


def _dated_note(
    note_id: str, title: str, updated: str, *, content: str = "Body.", **kw: Any
) -> NoteRecord:
    return NoteRecord(
        id=note_id,
        title=title,
        content=content,
        metadata={"updated_at": updated},
        **kw,
    )


def test_bare_knowledge_renders_recent_list_newest_first(
    lithos_lens_config_env: Path,
) -> None:
    """The bare landing is a RECENCY list: notes render newest-first by their
    ``updated`` stamp regardless of dataset/insertion order (lithos_list has no
    ordering parameter — recent_notes owns the sort), with dates shown."""
    notes = {
        "oldest": _dated_note("oldest", "Oldest note", "2026-07-01T10:00:00+00:00"),
        "newest": _dated_note("newest", "Newest note", "2026-08-09T10:00:00+00:00"),
        "middle": _dated_note("middle", "Middle note", "2026-08-02T10:00:00+00:00"),
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge")

    assert response.status_code == 200
    assert "Recently updated" in response.text
    positions = {
        note_id: response.text.index(f'href="/note/{note_id}?next=')
        for note_id in ("newest", "middle", "oldest")
    }
    assert positions["newest"] < positions["middle"] < positions["oldest"]
    # Each row carries its update date, Lens-formatted.
    assert "09/08/2026" in response.text
    assert "02/08/2026" in response.text


def test_bare_knowledge_uses_recent_notes_not_search(
    lithos_lens_config_env: Path,
) -> None:
    """No query → the recent browse path (recent_notes with the configured
    recent_limit), never lithos_search."""
    notes = {"n": _dated_note("n", "A note", "2026-08-01T10:00:00+00:00")}
    fake = FakeLithosClient(dataset=_dataset(notes))
    recent_calls: list[dict[str, Any]] = []
    search_calls: list[dict[str, Any]] = []
    original_recent = fake.recent_notes
    original_search = fake.search_notes

    async def record_recent(**kwargs: Any) -> Any:
        recent_calls.append(kwargs)
        return await original_recent(**kwargs)

    async def record_search(query: str, **kwargs: Any) -> list[SearchResult]:
        search_calls.append({"query": query, **kwargs})
        return await original_search(query, **kwargs)

    fake.recent_notes = record_recent  # type: ignore[method-assign]
    fake.search_notes = record_search  # type: ignore[method-assign]

    with _client(lithos_lens_config_env, fake) as client:
        client.get("/knowledge")

    assert recent_calls == [{"tags": None, "limit": 20}]  # [knowledge].recent_limit
    assert search_calls == []


def test_knowledge_tag_browse_forwards_tag_and_orders_newest_first(
    lithos_lens_config_env: Path,
) -> None:
    """Tag-only browsing forwards ``tags=[tag]`` to recent_notes and stays a
    recency list over the tagged subset."""
    notes = {
        "old-x": _dated_note(
            "old-x", "Old X", "2026-07-01T10:00:00+00:00", tags=("project:x",)
        ),
        "new-x": _dated_note(
            "new-x", "New X", "2026-08-09T10:00:00+00:00", tags=("project:x",)
        ),
        "y": _dated_note(
            "y", "Y note", "2026-08-10T10:00:00+00:00", tags=("project:y",)
        ),
    }
    fake = FakeLithosClient(dataset=_dataset(notes))
    recent_calls: list[dict[str, Any]] = []
    original_recent = fake.recent_notes

    async def record_recent(**kwargs: Any) -> Any:
        recent_calls.append(kwargs)
        return await original_recent(**kwargs)

    fake.recent_notes = record_recent  # type: ignore[method-assign]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?tag=project:x")

    assert recent_calls == [{"tags": ["project:x"], "limit": 20}]
    assert response.status_code == 200
    assert 'href="/note/y' not in response.text
    assert response.text.index('href="/note/new-x?next=') < response.text.index(
        'href="/note/old-x?next='
    )


def test_fake_recent_notes_sorts_and_truncates_like_the_real_leg() -> None:
    """recent_limit is respected AFTER the newest-first sort: with limit=2 the
    two newest notes survive, not the first two inserted."""
    notes = {
        "oldest": _dated_note("oldest", "Oldest", "2026-07-01T10:00:00+00:00"),
        "newest": _dated_note("newest", "Newest", "2026-08-09T10:00:00+00:00"),
        "middle": _dated_note("middle", "Middle", "2026-08-02T10:00:00+00:00"),
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    rows = asyncio.run(fake.recent_notes(limit=2))

    assert [row.id for row in rows] == ["newest", "middle"]
    assert rows[0].updated == "2026-08-09T10:00:00+00:00"


# ── landing form: the active tag round-trips through a search ──────────


def test_landing_form_retains_active_tag(lithos_lens_config_env: Path) -> None:
    """Searching from /knowledge?tag=… must keep the filter: the form carries
    the active tag as a hidden input (escaped), and shows the active-filter
    line with a clear link. Without a tag, neither renders."""
    fake = FakeLithosClient(dataset=_dataset({}))

    with _client(lithos_lens_config_env, fake) as client:
        tagged = client.get("/knowledge?tag=project:x")
        bare = client.get("/knowledge")

    assert '<input type="hidden" name="tag" value="project:x">' in tagged.text
    assert "Filtered by" in tagged.text
    assert 'name="tag"' not in bare.text
    assert "Filtered by" not in bare.text


def test_landing_form_escapes_hostile_tag_value(lithos_lens_config_env: Path) -> None:
    fake = FakeLithosClient(dataset=_dataset({}))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get('/knowledge?tag="><script>alert(1)</script>')

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text


def test_search_submitted_from_tag_page_stays_filtered(
    lithos_lens_config_env: Path,
) -> None:
    """End-to-end round-trip: submit the landing form exactly as a browser
    would from /knowledge?tag=… (its q input + the hidden tag input) and the
    search stays scoped to the tag."""
    notes = {
        "a": _dated_note(
            "a",
            "Match A",
            "2026-08-01T10:00:00+00:00",
            content="shared body",
            tags=("project:x",),
        ),
        "b": _dated_note(
            "b",
            "Match B",
            "2026-08-02T10:00:00+00:00",
            content="shared body",
            tags=("project:y",),
        ),
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        tag_page = client.get("/knowledge?tag=project:x")
        # The form's GET action with its two inputs, as submitted.
        response = client.get("/knowledge", params={"q": "shared", "tag": "project:x"})

    assert '<input type="hidden" name="tag" value="project:x">' in tag_page.text
    assert response.status_code == 200
    assert 'href="/note/a?next=' in response.text
    assert 'href="/note/b' not in response.text
    # The results page still shows (and can clear) the active filter.
    assert "Filtered by" in response.text
    assert "clear filter" in response.text


# ── nav search box (on every page) ─────────────────────────────────────


def test_nav_search_box_present_on_tasks_page(lithos_lens_config_env: Path) -> None:
    fake = FakeLithosClient(dataset=_dataset({}))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    assert response.status_code == 200
    assert 'class="nav-search"' in response.text
    assert 'action="/knowledge"' in response.text


def test_nav_search_box_present_on_note_page(lithos_lens_config_env: Path) -> None:
    notes = {"n": NoteRecord(id="n", title="A note", content="body")}
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/n")

    assert response.status_code == 200
    assert 'class="nav-search"' in response.text


# ── concrete client transport (lithos_search) ──────────────────────────


class _StubLithosClient(LithosClient):
    """LithosClient with the MCP transport stubbed out (records exact calls)."""

    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        super().__init__(LithosConfig())
        self._payloads = payloads
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self._payloads.get(name, {})


def _run_client(client: LithosClient, coro: Any) -> Any:
    async def _driver() -> Any:
        try:
            return await coro
        finally:
            await client.close()

    return asyncio.run(_driver())


REAL_LITHOS_SEARCH_PAYLOAD: dict[str, Any] = {
    "results": [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "title": "Influx migration plan",
            "path": "plans/influx-migration.md",
            "snippet": "Cut over the ingest path first, then # backfill",
            "updated_at": "2026-08-01T10:00:00+00:00",
            "score": 0.91,
        }
    ],
    "total": 1,
}


def test_client_search_notes_reads_the_results_envelope() -> None:
    client = _StubLithosClient({"lithos_search": REAL_LITHOS_SEARCH_PAYLOAD})

    rows = _run_client(client, client.search_notes("ingest"))

    assert [row.id for row in rows] == ["11111111-1111-4111-8111-111111111111"]
    assert rows[0].title == "Influx migration plan"
    assert rows[0].path == "plans/influx-migration.md"
    assert rows[0].snippet == "Cut over the ingest path first, then # backfill"
    assert rows[0].updated == "2026-08-01T10:00:00+00:00"


def test_client_search_notes_drops_a_snippet_title_line_repeating_the_title() -> None:
    # The live path: the real client's own normalization, on the vendored
    # lithos_search row shape, with a snippet opening on the note's title.
    row = dict(REAL_LITHOS_SEARCH_PAYLOAD["results"][0])
    row["snippet"] = "# Influx migration plan\n\n## Cutover\n\nThe influx cutover runs…"
    client = _StubLithosClient({"lithos_search": {"results": [row], "total": 1}})

    rows = _run_client(client, client.search_notes("influx"))

    assert rows[0].title == "Influx migration plan"
    assert rows[0].snippet == "## Cutover\n\nThe influx cutover runs…"


# A server-windowed snippet well past any plausible Lens-side cap (the fake's
# own 160-char window included), ending in a suffix only a whole snippet keeps.
_LONG_SNIPPET_BODY = "The influx cutover runs in three phases. " * 15 + "TAIL-7f3e9"


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        # Matching title line: dropped, the rest shown whole.
        ("# Influx migration plan\n\n" + _LONG_SNIPPET_BODY, _LONG_SNIPPET_BODY),
        # Non-matching title line: the snippet exactly as Lithos sent it.
        ("# Other\n\n" + _LONG_SNIPPET_BODY, "# Other\n\n" + _LONG_SNIPPET_BODY),
    ],
)
def test_client_search_notes_keeps_a_long_snippet_whole(
    snippet: str, expected: str
) -> None:
    # Lithos windows the snippet; Lens adds no truncation of its own.
    row = dict(REAL_LITHOS_SEARCH_PAYLOAD["results"][0])
    row["snippet"] = snippet
    client = _StubLithosClient({"lithos_search": {"results": [row], "total": 1}})

    rows = _run_client(client, client.search_notes("influx"))

    assert rows[0].snippet == expected


class _ServerSnippetFake(FakeLithosClient):
    """Answers ``search_notes`` with one row carrying a given server snippet."""

    def __init__(self, snippet: str) -> None:
        super().__init__(dataset=_dataset({}))
        self._snippet = snippet

    async def search_notes(
        self, query: str, *, tags: list[str] | None = None, limit: int | None = None
    ) -> list[SearchResult]:
        return [
            normalize_search_result(
                {
                    "id": "plan",
                    "title": "Influx migration plan",
                    "snippet": self._snippet,
                }
            )
        ]


@pytest.mark.parametrize("heading", ["# Influx migration plan", "# Other"])
def test_knowledge_search_card_shows_a_long_server_snippet_whole(
    lithos_lens_config_env: Path, heading: str
) -> None:
    fake = _ServerSnippetFake(f"{heading}\n\n{_LONG_SNIPPET_BODY}")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=influx")

    assert response.status_code == 200
    assert _LONG_SNIPPET_BODY in response.text
    assert ("# Other" in response.text) is (heading == "# Other")


def test_client_search_notes_sends_hybrid_mode_and_filters() -> None:
    client = _StubLithosClient({"lithos_search": {"results": []}})

    _run_client(
        client,
        client.search_notes("rollback", tags=["kind:runbook"], limit=10),
    )

    assert client.calls == [
        (
            "lithos_search",
            {
                "query": "rollback",
                "mode": "hybrid",
                "tags": ["kind:runbook"],
                "limit": 10,
            },
        )
    ]


def test_client_search_notes_ignores_the_list_items_key() -> None:
    # "items" is lithos_list's container; lithos_search answers under "results".
    # A payload using the wrong key yields nothing rather than silently reading
    # the wrong envelope.
    client = _StubLithosClient({"lithos_search": {"items": [{"id": "x"}]}})

    rows = _run_client(client, client.search_notes("x"))

    assert rows == []


# ── config: [knowledge].search_limit / recent_limit ────────────────────


def test_config_defaults_search_and_recent_limits(lithos_lens_config_env: Path) -> None:
    config = load_config(lithos_lens_config_env)

    assert config.knowledge.search_limit == 20
    assert config.knowledge.recent_limit == 20
    assert config.knowledge.list_chip_fanout_cap == 40


def test_config_reads_search_and_recent_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "lithos-lens.toml"
    config_path.write_text(
        "[lithos-lens]\n"
        'environment = "test"\n'
        "[lithos-lens.knowledge]\n"
        "search_limit = 5\n"
        "recent_limit = 7\n"
        "list_chip_fanout_cap = 9\n"
    )

    config = load_config(config_path)

    assert config.knowledge.search_limit == 5
    assert config.knowledge.recent_limit == 7
    assert config.knowledge.list_chip_fanout_cap == 9


@pytest.mark.parametrize(
    "key", ["search_limit", "recent_limit", "list_chip_fanout_cap"]
)
def test_config_rejects_oversized_landing_limit(tmp_path: Path, key: str) -> None:
    config_path = tmp_path / "lithos-lens.toml"
    config_path.write_text(
        "[lithos-lens]\n"
        'environment = "test"\n'
        "[lithos-lens.knowledge]\n"
        f"{key} = 100000\n"
    )

    with pytest.raises(ConfigError, match=key):
        load_config(config_path)


# ── landing metadata chips (type, status, scope, namespace, confidence) ─


def _record_reads(fake: FakeLithosClient) -> list[tuple[str, int | None]]:
    """Record every ``read_note`` the landing makes, in order."""
    reads: list[tuple[str, int | None]] = []
    original_read = fake.read_note

    async def record_read(
        knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        reads.append((knowledge_id, max_length))
        return await original_read(knowledge_id, max_length=max_length)

    fake.read_note = record_read  # type: ignore[method-assign]
    return reads


def _row_html(html: str, note_id: str) -> str:
    """The one ``<li>`` (card or row) whose link opens ``note_id``."""
    anchor = html.index(f'href="/note/{note_id}?next=')
    return html[html.rindex("<li", 0, anchor) : html.index("</li>", anchor)]


def _set_chip_cap(config_path: Path, cap: int) -> None:
    with config_path.open("a") as handle:
        handle.write(f"\n[lithos-lens.knowledge]\nlist_chip_fanout_cap = {cap}\n")


_QUARANTINED = NoteRecord(
    id="hypo",
    title="Ingest hypothesis",
    content="The ingest path drops late points.",
    metadata={
        "note_type": "hypothesis",
        "status": "quarantined",
        "namespace": "research",
        "access_scope": "shared",
        "confidence": 0.3,
        "updated_at": "2026-08-02T10:00:00+00:00",
    },
)
_SUMMARY = NoteRecord(
    id="summary",
    title="Ingest summary",
    content="The ingest path is healthy.",
    metadata={
        "note_type": "summary",
        "status": "active",
        "namespace": "influx",
        "access_scope": "task",
        "supersedes": "hypo",
        "updated_at": "2026-08-01T10:00:00+00:00",
    },
)


def test_search_card_carries_the_notes_standing_as_compact_chips(
    lithos_lens_config_env: Path,
) -> None:
    """A quarantined hypothesis and a shared summary must not look identical in
    a result list: each card carries the note page's chips, compact."""
    fake = FakeLithosClient(
        dataset=_dataset({"hypo": _QUARANTINED, "summary": _SUMMARY})
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=ingest")

    assert response.status_code == 200
    card = _row_html(response.text, "hypo")
    assert 'class="note-chips note-chips-compact"' in card
    # The red status chip — the class `.note-status-quarantined` colours.
    assert (
        '<span class="chip note-status note-status-quarantined">quarantined</span>'
        in card
    )
    assert '<span class="chip note-type">hypothesis</span>' in card
    assert '<span class="chip note-namespace">research</span>' in card
    assert '<span class="chip note-confidence">confidence 30%</span>' in card
    # `shared` is the quiet default: no scope chip for it.
    assert "note-scope" not in card
    other = _row_html(response.text, "summary")
    assert "note-status-active" in other
    assert '<span class="chip note-namespace">influx</span>' in other
    assert '<span class="chip note-scope">task</span>' in other
    # `supersedes` is a link to another note; it stays on the note page.
    assert "note-supersedes" not in other
    assert "Chips shown for the first" not in response.text


def test_recent_row_carries_compact_chips(lithos_lens_config_env: Path) -> None:
    fake = FakeLithosClient(dataset=_dataset({"hypo": _QUARANTINED}))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge")

    row = _row_html(response.text, "hypo")
    assert 'class="note-chips note-chips-compact"' in row
    assert "note-status-quarantined" in row
    assert '<span class="chip note-namespace">research</span>' in row


def test_note_page_keeps_its_full_chip_row(lithos_lens_config_env: Path) -> None:
    """The note page renders the same partial, NOT compact, and with the
    `supersedes` link the list rows leave out."""
    fake = FakeLithosClient(dataset=_dataset({"summary": _SUMMARY}))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/summary")

    assert '<div class="note-chips">' in response.text
    assert "note-chips-compact" not in response.text
    assert 'replaces: <a href="/note/hypo">hypo</a>' in response.text


def test_landing_reads_each_row_once_with_the_cheap_read(
    lithos_lens_config_env: Path,
) -> None:
    fake = FakeLithosClient(
        dataset=_dataset({"hypo": _QUARANTINED, "summary": _SUMMARY})
    )
    reads = _record_reads(fake)

    with _client(lithos_lens_config_env, fake) as client:
        client.get("/knowledge?q=ingest")

    assert sorted(reads) == [("hypo", 1), ("summary", 1)]


def test_an_id_shown_twice_is_read_once(lithos_lens_config_env: Path) -> None:
    """The per-request cache: rows sharing an id share one read, and both
    carry its chips."""
    fake = FakeLithosClient(dataset=_dataset({"hypo": _QUARANTINED}))
    reads = _record_reads(fake)

    async def twice(query: str, **kwargs: Any) -> list[SearchResult]:
        row = SearchResult(id="hypo", title="Ingest hypothesis")
        return [row, row]

    fake.search_notes = twice  # type: ignore[method-assign]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=ingest")

    assert reads == [("hypo", 1)]
    assert response.text.count("note-status-quarantined") == 2


def _dated_quarantined(*days: int) -> dict[str, NoteRecord]:
    """Quarantined notes ``n<day>``, inserted in the order given.

    Given newest-first, the search branch (the fake answers in insertion
    order) and the recent branch (newest-first) list the rows identically, so
    one set of row assertions holds for both.
    """
    return {
        f"n{day}": NoteRecord(
            id=f"n{day}",
            title=f"Note {day}",
            content="Body.",
            metadata={
                "status": "quarantined",
                "updated_at": f"2026-08-0{day}T10:00:00+00:00",
            },
        )
        for day in days
    }


# Both landing branches: separate template branches, each with its own
# chips-capped notice.
_LANDING_BRANCHES = pytest.mark.parametrize(
    "path", ["/knowledge?q=Body", "/knowledge"], ids=["search", "recent"]
)


@_LANDING_BRANCHES
def test_rows_past_the_chip_cap_are_chipless_and_the_page_says_so(
    lithos_lens_config_env: Path, path: str
) -> None:
    _set_chip_cap(lithos_lens_config_env, 2)
    fake = FakeLithosClient(dataset=_dataset(_dated_quarantined(4, 3, 2, 1)))
    reads = _record_reads(fake)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(path)

    # n4 and n3 are the first two rows, and the only two read.
    assert sorted(reads) == [("n3", 1), ("n4", 1)]
    assert "note-status-quarantined" in _row_html(response.text, "n4")
    assert "note-status-quarantined" in _row_html(response.text, "n3")
    assert "note-chips" not in _row_html(response.text, "n2")
    assert "note-chips" not in _row_html(response.text, "n1")
    assert "Chips shown for the first 2 notes." in response.text


@_LANDING_BRANCHES
def test_a_list_exactly_at_the_cap_says_nothing_about_it(
    lithos_lens_config_env: Path, path: str
) -> None:
    _set_chip_cap(lithos_lens_config_env, 2)
    fake = FakeLithosClient(dataset=_dataset(_dated_quarantined(2, 1)))
    reads = _record_reads(fake)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(path)

    assert sorted(reads) == [("n1", 1), ("n2", 1)]
    assert "note-status-quarantined" in _row_html(response.text, "n2")
    assert "note-status-quarantined" in _row_html(response.text, "n1")
    assert "Chips shown for the first" not in response.text


def _css_rule(css: str, selector: str) -> str:
    """The declarations of ``selector``'s one rule in lens.css."""
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match is not None, f"lens.css has no {selector} rule"
    return match.group(1)


def _rem(declarations: str, prop: str) -> float:
    match = re.search(rf"(?<![-\w]){prop}:\s*([\d.]+)rem;", declarations)
    assert match is not None, f"no {prop} in rem"
    return float(match.group(1))


def test_compact_chip_row_stylesheet_is_one_smaller_line_that_scrolls() -> None:
    """The compact variant's layout contract (§5.7), pinned on the rules.

    One line (no wrap), smaller type than the note page's chips, and a row too
    wide for its card SCROLLS: it once clipped with ``overflow: hidden``, so at
    320px the namespace and confidence chips were in the markup and nowhere on
    screen. The landing's two grids bound their track, or one chip row widens
    the page instead of scrolling. The browser-truth check — one line, smaller,
    every chip reachable, no page overflow at 320px — is in the Playwright
    screenshot suite (``compactChipRowsAreWhole``); this guards the rules.
    """
    css = (
        Path(__file__).parent.parent / "src" / "lithos_lens" / "static" / "lens.css"
    ).read_text()
    row = _css_rule(css, ".note-chips-compact")
    assert re.search(r"flex-wrap:\s*nowrap;", row)
    assert re.search(r"overflow-x:\s*auto;", row)
    assert not re.search(r"(?<![-\w])overflow:\s*hidden;", row)
    chip = _css_rule(css, ".note-chips-compact .chip")
    assert re.search(r"white-space:\s*nowrap;", chip)
    assert re.search(r"flex:\s*none;", chip)
    assert _rem(chip, "font-size") < _rem(_css_rule(css, ".chip"), "font-size")
    for grid in (".knowledge-cards", ".knowledge-landing"):
        assert re.search(
            r"grid-template-columns:\s*minmax\(0,\s*1fr\);", _css_rule(css, grid)
        ), grid


def test_a_failed_chip_read_leaves_only_that_row_chipless(
    lithos_lens_config_env: Path,
) -> None:
    fake = FakeLithosClient(
        dataset=_dataset({"hypo": _QUARANTINED, "summary": _SUMMARY})
    )
    original_read = fake.read_note

    async def flaky_read(
        knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        if knowledge_id == "summary":
            raise LithosToolError("upstream timeout", code="timeout")
        return await original_read(knowledge_id, max_length=max_length)

    fake.read_note = flaky_read  # type: ignore[method-assign]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=ingest")

    assert response.status_code == 200
    summary_card = _row_html(response.text, "summary")
    assert "Ingest summary" in summary_card
    assert "note-chips" not in summary_card
    assert "note-status-quarantined" in _row_html(response.text, "hypo")
    assert "currently unavailable" not in response.text


def test_load_list_chips_dedupes_caps_and_survives_failures() -> None:
    class Reader:
        def __init__(self) -> None:
            self.reads: list[str] = []

        async def read_note(
            self, knowledge_id: str, *, max_length: int | None = None
        ) -> NoteRecord | None:
            self.reads.append(knowledge_id)
            if knowledge_id == "boom":
                raise RuntimeError("down")
            if knowledge_id == "gone":
                return None
            return NoteRecord(
                id=knowledge_id, title="", content="", metadata={"status": "active"}
            )

    reader = Reader()
    chips = asyncio.run(
        load_list_chips(reader, ["a", "boom", "a", "gone", "", "b", "c"], cap=4)
    )

    assert reader.reads == ["a", "boom", "gone", "b"]
    assert chips.fanout == 4
    assert chips.capped_at == 4
    assert chips.for_note("a") is not None
    assert chips.for_note("b") is not None
    assert chips.for_note("boom") is None
    assert chips.for_note("gone") is None
    assert chips.for_note("c") is None


def test_load_list_chips_under_the_cap_reports_no_cap() -> None:
    class Reader:
        async def read_note(
            self, knowledge_id: str, *, max_length: int | None = None
        ) -> NoteRecord | None:
            return NoteRecord(id=knowledge_id, title="", content="")

    chips = asyncio.run(load_list_chips(Reader(), ["a", "b"], cap=2))

    assert chips.fanout == 2
    assert chips.capped_at == 0
    # Frontmatter with none of the standing fields renders no chip row.
    assert chips.for_note("a") is None
