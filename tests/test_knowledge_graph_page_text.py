"""K2 slice 3 — the `/knowledge/graph` text against its payload, and its lifecycles.

``test_knowledge_graph_page.py`` covers the page's cases one by one; this file
holds what the round-1 review found unprotected:

- the text **agrees** with the payload label for label, not only id for id:
  every node's visible label and link, every typed edge's ends, arrow and
  weight, every layer pair under the section K1 names it by, layer-only and
  pending nodes, an unknown weight, and the D12 section order;
- the query grammar's exactness (a link carries the threshold it was drawn
  with; a namespace is matched as stored) and the configured defaults;
- the facts-cap line, the stale-snapshot warning, the over-bound focus's
  ``as_of`` and the full facts tally on the span, each through a real
  cache lifecycle on the app's own ``AppState``.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lithos_lens.config import load_config
from lithos_lens.fake_knowledge_dataset import knowledge_edge_rows
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.knowledge_edges import EdgeTable, KnowledgeEdge
from lithos_lens.knowledge_graph_routes import (
    KnowledgeGraphParams,
    knowledge_graph_url,
    parse_knowledge_graph_params,
)
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app
from tests.conftest import metric_snapshot, snapshot_value

PLAN = "note-influx-plan"
CAPACITY = "note-influx-capacity"
ROLLBACK = "note-influx-rollback"
LEGACY = "note-influx-legacy-ingest"
ROUTE = "/knowledge/graph"

# Notes reached only by a wiki-link or provenance pair: no facts read.
LAYER_LINK = "note-layer-link"
LAYER_BACKLINK = "note-layer-backlink"
LAYER_SOURCE = "note-layer-source"

_PAYLOAD = re.compile(
    r'<script type="application/json" data-knowledge-graph-payload>(.*?)</script>',
    re.S,
)


# ── helpers ────────────────────────────────────────────────────────────


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


def _get(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    return response.text


def _payload(page: str) -> dict[str, Any]:
    match = _PAYLOAD.search(page)
    assert match is not None, "no payload embedded"
    return json.loads(match.group(1))


def _first(pattern: str, text: str) -> str:
    """Group 1 of ``pattern``'s first match in ``text``, which must match."""
    match = re.search(pattern, text, re.S)
    assert match is not None, pattern
    return match.group(1)


def _lens(client: TestClient) -> Any:
    """The app's ``AppState``."""
    return cast(Any, client.app).state.lens


