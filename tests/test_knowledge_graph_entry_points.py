"""K2 slice 6 — entry points into `/knowledge/graph` (PRD D13).

The PRD's Entry points cases, through the real routes against the fake's demo
knowledge dataset (``fake_knowledge_dataset``): the note page's Open in graph
and per-edge links, the unresolved-contradiction banner read from the
edge-table snapshot alone, the landing's Browse the graph line with the
snapshot's unresolved count, and the search cards' links.

The demo table has two unresolved ``contradicts`` rows (plan ↔ rollback,
capacity ↔ legacy) and one resolved (legacy ↔ plan, ``superseded``), so a
note with only a resolved contradiction is composed from it.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.fake_dataset import demo_dataset
from lithos_lens.fake_knowledge_dataset import knowledge_edge_rows
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge import RelatedRef
from lithos_lens.knowledge_edges import EdgeTable, EdgeTableSnapshot, KnowledgeEdge
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app

PLAN = "note-influx-plan"
CAPACITY = "note-influx-capacity"
ROLLBACK = "note-influx-rollback"
LEGACY = "note-influx-legacy-ingest"

UNRESOLVED_PLAN_ROLLBACK = "edge_e1f4a8c27b90"
RESOLVED_LEGACY_PLAN = "edge_b6e0f27d4c18"
#: legacy → plan ``related_to`` at consolidation weight, under the 0.1 floor.
FAINT_LEGACY_PLAN = "edge_15d0c3e8f972"

_BANNER = re.compile(
    r'<section class="banner banner-warning" role="status" '
    r"data-contradiction-banner>(.*?)</section>",
    re.S,
)
_BANNER_LINE = re.compile(r'<p data-contradiction="([^"]+)">(.*?)</p>', re.S)


class _Unreadable(FakeLithosClient):
    async def edge_list(self, **filters: Any) -> tuple[KnowledgeEdge, ...]:
        raise RuntimeError("lithos down")


class _Offline(FakeLithosClient):
    async def health(self) -> Any:
        return "unreachable"


class _BrokenList(FakeLithosClient):
    async def recent_notes(self, **kwargs: Any) -> Any:
        raise RuntimeError("lithos_list failed")


class _ReadRecorder(FakeLithosClient):
    """Records every ``lithos_read`` — ``tool_calls`` does not (F9)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reads: list[tuple[str, int | None]] = []

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        self.reads.append((knowledge_id, max_length))
        return await super().read_note(knowledge_id, max_length=max_length)


class _Ticks:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _lens(client: TestClient) -> Any:
    return cast(Any, client.app).state.lens


def _edge_lists(fake: FakeLithosClient) -> list[tuple[str, dict[str, Any]]]:
    return [call for call in fake.tool_calls if call[0] == "lithos_edge_list"]


def _client(config_path: Path, fake: Any = None) -> TestClient:
    return TestClient(
        create_app(
            load_config(config_path),
            lithos_client_factory=lambda _: fake or FakeLithosClient(),
        )
    )


def _get(config_path: Path, path: str, fake: Any = None) -> str:
    with _client(config_path, fake) as client:
        response = client.get(path)
    assert response.status_code == 200
    return response.text


