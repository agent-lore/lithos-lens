"""K1-S2 — wiki-link tokenizer + per-click resolver route."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import LithosConfig, load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge import (
    RelatedNeighborhood,
    RelatedRef,
    ResolveOutcome,
    render_markdown,
    resolve_wiki_link,
    wiki_link_href,
)
from lithos_lens.lithos_client import LithosClient
from lithos_lens.tasks import NoteRecord, NoteSummary
from lithos_lens.web import create_app

A_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _run(coro):
    return asyncio.run(coro)


# ── tokenizer (pure, table-driven) ─────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        # [[target]] — display defaults to the target.
        (
            "See [[note-a]] here",
            '<a href="/knowledge/resolve?target=note-a&amp;from=src"'
            ' class="wiki-link">note-a</a>',
        ),
        # [[target|display]] — display text wins, path target is url-encoded.
        (
            "See [[folder/note|Nice Name]]",
            '<a href="/knowledge/resolve?target=folder%2Fnote&amp;from=src"'
            ' class="wiki-link">Nice Name</a>',
        ),
    ],
)
def test_wiki_links_render_as_resolver_anchors(body: str, needle: str) -> None:
    assert needle in render_markdown(body, from_id="src")


def test_multiple_wiki_links_in_one_paragraph_all_render() -> None:
    html = render_markdown("[[a]] then [[b]] then [[c]]", from_id="src")
    assert html.count('class="wiki-link"') == 3
    assert ">a</a>" in html and ">b</a>" in html and ">c</a>" in html


def test_wiki_link_inside_code_fence_stays_literal() -> None:
    html = render_markdown("```\n[[not-a-link]]\n```", from_id="src")
    assert "wiki-link" not in html
    assert "[[not-a-link]]" in html


def test_wiki_link_inside_inline_code_stays_literal() -> None:
    html = render_markdown("`[[literal]]` but [[real]] links", from_id="src")
    assert "<code>[[literal]]</code>" in html
    assert 'class="wiki-link">real</a>' in html


def test_wiki_link_display_text_is_escaped() -> None:
    # The display half is agent-authored; it must be HTML-escaped, not injected.
    html = render_markdown("[[t|<script>x</script>]]", from_id="src")
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_from_id_rides_along_in_the_resolver_href() -> None:
    html = render_markdown("[[a]]", from_id="note-42")
    assert "from=note-42" in html


def test_text_without_wiki_links_is_untouched() -> None:
    # A stray unmatched "[[" must not swallow surrounding text.
    html = render_markdown("prose with [[ dangling and a # Heading", from_id="src")
    assert "prose with [[ dangling and a # Heading" in html
    assert "wiki-link" not in html


def test_wiki_syntax_inside_markdown_link_label_stays_literal() -> None:
    """A wiki pattern inside an existing Markdown link label must NOT be
    rewritten: splicing an <a> inside an <a> emits invalid nested anchors whose
    repair is browser-dependent. Inside a link, the syntax stays literal."""
    html = render_markdown("[outer [[inner]]](https://example.com)", "src")

    assert html.count("<a ") == 1
    assert "https://example.com" in html
    assert "[[inner]]" in html
    assert "/knowledge/resolve" not in html


def test_wiki_shaped_text_inside_autolink_stays_literal() -> None:
    """Autolink labels are text children inside link_open/link_close too."""
    html = render_markdown("<https://example.com/[[x]]>", "src")

    assert html.count("<a ") == 1
    assert "/knowledge/resolve" not in html


def test_wiki_link_after_a_markdown_link_still_renders() -> None:
    """Leaving a link's label literal must not disarm splicing AFTER the link
    closes in the same inline run."""
    html = render_markdown("[label](https://example.com) then [[note-a]]", "src")

    assert html.count("<a ") == 2
    assert 'class="wiki-link"' in html
    assert "target=note-a" in html


def test_wiki_link_href_url_encodes_target_and_from() -> None:
    assert (
        wiki_link_href("a/b c", "n 1") == "/knowledge/resolve?target=a%2Fb+c&from=n+1"
    )


# ── resolver decision table (pure) ─────────────────────────────────────


class ResolverFake:
    """Controls each resolver branch: path probe, outgoing links, title search."""

    def __init__(
        self,
        *,
        path_hit: NoteRecord | None = None,
        links: tuple[RelatedRef, ...] = (),
        list_result: tuple[NoteSummary, ...] = (),
    ) -> None:
        self.path_hit = path_hit
        self.links = links
        self.list_result = list_result
        self.path_calls: list[str] = []
        self.list_calls: list[str | None] = []
        self.related_calls: list[str] = []

    async def read_note_by_path(self, path: str) -> NoteRecord | None:
        self.path_calls.append(path)
        return self.path_hit

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        self.related_calls.append(knowledge_id)
        return RelatedNeighborhood(links=self.links)

    async def list_notes(
        self,
        *,
        title_contains: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]:
        self.list_calls.append(title_contains)
        return list(self.list_result)


def test_resolver_uuid_target_redirects_without_probing() -> None:
    fake = ResolverFake()

    outcome = _run(resolve_wiki_link(fake, A_UUID, "src"))

    assert outcome == ResolveOutcome(kind="redirect", target=A_UUID, target_id=A_UUID)
    # A UUID is authoritative — no path probe or title search is spent.
    assert fake.path_calls == []
    assert fake.list_calls == []


def test_resolver_path_probe_hit_redirects() -> None:
    fake = ResolverFake(path_hit=NoteRecord(id="found-id", title="Found", content=""))

    outcome = _run(resolve_wiki_link(fake, "folder/note", "src"))

    assert outcome.kind == "redirect"
    assert outcome.target_id == "found-id"
    # The probe appends ".md" to cover the [[folder/note]] path convention.
    assert fake.path_calls == ["folder/note.md"]


def test_resolver_single_title_candidate_redirects() -> None:
    fake = ResolverFake(
        list_result=(NoteSummary(id="only", title="Only Match", path="p/only.md"),)
    )

    outcome = _run(resolve_wiki_link(fake, "Only Match", "src"))

    assert outcome.kind == "redirect"
    assert outcome.target_id == "only"


def test_resolver_confident_outgoing_link_redirects() -> None:
    # The source note already links to a note whose title equals the target.
    fake = ResolverFake(links=(RelatedRef(id="linked", title="Design Doc"),))

    outcome = _run(resolve_wiki_link(fake, "Design Doc", "src"))

    assert outcome.kind == "redirect"
    assert outcome.target_id == "linked"


def test_resolver_multiple_candidates_disambiguate() -> None:
    fake = ResolverFake(
        list_result=(
            NoteSummary(id="a", title="Plan A", path="p/a.md"),
            NoteSummary(id="b", title="Plan B", path="p/b.md"),
        )
    )

    outcome = _run(resolve_wiki_link(fake, "Plan", "src"))

    assert outcome.kind == "disambiguation"
    assert [c.id for c in outcome.candidates] == ["a", "b"]
    assert [c.path for c in outcome.candidates] == ["p/a.md", "p/b.md"]


def test_resolver_dedupes_link_and_list_candidate_by_id() -> None:
    # The same note surfaces via both the outgoing link and the title search;
    # it must collapse to one confident candidate, not two.
    fake = ResolverFake(
        links=(RelatedRef(id="dup", title="Shared"),),
        list_result=(NoteSummary(id="dup", title="Shared", path="p/dup.md"),),
    )

    outcome = _run(resolve_wiki_link(fake, "Shared", "src"))

    assert outcome.kind == "redirect"
    assert outcome.target_id == "dup"


def test_resolver_duplicated_candidate_keeps_the_list_provided_path() -> None:
    """§6.3 requires paths on the disambiguation page. The outgoing-link
    candidate arrives pathless first; when the title search returns the SAME
    note with its path, the entries merge — ranking preserved, missing fields
    filled — instead of the richer row being dropped."""
    fake = ResolverFake(
        links=(RelatedRef(id="dup", title="Shared"),),
        list_result=(
            NoteSummary(id="dup", title="Shared", path="p/dup.md"),
            NoteSummary(id="other", title="Shared Too", path="p/other.md"),
        ),
    )

    outcome = _run(resolve_wiki_link(fake, "Shared", "src"))

    assert outcome.kind == "disambiguation"
    # The outgoing-link candidate still ranks first…
    assert [c.id for c in outcome.candidates] == ["dup", "other"]
    # …but now carries the path the title search supplied.
    assert outcome.candidates[0].path == "p/dup.md"
    assert outcome.candidates[0].title == "Shared"


def test_resolver_zero_candidates_is_unresolved_with_search() -> None:
    fake = ResolverFake()

    outcome = _run(resolve_wiki_link(fake, "ghost/target", "src"))

    assert outcome.kind == "unresolved"
    assert outcome.search_query == "ghost/target"


def test_resolver_uses_last_path_component_for_title_search() -> None:
    fake = ResolverFake()

    _run(resolve_wiki_link(fake, "folder/sub/leaf", "src"))

    assert fake.list_calls == ["leaf"]


def test_resolver_empty_target_is_unresolved() -> None:
    fake = ResolverFake()

    outcome = _run(resolve_wiki_link(fake, "   ", "src"))

    assert outcome.kind == "unresolved"
    assert fake.path_calls == []


def test_resolver_survives_related_failure() -> None:
    class FailingRelated(ResolverFake):
        async def related(self, knowledge_id: str) -> RelatedNeighborhood:
            raise RuntimeError("related down")

    fake = FailingRelated(
        list_result=(NoteSummary(id="one", title="One", path="p/one.md"),)
    )

    outcome = _run(resolve_wiki_link(fake, "One", "src"))

    # A dead cross-check degrades to the title search, not a 500.
    assert outcome.kind == "redirect"
    assert outcome.target_id == "one"


# ── security: path-traversal probe guard (security/f-001) ──────────────


@pytest.mark.parametrize(
    "target",
    [
        "../../../secret/note",
        "/etc/passwd",
        "a/../../b",
        "notes\\..\\secret",
        "note\x00.md",
    ],
)
def test_resolver_never_path_probes_traversal_or_absolute_targets(
    target: str,
) -> None:
    # A traversal/absolute target must NOT be forwarded to read_note_by_path:
    # even though this fake would "find" a note, the probe is skipped so it
    # can't leak an id (or act as an existence oracle) via the redirect.
    fake = ResolverFake(
        path_hit=NoteRecord(id="leaked-secret", title="Secret", content="")
    )

    outcome = _run(resolve_wiki_link(fake, target, "src"))

    assert fake.path_calls == []
    assert outcome.kind == "unresolved"


def test_resolver_still_probes_ordinary_dotted_names() -> None:
    # The guard rejects ".." path *segments*, not any dot — a plain dotted name
    # must still be probed so real notes keep resolving.
    fake = ResolverFake(
        path_hit=NoteRecord(id="v1.2-notes", title="Release", content="")
    )

    outcome = _run(resolve_wiki_link(fake, "releases/v1.2-notes", "src"))

    assert fake.path_calls == ["releases/v1.2-notes.md"]
    assert outcome.kind == "redirect"


# ── security: linear wiki-link scan (security/f-002) ───────────────────


def test_bracket_run_renders_no_wiki_link_and_stays_fast() -> None:
    """Regression for the O(n²) backtracking DoS: a long run of ``[`` once
    presented a ``[[`` start at every offset, so the wiki-link pass backtracked
    quadratically (the finding measured ~8.5 s for 30 KB). Excluding ``[`` from
    the target class makes each start fail in O(1). Black-box through the public
    render so the guard is pinned where it's actually reached — a viewer request
    to render a note body — and asserts both that no anchor is produced and that
    the render doesn't blow up."""
    import time

    body = "[" * 30_000
    start = time.perf_counter()
    html = render_markdown(body, from_id="src")
    elapsed = time.perf_counter() - start

    assert "wiki-link" not in html
    assert elapsed < 2.0  # pre-fix: several seconds and rising with n²


# ── resolver route integration ─────────────────────────────────────────


def _client(config_path: Path, fake: FakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def _dataset(
    notes: dict[str, NoteRecord],
    note_paths: dict[str, str] | None = None,
) -> FakeLithosDataset:
    return FakeLithosDataset(notes=notes, note_paths=note_paths or {})


def test_resolve_route_uuid_redirects(lithos_lens_config_env: Path) -> None:
    fake = FakeLithosClient(dataset=_dataset({}))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            f"/knowledge/resolve?target={A_UUID}&from=src",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"] == f"/note/{A_UUID}"


def test_resolve_route_path_probe_redirects(lithos_lens_config_env: Path) -> None:
    """The probe maps a [[folder/note]] path to a DISTINCT document UUID via
    the dataset's explicit note_paths — not a path-stem==id identity, which
    would make this test tautological."""
    target_id = "33333333-3333-4333-8333-333333333333"
    notes = {target_id: NoteRecord(id=target_id, title="Target Note", content="Body.")}
    fake = FakeLithosClient(
        dataset=_dataset(notes, note_paths={"guides/target-note.md": target_id})
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/knowledge/resolve?target=guides/target-note&from=src",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"] == f"/note/{target_id}"


def test_resolve_route_disambiguation_lists_candidates(
    lithos_lens_config_env: Path,
) -> None:
    notes = {
        "shared-one": NoteRecord(id="shared-one", title="Shared design", content=""),
        "shared-two": NoteRecord(id="shared-two", title="Shared rollout", content=""),
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/knowledge/resolve?target=Shared&from=src",
            follow_redirects=False,
        )

    assert response.status_code == 200
    assert "Which" in response.text
    assert "Shared design" in response.text
    assert "Shared rollout" in response.text
    assert "/note/shared-one" in response.text
    assert "/note/shared-two" in response.text


def test_resolve_route_unresolved_offers_search(lithos_lens_config_env: Path) -> None:
    fake = FakeLithosClient(dataset=_dataset({}))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/knowledge/resolve?target=nothing-here&from=src",
            follow_redirects=False,
        )

    assert response.status_code == 200
    assert "Unresolved link" in response.text
    assert "/knowledge?q=nothing-here" in response.text


def test_resolve_route_offline_renders_unresolved(
    lithos_lens_config_env: Path,
) -> None:
    fake = FakeLithosClient(dataset=_dataset({}))

    async def _degraded() -> str:
        return "degraded"

    fake.health = _degraded  # type: ignore[assignment]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/knowledge/resolve?target=whatever&from=src",
            follow_redirects=False,
        )

    assert response.status_code == 200
    assert "Unresolved link" in response.text
    assert "offline or degraded" in response.text


def test_note_body_wiki_link_points_at_resolver(lithos_lens_config_env: Path) -> None:
    notes = {
        "root": NoteRecord(
            id="root",
            title="Root",
            content="Follow [[note-influx-rollback]] for the abort route.",
        )
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/root")

    assert response.status_code == 200
    assert 'href="/knowledge/resolve?target=note-influx-rollback&amp;from=root"' in (
        response.text
    )


# ── /knowledge landing (correctness/f-001: search link is not a dead end) ──


def test_unresolved_search_link_reaches_a_real_search_page(
    lithos_lens_config_env: Path,
) -> None:
    # The resolver's unresolved page advertises /knowledge?q=<target>; that
    # target must render a real search surface, not a 404 dead end.
    notes = {
        "influx-plan": NoteRecord(
            id="influx-plan", title="Influx migration plan", content=""
        )
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=Influx", follow_redirects=False)

    assert response.status_code == 200
    assert "Results for" in response.text
    assert "Influx migration plan" in response.text
    assert "/note/influx-plan" in response.text


def test_knowledge_landing_without_query_lists_recent(
    lithos_lens_config_env: Path,
) -> None:
    notes = {
        "a": NoteRecord(id="a", title="Note A", content=""),
        "b": NoteRecord(id="b", title="Note B", content=""),
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge")

    assert response.status_code == 200
    assert "Recently updated" in response.text
    assert "Note A" in response.text
    assert "Note B" in response.text


def test_knowledge_landing_empty_query_result(
    lithos_lens_config_env: Path,
) -> None:
    fake = FakeLithosClient(dataset=_dataset({}))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=no-such-note")

    assert response.status_code == 200
    assert "No matching notes." in response.text


def test_knowledge_query_search_is_capped(lithos_lens_config_env: Path) -> None:
    # Regression for security/f-003: a broad ?q= must not render an unbounded
    # result set — it is capped like the recent list and resolver candidates.
    notes = {
        f"n-{i}": NoteRecord(id=f"n-{i}", title=f"Match {i:02d}", content="")
        for i in range(25)
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?q=Match")

    assert response.status_code == 200
    assert response.text.count('href="/note/') == 20


def test_knowledge_tag_search_is_capped(lithos_lens_config_env: Path) -> None:
    notes = {
        f"n-{i}": NoteRecord(
            id=f"n-{i}", title=f"Note {i:02d}", content="", tags=("project:x",)
        )
        for i in range(25)
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/knowledge?tag=project:x")

    assert response.status_code == 200
    assert response.text.count('href="/note/') == 20


# ── concrete client (transport contract) ───────────────────────────────
#
# REAL_LITHOS_LIST_PAYLOAD mirrors the response built by the lithos
# ``lithos_list`` tool: rows under the "items" key (its one and only container
# key since the very first implementation) plus "total". Not an invented shape.

REAL_LITHOS_LIST_PAYLOAD: dict[str, Any] = {
    "items": [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "title": "Influx migration plan",
            "path": "plans/influx-migration.md",
            "updated": "2026-08-01T10:00:00+00:00",
            "tags": ["project:influx", "kind:plan"],
            "source_url": "",
            "derived_from_ids": [],
            "metadata": {},
        },
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "title": "Influx rollback route",
            "path": "runbooks/influx-rollback.md",
            "updated": "2026-08-02T10:00:00+00:00",
            "tags": ["project:influx", "kind:runbook"],
            "source_url": "",
            "derived_from_ids": [],
            "metadata": {},
        },
    ],
    "total": 2,
}


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


def test_client_list_notes_reads_the_real_items_envelope() -> None:
    client = _StubLithosClient({"lithos_list": REAL_LITHOS_LIST_PAYLOAD})

    rows = _run_client(client, client.list_notes())

    assert [row.id for row in rows] == [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    assert rows[0].title == "Influx migration plan"
    assert rows[0].path == "plans/influx-migration.md"
    assert rows[0].updated == "2026-08-01T10:00:00+00:00"
    assert rows[0].tags == ("project:influx", "kind:plan")
    assert client.calls == [("lithos_list", {})]


def test_client_list_notes_sends_filters_and_limit() -> None:
    client = _StubLithosClient({"lithos_list": {"items": [], "total": 0}})

    _run_client(
        client,
        client.list_notes(title_contains="rollback", tags=["kind:runbook"], limit=10),
    )

    assert client.calls == [
        (
            "lithos_list",
            {
                "title_contains": "rollback",
                "tags": ["kind:runbook"],
                "limit": 10,
            },
        )
    ]


def test_client_list_notes_does_not_honor_invented_alias_keys() -> None:
    """ "notes"/"documents"/"results" were never lithos_list container keys in
    any Lithos version ("results" belongs to lithos_search); honoring them is
    how the items-key bug hid. A payload using them yields nothing."""
    client = _StubLithosClient(
        {"lithos_list": {"notes": [{"id": "x"}], "documents": [{"id": "y"}]}}
    )

    rows = _run_client(client, client.list_notes())

    assert rows == []


def test_client_read_note_by_path_sends_probe_arguments() -> None:
    payload = {
        "id": "22222222-2222-4222-8222-222222222222",
        "title": "Influx rollback route",
        "content": "#",
        "metadata": {},
    }
    client = _StubLithosClient({"lithos_read": payload})

    note = _run_client(client, client.read_note_by_path("runbooks/influx-rollback.md"))

    # The path maps to a DISTINCT document UUID — the probe's whole point.
    assert note is not None
    assert note.id == "22222222-2222-4222-8222-222222222222"
    name, arguments = client.calls[0]
    assert name == "lithos_read"
    assert arguments == {
        "path": "runbooks/influx-rollback.md",
        "agent_id": client._config.agent_id,  # noqa: SLF001
        "max_length": 1,
    }


def test_client_read_note_by_path_maps_doc_not_found_to_none() -> None:
    client = _StubLithosClient(
        {
            "lithos_read": {
                "status": "error",
                "code": "doc_not_found",
                "message": "Document not found: nope.md",
            }
        }
    )

    assert _run_client(client, client.read_note_by_path("nope.md")) is None
