"""K2 slice 4 — the canvas's server half (``knowledge/graph_canvas.html``).

What the canvas needs from the page and cannot derive itself, driven through
the real route against the fake's demo knowledge dataset: the assets load only
for a drawn view (D1); the toolbar and canvas sit after the scope line and
before the panel host (D2); the ``colour=`` key is in the one URL grammar, so
every server-built link — the node panel's Centre on this included — keeps it
(D5); each payload edge carries its style (D6); the payload carries the
provenance facets (D9) and the unresolved-contradictions count the toolbar
states (D8). The browser half is ``tests/test_knowledge_graph_js.py``.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge_graph_routes import (
    KnowledgeGraphParams,
    knowledge_graph_url,
    parse_knowledge_graph_params,
)
from lithos_lens.web import create_app

PLAN = "note-influx-plan"
CAPACITY = "note-influx-capacity"
ROUTE = "/knowledge/graph"
_PAYLOAD = re.compile(
    r'<script type="application/json" data-knowledge-graph-payload>(.*?)</script>',
    re.S,
)
_ASSETS = ("vendor/cytoscape.min.js", "knowledge_graph.js")


class _Offline(FakeLithosClient):
    async def health(self) -> Any:
        return "unreachable"


def _client(config_path: Path, fake: Any = None) -> TestClient:
    return TestClient(
        create_app(
            load_config(config_path),
            lithos_client_factory=lambda _: fake or FakeLithosClient(),
        )
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


def _get(config_path: Path, path: str, fake: Any = None) -> str:
    with _client(config_path, fake) as client:
        response = client.get(path)
    assert response.status_code == 200
    return response.text


def _payload(page: str) -> dict[str, Any]:
    match = _PAYLOAD.search(page)
    assert match is not None, "no payload embedded"
    return json.loads(match.group(1))


def _has_assets(page: str) -> bool:
    found = [asset in page for asset in _ASSETS]
    assert len(set(found)) == 1, "Cytoscape and the canvas load together or not at all"
    return found[0]


# ── D1: the assets, only for a drawn view ──────────────────────────────


@pytest.mark.parametrize(
    "query", [f"?focus={PLAN}", "?type=contradicts", "?namespace=influx"]
)
def test_a_drawn_view_loads_cytoscape_the_canvas_and_its_toolbar(
    lithos_lens_config_env: Path, query: str
) -> None:
    page = _get(lithos_lens_config_env, f"{ROUTE}{query}")

    assert _has_assets(page)
    # Both hidden until the script has drawn: no empty box without it.
    assert re.search(r"<div [^>]*data-kgraph-toolbar hidden>", page)
    assert re.search(r"<div [^>]*data-kgraph-canvas [^>]*hidden>", page)


@pytest.mark.parametrize(
    ("query", "config", "fake"),
    [
        ("", (), None),  # the picker
        (f"?focus={PLAN}&depth=2", ("graph_focus_max_nodes = 4",), None),  # refused
        (f"?focus={PLAN}", (), _Offline()),
        ("?namespace=nobody-writes-here", (), None),  # an empty scope
    ],
    ids=["picker", "refusal", "offline", "empty"],
)
def test_nothing_to_draw_loads_no_canvas(
    lithos_lens_config_env: Path, query: str, config: tuple[str, ...], fake: Any
) -> None:
    if config:
        _set_knowledge(lithos_lens_config_env, *config)
    page = _get(lithos_lens_config_env, f"{ROUTE}{query}", fake)

    assert not _has_assets(page)
    assert "data-kgraph-canvas" not in page
    assert "data-kgraph-toolbar" not in page


# ── D2: placement ──────────────────────────────────────────────────────


def test_the_toolbar_and_canvas_sit_between_the_scope_line_and_the_panel_host(
    lithos_lens_config_env: Path,
) -> None:
    page = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&selected={CAPACITY}")

    order = [
        page.index(marker)
        for marker in (
            "data-kgraph-scope",
            "data-kgraph-toolbar",
            "data-kgraph-canvas",
            "data-kgraph-key",
            'id="kgraph-panel"',
            "data-kgraph-legend",
        )
    ]
    assert order == sorted(order)
    # The panel is still the server's, in its host, full width under the canvas.
    assert 'data-kgraph-panel="node"' in page


# ── D5: colour= in the one URL grammar ─────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("type", "type"),
        (" type ", "type"),
        ("namespace", "namespace"),
        ("", "namespace"),
        ("TYPE", "namespace"),
        ("status", "namespace"),
    ],
)
def test_colour_is_type_or_else_namespace(raw: str, expected: str) -> None:
    params = parse_knowledge_graph_params({"focus": PLAN, "colour": raw})

    assert params.colour == expected


def test_the_url_builder_writes_colour_only_when_it_is_type() -> None:
    by_type = KnowledgeGraphParams(focus=PLAN, colour="type")

    assert knowledge_graph_url(by_type) == f"{ROUTE}?focus={PLAN}&colour=type"
    assert (
        knowledge_graph_url(KnowledgeGraphParams(focus=PLAN)) == f"{ROUTE}?focus={PLAN}"
    )
    # A scope change keeps the operator's colour mode.
    assert knowledge_graph_url(by_type, focus=CAPACITY) == (
        f"{ROUTE}?focus={CAPACITY}&colour=type"
    )
    round_trip = parse_knowledge_graph_params(
        dict(
            pair.split("=")
            for pair in knowledge_graph_url(by_type).split("?")[1].split("&")
        )
    )
    assert round_trip == by_type


def test_a_node_panels_centre_on_this_keeps_the_colour_mode(
    lithos_lens_config_env: Path,
) -> None:
    """Brief S2: the canvas has no Centre on this of its own — the node panel
    a canvas click opens carries it, and at ``colour=type`` it keeps the mode,
    as does every panel link the page writes."""
    with _client(lithos_lens_config_env) as client:
        page = client.get(f"{ROUTE}?focus={PLAN}&colour=type&selected={CAPACITY}").text
        render = re.search(r'data-kgraph-render="([^"]+)"', page)
        assert render is not None
        fragment = client.get(
            f"{ROUTE}/panel?focus={PLAN}&colour=type&selected={CAPACITY}"
            f"&render={render.group(1)}"
        )

    for body in (page, fragment.text):
        centre = re.search(r'<a href="([^"]+)" data-kgraph-centre>', body)
        assert centre is not None
        assert html_lib.unescape(centre.group(1)) == (
            f"{ROUTE}?focus={CAPACITY}&colour=type"
        )
    assert "HX-Redirect" not in fragment.headers
    edge_links = re.findall(r'class="kgraph-edge-link" href="([^"]+)"', page)
    assert edge_links
    assert all("colour=type" in html_lib.unescape(href) for href in edge_links)
    assert _payload(page)["colour"] == "type"
    assert _payload(_get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}"))[
        "colour"
    ] == ("namespace")


# ── D6, D8, D9: what the payload carries for the canvas ────────────────


def test_each_edge_carries_its_style_and_a_layer_pair_its_layer_class(
    lithos_lens_config_env: Path,
) -> None:
    edges = {
        edge["id"]: edge
        for edge in _payload(
            _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&depth=2")
        )["edges"]
    }

    def style(edge_id: str) -> tuple[str, str, bool, str]:
        s = edges[edge_id]["style"]
        return (s["class"], s["stroke"], s["arrowhead"], s["label"])

    assert style("edge_4c1e9a7b20d3") == ("kedge-supports", "solid", True, "")
    assert style("edge_7d2c0e95b463") == ("kedge-derived-from", "dotted", True, "")
    assert style("edge_e1f4a8c27b90") == (
        "kedge-contradicts kedge-unresolved",
        "dashed",
        False,
        "",
    )
    assert style("edge_b6e0f27d4c18") == (
        "kedge-contradicts kedge-resolved",
        "dashed",
        False,
        "superseded",
    )
    assert style("edge_f29d84a6130c") == ("kedge-unknown", "solid", True, "assesses")
    assert style("edge_9b2f61c0a4e8") == ("kedge-related-to", "solid", False, "")
    assert style("edge_6f47e2d91b35") == ("kedge-analogy-to", "solid", False, "")
    layers = [edge for edge in edges.values() if edge["kind"] != "typed"]
    assert layers
    for edge in layers:
        expected = (
            "kedge-wiki-link" if edge["kind"] == "wiki_link" else "kedge-provenance"
        )
        assert edge["style"]["class"] == expected
        assert edge["style"]["arrowhead"] is True


def test_the_payload_carries_the_provenance_facets_and_the_unresolved_count(
    lithos_lens_config_env: Path,
) -> None:
    payload = _payload(
        _get(
            lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&depth=2&provenance=inferred"
        )
    )

    facets = {facet["group"]: facet for facet in payload["provenance_facets"]}
    assert list(facets) == ["inferred", "reinforced", "declared", "other"]
    assert facets["inferred"]["shown"] is True
    assert [facets[g]["shown"] for g in ("reinforced", "declared", "other")] == [
        False,
        False,
        False,
    ]
    assert all(facet["count"] > 0 for facet in facets.values())
    # Counted over what is drawn: of depth 2's two unresolved contradictions,
    # e1f4 (no provenance: "other") is filtered out and 38c9 (inferred) kept.
    assert payload["unresolved_contradictions"] == 1
    every = _payload(_get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&depth=2"))
    assert every["unresolved_contradictions"] == 2


def test_the_toolbar_states_the_unresolved_count_only_when_there_is_one(
    lithos_lens_config_env: Path,
) -> None:
    focus = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}")
    supports = _get(lithos_lens_config_env, f"{ROUTE}?type=supports")

    # Depth 1: e1f4 is unresolved, b6e0 is resolved (superseded).
    assert _payload(focus)["unresolved_contradictions"] == 1
    count = re.search(r"data-kgraph-unresolved-count>([^<]+)<", focus)
    assert count is not None and count.group(1) == "1 unresolved contradiction"
    assert _payload(supports)["unresolved_contradictions"] == 0
    assert "data-kgraph-unresolved-count" not in supports


def test_the_toolbar_offers_only_the_provenance_groups_present(
    lithos_lens_config_env: Path,
) -> None:
    page = _get(lithos_lens_config_env, f"{ROUTE}?type=derived_from")

    offered = re.findall(r'data-kgraph-provenance="([^"]+)"', page)
    assert offered == [facet["group"] for facet in _payload(page)["provenance_facets"]]
    assert offered == ["declared"]
    # Global mode has no depth control.
    assert "data-kgraph-depth" not in page
