"""K1-S2 — wiki-link tokenizer + per-click resolver route."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
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


# ── resolver route integration ─────────────────────────────────────────


def _client(config_path: Path, fake: FakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def _dataset(notes: dict[str, NoteRecord]) -> FakeLithosDataset:
    return FakeLithosDataset(notes=notes)


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
    notes = {
        "target-note": NoteRecord(
            id="target-note", title="Target Note", content="Body."
        )
    }
    fake = FakeLithosClient(dataset=_dataset(notes))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/knowledge/resolve?target=target-note&from=src",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/note/target-note"


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
