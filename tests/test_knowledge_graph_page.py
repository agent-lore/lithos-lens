"""K2 slice 3 — the `/knowledge/graph` page (``knowledge_graph_routes.py``).

The PRD's Page cases except the panels, driven through the real route against
the fake's demo knowledge dataset (14 edge rows over five notes — see
``fake_knowledge_dataset.knowledge_edge_rows``): the text names every node and
edge the payload carries; the picker renders the snapshot's facets and the
contradictions link; ``type=contradicts`` lists unresolved first; scopes over
a cap are refused with their count and remedy (the caps lowered through
config, since no fixture scope comes near 250 or 500); the hidden counts match
the filters; the legend lists only present types; the scope line states
``as_of``; and the ``lens.knowledge.graph`` telemetry, per mode, with no query
value on a metric label.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lithos_lens.config import load_config
from lithos_lens.fake_knowledge_dataset import DANGLING_NOTE_ID, knowledge_edge_rows
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge import RelatedNeighborhood
from lithos_lens.knowledge_edges import EdgeFacets, KnowledgeEdge
from lithos_lens.knowledge_graph_routes import (
    PICKER_TOP_NAMESPACES,
    KnowledgeGraphParams,
    KnowledgeGraphPicker,
    knowledge_graph_url,
    parse_knowledge_graph_params,
)
from lithos_lens.knowledge_graph_view import provenance_group
from lithos_lens.tasks import NoteRecord
from lithos_lens.template_vocabulary import short_id
from lithos_lens.web import create_app
from tests.conftest import metric_snapshot, snapshot_value

PLAN = "note-influx-plan"
CAPACITY = "note-influx-capacity"
ROLLBACK = "note-influx-rollback"
LEGACY = "note-influx-legacy-ingest"

ROUTE = "/knowledge/graph"
_PAYLOAD = re.compile(
    r'<script type="application/json" data-knowledge-graph-payload>(.*?)</script>',
    re.S,
)


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


def _payload(html: str) -> dict[str, Any]:
    match = _PAYLOAD.search(html)
    assert match is not None, "no payload embedded"
    return json.loads(match.group(1))


def _text_edges(html: str) -> list[str]:
    return re.findall(r'data-kgraph-edge="([^"]+)"', html)


def _text_nodes(html: str) -> set[str]:
    return set(re.findall(r'data-kgraph-node="([^"]+)"', html))


def _rows() -> list[dict[str, Any]]:
    return [dict(row) for row in knowledge_edge_rows()]


# ── the query grammar and the one URL builder ─────────────────────────


def test_blank_values_are_absent_and_no_scope_is_the_picker() -> None:
    params = parse_knowledge_graph_params(
        {"focus": "  ", "type": "", "namespace": " ", "depth": ""}
    )
    assert params == KnowledgeGraphParams()
    assert params.mode == "picker"


def test_focus_wins_over_type_and_namespace() -> None:
    params = parse_knowledge_graph_params(
        {"focus": PLAN, "type": "supports", "namespace": "influx"}
    )
    assert (params.mode, params.type, params.namespace) == ("focus", None, None)
    assert parse_knowledge_graph_params({"namespace": "influx"}).mode == "global"
    assert parse_knowledge_graph_params({"type": "supports"}).mode == "global"


@pytest.mark.parametrize(
    ("raw", "expected"), [("1", 1), ("2", 2), ("3", None), ("0", None), ("x", None)]
)
def test_depth_is_one_or_two_else_the_default(raw: str, expected: int | None) -> None:
    params = parse_knowledge_graph_params({"focus": PLAN, "depth": raw})
    assert params.depth == expected
    assert params.depth_or(1) == (expected or 1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.5", 0.5), ("-2", 0.0), ("7", 1.0), ("nan", None), ("heavy", None)],
)
def test_min_weight_is_clamped_and_unreadable_falls_back(
    raw: str, expected: float | None
) -> None:
    params = parse_knowledge_graph_params({"focus": PLAN, "min_weight": raw})
    assert params.min_weight == expected
    assert params.filters(0.1).min_weight == (0.1 if expected is None else expected)


def test_provenance_keeps_known_groups_in_order_and_unknown_means_all() -> None:
    params = parse_knowledge_graph_params({"provenance": "declared, bogus,inferred"})
    assert params.provenance == ("inferred", "declared")
    assert params.filters(0.1).provenance == {"inferred", "declared"}
    every = parse_knowledge_graph_params({"provenance": "bogus"})
    assert every.filters(0.1).provenance == {
        "inferred",
        "reinforced",
        "declared",
        "other",
    }


def test_the_url_builder_round_trips_through_the_parser() -> None:
    params = KnowledgeGraphParams(
        focus=PLAN,
        depth=2,
        min_weight=0.35,
        provenance=("inferred",),
        edge="edge_x",
    )
    url = knowledge_graph_url(params)
    assert url == (
        "/knowledge/graph?focus=note-influx-plan&depth=2&min_weight=0.35"
        "&provenance=inferred&edge=edge_x"
    )
    query = dict(re.findall(r"([a-z_]+)=([^&]*)", url.split("?", 1)[1]))
    assert parse_knowledge_graph_params(query) == params
    # One selection per URL (S5): selected= round-trips the same way.
    node = replace(params, edge="", selected="n")
    node_url = knowledge_graph_url(node)
    assert node_url.endswith("&provenance=inferred&selected=n")
    node_query = dict(re.findall(r"([a-z_]+)=([^&]*)", node_url.split("?", 1)[1]))
    assert parse_knowledge_graph_params(node_query) == node
    assert knowledge_graph_url() == "/knowledge/graph"
    assert (
        knowledge_graph_url(type="contradicts") == "/knowledge/graph?type=contradicts"
    )
    # A focus link drops the global scope, as the parser would.
    scoped = KnowledgeGraphParams(type="supports", namespace="ns a&b")
    assert knowledge_graph_url(scoped) == (
        "/knowledge/graph?type=supports&namespace=ns+a%26b"
    )
    assert knowledge_graph_url(scoped, focus=PLAN) == f"/knowledge/graph?focus={PLAN}"


# ── the picker ─────────────────────────────────────────────────────────


def test_the_picker_renders_the_snapshot_facets_and_the_contradictions_link(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, ROUTE)

    rows = _rows()
    for edge_type in {row["type"] for row in rows}:
        count = sum(1 for row in rows if row["type"] == edge_type)
        assert (
            f'<a href="/knowledge/graph?type={edge_type}">{edge_type}</a></td>'
            f"<td>{count}</td>"
        ) in html
    for namespace in {row["namespace"] for row in rows}:
        count = sum(1 for row in rows if row["namespace"] == namespace)
        assert (
            f'<a href="/knowledge/graph?namespace={namespace}">{namespace}</a></td>'
            f"<td>{count}</td>"
        ) in html
    # Two NULL-state contradicts rows; the superseded one is resolved.
    assert (
        '<a href="/knowledge/graph?type=contradicts">2 unresolved contradictions</a>'
        in html
    )
    assert "data-kgraph-scope-form" in html
    # No graph, so no payload and no search form of its own (S3 cut S1).
    assert _PAYLOAD.search(html) is None
    assert 'action="/knowledge"' in html  # only the nav's
    assert html.count('role="search"') == 1


def test_the_picker_puts_namespaces_past_the_top_twenty_behind_a_disclosure() -> None:
    namespaces = {f"ns-{index:02d}": 100 - index for index in range(25)}
    picker = KnowledgeGraphPicker(EdgeFacets(namespaces=namespaces))
    assert len(picker.top_namespaces) == PICKER_TOP_NAMESPACES
    assert [name for name, _ in picker.more_namespaces] == [
        f"ns-{index:02d}" for index in range(20, 25)
    ]


def test_a_picker_over_the_bound_says_so_and_offers_only_typed_scopes(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_edge_table_max_edges = 3")
    html = _get(lithos_lens_config_env, ROUTE)

    assert "Graph too large to index: 14 edges over the 3 bound." in html
    assert "data-kgraph-scope-form" in html
    assert "data-picker-types" not in html
    assert "data-picker-contradictions" not in html


def test_an_unreadable_table_leaves_the_picker_with_a_typed_scope(
    lithos_lens_config_env: Path,
) -> None:
    class Failing(FakeLithosClient):
        async def edge_list(self, **filters: Any) -> tuple[KnowledgeEdge, ...]:
            raise RuntimeError("lithos down")

    html = _get(lithos_lens_config_env, ROUTE, Failing())

    assert "data-picker-unavailable" in html
    assert "could not be read" in html
    assert "data-kgraph-scope-form" in html
    assert "data-picker-contradictions" not in html


# ── the text baseline agrees with the payload ─────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        f"{ROUTE}?focus={PLAN}",
        f"{ROUTE}?focus={PLAN}&depth=2&min_weight=0",
        f"{ROUTE}?focus={CAPACITY}&depth=2",
        f"{ROUTE}?namespace=influx",
        f"{ROUTE}?type=contradicts",
    ],
)
def test_the_text_names_every_node_and_edge_the_payload_carries(
    lithos_lens_config_env: Path, path: str
) -> None:
    html = _get(lithos_lens_config_env, path)
    payload = _payload(html)

    typed = {edge["id"] for edge in payload["edges"] if edge["kind"] == "typed"}
    assert typed  # every scope here draws something
    text_edges = _text_edges(html)
    assert set(text_edges) == typed
    assert len(text_edges) == len(typed)  # each listed once
    assert _text_nodes(html) == {node["id"] for node in payload["nodes"]}


def test_a_focus_reads_by_direction_from_the_focus(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}")

    def entry(edge_id: str) -> str:
        match = re.search(rf'data-kgraph-edge="{edge_id}">(.*?)</li>', html, re.S)
        assert match is not None
        return " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split())

    # plan refines legacy (outgoing), capacity supports plan (incoming),
    # plan contradicts rollback (symmetric) — each with its weight.
    assert entry("edge_a07c5f3e18b2") == "→ Legacy ingest approach (0.74)"
    assert entry("edge_4c1e9a7b20d3") == "← Influx capacity report (0.82)"
    assert entry("edge_e1f4a8c27b90") == "↔ Influx rollback route (0.80)"
    assert f'href="/knowledge/graph?focus={PLAN}&amp;edge=edge_a07c5f3e18b2"' in html
    # The focus note with its K1 chips, through NoteMetadata's slug.
    assert 'class="chip note-status note-status-active"' in html
    assert "Wiki-links and provenance" in html
    assert "Outgoing links" in html


def test_a_ghost_is_its_short_id_never_a_link_and_listed_as_missing(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={CAPACITY}")

    ghost = short_id(DANGLING_NOTE_ID)
    assert (
        f'<span class="kgraph-ghost" data-kgraph-node="{DANGLING_NOTE_ID}">'
        f"{ghost}</span>"
    ) in html
    assert f'href="/note/{DANGLING_NOTE_ID}"' not in html
    assert "note not found" in html
    assert "Edges to missing notes" in html


def test_a_failed_related_read_costs_only_the_layers(
    lithos_lens_config_env: Path,
) -> None:
    class NoRelated(FakeLithosClient):
        async def related(self, knowledge_id: str) -> RelatedNeighborhood:
            raise RuntimeError("timeout")

    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}", NoRelated())

    assert "Wiki-links and provenance could not be loaded." in html
    assert "edge_a07c5f3e18b2" in html  # the typed edges are still there


def test_an_edge_param_opens_a_drawn_edges_panel_else_is_ignored(
    lithos_lens_config_env: Path,
) -> None:
    named = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&edge=edge_a07c5f3e18b2")
    assert 'data-kgraph-panel="edge" data-kgraph-panel-id="edge_a07c5f3e18b2"' in named
    line = re.search(r"<h2 data-kgraph-sentence>(.*?)</h2>", named, re.S)
    assert line is not None
    text = " ".join(re.sub(r"<[^>]+>", "", line.group(1)).split())
    assert text == "Influx migration plan refines Legacy ingest approach"

    ignored = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&edge=edge_nope")
    assert "data-kgraph-panel=" not in ignored


def test_untrusted_titles_cannot_close_the_payload_script(
    lithos_lens_config_env: Path,
) -> None:
    hostile = "</script><script>alert(1)</script>"

    class Hostile(FakeLithosClient):
        async def read_note(
            self, knowledge_id: str, *, max_length: int | None = None
        ) -> NoteRecord | None:
            note = await super().read_note(knowledge_id, max_length=max_length)
            return None if note is None else replace(note, title=hostile)

    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}", Hostile())

    assert hostile not in html
    assert any(node["title"] == hostile for node in _payload(html)["nodes"])


# ── the contradictions queue ──────────────────────────────────────────


def test_type_contradicts_lists_unresolved_first_with_the_rationale(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, f"{ROUTE}?type=contradicts")

    unresolved = sorted(
        row["edge_id"]
        for row in _rows()
        if row["type"] == "contradicts" and row["conflict_state"] is None
    )
    resolved = [
        row["edge_id"]
        for row in _rows()
        if row["type"] == "contradicts" and row["conflict_state"] is not None
    ]
    # Same created_at throughout the fixture, so edge_id orders within state.
    assert _text_edges(html) == [*unresolved, *resolved]
    assert "Contradictions queue" in html

    def entry(edge_id: str) -> str:
        match = re.search(rf'data-kgraph-edge="{edge_id}">(.*?)</li>', html, re.S)
        assert match is not None
        return " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split())

    assert entry("edge_38c9d1f5e6a7") == (
        "Influx capacity report contradicts Legacy ingest approach · influx · 0.70 · "
        "unresolved · The legacy ingest note sizes the cluster for half the write "
        "rate this report measured."
    )
    # No evidence: no rationale segment.
    assert entry("edge_e1f4a8c27b90").endswith("· 0.80 · unresolved")
    assert "resolved: superseded" in entry("edge_b6e0f27d4c18")
    # The legend lists only what is drawn.
    assert re.findall(r'data-legend-type="([^"]+)"', html) == ["contradicts"]


# ── refusals ───────────────────────────────────────────────────────────


def test_a_namespace_over_the_global_cap_is_refused_with_its_count(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_global_max_nodes = 4")
    html = _get(lithos_lens_config_env, f"{ROUTE}?namespace=influx")

    assert 'data-kgraph-refusal="too_many_nodes"' in html
    assert "5 notes in this view, over the 4 cap. min_weight=0.7 shows 4." in html
    assert 'href="/knowledge/graph?namespace=influx&amp;min_weight=0.7"' in html
    assert "data-kgraph-edges" not in html and "data-kgraph-legend" not in html
    refusal = _payload(html)["refusal"]
    assert (refusal["count"], refusal["cap"]) == (5, 4)
    assert _payload(html)["nodes"] == []


def test_a_focus_over_the_cap_names_depth_one_as_the_remedy(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_focus_max_nodes = 4")
    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&depth=2")

    assert "5 notes in this view, over the 4 cap. depth=1 shows 4." in html
    assert f'href="/knowledge/graph?focus={PLAN}&amp;depth=1"' in html
    assert "data-kgraph-edges" not in html
    # The scope line names the refused focus by its id: no facts were read.
    assert f'<span class="kgraph-id">{PLAN}</span>' in html


def test_a_depth_one_focus_over_the_cap_names_a_weight_filter(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_focus_max_nodes = 3")
    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&depth=1")

    assert "4 notes in this view, over the 3 cap. min_weight=0.9 shows 3." in html
    assert f'href="/knowledge/graph?focus={PLAN}&amp;depth=1&amp;min_weight=0.9"' in (
        html
    )


def test_an_unreadable_table_refuses_the_scope_without_a_time(
    lithos_lens_config_env: Path,
) -> None:
    class Failing(FakeLithosClient):
        async def edge_list(self, **filters: Any) -> tuple[KnowledgeEdge, ...]:
            raise RuntimeError("lithos down")

    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}", Failing())

    assert 'data-kgraph-refusal="unavailable"' in html
    assert "The knowledge edge table could not be read." in html
    assert "kgraph-as-of" not in html


# ── hidden counts, legend, scope line ─────────────────────────────────


def _incident(note_id: str) -> list[dict[str, Any]]:
    return [row for row in _rows() if note_id in (row["from_id"], row["to_id"])]


def _hidden_line(html: str, key: str) -> str:
    match = re.search(rf"<li data-hidden-{key}>(.*?)</li>", html, re.S)
    assert match is not None
    return " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split())


@pytest.mark.parametrize(
    ("query", "min_weight", "groups"),
    [
        ("", 0.1, None),
        ("&min_weight=0", 0.0, None),
        ("&min_weight=0.75", 0.75, None),
        ("&provenance=inferred", 0.1, {"inferred"}),
        ("&provenance=reinforced,declared", 0.1, {"reinforced", "declared"}),
    ],
)
def test_hidden_counts_match_the_filters(
    lithos_lens_config_env: Path,
    query: str,
    min_weight: float,
    groups: set[str] | None,
) -> None:
    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}{query}")

    rows = _incident(PLAN)
    by_weight = sum(1 for row in rows if row["weight"] < min_weight)
    by_provenance = sum(
        1
        for row in rows
        if groups is not None and provenance_group(row["provenance_type"]) not in groups
    )
    assert _hidden_line(html, "weight").startswith(
        f"{by_weight} edge{'' if by_weight == 1 else 's'} below {min_weight} hidden"
    )
    assert _hidden_line(html, "provenance").startswith(
        f"{by_provenance} edge{'' if by_provenance == 1 else 's'} hidden by the "
        "provenance filter"
    )
    hidden = _payload(html)["hidden"]
    assert (hidden["by_weight"], hidden["by_provenance"]) == (by_weight, by_provenance)


def test_the_depth_two_would_be_count_is_stated(lithos_lens_config_env: Path) -> None:
    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}")

    assert re.search(r"depth 2 would draw 5 notes", html)
    assert f'href="/knowledge/graph?focus={PLAN}&amp;depth=2"' in html


def test_the_legend_lists_only_present_types_in_table_order(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}")

    legend = re.findall(r'data-legend-type="([^"]+)"', html)
    # related_to is present only as the 0.03 edge, hidden at 0.1.
    assert legend == [
        "supports",
        "refines",
        "is_example_of",
        "depends_on",
        "derived_from",
        "contradicts",
        "assesses",
    ]
    # The wiki-link layer is drawn too: its line follows, keyed as a layer.
    assert re.findall(r'data-legend-layer="([^"]+)"', html) == ["wiki_link"]
    assert [line["type"] for line in _payload(html)["legend"]] == [
        *legend,
        "wiki_link",
    ]


def test_the_scope_line_states_as_of_and_the_ttl(lithos_lens_config_env: Path) -> None:
    html = _get(lithos_lens_config_env, f"{ROUTE}?namespace=influx")

    as_of = _payload(html)["as_of"]
    stamp = f"{as_of[:10]} {as_of[11:16]} UTC"
    assert f"Edge table as of {stamp}; may be up to 300 s stale." in html
    assert "Edges in namespace <code>influx</code>" in html


def test_an_over_bound_global_read_is_stated_as_read_directly(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_edge_table_max_edges = 3")
    html = _get(lithos_lens_config_env, f"{ROUTE}?type=contradicts")

    assert "Edges read directly from Lithos at" in html
    assert "may be up to" not in html
    assert len(_text_edges(html)) == 3


class _OfflineRecorder(FakeLithosClient):
    """Offline, and recording every graph data read it is asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.data_reads: list[str] = []

    async def health(self) -> Any:
        return "unreachable"

    async def edge_list(self, **filters: Any) -> tuple[KnowledgeEdge, ...]:
        self.data_reads.append("edge_list")
        return await super().edge_list(**filters)

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        self.data_reads.append("read_note")
        return await super().read_note(knowledge_id, max_length=max_length)

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        self.data_reads.append("related")
        return await super().related(knowledge_id)


