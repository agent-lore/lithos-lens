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

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.fake_dataset import demo_dataset
from lithos_lens.fake_knowledge_dataset import knowledge_edge_rows
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge_edges import KnowledgeEdge
from lithos_lens.knowledge_graph_routes import knowledge_graph_edge_url
from lithos_lens.web import create_app

PLAN = "note-influx-plan"
ROLLBACK = "note-influx-rollback"
LEGACY = "note-influx-legacy-ingest"

UNRESOLVED_PLAN_ROLLBACK = "edge_e1f4a8c27b90"
RESOLVED_LEGACY_PLAN = "edge_b6e0f27d4c18"

_BANNER = re.compile(
    r'<section class="banner banner-warning" role="status" '
    r"data-contradiction-banner>(.*?)</section>",
    re.S,
)
_BANNER_LINE = re.compile(r'<p data-contradiction="([^"]+)">(.*?)</p>', re.S)


class _Unreadable(FakeLithosClient):
    async def edge_list(self, **filters: Any) -> tuple[KnowledgeEdge, ...]:
        raise RuntimeError("lithos down")


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


def test_an_edge_link_below_the_weight_floor_carries_its_own_weight() -> None:
    """A focus hides edges under ``graph_min_weight_default``; the link to
    one names its weight so the page draws it and opens the panel."""
    assert (
        knowledge_graph_edge_url("n", "e", 0.03, floor=0.1)
        == "/knowledge/graph?focus=n&min_weight=0.03&edge=e"
    )
    assert knowledge_graph_edge_url("n", "e", 0.1, floor=0.1) == (
        "/knowledge/graph?focus=n&edge=e"
    )
    assert knowledge_graph_edge_url("n", "e", None, floor=0.1) == (
        "/knowledge/graph?focus=n&edge=e"
    )


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
    # Above the body, straight after the header.
    assert html.index("</header>") < html.index("data-contradiction-banner")
    assert html.index("data-contradiction-banner") < html.index("markdown-body")


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


def test_a_faint_contradictions_view_link_keeps_it_drawn(
    lithos_lens_config_env: Path,
) -> None:
    rows = [
        {**row, "weight": 0.05} if row["edge_id"] == UNRESOLVED_PLAN_ROLLBACK else row
        for row in _demo_rows_except_contradictions(UNRESOLVED_PLAN_ROLLBACK)
    ]
    fake = _with_edges(*rows)

    with _client(lithos_lens_config_env, fake) as client:
        note = client.get(f"/note/{PLAN}").text
        url = (
            f"/knowledge/graph?focus={PLAN}&min_weight=0.05"
            f"&edge={UNRESOLVED_PLAN_ROLLBACK}"
        )
        assert f'<a href="{url.replace("&", "&amp;")}">view</a>' in note
        graph = client.get(url).text
    assert (
        f'data-kgraph-panel="edge" data-kgraph-panel-id="{UNRESOLVED_PLAN_ROLLBACK}"'
    ) in graph


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