def _plain(fragment: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _entry_text(page: str, edge_id: str) -> str:
    match = re.search(
        rf'<li data-kgraph-edge="{re.escape(edge_id)}">(.*?)</li>', page, re.S
    )
    assert match is not None, f"no text entry for {edge_id}"
    return _plain(match.group(1))


def _node_mentions(page: str, node_id: str) -> list[tuple[str, str]]:
    """Every (tag, label) the text names ``node_id`` with."""
    pattern = (
        rf'<(a|span)\b[^>]*data-kgraph-node="{re.escape(node_id)}"[^>]*>(.*?)</\1>'
    )
    return [(tag, html_lib.unescape(label)) for tag, label in re.findall(pattern, page)]


def _weight(edge: dict[str, Any]) -> str:
    weight = edge["weight"]
    return "weight unknown" if weight is None else f"{weight:.2f}"


def _shown(node: dict[str, Any]) -> str:
    """How a node reads in the text: its label, marked when a ghost or pending."""
    if node["ghost"]:
        return f"{node['label']} note not found"
    if node["facts_state"] == "pending":
        return f"{node['label']} facts pending"
    return node["label"]


def assert_text_agrees(page: str) -> dict[str, Any]:
    """The text names exactly what the payload carries, the way it carries it."""
    payload = _payload(page)
    nodes = {node["id"]: node for node in payload["nodes"]}
    focus = payload["focus"] if payload["mode"] == "focus" else None

    # Nodes: every one named, every mention with the payload's label; a ghost
    # never linked, every other node linked to its note, pending marked.
    for node_id, node in nodes.items():
        mentions = _node_mentions(page, node_id)
        assert mentions, f"{node_id} is not named in the text"
        for tag, label in mentions:
            assert label == node["label"], (node_id, label, node["label"])
            assert tag == ("span" if node["ghost"] else "a"), (node_id, tag)
        if not node["ghost"]:
            assert f'href="/note/{quote(node_id, safe="")}"' in page
        if node["facts_state"] == "pending":
            assert re.search(
                rf'data-kgraph-node="{re.escape(node_id)}"[^>]*>[^<]*</a> '
                r'<span class="kgraph-mark">facts pending</span>',
                page,
            )
    named = set(re.findall(r'data-kgraph-node="([^"]+)"', page))
    assert named == set(nodes)

    # Typed edges: listed once each, ends, arrow and weight as the payload says.
    typed = [edge for edge in payload["edges"] if edge["kind"] == "typed"]
    listed = re.findall(r'data-kgraph-edge="([^"]+)"', page)
    assert sorted(listed) == sorted(edge["id"] for edge in typed)
    for edge in typed:
        text = _entry_text(page, edge["id"])
        symmetric = edge["direction"] == "symmetric"
        source, target = _shown(nodes[edge["from"]]), _shown(nodes[edge["to"]])
        if payload["mode"] == "global" and edge["type"] == "contradicts":
            expected = f"{source} contradicts {target} · "
        elif focus is not None and edge["from"] == focus:
            expected = f"{'↔' if symmetric else '→'} {target} ({_weight(edge)})"
        elif focus is not None and edge["to"] == focus:
            expected = f"{'↔' if symmetric else '←'} {source} ({_weight(edge)})"
        else:
            expected = (
                f"{source} {'↔' if symmetric else '→'} {target} ({_weight(edge)})"
            )
        assert text.startswith(expected), (edge["id"], text, expected)
        if payload["mode"] == "global" and edge["type"] == "contradicts":
            assert f" · {_weight(edge)} · " in text
        assert re.search(rf'href="[^"]*edge={re.escape(edge["id"])}"', page)

    # Layer pairs: under the section K1 names them by, from the focus.
    relation = {
        ("wiki_link", True): "links_to",
        ("wiki_link", False): "linked_from",
        ("provenance", True): "source",
        ("provenance", False): "derived",
    }
    for edge in payload["edges"]:
        if edge["kind"] == "typed":
            continue
        outgoing = edge["from"] == focus
        other = edge["to"] if outgoing else edge["from"]
        assert re.search(
            rf'<li data-kgraph-layer="{relation[(edge["kind"], outgoing)]}">'
            rf'[^\n]*data-kgraph-node="{re.escape(other)}"',
            page,
        ), (edge["id"], relation[(edge["kind"], outgoing)])

    # D12 order: scope, legend, focus note, edges, layers, hidden, payload.
    marks = [
        "data-kgraph-scope",
        "data-kgraph-legend",
        "data-kgraph-focus",
        "data-kgraph-edges",
        "data-kgraph-layers",
        "data-kgraph-hidden",
        "data-knowledge-graph-payload",
    ]
    if focus is None:
        marks = [
            m for m in marks if m not in ("data-kgraph-focus", "data-kgraph-layers")
        ]
    positions = [page.index(mark) for mark in marks]
    assert positions == sorted(positions), marks
    return payload


def _layered_fake() -> FakeLithosClient:
    """The demo dataset plus all four layer groups around the plan (layer-only
    notes among them, a provenance pair already drawn as ``derived_from``) and
    a typed edge with no known weight."""
    fake = FakeLithosClient()
    unknown_weight = {
        **dict(knowledge_edge_rows()[0]),
        "edge_id": "edge_unknown_weight",
        "from_id": PLAN,
        "to_id": LEGACY,
        "type": "supports",
        "weight": None,
        "evidence": None,
    }
    neighbourhoods = dict(fake.dataset.related_neighborhoods)
    neighbourhoods[PLAN] = RelatedNeighborhood(
        links=(
            RelatedRef(id=ROLLBACK, title="Influx rollback route"),
            RelatedRef(id=LAYER_LINK, title="Linked & <odd> note"),
        ),
        backlinks=(RelatedRef(id=LAYER_BACKLINK, title="A note linking here"),),
        sources=(RelatedRef(id=LAYER_SOURCE, title="Where the plan came from"),),
        derived=(RelatedRef(id=ROLLBACK, title="Influx rollback route"),),
    )
    fake.dataset = replace(
        fake.dataset,
        knowledge_edges=(*fake.dataset.knowledge_edges, unknown_weight),
        related_neighborhoods=neighbourhoods,
    )
    return fake


# ── agreement ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        f"{ROUTE}?focus={PLAN}",
        f"{ROUTE}?focus={PLAN}&depth=2&min_weight=0",
        f"{ROUTE}?focus={CAPACITY}&depth=2",
        f"{ROUTE}?namespace=influx",
        f"{ROUTE}?namespace=influx&min_weight=0",
        f"{ROUTE}?type=contradicts",
    ],
)
def test_the_text_agrees_with_the_payload_label_for_label(
    lithos_lens_config_env: Path, path: str
) -> None:
    with _client(lithos_lens_config_env, _layered_fake()) as client:
        assert_text_agrees(_get(client, path))