def _set_knowledge(config_path: Path, *lines: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    block = "\n".join(lines)
    config_path.write_text(
        text.replace("[lithos-lens.knowledge]", f"[lithos-lens.knowledge]\n{block}", 1)
        if "[lithos-lens.knowledge]" in text
        else f"{text}\n[lithos-lens.knowledge]\n{block}\n",
        encoding="utf-8",
    )


def _with_edges(*rows: dict[str, object]) -> FakeLithosClient:
    return FakeLithosClient(dataset=replace(demo_dataset(), knowledge_edges=rows))


def _plan_dataset(rows: list[dict[str, object]], *extra: RelatedRef) -> Any:
    """The demo with ``rows`` as its edge table and ``extra`` typed refs on
    the plan's related panel, after its own."""
    dataset = demo_dataset()
    plan = dataset.related_neighborhoods[PLAN]
    neighborhoods = {
        **dataset.related_neighborhoods,
        PLAN: replace(plan, edges=plan.edges + extra),
    }
    return replace(
        dataset, knowledge_edges=tuple(rows), related_neighborhoods=neighborhoods
    )


def _with_plan_edges(
    rows: list[dict[str, object]], *extra: RelatedRef, cls: type = FakeLithosClient
) -> Any:
    return cls(dataset=_plan_dataset(rows, *extra))


def _faint_ref(weight: float, edge_id: str = FAINT_LEGACY_PLAN) -> RelatedRef:
    """The plan's related-panel row for legacy → plan ``related_to``."""
    return RelatedRef(
        id=LEGACY,
        edge_type="related_to",
        weight=weight,
        direction="incoming",
        edge_id=edge_id,
    )


def _with_faint_weight(weight: float) -> list[dict[str, object]]:
    """The demo table with legacy → plan ``related_to`` at ``weight``."""
    return [
        {**row, "weight": weight} if row["edge_id"] == FAINT_LEGACY_PLAN else dict(row)
        for row in knowledge_edge_rows()
    ]


def _row(edge_id: str, **changes: object) -> dict[str, object]:
    row = next(dict(r) for r in knowledge_edge_rows() if r["edge_id"] == edge_id)
    return {**row, **changes}


def _row_links(html: str) -> dict[str, str]:
    """Each typed-edge row's "in graph" href (unescaped), by its edge id."""
    edges = html.split('id="related-edges"', 1)[1].split("</ul>", 1)[0]
    hrefs = re.findall(r'<a href="([^"]+)" data-edge-graph-link>in graph</a>', edges)
    return {
        re.search(r"edge=([^&]+)", href).group(1): href  # type: ignore[union-attr]
        for href in map(html_lib.unescape, hrefs)
    }


def _panel_opened(html: str, edge_id: str) -> bool:
    return f'data-kgraph-panel="edge" data-kgraph-panel-id="{edge_id}"' in html


def _demo_rows_except_contradictions(*keep: str) -> list[dict[str, object]]:
    """The demo table with only the named ``contradicts`` rows kept."""
    return [
        dict(row)
        for row in knowledge_edge_rows()
        if row["type"] != "contradicts" or row["edge_id"] in keep
    ]


def _banner_lines(html: str) -> list[tuple[str, str]]:
    """``(edge id, text)`` per banner line; [] when there is no banner."""
    banners = _BANNER.findall(html)
    if not banners:
        return []
    assert len(banners) == 1, "several contradictions share one banner"
    return [
        (edge_id, " ".join(re.sub(r"<[^>]+>", "", body).split()))
        for edge_id, body in _BANNER_LINE.findall(banners[0])
    ]


# ── the note page's links ──────────────────────────────────────────────


def test_the_note_page_opens_in_the_graph_from_the_heading_and_summary(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, f"/note/{PLAN}")

    link = f'<a href="/knowledge/graph?focus={PLAN}" data-open-in-graph>'
    heading = re.search(r"<h2>Related (.*?)</h2>", html, re.S)
    assert heading is not None
    assert f"{link}Open in graph</a>" in heading.group(1)
    summary = re.search(r"data-related-summary>(.*?)</p>", html, re.S)
    assert summary is not None
    # After the counts, as one more " · " item.
    line = summary.group(1)
    assert line.index("related-summary-counts") < line.index(f"· {link}open in graph")


def test_each_typed_edge_row_links_to_its_edge_in_the_graph(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, "/note/note-influx-capacity")

    edges = html.split('id="related-edges"', 1)[1].split("</ul>", 1)[0]
    links = re.findall(r'<a href="([^"]+)" data-edge-graph-link>in graph</a>', edges)
    focus = "/knowledge/graph?focus=note-influx-capacity&amp;edge="
    assert links == [
        f"{focus}edge_4c1e9a7b20d3",
        f"{focus}edge_9b2f61c0a4e8",
        f"{focus}edge_38c9d1f5e6a7",
    ]


def test_a_row_link_lands_on_the_edges_panel(lithos_lens_config_env: Path) -> None:
    html = _get(
        lithos_lens_config_env,
        "/knowledge/graph?focus=note-influx-capacity&edge=edge_9b2f61c0a4e8",
    )
    assert 'data-kgraph-panel="edge" data-kgraph-panel-id="edge_9b2f61c0a4e8"' in html


def test_a_faint_typed_rows_link_is_bare_and_opens_the_panel(
    lithos_lens_config_env: Path,
) -> None:
    """F7 / f-002: the focus hides a 0.03 row under the default 0.1 floor,
    but the page draws the edge ``edge=`` names whatever the filters say, so
    the row's link carries no ``min_weight=``."""
    fake = _with_plan_edges(list(knowledge_edge_rows()), _faint_ref(0.03))
    with _client(lithos_lens_config_env, fake) as client:
        href = _row_links(client.get(f"/note/{PLAN}").text)[FAINT_LEGACY_PLAN]
        assert href == f"/knowledge/graph?focus={PLAN}&edge={FAINT_LEGACY_PLAN}"
        assert _panel_opened(client.get(href).text, FAINT_LEGACY_PLAN)
        # Unselected, the same focus still hides it.
        plain = client.get(f"/knowledge/graph?focus={PLAN}").text
        assert FAINT_LEGACY_PLAN not in plain


def test_row_links_carry_no_weight_whatever_the_configured_floor(
    lithos_lens_config_env: Path,
) -> None:
    """At a 0.6 floor the capacity note's 0.5 ``related_to`` row's link is
    as bare as its 0.82 and 0.7 rows', and each opens its panel."""
    _set_knowledge(lithos_lens_config_env, "graph_min_weight_default = 0.6")
    with _client(lithos_lens_config_env) as client:
        links = _row_links(client.get(f"/note/{CAPACITY}").text)
        focus = f"/knowledge/graph?focus={CAPACITY}"
        assert links == {
            edge_id: f"{focus}&edge={edge_id}"
            for edge_id in (
                "edge_4c1e9a7b20d3",
                "edge_9b2f61c0a4e8",
                "edge_38c9d1f5e6a7",
            )
        }
        for edge_id, href in links.items():
            assert _panel_opened(client.get(href).text, edge_id), edge_id


@pytest.mark.parametrize(
    ("warm", "upstream"),
    [(0.03, 0.06), (0.06, 0.03), (0.03, 0.5), (0.5, 0.03), (0.5, -0.2), (-0.2, 0.5)],
    ids=[
        "reinforced",
        "decayed",
        "up-across-floor",
        "down-across-floor",
        "to-negative",
        "from-negative",
    ],  # fmt: skip
)
def test_an_entry_link_opens_its_panel_whatever_the_weight_did_since_warming(
    lithos_lens_config_env: Path, warm: float, upstream: float
) -> None:
    """f-001 / f-002: the snapshot is warmed at ``warm``; upstream then moves
    the edge to ``upstream`` (reinforcement, decay, across the 0.1 floor, to
    or from a negative weight) and the note page's ``lithos_related`` reports
    the new weight. The row's link opens the panel from the warm snapshot,
    and neither the note nor the graph page fetches the table again."""
    fake = _with_plan_edges(_with_faint_weight(warm), _faint_ref(warm))
    with _client(lithos_lens_config_env, fake) as client:
        client.get("/knowledge/graph")  # warms the snapshot
        table = _lens(client).edge_table
        fetches, edge_lists = table.fetches, _edge_lists(fake)
        fake.dataset = _plan_dataset(_with_faint_weight(upstream), _faint_ref(upstream))

        href = _row_links(client.get(f"/note/{PLAN}").text)[FAINT_LEGACY_PLAN]
        assert href == f"/knowledge/graph?focus={PLAN}&edge={FAINT_LEGACY_PLAN}"
        assert _panel_opened(client.get(href).text, FAINT_LEGACY_PLAN)
        assert (table.fetches, _edge_lists(fake)) == (fetches, edge_lists)


def test_an_edge_the_snapshot_lacks_renders_a_notice_and_fetches_nothing(
    lithos_lens_config_env: Path,
) -> None:
    """f-002: an edge created after the snapshot was taken is not refetched
    for: the panel host says it is not in the snapshot, as of when, and that
    the snapshot refreshes every TTL — page and fragment alike."""
    new_edge = "edge_created_since"
    fake = _with_plan_edges(list(knowledge_edge_rows()))
    with _client(lithos_lens_config_env, fake) as client:
        client.get("/knowledge/graph")  # warms the snapshot
        table = _lens(client).edge_table
        fetches, edge_lists = table.fetches, _edge_lists(fake)
        snapshot = table.current
        assert isinstance(snapshot, EdgeTableSnapshot)
        fake.dataset = _plan_dataset(
            [*knowledge_edge_rows(), {**_row(FAINT_LEGACY_PLAN), "edge_id": new_edge}],
            _faint_ref(0.5, new_edge),
        )

        href = _row_links(client.get(f"/note/{PLAN}").text)[new_edge]
        page = client.get(href).text
        fragment = client.get(
            href.replace("/knowledge/graph?", "/knowledge/graph/panel?")
        )
        assert (table.fetches, _edge_lists(fake)) == (fetches, edge_lists)

    as_of = snapshot.as_of.strftime("%Y-%m-%d %H:%M UTC")
    notice = (
        f"Edge {new_edge} is not in the current edge snapshot (as of {as_of}). "
        "The snapshot refreshes every 300 s, so a new edge appears within that "
        "window."
    )
    host = page.split("data-kgraph-panel-host", 1)[1].split("</div>", 1)[0]
    assert 'data-kgraph-panel="none"' in host
    assert notice in " ".join(re.sub(r"<[^>]+>", " ", host).split())
    assert notice in " ".join(re.sub(r"<[^>]+>", " ", fragment.text).split())
    assert not _panel_opened(page, new_edge)


# ── the unresolved-contradiction banner ────────────────────────────────


def test_an_unresolved_contradiction_gets_a_banner_naming_the_other_note(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, f"/note/{PLAN}")

    assert _banner_lines(html) == [
        (
            UNRESOLVED_PLAN_ROLLBACK,
            "This note is contradicted by Influx rollback route — view",
        )
    ]
    banner = _BANNER.findall(html)[0]
    assert f'<a href="/note/{ROLLBACK}"><em>Influx rollback route</em></a>' in banner
    assert (
        f'<a href="/knowledge/graph?focus={PLAN}&amp;edge={UNRESOLVED_PLAN_ROLLBACK}">'
        "view</a>"
    ) in banner
    # The resolved legacy ↔ plan row is not in it.
    assert RESOLVED_LEGACY_PLAN not in banner
    # In the note's article, straight after its header: before tags and body.
    article = html.split('<article class="detail-panel">', 1)[1]
    article = article.split("</article>", 1)[0]
    after_header = article.split("</header>", 1)[1]
    assert after_header.lstrip().startswith(
        '<section class="banner banner-warning" role="status" '
        "data-contradiction-banner>"
    )
    banner_at = after_header.index("data-contradiction-banner")
    assert banner_at < after_header.index('<div class="tag-list">')
    assert banner_at < after_header.index("markdown-body")


def test_the_banners_view_link_opens_the_edge_panel(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(
        lithos_lens_config_env,
        f"/knowledge/graph?focus={PLAN}&edge={UNRESOLVED_PLAN_ROLLBACK}",
    )
    assert (
        f'data-kgraph-panel="edge" data-kgraph-panel-id="{UNRESOLVED_PLAN_ROLLBACK}"'
    ) in html


def test_a_resolved_contradiction_shows_no_banner(lithos_lens_config_env: Path) -> None:
    fake = _with_edges(*_demo_rows_except_contradictions(RESOLVED_LEGACY_PLAN))

    html = _get(lithos_lens_config_env, f"/note/{PLAN}", fake)

    assert "data-contradiction-banner" not in html
    assert "Influx migration plan" in html


def test_several_contradictions_share_one_banner_one_line_each(
    lithos_lens_config_env: Path,
) -> None:
    """The legacy note is not on the plan's related panel, so the banner names
    it by id rather than reading its title."""
    rows = _demo_rows_except_contradictions(UNRESOLVED_PLAN_ROLLBACK)
    rows.append(
        {
            **next(
                dict(row)
                for row in knowledge_edge_rows()
                if row["edge_id"] == RESOLVED_LEGACY_PLAN
            ),
            "conflict_state": None,
        }
    )

    html = _get(lithos_lens_config_env, f"/note/{PLAN}", _with_edges(*rows))

    assert _banner_lines(html) == [
        (
            UNRESOLVED_PLAN_ROLLBACK,
            "This note is contradicted by Influx rollback route — view",
        ),
        (RESOLVED_LEGACY_PLAN, f"This note is contradicted by {LEGACY} — view"),
    ]
    assert f"edge={RESOLVED_LEGACY_PLAN}" in html


@pytest.mark.parametrize("weight", [0.05, -0.3, 0.9])
def test_a_contradictions_view_link_is_bare_and_keeps_it_drawn(
    lithos_lens_config_env: Path, weight: float
) -> None:
    """Faint, negative or strong, the banner's link names only the focus and
    the edge, and the graph draws that edge with its panel open."""
    rows = [
        {**row, "weight": weight} if row["edge_id"] == UNRESOLVED_PLAN_ROLLBACK else row
        for row in _demo_rows_except_contradictions(UNRESOLVED_PLAN_ROLLBACK)
    ]
    fake = _with_edges(*rows)

    with _client(lithos_lens_config_env, fake) as client:
        note = client.get(f"/note/{PLAN}").text
        fetches = _lens(client).edge_table.fetches
        url = f"/knowledge/graph?focus={PLAN}&edge={UNRESOLVED_PLAN_ROLLBACK}"
        assert f'<a href="{url.replace("&", "&amp;")}">view</a>' in note
        graph = client.get(url).text
        assert _lens(client).edge_table.fetches == fetches
    assert _panel_opened(graph, UNRESOLVED_PLAN_ROLLBACK)


def test_a_stale_snapshot_still_serves_the_banner(
    lithos_lens_config_env: Path,
) -> None:
    """D4: past the TTL a failed refetch answers the last snapshot marked
    stale, and the note page still names the contradiction from it."""
    ticks, fail = _Ticks(), [False]
    with _client(lithos_lens_config_env) as client:
        lens = _lens(client)
        upstream = lens.lithos_client

        async def fetch(
            edge_type: str | None, namespace: str | None
        ) -> tuple[KnowledgeEdge, ...]:
            if fail[0]:
                raise RuntimeError("lithos briefly unavailable")
            return await upstream.edge_list(type=edge_type, namespace=namespace)

        lens.edge_table = EdgeTable(fetch, ttl_s=300, ticks=ticks)
        assert "data-contradiction-banner" in client.get(f"/note/{PLAN}").text

        ticks.now += 301
        fail[0] = True
        html = client.get(f"/note/{PLAN}").text
        current = lens.edge_table.current
        assert isinstance(current, EdgeTableSnapshot) and current.stale
        assert lens.edge_table.fetches == 2

    assert _banner_lines(html) == [
        (
            UNRESOLVED_PLAN_ROLLBACK,
            "This note is contradicted by Influx rollback route — view",
        )
    ]
    assert (
        f'<a href="/knowledge/graph?focus={PLAN}&amp;edge={UNRESOLVED_PLAN_ROLLBACK}">'
        "view</a>"
    ) in html


def test_the_banner_reads_no_note_to_name_the_other_endpoint(
    lithos_lens_config_env: Path,
) -> None:
    """D5: the banner names B from what the related panel already resolved.
    With the title fan-out capped at 1, the plan's typed rollback row is read
    and a typed legacy row past the cap is not — and the banner, which lists
    both contradictions, adds no read: legacy is named by its id."""
    _set_knowledge(lithos_lens_config_env, "related_title_fanout_cap = 1")
    rows = _demo_rows_except_contradictions(UNRESOLVED_PLAN_ROLLBACK)
    rows.append(_row(RESOLVED_LEGACY_PLAN, conflict_state=None))
    fake = _with_plan_edges(
        rows,
        RelatedRef(
            id=LEGACY,
            edge_type="contradicts",
            weight=0.9,
            direction="incoming",
            edge_id=RESOLVED_LEGACY_PLAN,
        ),
        cls=_ReadRecorder,
    )

    html = _get(lithos_lens_config_env, f"/note/{PLAN}", fake)

    assert _banner_lines(html) == [
        (
            UNRESOLVED_PLAN_ROLLBACK,
            "This note is contradicted by Influx rollback route — view",
        ),
        (RESOLVED_LEGACY_PLAN, f"This note is contradicted by {LEGACY} — view"),
    ]
    # The page's own reads: the note, then the one capped title read.
    assert fake.reads == [(PLAN, None), (ROLLBACK, 1)]


@pytest.mark.parametrize(
    ("path", "fake_cls", "absent"),
    [
        ("/knowledge?q=influx", FakeLithosClient, "data-unresolved-contradictions"),
        ("/knowledge", _Offline, "data-unresolved-contradictions"),
        ("/knowledge", _BrokenList, "data-unresolved-contradictions"),
        (f"/note/{PLAN}", _Offline, "data-contradiction-banner"),
        ("/note/no-such-note", FakeLithosClient, "data-contradiction-banner"),
    ],
    ids=["search", "landing-offline", "landing-error", "note-offline", "not-found"],
)
def test_pages_that_show_no_graph_entry_read_no_snapshot(
    lithos_lens_config_env: Path, path: str, fake_cls: type, absent: str
) -> None:
    """D4/D6: only the browse landing and a loaded note read the snapshot;
    search, offline, error and not-found renders cost no edge-table fetch."""
    fake = fake_cls()
    with _client(lithos_lens_config_env, fake) as client:
        html = client.get(path).text
        assert _lens(client).edge_table.fetches == 0
    assert _edge_lists(fake) == []
    assert absent not in html


def test_no_banner_when_the_snapshot_is_unavailable(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, f"/note/{PLAN}", _Unreadable())

    assert "data-contradiction-banner" not in html
    assert "Influx migration plan" in html


def test_no_banner_when_the_snapshot_is_refused(lithos_lens_config_env: Path) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_edge_table_max_edges = 3")

    html = _get(lithos_lens_config_env, f"/note/{PLAN}")

    assert "data-contradiction-banner" not in html


def test_the_banner_costs_no_lithos_call_on_a_warm_snapshot(
    lithos_lens_config_env: Path,
) -> None:
    fake = FakeLithosClient()
    with _client(lithos_lens_config_env, fake) as client:
        client.get("/knowledge/graph")  # warms the snapshot
        table = client.app.state.lens.edge_table  # type: ignore[attr-defined]
        fetches = table.fetches
        edge_lists = [call for call in fake.tool_calls if call[0] == "lithos_edge_list"]

        html = client.get(f"/note/{PLAN}").text

        assert "data-contradiction-banner" in html
        assert table.fetches == fetches
        assert [
            call for call in fake.tool_calls if call[0] == "lithos_edge_list"
        ] == edge_lists


# ── the landing and search ─────────────────────────────────────────────


def test_the_landing_links_the_graph_with_the_unresolved_count(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, "/knowledge")

    line = re.search(r'<p class="knowledge-browse-links">(.*?)</p>', html, re.S)
    assert line is not None
    assert '<a href="/knowledge/tags">Browse tags</a>' in line.group(1)
    assert (
        '<a href="/knowledge/graph" data-browse-graph>Browse the graph</a>'
        in line.group(1)
    )
    # The demo table's two NULL-state contradicts rows.
    assert (
        '<a href="/knowledge/graph?type=contradicts" data-unresolved-contradictions>'
        "2 unresolved contradictions</a>"
    ) in line.group(1)


def test_the_landing_count_is_absent_when_the_snapshot_is_unavailable(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, "/knowledge", _Unreadable())

    assert "Browse the graph" in html
    assert "data-unresolved-contradictions" not in html


def test_each_search_card_links_into_the_graph(lithos_lens_config_env: Path) -> None:
    html = _get(lithos_lens_config_env, "/knowledge?q=influx")

    cards = re.findall(r'<li class="knowledge-card">(.*?)</li>', html, re.S)
    assert cards
    for card in cards:
        note_id = re.search(r'href="/note/([^"?]+)', card)
        assert note_id is not None
        assert (
            f'<a href="/knowledge/graph?focus={note_id.group(1)}" data-card-graph-link>'
            "in graph</a>"
        ) in card
    # Searching reads no snapshot: the count is the browse branch's.
    assert "data-unresolved-contradictions" not in html
