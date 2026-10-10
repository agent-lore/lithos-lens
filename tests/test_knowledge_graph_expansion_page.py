"""K2 slice 8 — the expansion page (D16) through the app (TestClient).

The PRD's "Expansion page" cases, except full-page URL carrying (S10): an
expansion's edges are in both the text and the payload and agree; the scope
line names applied expansions with remove links and dependant previews; "Not
shown" lists unreached and refused requests; the node panel's Show its
neighbours (with its counts, or why not), Collapse, and neither on the focus;
Centre on this drops ``expand=``; shared-note selections survive a collapse
and removed targets leave no stale selection or pin; a layer-only note expands
without its own related read; and a later request uses the latest edges and
facts, with the actual upstream calls recorded on warm and expired caches.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.datastructures import QueryParams

from lithos_lens.config import load_config
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.graph_cache import graph_fanout_gate
from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.knowledge_edges import EdgeTable, KnowledgeEdge
from lithos_lens.knowledge_facts import NoteFactsCache
from lithos_lens.knowledge_graph_routes import (
    KnowledgeGraphParams,
    knowledge_graph_url,
    parse_knowledge_graph_params,
)
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app
from tests.test_knowledge_graph_expansion import LAYERS, ROWS, edge

ROUTE = "/knowledge/graph"
PANEL = "/knowledge/graph/panel"

_HOST = re.compile(
    r'<div id="kgraph-panel" class="kgraph-panel-host" data-kgraph-panel-host'
    r' data-kgraph-render="([^"]*)">'
    r"(.*?)</div>\s*<section class=\"panel kgraph-legend\"",
    re.S,
)


class _Graph(FakeLithosClient):
    """The expansion fixture's rows and layers (both changeable between
    requests), every graph read recorded; each note titled "Title <id>"
    unless ``titles`` says otherwise, and the ``missing`` ones not found."""

    def __init__(
        self,
        rows: tuple[KnowledgeEdge, ...] = ROWS,
        layers: RelatedNeighborhood = LAYERS,
        missing: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.rows: list[KnowledgeEdge] = list(rows)
        self.layers = layers
        self.missing = set(missing)
        self.titles: dict[str, str] = {}
        self.calls: list[str] = []

    async def edge_list(self, **filters: Any) -> tuple[KnowledgeEdge, ...]:
        self.calls.append("edge_list")
        return tuple(self.rows)

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        self.calls.append(f"related:{knowledge_id}")
        return self.layers

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        self.calls.append(f"read:{knowledge_id}")
        if knowledge_id in self.missing:
            return None
        title = self.titles.get(knowledge_id, f"Title {knowledge_id}")
        return NoteRecord(id=knowledge_id, title=title, content="")


def _client(config_path: Path, fake: _Graph | None = None) -> TestClient:
    graph = fake or _Graph()
    return TestClient(
        create_app(load_config(config_path), lithos_client_factory=lambda _: graph)
    )


def _set_knowledge(config_path: Path, *lines: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    block = "\n".join(lines)
    config_path.write_text(
        text.replace("[lithos-lens.knowledge]", f"[lithos-lens.knowledge]\n{block}", 1)
        if "[lithos-lens.knowledge]" in text
        else f"{text}\n[lithos-lens.knowledge]\n{block}\n",
        encoding="utf-8",
    )


def _get(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    return response.text


def _page(config_path: Path, path: str) -> str:
    with _client(config_path) as client:
        return _get(client, path)


def _first(pattern: str, html: str) -> str:
    match = re.search(pattern, html, re.S)
    assert match is not None, pattern
    return match.group(1)


def _payload(html: str) -> dict[str, Any]:
    return json.loads(_first(r"data-knowledge-graph-payload>(.*?)</script>", html))


def _plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split()).replace(" ;", ";")


def _host(html: str) -> str:
    match = _HOST.search(html)
    assert match is not None, "no panel host"
    return match.group(2).strip()


def _render_id(html: str) -> str:
    match = _HOST.search(html)
    assert match is not None, "no panel host"
    return match.group(1)


def _href(html: str, marker: str) -> str:
    """The href of the ``<a>`` carrying ``marker``."""
    tag = _first(rf"(<a [^>]*{marker}[^>]*>)", html)
    return _first(r'href="([^"]*)"', tag).replace("&amp;", "&")


def _hrefs(html: str) -> list[str]:
    return [
        h.replace("&amp;", "&") for h in re.findall(r'(?:href|hx-get)="([^"]*)"', html)
    ]


def _scope_line(html: str) -> str:
    return _first(r"(<section class=\"kgraph-scope\".*?</section>)", html)


def _not_shown(html: str, root: str) -> str:
    return _first(rf'(<li data-kgraph-expansion="{root}".*?</li>)', html)


def _url(query: str) -> str:
    return f"{ROUTE}?{query}"


# ── the parser and the URL builder ─────────────────────────────────────


def test_expand_repeats_in_order_without_duplicates_or_the_focus() -> None:
    params = parse_knowledge_graph_params(
        QueryParams("focus=F&expand=A&expand=B&expand=A&expand=F&expand=%20&expand=C")
    )

    assert params.expand == ("A", "B", "C")
    # A plain mapping carries one value per key, and still parses.
    assert parse_knowledge_graph_params({"focus": "F", "expand": "A"}).expand == ("A",)
    # Outside focus mode there is nothing to expand.
    scoped = parse_knowledge_graph_params(QueryParams("type=supports&expand=A"))
    assert scoped.expand == ()


def test_the_url_builder_writes_expansions_in_order_and_a_new_scope_drops_them() -> (
    None
):
    params = KnowledgeGraphParams(focus="F", depth=2, expand=("A", "C"))

    assert knowledge_graph_url(params) == f"{ROUTE}?focus=F&depth=2&expand=A&expand=C"
    # Depth and filter links keep the requests (later ones may go unreached).
    assert knowledge_graph_url(params, depth=1, min_weight=0.5) == (
        f"{ROUTE}?focus=F&depth=1&expand=A&expand=C&min_weight=0.5"
    )
    # Centre on this, or any new focus or scope, starts afresh.
    assert knowledge_graph_url(params, focus="C", selected="", edge="") == (
        f"{ROUTE}?focus=C&depth=2"
    )
    assert knowledge_graph_url(params, focus="", type="supports") == (
        f"{ROUTE}?type=supports"
    )


# ── the text and the payload ───────────────────────────────────────────


def test_an_expansions_edges_are_in_the_text_and_the_payload_and_agree(
    lithos_lens_config_env: Path,
) -> None:
    page = _page(lithos_lens_config_env, _url("focus=F&expand=A&expand=C"))
    payload = _payload(page)

    typed = {e["id"] for e in payload["edges"] if e["kind"] == "typed"}
    listed = set(re.findall(r'<li data-kgraph-edge="([^"]+)">', page))
    assert typed == listed == {"e-fa", "e-fb", "e-ac", "e-ad", "e-la", "e-ce"}
    entry = _first(r'<li data-kgraph-edge="e-ce">(.*?)</li>', page)
    assert _plain(entry) == "Title C → Title E (0.80)"
    assert [step["id"] for step in payload["expansions"]] == ["A", "C"]
    nodes = {n["id"]: n for n in payload["nodes"]}
    assert (nodes["E"]["via"], nodes["E"]["hop"]) == ("C", 3)
    for node_id in nodes:
        assert f'data-kgraph-node="{node_id}"' in page


def test_the_scope_line_names_applied_expansions_with_remove_links_and_previews(
    lithos_lens_config_env: Path,
) -> None:
    page = _page(lithos_lens_config_env, _url("focus=F&expand=A&expand=C&expand=B"))
    scope = _scope_line(page)

    assert "expanded:" in _plain(scope)
    for root in ("A", "C", "B"):
        assert f'data-kgraph-expanded="{root}"' in scope
    # Removing A removes C, which A first drew; B stays.
    assert _href(scope, 'data-kgraph-expansion-remove="A"') == _url("focus=F&expand=B")
    preview_a = _first(r'data-kgraph-expanded="A">(.*?)</span>\)', scope)
    assert "also removes 1 later expansion: Title C" in _plain(preview_a)
    assert _href(scope, 'data-kgraph-expansion-remove="C"') == _url(
        "focus=F&expand=A&expand=B"
    )
    preview_c = _first(r'(data-kgraph-expanded="C">.*?</span>)', scope)
    assert "also removes" not in preview_c


def test_not_shown_lists_unreached_and_refused_requests_each_removable(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_focus_max_nodes = 7")
    page = _page(
        lithos_lens_config_env,
        _url("focus=F&expand=NOWHERE&expand=A&expand=B&expand=C"),
    )

    unreached = _not_shown(page, "NOWHERE")
    assert "was not applied: it was not drawn when its turn came" in _plain(unreached)
    assert _href(unreached, "data-kgraph-expansion-remove") == _url(
        "focus=F&expand=A&expand=B&expand=C"
    )
    refused = _not_shown(page, "B")
    assert _plain(refused).startswith(
        "Expanding Title B would add 1 visible note; "
        "8 notes would count towards the 7 cap — remove"
    )
    assert _href(refused, "data-kgraph-expansion-remove") == _url(
        "focus=F&expand=NOWHERE&expand=A&expand=C"
    )
    assert "min_weight" not in refused  # no weight remedy for a refused step (S1)
    assert 'data-kgraph-expansion="A"' not in page  # applied: not under Not shown
    assert [s["state"] for s in _payload(page)["expansions"]] == [
        "unreached",
        "applied",
        "refused",
        "applied",
    ]


# ── the node panel ─────────────────────────────────────────────────────


def _action(page: str) -> str:
    return _first(r"(<p data-kgraph-expansion-action=.*?</p>)", _host(page))


def test_the_panel_offers_show_its_neighbours_as_a_plain_link_with_its_counts(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env) as client:
        page = _get(client, _url("focus=F&selected=B"))
        action = _action(page)
        assert 'data-kgraph-expansion-action="available"' in action
        assert _plain(action) == (
            "Show its neighbours — adds 2 notes and 3 edges; "
            "6 notes would count towards the 250 cap"
        )
        href = _href(action, "data-kgraph-expand")
        assert href == _url("focus=F&expand=B&selected=B")
        assert "hx-get" not in action

        expanded = _get(client, href)
        collapse = _action(expanded)
        assert 'data-kgraph-expansion-action="expanded"' in collapse
        assert _href(collapse, "data-kgraph-collapse") == _url("focus=F&selected=B")
        assert {"D", "G"} <= {n["id"] for n in _payload(expanded)["nodes"]}


def test_an_edge_only_expansion_says_it_adds_edges_between_drawn_notes(
    lithos_lens_config_env: Path,
) -> None:
    page = _page(lithos_lens_config_env, _url("focus=F&selected=L2"))

    assert _plain(_action(page)) == (
        "Show its neighbours — adds 1 edge between notes already drawn; "
        "4 notes would count towards the 250 cap"
    )


def test_the_panel_says_why_it_cannot_show_neighbours(
    lithos_lens_config_env: Path,
) -> None:
    complete = _page(lithos_lens_config_env, _url("focus=F&depth=2&selected=A"))
    assert _plain(_action(complete)) == "All its edges are drawn."

    _set_knowledge(lithos_lens_config_env, "graph_focus_max_nodes = 5")
    over = _page(lithos_lens_config_env, _url("focus=F&selected=B"))
    action = _action(over)
    assert 'data-kgraph-expansion-action="over_cap"' in action
    assert "data-kgraph-expand" not in action
    assert _plain(action) == (
        "Show its neighbours is not offered: it would add 2 visible notes and "
        "3 edges, and 6 notes would count towards the 5 cap"
    )


def test_the_focus_has_neither_action_and_centre_on_this_drops_expand(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env) as client:
        focus = _get(client, _url("focus=F&expand=A&selected=F"))
        assert "data-kgraph-expansion-action" not in _host(focus)

        page = _get(client, _url("focus=F&expand=A&selected=C"))
    assert _href(_host(page), "data-kgraph-centre") == _url("focus=C")


def test_collapse_previews_its_dependants_beside_the_link(
    lithos_lens_config_env: Path,
) -> None:
    page = _page(lithos_lens_config_env, _url("focus=F&expand=A&expand=C&selected=A"))
    action = _action(page)

    assert _href(action, "data-kgraph-collapse") == _url("focus=F&selected=A")
    assert _plain(action) == "Collapse — also removes 1 later expansion: Title C"


@pytest.mark.parametrize(
    ("selection", "after"),
    [
        # D was first drawn by A, but B still reaches it: the selection stays.
        ("selected=D", "focus=F&expand=B&selected=D"),
        # E was C's alone, and C goes with A: selection and pin are cleared.
        ("selected=E", "focus=F&expand=B"),
        ("selected=E&pin=e-ce", "focus=F&expand=B"),
        ("edge=e-ce", "focus=F&expand=B"),
        ("edge=e-bd", "focus=F&expand=B&edge=e-bd"),
    ],
)
def test_a_collapse_keeps_shared_selections_and_clears_removed_ones(
    lithos_lens_config_env: Path, selection: str, after: str
) -> None:
    with _client(lithos_lens_config_env) as client:
        page = _get(client, _url(f"focus=F&expand=A&expand=C&expand=B&{selection}"))
        href = _href(_scope_line(page), 'data-kgraph-expansion-remove="A"')
        assert href == _url(after)
        # What the link loads draws its selection, if it kept one.
        loaded = _get(client, href)
    if "selected=D" in after:
        assert 'data-kgraph-panel-id="D"' in _host(loaded)


def test_a_selection_or_pin_the_view_does_not_draw_leaves_no_link(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env) as client:
        # E is drawn only by C's expansion: on a page without it, no link
        # pins it, and no panel shows.
        stale = _get(client, _url("focus=F&selected=E"))
        assert _host(stale) == ""
        assert not [h for h in _hrefs(stale) if "selected=E" in h]

        page = _get(client, _url("focus=F&selected=A&pin=e-ce"))
        assert 'data-kgraph-panel-id="A"' in _host(page)
        assert not [h for h in _hrefs(page) if "pin=" in h]
        # A click on that page is answered from its own view, not redirected.
        response = client.get(f"{PANEL}?focus=F&selected=B&render={_render_id(page)}")
    assert "HX-Redirect" not in response.headers
    assert 'data-kgraph-panel-id="B"' in response.text


def test_panel_links_carry_the_expansions_and_a_render_is_found_only_with_them(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env) as client:
        page = _get(client, _url("focus=F&expand=A"))
        render = _render_id(page)
        edge_get = _first(r'hx-get="([^"]*edge=e-ac[^"]*)"', page)
        assert "expand=A" in edge_get and f"render={render}" in edge_get
        # A canvas click on C: the page's query, the selection, its render.
        fragment = client.get(f"{PANEL}?focus=F&expand=A&selected=C&render={render}")
        assert "HX-Redirect" not in fragment.headers
        assert 'data-kgraph-panel-id="C"' in fragment.text
        assert "data-kgraph-expansion-action" in fragment.text

        other = client.get(f"{PANEL}?focus=F&selected=A&render={render}")
    assert other.headers["HX-Redirect"] == _url("focus=F&selected=A")


# ── reads and freshness ────────────────────────────────────────────────


def test_a_layer_only_note_expands_without_its_own_related_read(
    lithos_lens_config_env: Path,
) -> None:
    graph = _Graph()
    with _client(lithos_lens_config_env, graph) as client:
        page = _get(client, _url("focus=F&expand=L&selected=L"))

    assert [c for c in graph.calls if c.startswith("related")] == ["related:F"]
    assert {"read:L", "read:M"} <= set(graph.calls)
    assert "read:L2" not in graph.calls
    nodes = {n["id"]: n for n in _payload(page)["nodes"]}
    assert (nodes["L"]["layer_only"], nodes["L"]["via"]) == (False, None)
    assert 'data-kgraph-expanded="L"' in _scope_line(page)
    # Its typed edges join the text, and it stays under its wiki-link heading.
    assert '<li data-kgraph-edge="e-lm">' in page
    assert re.search(r'data-kgraph-layer="links_to"><a [^>]*data-kgraph-node="L"', page)


class _Ticks:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _on_ticks(client: TestClient, graph: _Graph, ticks: _Ticks) -> None:
    """The app's edge table (TTL 300 s) and facts cache (TTL 600 s) on a test
    clock, reading through ``graph``."""
    lens = cast(Any, client.app).state.lens
    lens.edge_table = EdgeTable(
        lambda t, ns: graph.edge_list(type=t, namespace=ns), ttl_s=300, ticks=ticks
    )
    lens.note_facts = NoteFactsCache(
        lambda note_id: graph.read_note(note_id, max_length=1),
        graph_fanout_gate,
        ttl_s=600,
        ticks=ticks,
    )


def test_each_request_uses_the_latest_data_and_records_its_actual_calls(
    lithos_lens_config_env: Path,
) -> None:
    graph, ticks = _Graph(), _Ticks()
    with _client(lithos_lens_config_env, graph) as client:
        _on_ticks(client, graph, ticks)
        query = _url("focus=F&expand=A&selected=C")

        graph.calls.clear()
        _get(client, query)
        cold = list(graph.calls)
        assert cold[:2] == ["edge_list", "related:F"]
        assert {c for c in cold[2:]} == {f"read:{n}" for n in "FABLCD"}

        # Warm caches: the focus's related read remains; nothing else.
        graph.calls.clear()
        _get(client, query)
        assert graph.calls == ["related:F"]

        # The edges and a title change; both caches expire.
        graph.rows = [row for row in graph.rows if row.edge_id != "e-ad"]
        graph.rows.append(edge("e-an", "A", "N"))
        graph.titles["C"] = "Renamed C"
        ticks.now += 601
        graph.calls.clear()
        page = _get(client, query)
    assert graph.calls[:2] == ["edge_list", "related:F"]
    assert {c for c in graph.calls[2:]} == {f"read:{n}" for n in "FABLCN"}
    payload = _payload(page)
    ids = {n["id"] for n in payload["nodes"]}
    assert "N" in ids and "D" not in ids
    assert {e["id"] for e in payload["edges"]} >= {"e-an"}
    assert '<li data-kgraph-edge="e-an">' in page
    assert '<li data-kgraph-edge="e-ad">' not in page
    labels = {n["id"]: n["label"] for n in payload["nodes"]}
    assert labels["C"] == "Renamed C"
    # The text, the panel and the payload name C alike.
    assert _plain(_first(r'<li data-kgraph-edge="e-ac">(.*?)</li>', page)) == (
        "Title A → Renamed C (0.80)"
    )
    assert "Renamed C" in _plain(_host(page))
    assert [s["added_nodes"] for s in payload["expansions"]] == [2]


# ── telemetry ──────────────────────────────────────────────────────────


def _graph_span(spans: InMemorySpanExporter) -> dict[str, Any]:
    matching = [
        dict(span.attributes or {})
        for span in spans.get_finished_spans()
        if (span.attributes or {}).get("http.route") == ROUTE
    ]
    assert len(matching) == 1
    return matching[0]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("focus=F&expand=NOWHERE&expand=A&expand=B&expand=C", (4, 2, 1, 1)),
        ("focus=F", (0, 0, 0, 0)),
        ("", (0, 0, 0, 0)),
    ],
)
def test_the_request_span_counts_the_expansions(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    query: str,
    expected: tuple[int, int, int, int],
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_focus_max_nodes = 7")
    _page(lithos_lens_config_env, _url(query) if query else ROUTE)

    attributes = _graph_span(spans)
    prefix = "lens.knowledge.graph.expansions"
    assert (
        tuple(
            attributes[f"{prefix}.{key}"]
            for key in ("requested", "applied", "unreached", "refused")
        )
        == expected
    )


# ── round 2: review findings ───────────────────────────────────────────


def test_show_its_neighbours_moves_an_unreached_request_last_and_applies_it(
    lithos_lens_config_env: Path,
) -> None:
    """correctness f-001/f-004: C's request comes before A's, which draws C,
    so it goes unreached; C is drawn and can still be expanded."""
    with _client(lithos_lens_config_env) as client:
        page = _get(client, _url("focus=F&expand=C&expand=A&selected=C"))
        assert _plain(_not_shown(page, "C")).startswith(
            "Expanding Title C was not applied: it was not drawn when its turn "
            "came in the expansion order — remove"
        )
        href = _href(_action(page), "data-kgraph-expand")
        assert href == _url("focus=F&expand=A&expand=C&selected=C")

        expanded = _get(client, href)
    assert [s["state"] for s in _payload(expanded)["expansions"]] == [
        "applied",
        "applied",
    ]
    assert '<li data-kgraph-edge="e-ce">' in expanded
    assert 'data-kgraph-expansion-action="expanded"' in _action(expanded)


def test_a_stale_pin_left_in_the_address_bar_does_not_redirect_panel_clicks(
    lithos_lens_config_env: Path,
) -> None:
    """correctness f-002: the pinned edge goes from the data; the browser
    reloads the same URL, still pinning it, and the canvas builds its panel
    requests from that address bar."""
    graph, ticks = _Graph(), _Ticks()
    query = "focus=F&expand=A&expand=C&selected=A&pin=e-ce"
    with _client(lithos_lens_config_env, graph) as client:
        _on_ticks(client, graph, ticks)
        _get(client, _url(query))
        graph.rows = [row for row in graph.rows if row.edge_id != "e-ce"]
        ticks.now += 301
        page = _get(client, _url(query))
        assert not [h for h in _hrefs(page) if "pin=" in h]

        render = _render_id(page)
        clicks = [
            client.get(
                f"{PANEL}?{query.replace('selected=A', f'selected={n}')}"
                f"&render={render}"
            )
            for n in ("B", "D", "C")
        ]
    for response, note in zip(clicks, ("B", "D", "C"), strict=True):
        assert "HX-Redirect" not in response.headers
        assert f'data-kgraph-panel-id="{note}"' in response.text
        assert not [h for h in _hrefs(response.text) if "pin=" in h]


# Focus F, depth 1: requests A, C, E, B make a chain A → C → E → K, and B
# reaches C by another edge (an alternate branch to an expanded root).
CHAIN_ROWS = (
    edge("fa", "F", "A"),
    edge("fb", "F", "B"),
    edge("ac", "A", "C"),
    edge("ce", "C", "E"),
    edge("ek", "E", "K"),
    edge("bc", "B", "C"),
)
CHAIN = "focus=F&expand=A&expand=C&expand=E&expand=B"


def test_collapse_removes_a_transitive_chain_and_keeps_a_shared_root_drawn(
    lithos_lens_config_env: Path,
) -> None:
    """test-quality f-001: removing A removes C (drawn by A) and E (drawn by
    C), though B still reaches C; C stays drawn, E and K do not."""
    graph = _Graph(rows=CHAIN_ROWS, layers=RelatedNeighborhood())
    with _client(lithos_lens_config_env, graph) as client:
        page = _get(client, _url(f"{CHAIN}&selected=C"))
        scope = _scope_line(page)
        remove_a = _href(scope, 'data-kgraph-expansion-remove="A"')
        assert remove_a == _url("focus=F&expand=B&selected=C")
        preview = _first(r'(data-kgraph-expanded="A">.*?</span>\))', scope)
        assert "also removes 2 later expansions: Title C, Title E" in _plain(preview)
        assert _href(scope, 'data-kgraph-expansion-remove="E"') == _url(
            "focus=F&expand=A&expand=C&expand=B&selected=C"
        )
        # C's own Collapse takes E with it, and keeps C selected.
        action = _action(page)
        assert _href(action, "data-kgraph-collapse") == _url(
            "focus=F&expand=A&expand=B&selected=C"
        )
        assert _plain(action) == "Collapse — also removes 1 later expansion: Title E"

        rebuilt = _get(client, remove_a)
    payload = _payload(rebuilt)
    assert {n["id"] for n in payload["nodes"]} == {"F", "A", "B", "C"}
    assert {e["id"] for e in payload["edges"]} == {"fa", "fb", "bc"}
    assert [(s["id"], s["state"]) for s in payload["expansions"]] == [("B", "applied")]
    c = next(n for n in payload["nodes"] if n["id"] == "C")
    assert (c["via"], c["expanded"], c["expansion"]["state"]) == (
        "B",
        False,
        "available",
    )
    assert 'data-kgraph-panel-id="C"' in _host(rebuilt)


def test_each_request_reads_the_latest_layers_for_its_expansion_roots(
    lithos_lens_config_env: Path,
) -> None:
    """test-quality f-004: the focus's layers change between warm requests,
    adding and then removing a layer-only expansion root."""
    rows = (*ROWS, edge("e-l3q", "L3", "Q"))
    graph, ticks = _Graph(rows=rows), _Ticks()
    url = _url("focus=F&expand=L3&selected=L3")
    with _client(lithos_lens_config_env, graph) as client:
        _on_ticks(client, graph, ticks)
        before = _get(client, url)
        assert [s["state"] for s in _payload(before)["expansions"]] == ["unreached"]
        assert _host(before) == ""
        assert not [h for h in _hrefs(before) if "selected=L3" in h]

        graph.layers = RelatedNeighborhood(
            links=(*LAYERS.links, RelatedRef(id="L3", title="New link")),
            backlinks=LAYERS.backlinks,
        )
        graph.calls.clear()
        added = _get(client, url)
        # Warm snapshot and facts: the focus's layers, and the two notes the
        # promotion and the expansion made typed.
        assert graph.calls[0] == "related:F"
        assert sorted(graph.calls[1:]) == ["read:L3", "read:Q"]
        payload = _payload(added)
        assert [s["state"] for s in payload["expansions"]] == ["applied"]
        l3 = next(n for n in payload["nodes"] if n["id"] == "L3")
        assert (l3["layer_only"], l3["label"]) == (False, "Title L3")
        assert '<li data-kgraph-edge="e-l3q">' in added
        assert 'data-kgraph-expansion-action="expanded"' in _action(added)

        graph.layers = RelatedNeighborhood(backlinks=LAYERS.backlinks)
        gone = _get(client, url)
    assert [s["state"] for s in _payload(gone)["expansions"]] == ["unreached"]
    assert "L3" not in {n["id"] for n in _payload(gone)["nodes"]}
    assert '<li data-kgraph-edge="e-l3q">' not in gone
    assert _host(gone) == ""
    assert not [h for h in _hrefs(gone) if "selected=L3" in h]


def test_a_rendered_panels_preview_is_its_displayed_view_until_reloaded(
    lithos_lens_config_env: Path,
) -> None:
    """test-quality f-004: the data changes after an expanded render; that
    page's panel click still previews the view it shows, reading nothing, and
    the collapse link it offers assembles afresh."""
    graph, ticks = _Graph(), _Ticks()
    with _client(lithos_lens_config_env, graph) as client:
        _on_ticks(client, graph, ticks)
        query = "focus=F&expand=A&expand=C"
        page = _get(client, _url(query))
        graph.rows = [row for row in graph.rows if row.edge_id != "e-ac"]
        graph.titles["C"] = "Renamed C"
        ticks.now += 601

        graph.calls.clear()
        fragment = _get(client, f"{PANEL}?{query}&selected=A&render={_render_id(page)}")
        assert graph.calls == []
        action = _first(r"(<p data-kgraph-expansion-action=.*?</p>)", fragment)
        assert _plain(action) == "Collapse — also removes 1 later expansion: Title C"
        href = _href(action, "data-kgraph-collapse")
        assert href == _url("focus=F&selected=A")

        loaded = _get(client, href)
    assert graph.calls[:2] == ["edge_list", "related:F"]
    assert "C" not in {n["id"] for n in _payload(loaded)["nodes"]}


def test_a_ghost_can_show_its_neighbours_and_stays_drawn(
    lithos_lens_config_env: Path,
) -> None:
    """test-quality f-005: B's note is not found; its typed edges are in the
    snapshot, so it expands like any drawn note (D6)."""
    with _client(lithos_lens_config_env, _Graph(missing=("B",))) as client:
        page = _get(client, _url("focus=F&selected=B"))
        b = next(n for n in _payload(page)["nodes"] if n["id"] == "B")
        assert b["ghost"] and b["expansion"]["state"] == "available"
        assert (b["undrawn_nodes"], b["undrawn_edges"]) == (2, 3)
        action = _action(page)
        assert _plain(action).startswith(
            "Show its neighbours — adds 2 notes and 3 edges"
        )
        href = _href(action, "data-kgraph-expand")
        assert href == _url("focus=F&expand=B&selected=B")

        expanded = _get(client, href)
    nodes = {n["id"]: n for n in _payload(expanded)["nodes"]}
    assert (nodes["B"]["ghost"], nodes["B"]["expanded"]) == (True, True)
    assert [(nodes[n]["via"], nodes[n]["hop"]) for n in ("D", "G")] == [("B", 2)] * 2
    assert {"e-bd", "e-bg", "e-l2b"} <= set(
        re.findall(r'<li data-kgraph-edge="([^"]+)">', expanded)
    )
    assert "data-kgraph-collapse" in _action(expanded)


def test_expanded_counts_on_the_page_keep_base_depth_predictions(
    lithos_lens_config_env: Path,
) -> None:
    """test-quality f-003: the depth links state the base predictions; the
    provenance facet counts the final drawing's rows."""
    base = _page(lithos_lens_config_env, _url("focus=F"))
    page = _page(lithos_lens_config_env, _url("focus=F&expand=A"))

    line = _first(r'(<li data-would-be="2">.*?</li>)', page)
    assert _plain(line).startswith("depth 2 would draw 8 notes")
    assert _plain(_first(r'(<li data-would-be="2">.*?</li>)', base)) == _plain(line)
    assert _href(line, "depth=2") == _url("focus=F&depth=2&expand=A")
    assert _payload(page)["would_be_nodes"] == _payload(base)["would_be_nodes"]
    assert _payload(page)["provenance_facets"] == [
        {"group": "inferred", "count": 6, "shown": True}
    ]
    assert _payload(base)["provenance_facets"][0]["count"] == 2


@pytest.mark.parametrize("cap", [7, 6])
def test_an_undrawn_pin_does_not_change_the_offered_expansion(
    lithos_lens_config_env: Path, cap: int
) -> None:
    """correctness f-005 (round 3): e-aw is pinned but not drawn at depth 1.
    The panel's counts and offer are those of the link it emits, which
    drops that pin — and the link draws exactly that."""
    _set_knowledge(lithos_lens_config_env, f"graph_focus_max_nodes = {cap}")
    with _client(lithos_lens_config_env) as client:
        page = _get(client, _url("focus=F&selected=A&pin=e-aw"))
        action = _action(page)
        assert _plain(action) == (
            "Show its neighbours — adds 2 notes and 3 edges; "
            f"6 notes would count towards the {cap} cap"
        )
        href = _href(action, "data-kgraph-expand")
        assert href == _url("focus=F&expand=A&selected=A")

        expanded = _get(client, href)
    payload = _payload(expanded)
    assert payload["expansions"] == [
        {"id": "A", "state": "applied", "added_nodes": 2, "added_edges": 3}
    ]
    assert "W" not in {n["id"] for n in payload["nodes"]}