def test_layers_layer_only_nodes_and_an_unknown_weight_agree(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, _layered_fake()) as client:
        page = _get(client, f"{ROUTE}?focus={PLAN}")
    payload = assert_text_agrees(page)

    layer_only = {n["id"] for n in payload["nodes"] if n["layer_only"]}
    assert layer_only == {LAYER_LINK, LAYER_BACKLINK, LAYER_SOURCE}
    assert {e["kind"] for e in payload["edges"]} == {
        "typed",
        "wiki_link",
        "provenance",
    }
    assert _entry_text(page, "edge_unknown_weight") == (
        "→ Legacy ingest approach (weight unknown)"
    )
    # The derived pair is drawn as the typed derived_from row: listed, marked.
    assert re.search(
        r'<li data-kgraph-layer="derived">.*?also a derived_from edge</span></li>', page
    )
    # The title is escaped wherever it appears in the text.
    assert "Linked &amp; &lt;odd&gt; note" in page


def test_unread_and_pending_nodes_read_the_same_everywhere(
    lithos_lens_config_env: Path,
) -> None:
    """f-001 and f-005: with the facts cap at 1, a layer entry names the
    node by the view's label (its id while unread), not the related read's
    inline title; and the cap line counts only the id-labelled notes."""
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    with _client(lithos_lens_config_env, _layered_fake()) as client:
        first = _get(client, f"{ROUTE}?focus={PLAN}")
        payload = assert_text_agrees(first)
        rollback = next(n for n in payload["nodes"] if n["id"] == ROLLBACK)
        assert (rollback["facts_state"], rollback["label"]) == ("unread", ROLLBACK)
        assert f'data-kgraph-node="{ROLLBACK}">{ROLLBACK}</a>' in first
        assert ">Influx rollback route<" not in first
        assert _plain(_first(r"<li data-facts-capped>(.*?)</li>", first)) == (
            "3 notes labelled by id (facts cap 1)"
        )

        # The second render reads the next node; then both cached notes change,
        # so the third has two re-reads due and room for one: the focus.
        second = _payload(_get(client, f"{ROUTE}?focus={PLAN}"))
        (second_read,) = (
            n["id"]
            for n in second["nodes"]
            if n["facts_state"] == "ok" and n["id"] != PLAN
        )
        facts = _lens(client).note_facts
        assert facts.apply_note_event("note.updated", {"id": PLAN, "title": "Plan v2"})
        assert facts.apply_note_event(
            "note.updated", {"id": second_read, "title": "Renamed neighbour"}
        )
        third = _get(client, f"{ROUTE}?focus={PLAN}")

    payload = assert_text_agrees(third)
    states = {
        n["id"]: n["facts_state"] for n in payload["nodes"] if not n["layer_only"]
    }
    assert states[PLAN] == "ok" and states[second_read] == "pending"
    assert sorted(s for s in states.values() if s == "unread") == ["unread"] * 2
    assert (payload["facts"]["capped"], payload["facts"]["capped_unread"]) == (3, 2)
    assert _plain(_first(r"<li data-facts-capped>(.*?)</li>", third)) == (
        "2 notes labelled by id (facts cap 1)"
    )
    assert _plain(_first(r"<li data-facts-pending>(.*?)</li>", third)) == (
        "1 note shown with last-known facts, re-read pending (facts cap 1)"
    )
    assert 'Renamed neighbour</a> <span class="kgraph-mark">facts pending' in third