@pytest.mark.parametrize(
    ("query", "mode"),
    [("", "picker"), (f"?focus={PLAN}", "focus"), ("?namespace=influx", "global")],
)
def test_offline_the_page_says_so_and_reads_nothing(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
    query: str,
    mode: str,
) -> None:
    fake = _OfflineRecorder()
    html = _get(lithos_lens_config_env, f"{ROUTE}{query}", fake)

    assert "The knowledge graph is unavailable." in html
    assert fake.data_reads == []
    assert not [call for call in fake.tool_calls if call[0] != "lithos_agent_register"]
    assert _PAYLOAD.search(html) is None
    attrs = _graph_attrs(_route_span(spans))
    assert (attrs["mode"], attrs["outcome"]) == (mode, "offline")
    assert {key: attrs[key] for key in _COUNT_ATTRS} == dict.fromkeys(_COUNT_ATTRS, 0)
    assert attrs["snapshot_age_s"] == 0
    # Focus mode states the depth it would have drawn: the configured default.
    assert attrs.get("depth") == (1 if mode == "focus" else None)
    assert (
        snapshot_value(
            metric_snapshot(metric_reader),
            "lens_knowledge_graph_renders_total",
            mode=mode,
            outcome="offline",
        ).value
        == 1
    )


# ── telemetry ──────────────────────────────────────────────────────────


