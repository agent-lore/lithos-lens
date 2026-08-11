"""K1-S6 — /knowledge hybrid search + recently-updated landing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import ConfigError, LithosConfig, load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge import SearchResult, normalize_search_result
from lithos_lens.lithos_client import LithosClient
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
    assert 'href="/note/plan"' in response.text
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
    assert 'href="/note/a"' in response.text
    assert 'href="/note/b"' not in response.text


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
        note_id: response.text.index(f'href="/note/{note_id}"')
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
    assert 'href="/note/y"' not in response.text
    assert response.text.index('href="/note/new-x"') < response.text.index(
        'href="/note/old-x"'
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
    assert 'href="/note/a"' in response.text
    assert 'href="/note/b"' not in response.text
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


def test_config_reads_search_and_recent_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "lithos-lens.toml"
    config_path.write_text(
        "[lithos-lens]\n"
        'environment = "test"\n'
        "[lithos-lens.knowledge]\n"
        "search_limit = 5\n"
        "recent_limit = 7\n"
    )

    config = load_config(config_path)

    assert config.knowledge.search_limit == 5
    assert config.knowledge.recent_limit == 7


@pytest.mark.parametrize("key", ["search_limit", "recent_limit"])
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