def test_a_typed_edge_spelled_like_a_layer_is_listed_once(
    lithos_lens_config_env: Path,
) -> None:
    """f-004: a stored type ``wiki_link`` is a typed edge like any other."""
    fake = FakeLithosClient()
    first, *rest = fake.dataset.knowledge_edges
    fake.dataset = replace(
        fake.dataset, knowledge_edges=({**first, "type": "wiki_link"}, *rest)
    )
    with _client(lithos_lens_config_env, fake) as client:
        page = _get(client, f"{ROUTE}?focus={PLAN}")

    assert_text_agrees(page)
    assert re.findall(r'data-kgraph-edge="([^"]+)"', page).count(first["edge_id"]) == 1
    assert page.count('data-kgraph-type="wiki_link"') == 1
    assert "wiki_link" in re.findall(r'data-legend-type="([^"]+)"', page)
    assert re.findall(r'data-legend-layer="([^"]+)"', page) == ["wiki_link"]


# ── the grammar's exactness and the configured defaults ───────────────


def _drawn(page: str) -> set[str]:
    return {e["id"] for e in _payload(page)["edges"] if e["kind"] == "typed"}


def test_an_edge_link_keeps_the_threshold_it_was_drawn_with(
    lithos_lens_config_env: Path,
) -> None:
    """f-002: 0.7000001 hides the 0.70 contradiction; its links must too."""
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?namespace=influx&min_weight=0.7000001")
        drawn = _drawn(page)
        assert "edge_38c9d1f5e6a7" not in drawn  # weight 0.7
        link = html_lib.unescape(
            _first(r'class="kgraph-edge-link" href="([^"]+)"', page)
        )
        assert "min_weight=0.7000001" in link
        assert _drawn(_get(client, link)) == drawn
    params = KnowledgeGraphParams(namespace="influx", min_weight=0.7000001)
    query = knowledge_graph_url(params).split("?", 1)[1]
    assert (
        parse_knowledge_graph_params(dict(pair.split("=") for pair in query.split("&")))
        == params
    )


def test_type_and_namespace_are_matched_as_stored_and_ids_carried_unchanged(
    lithos_lens_config_env: Path,
) -> None:
    """f-003: a namespace with surrounding spaces is a namespace; the picker's
    own facet link to it must draw it."""
    fake = FakeLithosClient()
    padded = {
        **dict(knowledge_edge_rows()[0]),
        "edge_id": "edge_pad",
        "namespace": " influx ",
    }
    fake.dataset = replace(
        fake.dataset, knowledge_edges=(*fake.dataset.knowledge_edges, padded)
    )
    with _client(lithos_lens_config_env, fake) as client:
        picker = _get(client, ROUTE)
        link = html_lib.unescape(
            _first(r'href="([^"]+)">\s*influx\s*</a></td><td>1</td>', picker)
        )
        assert link == f"{ROUTE}?namespace=+influx+"
        page = _get(client, link)
    assert _drawn(page) == {"edge_pad"}
    assert _payload(page)["scope"]["namespace"] == " influx "

    params = parse_knowledge_graph_params(
        {"type": " supports", "edge": " e ", "selected": "n ", "focus": ""}
    )
    assert (params.type, params.edge, params.selected) == (" supports", " e ", "n ")
    blank = parse_knowledge_graph_params({"namespace": "  ", "depth": " 2 "})
    assert (blank.mode, blank.depth) == ("picker", 2)