def _route_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    matching = [
        span
        for span in exporter.get_finished_spans()
        if (span.attributes or {}).get("http.route") == ROUTE
    ]
    assert len(matching) == 1
    return matching[0]


_COUNT_ATTRS = (
    "nodes",
    "edges",
    "hidden_by_weight",
    "hidden_by_provenance",
    "hidden_total",
    "facts.hits",
    "facts.reads",
    "facts.missing",
    "facts.capped",
    "facts.failed",
)


def _graph_attrs(span: ReadableSpan) -> dict[str, Any]:
    prefix = "lens.knowledge.graph."
    return {
        key[len(prefix) :]: value
        for key, value in (span.attributes or {}).items()
        if key.startswith(prefix)
    }


def test_a_focus_render_reports_its_counts_on_the_span(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    with _client(lithos_lens_config_env) as client:
        html = client.get(f"{ROUTE}?focus={PLAN}&depth=2").text

    payload = _payload(html)
    attrs = _graph_attrs(_route_span(spans))
    assert attrs["mode"] == "focus" and attrs["depth"] == 2
    assert attrs["nodes"] == len(payload["nodes"])
    assert attrs["edges"] == len(payload["edges"])
    assert attrs["hidden_by_weight"] == payload["hidden"]["by_weight"] > 0
    assert attrs["hidden_by_provenance"] == 0
    assert attrs["hidden_total"] == payload["hidden"]["total"]
    assert attrs["facts.reads"] == payload["facts"]["reads"] > 0
    assert attrs["facts.missing"] == payload["facts"]["missing"] == 1
    assert "refusal" not in attrs
    assert attrs["snapshot_age_s"] >= 0
    snapshot = metric_snapshot(metric_reader)
    assert (
        snapshot_value(
            snapshot,
            "lens_knowledge_graph_renders_total",
            mode="focus",
            outcome="rendered",
        ).value
        == 1
    )


def test_a_refused_global_render_reports_the_reason(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_global_max_nodes = 4")
    with _client(lithos_lens_config_env) as client:
        client.get(f"{ROUTE}?namespace=influx")

    attrs = _graph_attrs(_route_span(spans))
    assert attrs["mode"] == "global" and "depth" not in attrs
    assert attrs["refusal"] == "too_many_nodes"
    assert attrs["nodes"] == 0
    snapshot = metric_snapshot(metric_reader)
    assert (
        snapshot_value(
            snapshot,
            "lens_knowledge_graph_renders_total",
            mode="global",
            outcome="refused",
        ).value
        == 1
    )


def test_the_picker_reports_its_mode(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    with _client(lithos_lens_config_env) as client:
        client.get(ROUTE)

    attrs = _graph_attrs(_route_span(spans))
    assert attrs["mode"] == "picker" and "depth" not in attrs
    assert "refusal" not in attrs
    # Every count is set, zero: the picker draws nothing.
    assert {key: attrs[key] for key in _COUNT_ATTRS} == dict.fromkeys(_COUNT_ATTRS, 0)
    assert attrs["snapshot_age_s"] >= 0
    snapshot = metric_snapshot(metric_reader)
    assert (
        snapshot_value(
            snapshot,
            "lens_knowledge_graph_renders_total",
            mode="picker",
            outcome="rendered",
        ).value
        == 1
    )


def test_an_unreadable_table_counts_as_unavailable(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    class Failing(FakeLithosClient):
        async def edge_list(self, **filters: Any) -> tuple[KnowledgeEdge, ...]:
            raise RuntimeError("lithos down")

    with _client(lithos_lens_config_env, Failing()) as client:
        client.get(f"{ROUTE}?type=supports")

    assert _graph_attrs(_route_span(spans))["refusal"] == "unavailable"
    snapshot = metric_snapshot(metric_reader)
    assert (
        snapshot_value(
            snapshot,
            "lens_knowledge_graph_renders_total",
            mode="global",
            outcome="unavailable",
        ).value
        == 1
    )


def test_the_scope_never_reaches_a_metric_label(
    lithos_lens_config_env: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    """A focus id, type or namespace is operator input: one series per value
    is the cardinality failure the metric catalogue's rule forbids."""
    secrets = ("zzfocussecretzz", "zztypesecretzz", "zznamespacesecretzz")
    with _client(lithos_lens_config_env) as client:
        client.get(f"{ROUTE}?focus={secrets[0]}")
        client.get(f"{ROUTE}?type={secrets[1]}&namespace={secrets[2]}")

    data = metric_reader.get_metrics_data()
    assert data is not None
    for resource_metric in data.resource_metrics or []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                for point in metric.data.data_points:
                    labels = str(dict(point.attributes or {}))
                    assert not any(secret in labels for secret in secrets)


def test_nodes_past_the_facts_cap_are_their_full_id_linked_and_counted(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    html = _get(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}")

    payload = _payload(html)
    unread = [n["id"] for n in payload["nodes"] if n["facts_state"] == "unread"]
    # The focus is read first; its three typed neighbours are past the cap.
    assert sorted(unread) == sorted([CAPACITY, LEGACY, ROLLBACK])
    for node_id in unread:
        assert (
            f'<a href="/note/{node_id}" data-kgraph-node="{node_id}">{node_id}</a>'
            in (html)
        )
    line = re.search(r"<li data-facts-capped>(.*?)</li>", html, re.S)
    assert line is not None
    assert " ".join(line.group(1).split()) == "3 notes labelled by id (facts cap 1)"