def test_the_configured_depth_and_weight_apply_when_the_query_names_none(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(
        lithos_lens_config_env,
        "graph_default_depth = 2",
        "graph_min_weight_default = 0.75",
    )
    influx = [
        (str(r["edge_id"]), float(cast(float, r["weight"])))
        for r in knowledge_edge_rows()
        if r["namespace"] == "influx"
    ]
    with _client(lithos_lens_config_env) as client:
        for query in ("", "&depth=7&min_weight=heavy"):
            focus = _get(client, f"{ROUTE}?focus={PLAN}{query}")
            payload = _payload(focus)
            assert (payload["depth"], payload["filters"]["min_weight"]) == (2, 0.75)
            assert "· depth 2" in _plain(focus) and "min weight 0.75" in _plain(focus)
            assert all(
                e["weight"] >= 0.75 for e in payload["edges"] if e["kind"] == "typed"
            )
        explicit = _payload(
            _get(client, f"{ROUTE}?focus={PLAN}&depth=1&min_weight=0.2")
        )
        assert (explicit["depth"], explicit["filters"]["min_weight"]) == (1, 0.2)

        scoped = _get(client, f"{ROUTE}?namespace=influx")
        assert _drawn(scoped) == {
            edge_id for edge_id, weight in influx if weight >= 0.75
        }
        unfiltered = _get(client, f"{ROUTE}?namespace=influx&min_weight=0")
        assert _drawn(unfiltered) == {edge_id for edge_id, _ in influx}


# ── the picker's disclosure ───────────────────────────────────────────


def test_namespaces_past_the_top_twenty_are_behind_the_disclosure(
    lithos_lens_config_env: Path,
) -> None:
    template = dict(knowledge_edge_rows()[0])
    rows = []
    # ns-00 has 30 rows, ns-01 29 … ; ns-19 and ns-20 tie at 11, so the tie
    # is broken by name and ns-20 is the first one folded away.
    counts = {f"ns-{i:02d}": 30 - i for i in range(19)}
    counts |= {"ns-19": 11, "ns-20": 11, "ns-21": 3, "ns-22": 2, "ns-23": 1}
    for namespace, count in counts.items():
        rows += [
            {**template, "edge_id": f"e-{namespace}-{i}", "namespace": namespace}
            for i in range(count)
        ]
    fake = FakeLithosClient()
    fake.dataset = replace(fake.dataset, knowledge_edges=tuple(rows))
    with _client(lithos_lens_config_env, fake) as client:
        page = _get(client, ROUTE)

    top, _, folded = page.partition("data-picker-more-namespaces")
    table = top[top.index("data-picker-namespaces") :]
    ranked = re.findall(r"namespace=(ns-\d\d)\">ns-\d\d</a></td><td>(\d+)</td>", table)
    assert [name for name, _ in ranked] == [f"ns-{i:02d}" for i in range(20)]
    assert all(int(count) == counts[name] for name, count in ranked)
    rest = re.findall(r"namespace=(ns-\d\d)\">ns-\d\d</a></td><td>(\d+)</td>", folded)
    assert rest == [
        (name, str(counts[name])) for name in ("ns-20", "ns-21", "ns-22", "ns-23")
    ]
    assert "4 more namespaces" in folded
    # Folded away, not dropped: a closed disclosure the operator can open.
    assert '<details class="kgraph-more" data-picker-more-namespaces>' in page
    assert "<summary>4 more namespaces</summary>" in folded


# ── the snapshot lifecycle: stale, over-bound, age ────────────────────


class _Ticks:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _controlled_table(client: TestClient, ticks: _Ticks, fail: list[bool]) -> None:
    """Swap the app's edge table for one on a test clock whose fetch can fail."""
    lens = _lens(client)
    upstream = lens.lithos_client

    async def fetch(
        edge_type: str | None, namespace: str | None
    ) -> tuple[KnowledgeEdge, ...]:
        if fail[0]:
            raise RuntimeError("lithos briefly unavailable")
        return await upstream.edge_list(type=edge_type, namespace=namespace)

    lens.edge_table = EdgeTable(fetch, ttl_s=300, ticks=ticks)


def test_a_stale_snapshot_says_the_last_refresh_failed_until_it_recovers(
    lithos_lens_config_env: Path,
) -> None:
    ticks, fail = _Ticks(), [False]
    with _client(lithos_lens_config_env) as client:
        _controlled_table(client, ticks, fail)
        fresh = _payload(_get(client, f"{ROUTE}?type=contradicts"))["as_of"]

        ticks.now += 301
        fail[0] = True
        picker = _get(client, ROUTE)
        scoped = _get(client, f"{ROUTE}?type=contradicts")
        assert _payload(scoped)["as_of"] == fresh and _payload(scoped)["stale"]
        for page in (picker, scoped):
            assert "may be up to 300 s stale — the last refresh failed." in _plain(page)
            assert f'data-kgraph-as-of="{fresh}"' in page

        fail[0] = False
        recovered = _get(client, f"{ROUTE}?type=contradicts")
    assert "the last refresh failed" not in recovered
    assert _payload(recovered)["stale"] is False


def test_a_focus_refused_by_the_table_bound_states_when_it_was_read(
    lithos_lens_config_env: Path,
) -> None:
    """f-007: the table was read and counted, so the refusal has a time."""
    _set_knowledge(lithos_lens_config_env, "graph_edge_table_max_edges = 3")
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?focus={PLAN}")

    payload = _payload(page)
    assert payload["refusal"]["reason"] == "table_refused"
    assert payload["as_of"] is not None
    stamp = f"{payload['as_of'][:10]} {payload['as_of'][11:16]} UTC"
    assert f"Edge table as of {stamp}; may be up to 300 s stale." in _plain(page)


# ── the full facts tally and snapshot age on the span ─────────────────


def _graph_attrs(exporter: InMemorySpanExporter) -> list[dict[str, Any]]:
    prefix = "lens.knowledge.graph."
    return [
        {
            key[len(prefix) :]: value
            for key, value in (span.attributes or {}).items()
            if key.startswith(prefix)
        }
        for span in exporter.get_finished_spans()
        if (span.attributes or {}).get("http.route") == ROUTE
    ]


def _tally(attrs: dict[str, Any]) -> dict[str, int]:
    return {
        key: attrs[f"facts.{key}"]
        for key in ("hits", "reads", "missing", "capped", "failed")
    }


def test_the_span_carries_the_whole_facts_tally_as_the_page_counted_it(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    class FlakyCapacity(FakeLithosClient):
        async def read_note(
            self, knowledge_id: str, *, max_length: int | None = None
        ) -> NoteRecord | None:
            if knowledge_id == CAPACITY:
                raise RuntimeError("read timed out")
            return await super().read_note(knowledge_id, max_length=max_length)

    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 2")
    with _client(lithos_lens_config_env, FlakyCapacity()) as client:
        pages = [
            _payload(_get(client, f"{ROUTE}?focus={CAPACITY}")),
            _payload(_get(client, f"{ROUTE}?focus={CAPACITY}")),
            _payload(_get(client, f"{ROUTE}?namespace=influx")),
        ]

    recorded = _graph_attrs(spans)
    assert len(recorded) == 3
    for attrs, payload in zip(recorded, pages, strict=True):
        facts = payload["facts"]
        assert _tally(attrs) == {
            key: facts[key] for key in ("hits", "reads", "missing", "capped", "failed")
        }
        assert (attrs["nodes"], attrs["edges"]) == (
            len(payload["nodes"]),
            len(payload["edges"]),
        )
    first, second, scoped = (_tally(a) for a in recorded)
    # Capacity (the focus) fails, plan is read, the rest are past the cap.
    assert first["failed"] == 1 and first["reads"] == 2 and first["capped"] > 0
    # The second render is served from what the first cached.
    assert second["hits"] >= 1
    assert recorded[2]["mode"] == "global" and "depth" not in recorded[2]
    assert scoped["reads"] + scoped["hits"] + scoped["capped"] >= 1
    assert (
        snapshot_value(
            metric_snapshot(metric_reader),
            "lens_knowledge_graph_renders_total",
            mode="global",
            outcome="rendered",
        ).value
        == 1
    )


def test_the_span_reports_the_snapshot_age_on_the_tables_clock(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    ticks = _Ticks()
    with _client(lithos_lens_config_env) as client:
        _controlled_table(client, ticks, [False])
        _get(client, ROUTE)
        ticks.now += 42
        _get(client, ROUTE)

    ages = [attrs["snapshot_age_s"] for attrs in _graph_attrs(spans)]
    assert ages == [0.0, 42.0]


def test_the_stamp_is_utc_to_the_minute() -> None:
    from lithos_lens.knowledge_graph_routes import utc_minute

    moment = datetime(2026, 10, 8, 9, 5, 59, tzinfo=UTC)
    assert utc_minute(moment) == "2026-10-08 09:05 UTC"
