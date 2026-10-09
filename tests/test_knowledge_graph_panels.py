"""K2 slice 5 — the node and edge panels on `/knowledge/graph` (PRD D10).

Driven through the real routes against the fake's demo knowledge dataset
(``fake_knowledge_dataset.knowledge_edge_rows``): ``selected=`` renders the
node panel in the page with the note's relations in this view; ``edge=`` the
edge panel with its relation sentence, row and evidence; the fragment at
``/knowledge/graph/panel`` is the same partial for the same parameters; and
the cases the fixture does not carry — non-JSON and ``<script>`` evidence, a
caller's conflict marker — are injected through a ``FakeLithosClient.edge_list``
subclass, as S3's tests inject theirs, while a ``partial`` row is made by
patching the snapshot after a first read, as an ``edge.upserted`` event does.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from lithos_lens.config import load_config
from lithos_lens.fake_knowledge_dataset import DANGLING_NOTE_ID, knowledge_edge_rows
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.graph_cache import graph_fanout_gate
from lithos_lens.knowledge import RelatedNeighborhood
from lithos_lens.knowledge_edge_types import EdgeDirection
from lithos_lens.knowledge_edges import KnowledgeEdge, normalize_edge_list
from lithos_lens.knowledge_facts import NoteFactsCache
from lithos_lens.knowledge_graph_routes import (
    RENDERED_VIEWS_KEPT,
    KnowledgeGraphParams,
    RenderedViews,
    knowledge_graph_panel_url,
    knowledge_graph_url,
    parse_knowledge_graph_params,
)
from lithos_lens.knowledge_graph_view import (
    KnowledgeGraphEdge,
    KnowledgeGraphFilters,
    KnowledgeGraphView,
)
from lithos_lens.tasks import NoteRecord
from lithos_lens.template_vocabulary import short_id
from lithos_lens.web import create_app
from tests.conftest import metric_points, metric_snapshot, snapshot_value

PLAN = "note-influx-plan"
CAPACITY = "note-influx-capacity"
ROLLBACK = "note-influx-rollback"
LEGACY = "note-influx-legacy-ingest"

ROUTE = "/knowledge/graph"
PANEL = "/knowledge/graph/panel"

REFINES = "edge_a07c5f3e18b2"  # plan refines legacy, inferred, with a rationale
SUPPORTS = "edge_4c1e9a7b20d3"  # capacity supports plan, inferred
BARE_CONTRADICTION = "edge_e1f4a8c27b90"  # plan/rollback, no evidence or provenance
CONTRADICTION = "edge_38c9d1f5e6a7"  # capacity/legacy, unresolved, with a rationale
RESOLVED = "edge_b6e0f27d4c18"  # legacy/plan, superseded
UNKNOWN_TYPE = "edge_f29d84a6130c"  # capacity assesses plan

_HOST = re.compile(
    r'<div id="kgraph-panel" class="kgraph-panel-host" data-kgraph-panel-host'
    r' data-kgraph-render="([^"]*)">'
    r"(.*?)</div>\s*<section class=\"panel kgraph-legend\"",
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


def _get(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    return response.text


def _page(config_path: Path, path: str, fake: Any = None) -> str:
    with _client(config_path, fake) as client:
        return _get(client, path)


def _host(html: str) -> str:
    """The page's panel host, as rendered (``""`` when it holds no panel)."""
    match = _HOST.search(html)
    assert match is not None, "no panel host before the legend"
    return match.group(2).strip()


def _render_id(html: str) -> str:
    """The render id the page drew its view under (its panel links carry it)."""
    match = _HOST.search(html)
    assert match is not None, "no panel host before the legend"
    return match.group(1)


def _fragment(client: TestClient, query: str, page: str) -> str:
    """The panel fragment a click on ``page`` fetches for ``query``."""
    return _get(client, f"{PANEL}?{query}&render={_render_id(page)}").strip()


_RENDER = re.compile(r"(render=)[A-Za-z0-9_-]+")


def _unrendered(html: str) -> str:
    """``html`` with its render ids blanked: two renders' panels compared."""
    return _RENDER.sub(r"\1-", html)


def _payload(html: str) -> dict[str, Any]:
    raw = _first(r"data-knowledge-graph-payload>(.*?)</script>", html)
    return json.loads(raw)


def _plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def _first(pattern: str, html: str) -> str:
    match = re.search(pattern, html, re.S)
    assert match is not None, pattern
    return match.group(1)


# The fixture's facts for the notes the panels show (type, status, namespace,
# confidence chips, then the lede), each set distinct so a swap fails.
PLAN_FACTS = (
    ["summary", "active", "plans", "confidence 90%"],
    "Cut ingest over first, backfill after; abort via the feature gate.",
)
CAPACITY_FACTS = (["observation", "active", "reports", "confidence 80%"], "")
LEGACY_FACTS = (["hypothesis", "quarantined", "confidence 20%"], "")


def _facts(scope: str) -> tuple[list[str], str]:
    """The chips and the lede rendered in ``scope``: one note's facts."""
    chips = re.findall(r'<span class="chip note-[a-z-]+[^"]*">(.*?)</span>', scope)
    ledes = re.findall(r'<p class="kgraph-lede">(.*?)</p>', scope, re.S)
    assert len(ledes) <= 1, "more than one note's lede in scope"
    return [" ".join(chip.split()) for chip in chips], ledes[0] if ledes else ""


def _node_head(host: str) -> str:
    """The node panel above its degree line: the note's own title and facts."""
    return host[: host.index("data-kgraph-degree")]


def _card(host: str, note_id: str) -> str:
    return _first(rf'data-kgraph-card="{re.escape(note_id)}">(.*?)</article>', host)


def _row(edge_id: str) -> dict[str, Any]:
    return next(dict(row) for row in knowledge_edge_rows() if row["edge_id"] == edge_id)


class _ExtraEdges(FakeLithosClient):
    """The demo's edge table plus rows the fixture does not carry, in the
    vendored ``lithos_edge_list`` row shape, through the real normalizer."""

    def __init__(self, *rows: dict[str, Any]) -> None:
        super().__init__()
        self.extra = rows

    async def edge_list(self, **filters: Any) -> tuple[KnowledgeEdge, ...]:
        given = {key: value for key, value in filters.items() if value is not None}
        extra = [
            row
            for row in self.extra
            if all(row.get(key) == value for key, value in given.items())
        ]
        return await super().edge_list(**filters) + normalize_edge_list(
            {"results": extra}
        )


def _injected(edge_id: str, evidence: str | None, **changes: Any) -> dict[str, Any]:
    """The refines row (plan → legacy) under a new id with other evidence."""
    return {**_row(REFINES), "edge_id": edge_id, "evidence": evidence, **changes}


# ── one selection: the parser's precedence and the URL builder ─────────


@pytest.mark.parametrize(
    "query",
    [
        {"selected": PLAN, "edge": REFINES},
        {"edge": REFINES, "selected": PLAN},
    ],
)
def test_edge_wins_over_selected_whatever_the_order(query: dict[str, str]) -> None:
    params = parse_knowledge_graph_params({"focus": PLAN, **query})

    assert (params.edge, params.selected) == (REFINES, "")


def test_a_blank_edge_leaves_selected_standing() -> None:
    params = parse_knowledge_graph_params({"selected": PLAN, "edge": "  "})

    assert (params.edge, params.selected) == ("", PLAN)


def test_a_link_carries_one_selection_the_later_one() -> None:
    on_node = KnowledgeGraphParams(focus=PLAN, selected=CAPACITY)
    on_edge = KnowledgeGraphParams(focus=PLAN, edge=REFINES)

    assert knowledge_graph_url(on_node, edge=REFINES) == (
        f"{ROUTE}?focus={PLAN}&edge={REFINES}"
    )
    # A node opened from an edge's view keeps that edge drawn (pin=).
    assert knowledge_graph_url(on_edge, selected=CAPACITY) == (
        f"{ROUTE}?focus={PLAN}&selected={CAPACITY}&pin={REFINES}"
    )
    pinned = KnowledgeGraphParams(focus=PLAN, selected=CAPACITY, pin=REFINES)
    assert knowledge_graph_url(pinned, selected=LEGACY) == (
        f"{ROUTE}?focus={PLAN}&selected={LEGACY}&pin={REFINES}"
    )
    # A new edge is its own pin; a new scope keeps none.
    assert knowledge_graph_url(pinned, edge=SUPPORTS) == (
        f"{ROUTE}?focus={PLAN}&edge={SUPPORTS}"
    )
    assert knowledge_graph_url(pinned, focus=LEGACY, selected="", edge="") == (
        f"{ROUTE}?focus={LEGACY}"
    )
    # A change that sets neither keeps the selection the page carries.
    assert knowledge_graph_url(on_edge, depth=2) == (
        f"{ROUTE}?focus={PLAN}&depth=2&edge={REFINES}"
    )
    assert knowledge_graph_panel_url(on_node, edge=REFINES) == (
        f"{PANEL}?focus={PLAN}&edge={REFINES}"
    )


@pytest.mark.parametrize(
    "query",
    [f"selected={PLAN}&edge={REFINES}", f"edge={REFINES}&selected={PLAN}"],
)
def test_a_page_given_both_renders_the_edge_panel(
    lithos_lens_config_env: Path, query: str
) -> None:
    host = _host(_page(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&{query}"))

    assert 'data-kgraph-panel="edge"' in host
    assert f'data-kgraph-panel-id="{REFINES}"' in host
    assert 'data-kgraph-panel="node"' not in host


# ── the node panel ─────────────────────────────────────────────────────


def _relations(host: str) -> dict[str, list[str]]:
    """The node panel's groups: relation type → the entries' edge ids."""
    groups: dict[str, list[str]] = {}
    for kind, body in re.findall(
        r'data-kgraph-panel-type="([^"]+)">(.*?)</section>', host, re.S
    ):
        groups[kind] = re.findall(r'data-kgraph-panel-edge="([^"]+)"', body)
    return groups


def _panel_entry(host: str, edge_id: str) -> str:
    return _plain(
        _first(rf'data-kgraph-panel-edge="{re.escape(edge_id)}">(.*?)</li>', host)
    )


def test_selected_renders_the_node_panel_with_its_relations_in_view(
    lithos_lens_config_env: Path,
) -> None:
    html = _page(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&selected={PLAN}")
    host = _host(html)

    assert 'data-kgraph-panel="node"' in host
    assert f'<h2><a href="/note/{PLAN}" data-kgraph-node="{PLAN}">' in host
    assert 'class="chip note-status note-status-active"' in host
    assert _facts(_node_head(host)) == PLAN_FACTS
    # Centre on this is offered on the focus too (D6: no exception).
    centre = _first(r'<a href="([^"]+)" data-kgraph-centre>Centre on this</a>', host)
    assert centre == f"{ROUTE}?focus={PLAN}"
    groups = _relations(host)
    # Legend order, typed lines then the wiki-link layer; the 0.03 related_to
    # edge the default weight filter hides is not "in this view".
    assert list(groups) == [
        "supports",
        "refines",
        "is_example_of",
        "depends_on",
        "derived_from",
        "contradicts",
        "assesses",
        "wiki_link",
    ]
    assert "edge_15d0c3e8f972" not in host
    assert groups["contradicts"] == [BARE_CONTRADICTION, RESOLVED]  # queue order
    degree = int(_first(r"Degree in this view: (\d+)", host))
    assert degree == sum(len(ids) for ids in groups.values())
    # Each entry reads from the selected note, typed ones linked to their panel.
    assert _panel_entry(host, REFINES) == "→ Legacy ingest approach (0.74)"
    assert _panel_entry(host, SUPPORTS) == "← Influx capacity report (0.82)"
    assert f'hx-get="{PANEL}?focus={PLAN}&amp;edge={SUPPORTS}&amp;render=' in host


def test_a_neighbours_panel_reads_from_it_and_centres_on_it(
    lithos_lens_config_env: Path,
) -> None:
    html = _page(
        lithos_lens_config_env,
        f"{ROUTE}?focus={PLAN}&depth=2&min_weight=0&selected={CAPACITY}",
    )
    host = _host(html)

    # plan depends_on capacity reads incoming from capacity, not from the focus.
    assert _panel_entry(host, "edge_c5b1730f9ad6") == "← Influx migration plan (0.71)"
    centre = _first(r'<a href="([^"]+)" data-kgraph-centre>Centre on this</a>', host)
    assert centre == f"{ROUTE}?focus={CAPACITY}&amp;depth=2&amp;min_weight=0.0"


def test_centre_on_the_focus_keeps_depth_and_filters(
    lithos_lens_config_env: Path,
) -> None:
    host = _host(
        _page(
            lithos_lens_config_env,
            f"{ROUTE}?focus={PLAN}&depth=2&min_weight=0.5&provenance=inferred"
            f"&selected={PLAN}",
        )
    )

    centre = _first(r'<a href="([^"]+)" data-kgraph-centre>Centre on this</a>', host)
    assert centre == (
        f"{ROUTE}?focus={PLAN}&amp;depth=2&amp;min_weight=0.5&amp;provenance=inferred"
    )


def test_a_ghosts_panel_says_note_not_found_and_shows_its_id(
    lithos_lens_config_env: Path,
) -> None:
    host = _host(
        _page(
            lithos_lens_config_env,
            f"{ROUTE}?focus={CAPACITY}&selected={DANGLING_NOTE_ID}",
        )
    )

    assert 'data-kgraph-panel="node"' in host
    heading = _first(r"<h2>(.*?)</h2>", host)
    assert _plain(heading) == f"{short_id(DANGLING_NOTE_ID)} note not found"
    assert f"data-kgraph-full-id>{DANGLING_NOTE_ID}</p>" in host
    assert "note-chips" not in host
    assert f'href="/note/{DANGLING_NOTE_ID}"' not in host


def test_an_unread_nodes_panel_says_its_facts_were_not_read(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    host = _host(
        _page(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&selected={CAPACITY}")
    )

    assert f"data-kgraph-full-id>{CAPACITY}</p>" in host
    assert "Facts not read for this view." in host
    assert "note-chips" not in host


def test_a_pending_nodes_panel_shows_last_known_facts_marked_pending(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    with _client(lithos_lens_config_env) as client:
        _get(client, f"{ROUTE}?focus={PLAN}")  # reads the plan (the focus)
        _get(client, f"{ROUTE}?focus={ROLLBACK}")  # reads the rollback
        facts = client.app.state.lens.note_facts  # type: ignore[attr-defined]
        assert facts.apply_note_event("note.updated", {"id": ROLLBACK, "title": "r2"})
        assert facts.apply_note_event("note.updated", {"id": PLAN, "title": "v2"})
        # The cap re-reads the focus first: the plan stays pending.
        query = f"focus={ROLLBACK}&selected={PLAN}"
        page = _get(client, f"{ROUTE}?{query}")
        host = _host(page)
        fragment = _fragment(client, query, page)

    assert "Facts pending a re-read." in host
    # Its last-known facts: the title the event set, marked, and the chips and
    # lede read before the event — not its id.
    assert _plain(_first(r"<h2>(.*?)</h2>", host)) == "v2 facts pending"
    assert _facts(_node_head(host)) == PLAN_FACTS
    assert "data-kgraph-full-id" not in host
    assert "Facts not read for this view." not in host
    assert fragment == host


# ── the edge panel ─────────────────────────────────────────────────────


def _evidence(host: str) -> str:
    return _first(
        r"<section class=\"kgraph-evidence\" data-kgraph-evidence>(.*?)</section>", host
    )


def test_edge_renders_the_edge_panel_with_the_rationale_from_evidence_json(
    lithos_lens_config_env: Path,
) -> None:
    host = _host(_page(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&edge={REFINES}"))

    assert _plain(_first(r"<h2 data-kgraph-sentence>(.*?)</h2>", host)) == (
        "Influx migration plan refines Legacy ingest approach"
    )
    fields = _plain(_first(r'<dl class="kgraph-fields">(.*?)</dl>', host))
    assert fields == (
        "Type refines Weight 0.74 Namespace influx "
        "Provenance inferred by lithos-enrich · type inferred · actor lithos-enrich "
        "Created 2026-08-03T09:12:44.518230+00:00 "
        "Updated 2026-08-03T09:12:44.518230+00:00"
    )
    evidence = _evidence(host)
    assert (
        '<p class="edge-why-rationale" data-edge-rationale>The migration plan keeps '
        "the legacy ingest's stages"
    ).replace("'", "&#39;") in evidence
    assert "data-edge-model>model claude-haiku-4-5</span>" in evidence
    assert "data-edge-confidence>confidence 0.74</span>" in evidence
    assert "No rationale recorded" not in evidence
    # Not a contradiction: the cards stack, and no resolve slot.
    assert 'data-kgraph-cards="stack"' in host
    assert "data-kgraph-resolve-slot" not in host
    assert "data-kgraph-conflict-state" not in host
    # Both endpoints as cards, each with a link to its own node panel.
    assert re.findall(r'data-kgraph-card="([^"]+)"', host) == [PLAN, LEGACY]
    assert _facts(_card(host, PLAN)) == PLAN_FACTS
    assert _facts(_card(host, LEGACY)) == LEGACY_FACTS
    assert (
        f'hx-get="{PANEL}?focus={PLAN}&amp;selected={LEGACY}&amp;pin={REFINES}'
        "&amp;render="
    ) in host


@pytest.mark.parametrize(
    ("edge_id", "sentence"),
    [
        (SUPPORTS, "Influx capacity report supports Influx migration plan"),
        (
            "edge_7d2c0e95b463",
            "Influx rollback route is derived from Influx migration plan",
        ),
        (
            BARE_CONTRADICTION,
            "Influx migration plan and Influx rollback route contradict each other",
        ),
        (
            UNKNOWN_TYPE,
            "Influx capacity report assesses Influx migration plan "
            "(direction as recorded)",
        ),
    ],
)
def test_the_relation_sentence_follows_the_type_table(
    lithos_lens_config_env: Path, edge_id: str, sentence: str
) -> None:
    host = _host(_page(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&edge={edge_id}"))

    assert _plain(_first(r"<h2 data-kgraph-sentence>(.*?)</h2>", host)) == sentence


def test_null_evidence_reads_no_rationale_recorded(
    lithos_lens_config_env: Path,
) -> None:
    host = _host(
        _page(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&edge={BARE_CONTRADICTION}")
    )

    assert _plain(_evidence(host)) == "Evidence No rationale recorded."
    # No provenance recorded: the line is left out, not printed empty.
    assert "data-kgraph-provenance" not in host


def test_non_json_evidence_renders_as_escaped_text(
    lithos_lens_config_env: Path,
) -> None:
    fake = _ExtraEdges(
        _injected("edge_plain0001", "Linked by hand: see <b>both</b> & compare.")
    )
    host = _host(
        _page(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&edge=edge_plain0001", fake)
    )

    assert (
        '<p class="edge-why-raw" data-edge-evidence-raw>'
        "Linked by hand: see &lt;b&gt;both&lt;/b&gt; &amp; compare.</p>"
    ) in host
    assert "<b>both</b>" not in host
    assert "No rationale recorded" not in host


def test_script_evidence_does_not_execute(lithos_lens_config_env: Path) -> None:
    hostile = "<script>alert('edge')</script>"
    fake = _ExtraEdges(_injected("edge_hostile01", hostile))
    with _client(lithos_lens_config_env, fake) as client:
        page = _get(client, f"{ROUTE}?focus={PLAN}&edge=edge_hostile01")
        fragment = _get(client, f"{PANEL}?focus={PLAN}&edge=edge_hostile01")

    for html in (page, fragment):
        assert "<script>alert" not in html
        assert "&lt;script&gt;alert(&#39;edge&#39;)&lt;/script&gt;" in html


def test_an_object_without_a_rationale_keeps_its_chips(
    lithos_lens_config_env: Path,
) -> None:
    fake = _ExtraEdges(
        _injected("edge_chipsonly", '{"model": "m-1", "confidence": 0.5}')
    )
    host = _host(
        _page(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&edge=edge_chipsonly", fake)
    )

    evidence = _evidence(host)
    assert "No rationale recorded." in evidence
    assert "data-edge-model>model m-1</span>" in evidence
    assert "data-edge-confidence>confidence 0.50</span>" in evidence


def test_a_partial_edge_says_its_rationale_is_pending(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env) as client:
        _get(client, f"{ROUTE}?focus={PLAN}")  # the snapshot is held
        table = client.app.state.lens.edge_table  # type: ignore[attr-defined]
        identity = {key: _row(REFINES)[key] for key in ("from_id", "to_id", "type")}
        assert table.apply_upsert(
            {"edge_id": REFINES, "namespace": "influx", **identity}
        )
        assert table.apply_upsert(
            {
                "edge_id": "edge_from_event",
                "from_id": PLAN,
                "to_id": CAPACITY,
                "type": "supports",
                "namespace": "influx",
            }
        )
        replaced = _host(_get(client, f"{ROUTE}?focus={PLAN}&edge={REFINES}"))
        inserted = _host(_get(client, f"{ROUTE}?focus={PLAN}&edge=edge_from_event"))

    for host in (replaced, inserted):
        assert _plain(_evidence(host)) == (
            "Evidence Rationale pending the next edge-table fetch."
        )
        assert "data-edge-rationale" not in host
    # The replaced row keeps its last-known weight; the inserted one has none.
    assert "<dt>Weight</dt><dd>0.74</dd>" in replaced
    assert "<dt>Weight</dt><dd>weight unknown</dd>" in inserted
    assert "data-kgraph-provenance" not in inserted


# ── contradictions ─────────────────────────────────────────────────────


def test_a_contradiction_leads_with_its_state_and_sets_the_cards_side_by_side(
    lithos_lens_config_env: Path,
) -> None:
    host = _host(
        _page(lithos_lens_config_env, f"{ROUTE}?type=contradicts&edge={CONTRADICTION}")
    )

    assert host.index("data-kgraph-conflict-state") < host.index("data-kgraph-sentence")
    state = _first(r'data-kgraph-conflict-state="unresolved">(.*?)</p>', host)
    assert _plain(state) == "Unresolved"
    cards = _first(
        r'data-kgraph-cards="pair">(.*?)</div>\s*<p class="kgraph-resolve', host
    )
    assert 'class="kgraph-cards kgraph-cards-pair"' in host
    assert re.findall(r'data-kgraph-card="([^"]+)"', cards) == [CAPACITY, LEGACY]
    assert _facts(_card(cards, CAPACITY)) == CAPACITY_FACTS
    assert _facts(_card(cards, LEGACY)) == LEGACY_FACTS
    slot = _first(r"data-kgraph-resolve-slot>(.*?)</p>", host)
    assert slot == "Resolving a contradiction is not yet a Lens action."
    assert host.index("data-kgraph-cards") < host.index("data-kgraph-resolve-slot")
    # Reserved, not offered: no form, no button, no write.
    assert "<form" not in host and "<button" not in host
    assert re.search(
        r'<p class="edge-why-rationale" data-edge-rationale>The legacy', host
    )


def test_a_resolved_contradiction_names_its_resolution(
    lithos_lens_config_env: Path,
) -> None:
    host = _host(
        _page(lithos_lens_config_env, f"{ROUTE}?type=contradicts&edge={RESOLVED}")
    )

    state = _first(r'data-kgraph-conflict-state="resolved">(.*?)</p>', host)
    assert _plain(state) == "Resolved: one note supersedes the other superseded"
    assert "data-kgraph-resolve-slot" in host


def test_created_and_updated_are_each_shown_as_stored(
    lithos_lens_config_env: Path,
) -> None:
    """D7: an edge updated after it was created shows each stamp as stored —
    the resolved contradiction's resolution came three days later."""
    row = _row(RESOLVED)
    assert row["created_at"] != row["updated_at"]
    query = f"type=contradicts&edge={RESOLVED}"
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?{query}")
        fragment = _fragment(client, query, page)

    for panel in (_host(page), fragment):
        assert f"<dt>Created</dt><dd>{row['created_at']}</dd>" in panel
        assert f"<dt>Updated</dt><dd>{row['updated_at']}</dd>" in panel
        provenance = _plain(_first(r"<div data-kgraph-provenance>(.*?)</div>", panel))
        assert provenance == (
            f"Provenance inferred by {row['provenance_actor']} · type inferred "
            f"· actor {row['provenance_actor']}"
        )


def test_a_callers_marker_is_unresolved_shown_as_written(
    lithos_lens_config_env: Path,
) -> None:
    marked = {
        **_row(CONTRADICTION),
        "edge_id": "edge_marked001",
        "conflict_state": "pending",
    }
    host = _host(
        _page(
            lithos_lens_config_env,
            f"{ROUTE}?type=contradicts&edge=edge_marked001",
            _ExtraEdges(marked),
        )
    )

    state = _first(r'data-kgraph-conflict-state="unresolved">(.*?)</p>', host)
    assert _plain(state) == "Unresolved pending"


# ── what renders no panel ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        f"focus={PLAN}&selected=note-not-drawn",
        # In the snapshot, but not one of the focus's edges at depth 1.
        f"focus={PLAN}&edge=edge_9b2f61c0a4e8",
        # The weight filter hides it from a node selection (an ``edge=``
        # selection of it is drawn: see the entry-point tests).
        f"focus={PLAN}&selected={LEGACY}&min_weight=0.95",
    ],
)
def test_a_selection_not_drawn_renders_no_panel(
    lithos_lens_config_env: Path, query: str
) -> None:
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?{query}")
        fragment = _get(client, f"{PANEL}?{query}")

    assert _host(page) == ""
    assert 'data-kgraph-panel="none"' in fragment
    assert _plain(fragment) == "Not in this view."


@pytest.mark.parametrize(
    "edge_id",
    # Not a row of the snapshot; nor is a layer pair's synthetic id.
    ["edge_nope", f"wiki_link:{PLAN}->note-influx-runbook"],
)
def test_an_edge_the_snapshot_lacks_says_so_and_fetches_nothing(
    lithos_lens_config_env: Path, edge_id: str
) -> None:
    """f-002: no panel, and the host says the edge is not in the current
    snapshot and when that refreshes — with no fetch spent looking."""
    query = f"focus={PLAN}&edge={edge_id}"
    with _client(lithos_lens_config_env) as client:
        _get(client, f"{ROUTE}?focus={PLAN}")
        table = cast(Any, client.app).state.lens.edge_table
        fetches = table.fetches
        page = _get(client, f"{ROUTE}?{query}")
        fragment = _get(client, f"{PANEL}?{query}")
        assert table.fetches == fetches

    for text in map(html_lib.unescape, (_plain(_host(page)), _plain(fragment))):
        assert text.startswith(f"Edge {edge_id} is not in the current edge snapshot")
        assert text.endswith(
            "refreshes every 300 s, so a new edge appears within that window."
        )


def test_a_refused_view_or_the_picker_has_no_panel(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "graph_focus_max_nodes = 2")
    with _client(lithos_lens_config_env) as client:
        refused = _get(client, f"{ROUTE}?focus={PLAN}&selected={PLAN}")
        refused_fragment = _get(client, f"{PANEL}?focus={PLAN}&selected={PLAN}")
        picker_fragment = _get(client, f"{PANEL}?selected={PLAN}")

    assert 'data-kgraph-refusal="too_many_nodes"' in refused
    assert "data-kgraph-panel=" not in refused
    assert _plain(refused_fragment) == "Not in this view."
    assert _plain(picker_fragment) == "Not in this view."


class _ReadRecorder(FakeLithosClient):
    """Recording every graph data read it is asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.data_reads: list[str] = []

    async def edge_list(self, **filters: Any) -> tuple[KnowledgeEdge, ...]:
        self.data_reads.append("edge_list")
        return await super().edge_list(**filters)

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        self.data_reads.append(f"read_note:{knowledge_id}:{max_length}")
        return await super().read_note(knowledge_id, max_length=max_length)

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        self.data_reads.append("related")
        return await super().related(knowledge_id)


class _Offline(_ReadRecorder):
    async def health(self) -> Any:
        return "unreachable"


def test_an_offline_fragment_says_so_and_reads_nothing(
    lithos_lens_config_env: Path,
) -> None:
    fake = _Offline()
    with _client(lithos_lens_config_env, fake) as client:
        fragment = _get(client, f"{PANEL}?focus={PLAN}&selected={PLAN}")

    assert _plain(fragment) == "Lithos is offline."
    assert fake.data_reads == []


# ── the fragment equals the page's panel ───────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        f"focus={PLAN}&selected={PLAN}",
        f"focus={PLAN}&depth=2&min_weight=0&selected={ROLLBACK}",
        f"focus={CAPACITY}&selected={DANGLING_NOTE_ID}",
        f"focus={PLAN}&edge={REFINES}",
        f"type=contradicts&edge={CONTRADICTION}",
        f"namespace=influx&provenance=inferred&edge={SUPPORTS}",
    ],
)
def test_the_fragment_equals_the_server_rendered_panel(
    lithos_lens_config_env: Path, query: str
) -> None:
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?{query}")
        fragment = _fragment(client, query, page)
        hand_made = _get(client, f"{PANEL}?{query}").strip()

    assert "data-kgraph-panel=" in fragment
    assert "<html" not in fragment  # extends no layout
    # The page's own view, byte for byte: render id included.
    assert fragment == _host(page)
    # Without a render id the fragment draws the view afresh, as a full
    # request would now: the same panel under its own render id.
    assert _render_id(page) not in hand_made
    assert _unrendered(hand_made) == _unrendered(_host(page))


def test_a_click_reads_nothing_and_a_hand_made_fragment_reads_as_the_page(
    lithos_lens_config_env: Path,
) -> None:
    """A click names its page's view: no read at all. A fragment without one
    (or one drawn for another scope) makes the page's own reads — on a warm
    cache the one ``lithos_related`` a focus draw makes."""
    fake = _ReadRecorder()
    query = f"focus={PLAN}&edge={REFINES}"
    with _client(lithos_lens_config_env, fake) as client:
        focus_page = _get(client, f"{ROUTE}?focus={PLAN}")
        fake.data_reads.clear()
        _fragment(client, query, focus_page)
        click_reads = list(fake.data_reads)
        fake.data_reads.clear()
        _get(client, f"{PANEL}?{query}")
        hand_made_reads = list(fake.data_reads)
        fake.data_reads.clear()
        # The render id of a depth-1 view, sent with a depth-2 scope.
        _fragment(client, f"focus={PLAN}&depth=2&edge={REFINES}", focus_page)
        other_scope_reads = list(fake.data_reads)
        queue_page = _get(client, f"{ROUTE}?type=contradicts")
        fake.data_reads.clear()
        _fragment(client, f"type=contradicts&edge={CONTRADICTION}", queue_page)
        queue_click_reads = list(fake.data_reads)

    assert click_reads == []
    assert hand_made_reads == ["related"]
    # Not that view's scope: no panel from another view, and nothing read.
    assert other_scope_reads == []
    assert queue_click_reads == []


def test_the_fragment_is_its_pages_panel_whatever_another_tab_read(
    lithos_lens_config_env: Path,
) -> None:
    """f-002: each render spends its own facts cap from one shared cache, so
    another tab's render can read a node this page left unread. The click's
    fragment is drawn from this page's view, not from the cache as it is."""
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    query = f"focus={PLAN}&selected={ROLLBACK}"
    with _client(lithos_lens_config_env) as client:
        tab_a = _get(client, f"{ROUTE}?{query}")  # reads the focus only
        tab_b = _get(client, f"{ROUTE}?focus={PLAN}")  # reads the next node
        fragment = _fragment(client, query, tab_a)
        edge_query = f"focus={PLAN}&edge={BARE_CONTRADICTION}"  # plan ↔ rollback
        edge_fragment = _fragment(client, edge_query, tab_a)
        fresh = _get(client, f"{PANEL}?{query}").strip()

    # Tab B read the rollback: the cache now has its facts.
    assert any(
        n["id"] == ROLLBACK and n["facts_state"] == "ok"
        for n in _payload(tab_b)["nodes"]
    )
    # Tab A's click still shows tab A's rollback: unread, by its id.
    assert "Facts not read for this view." in _host(tab_a)
    assert fragment == _host(tab_a)
    assert "Facts not read for this view." in _card(edge_fragment, ROLLBACK)
    # A fragment with no page behind it draws now, as a reload would.
    assert "Facts not read for this view." not in fresh


class _Ticks:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_fragment_is_its_pages_panel_after_the_facts_ttl(
    lithos_lens_config_env: Path,
) -> None:
    """A page left open past the facts TTL: its click still shows the facts
    the page showed, not the cache's expired entry marked pending."""
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    ticks = _Ticks()
    query = f"focus={ROLLBACK}&selected={PLAN}"
    with _client(lithos_lens_config_env) as client:
        lens = client.app.state.lens  # type: ignore[attr-defined]
        upstream = lens.lithos_client
        lens.note_facts = NoteFactsCache(
            lambda note_id: upstream.read_note(note_id, max_length=1),
            graph_fanout_gate,
            ttl_s=10,
            fanout_cap=1,
            ticks=ticks,
        )
        _get(client, f"{ROUTE}?focus={PLAN}")  # caches the plan
        page = _get(client, f"{ROUTE}?{query}")  # reads the rollback; plan a hit
        ticks.now += 11  # both entries expire
        fragment = _fragment(client, query, page)
        fresh = _get(client, f"{PANEL}?{query}").strip()

    assert _facts(_node_head(_host(page))) == PLAN_FACTS
    assert "Facts pending a re-read." not in _host(page)
    assert fragment == _host(page)
    # Drawn now, the plan's entry has expired and the cap re-reads the focus:
    # the plan is on its last-known facts. The click did not see that.
    assert "Facts pending a re-read." in fresh


def test_a_render_outside_focus_mode_is_found_whatever_depth_the_page_had(
    lithos_lens_config_env: Path,
) -> None:
    """f-002: depth means nothing outside focus mode, and the page's links
    leave it out; the view is kept under the scope as those links spell it,
    so the page's own hx-get still finds it."""
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    fake = _ReadRecorder()
    query = f"type=contradicts&depth=2&edge={BARE_CONTRADICTION}"
    with _client(lithos_lens_config_env, fake) as client:
        page = _get(client, f"{ROUTE}?{query}")
        for _ in range(3):  # another tab reads the endpoints this page did not
            _get(client, f"{ROUTE}?type=contradicts")
        link = _link(
            _first(rf'data-kgraph-edge="{BARE_CONTRADICTION}">(.*?)</li>', page),
            'class="kgraph-edge-link"',
        )
        hx_get = _attr(link, "hx-get")
        assert "depth" not in hx_get
        fake.data_reads.clear()
        fragment = _get(client, hx_get).strip()

    assert fake.data_reads == []
    assert fragment == _host(page)


def test_a_global_node_panel_and_its_click_chain_word_centre_alike(
    lithos_lens_config_env: Path,
) -> None:
    """f-002: depth draws nothing outside focus mode and the page's links leave
    it out, so the parser drops it too: the full page's node panel and the
    same panel reached by its own edge → Node details clicks are one panel,
    "Centre on this" included."""
    query = f"type=contradicts&depth=2&selected={PLAN}"
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?{query}")
        edge_link = _link(
            _first(rf'data-kgraph-edge="{RESOLVED}">(.*?)</li>', page),
            'class="kgraph-edge-link"',
        )
        edge_fragment = _get(client, _attr(edge_link, "hx-get")).strip()
        details = _link(_card(edge_fragment, PLAN), "data-kgraph-node-details")
        node_fragment = _get(client, _attr(details, "hx-get")).strip()

    centre = r'<a href="([^"]+)" data-kgraph-centre>Centre on this</a>'
    assert _first(centre, _host(page)) == f"{ROUTE}?focus={PLAN}"
    assert _first(centre, node_fragment) == f"{ROUTE}?focus={PLAN}"
    assert node_fragment == _host(page)


def test_depth_is_dropped_outside_focus_mode() -> None:
    assert parse_knowledge_graph_params({"type": "x", "depth": "2"}).depth is None
    assert parse_knowledge_graph_params({"focus": PLAN, "depth": "2"}).depth == 2


class _FlakyHealth(_ReadRecorder):
    """Healthy for ``ok_probes`` more probes once armed, then unreachable."""

    def __init__(self) -> None:
        super().__init__()
        self.ok_probes: int | None = None

    async def health(self) -> Any:
        if self.ok_probes is None:
            return "ok"
        if self.ok_probes > 0:
            self.ok_probes -= 1
            return "ok"
        return "unreachable"


def test_an_outage_the_assemblys_own_probe_sees_is_offline(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """f-004: a fragment with no render id probes health, then the shared
    assembly probes again; when only that second probe sees Lithos gone, the
    answer is still "Lithos is offline", not "Not in this view"."""
    fake = _FlakyHealth()
    with _client(lithos_lens_config_env, fake) as client:
        start = time.monotonic() + 100
        clock = (start + 100 * step for step in range(1_000))  # every probe due
        monkeypatch.setattr("lithos_lens.state.monotonic", lambda: next(clock))
        fake.ok_probes = 1
        fake.data_reads.clear()
        fragment = _get(client, f"{PANEL}?focus={PLAN}&selected={PLAN}")

    assert fake.ok_probes == 0  # the first probe was healthy
    assert _plain(fragment) == "Lithos is offline."
    assert fake.data_reads == []


def _evicted(client: TestClient, page: str, query: str) -> Any:
    return client.get(f"{PANEL}?{query}&render={_render_id(page)}")


def test_a_render_no_longer_held_reloads_the_page_rather_than_mixing_views(
    lithos_lens_config_env: Path, metric_reader: InMemoryMetricReader
) -> None:
    """f-002: tab A's view evicted by tab B's reloads (nothing concurrent,
    the store never over its bound). A's click must not swap a panel drawn
    from another view beside A's old graph: htmx is sent to the full page
    with that selection, which draws graph and panel together."""
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    fake = _ReadRecorder()
    query = f"focus={PLAN}&selected={ROLLBACK}"
    with _client(lithos_lens_config_env, fake) as client:
        tab_a = _get(client, f"{ROUTE}?{query}")
        for _ in range(RENDERED_VIEWS_KEPT):
            _get(client, f"{ROUTE}?focus={PLAN}")
        fake.data_reads.clear()
        response = _evicted(client, tab_a, query)

    assert response.status_code == 200
    target = f"{ROUTE}?{query}"
    assert response.headers["HX-Redirect"] == target
    assert fake.data_reads == []
    # No panel from another view; a client that does not follow the header
    # gets the same way back.
    assert 'data-kgraph-panel="none"' in response.text
    assert f'href="{target.replace("&", "&amp;")}" data-kgraph-reload' in response.text
    assert "Influx rollback route" not in response.text
    assert _opens(metric_reader, "node", "fragment") == 0


def test_a_render_lost_to_a_restart_reloads_the_page(
    lithos_lens_config_env: Path,
) -> None:
    query = f"focus={PLAN}&edge={REFINES}"
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?{query}")
    with _client(lithos_lens_config_env) as restarted:
        response = _evicted(restarted, page, query)

    assert response.headers["HX-Redirect"] == f"{ROUTE}?{query}"
    assert 'data-kgraph-panel="edge"' not in response.text


def test_an_outage_after_the_page_answers_offline_even_for_a_held_view(
    lithos_lens_config_env: Path, metric_reader: InMemoryMetricReader
) -> None:
    """f-004: the offline check comes before a held view, as on the page."""
    fake = _ReadRecorder()
    query = f"focus={PLAN}&selected={PLAN}"
    with _client(lithos_lens_config_env, fake) as client:
        page = _get(client, f"{ROUTE}?{query}")
        # A full-page request has seen Lithos go away.
        client.app.state.lens.health.lithos = "unreachable"  # type: ignore[attr-defined]
        offline_page = _get(client, f"{ROUTE}?{query}")
        fake.data_reads.clear()
        fragment = _fragment(client, query, page)

    assert "The knowledge graph is unavailable." in offline_page
    assert _plain(fragment) == "Lithos is offline."
    assert 'data-kgraph-panel="node"' not in fragment
    assert fake.data_reads == []
    assert _opens(metric_reader, "node", "fragment") == 0
    assert _opens(metric_reader, "node", "url") == 1  # the healthy page only


def test_a_cold_fragment_draws_what_a_full_request_draws(
    lithos_lens_config_env: Path,
) -> None:
    """No page behind it (a restart, a hand-made URL): the fragment makes the
    page's reads, facts included, and shows what a full request shows."""
    query = f"focus={PLAN}&selected={PLAN}"
    with _client(lithos_lens_config_env) as client:
        cold = _get(client, f"{PANEL}?{query}").strip()
    with _client(lithos_lens_config_env) as client:
        page = _host(_get(client, f"{ROUTE}?{query}"))

    assert _facts(_node_head(cold)) == PLAN_FACTS
    assert _unrendered(cold) == _unrendered(page)


def test_an_unread_selection_costs_no_read_on_the_page_or_the_fragment(
    lithos_lens_config_env: Path,
) -> None:
    """D1: no facts lookup for an unread node. From a cold, capped cache the
    page — and a fragment with no page behind it — reads exactly its cap
    (the focus); a click on the page reads nothing at all."""
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    fake = _ReadRecorder()
    query = f"focus={PLAN}&selected={CAPACITY}"
    with _client(lithos_lens_config_env, fake) as client:
        page = _get(client, f"{ROUTE}?{query}")
        page_reads = list(fake.data_reads)
        fake.data_reads.clear()
        fragment = _fragment(client, query, page)
        click_reads = list(fake.data_reads)
    cold_fake = _ReadRecorder()
    with _client(lithos_lens_config_env, cold_fake) as client:
        cold_fragment = _get(client, f"{PANEL}?{query}")

    cap_read = ["edge_list", "related", f"read_note:{PLAN}:1"]
    assert page_reads == cap_read
    assert "Facts not read for this view." in _host(page)
    assert click_reads == []
    assert fragment == _host(page)
    assert cold_fake.data_reads == cap_read
    assert "Facts not read for this view." in cold_fragment


def test_rendered_views_are_kept_per_scope_and_bounded() -> None:
    views = RenderedViews(size=2)
    focus = KnowledgeGraphParams(focus=PLAN)
    refines = KnowledgeGraphEdge(
        REFINES, PLAN, LEGACY, "typed", "refines", EdgeDirection.DIRECTED
    )
    view = KnowledgeGraphView(
        mode="focus", filters=KnowledgeGraphFilters(), edges=(refines,)
    )
    first = views.keep(replace(focus, selected=PLAN), view)
    second = views.keep(focus, view)

    # Found for the same scope whatever drawn selection; not for another scope.
    assert views.get(first, replace(focus, edge=REFINES)) is view
    assert views.get(first, replace(focus, depth=2)) is None
    assert views.get("", focus) is None and views.get("nope", focus) is None
    # Bounded, least recently used out: first was just used, second goes.
    third = views.keep(focus, view)
    assert views.get(second, focus) is None
    assert views.get(first, focus) is view and views.get(third, focus) is view
    assert len({first, second, third}) == 3


def test_a_kept_view_answers_only_selections_that_keep_its_drawing() -> None:
    """Review f-004: a view whose ``edge=`` was drawn only by its exemption
    answers clicks that pin that edge; a click pinning another edge (or none)
    would draw without it, so it reloads the page instead. A view no
    exemption changed answers no pin and edges it draws, not an edge it
    does not (which might gain an exemption)."""
    views = RenderedViews()
    focus = KnowledgeGraphParams(focus=PLAN)
    refines = KnowledgeGraphEdge(
        REFINES, PLAN, LEGACY, "typed", "refines", EdgeDirection.DIRECTED
    )
    faint = replace(refines, id="edge_faint")
    plain = KnowledgeGraphView(
        mode="focus", filters=KnowledgeGraphFilters(), edges=(refines,)
    )
    pinned = replace(plain, edges=(refines, faint), pinned="edge_faint")
    on_plain = views.keep(replace(focus, edge=REFINES), plain)
    on_pinned = views.keep(replace(focus, edge="edge_faint"), pinned)

    assert views.get(on_plain, focus) is plain
    assert views.get(on_plain, replace(focus, selected=PLAN, pin=REFINES)) is plain
    assert views.get(on_plain, replace(focus, edge="edge_faint")) is None

    assert views.get(on_pinned, replace(focus, edge="edge_faint")) is pinned
    assert views.get(on_pinned, replace(focus, selected=PLAN, pin="edge_faint")) is (
        pinned
    )
    assert views.get(on_pinned, replace(focus, edge=REFINES)) is None
    assert views.get(on_pinned, replace(focus, selected=PLAN)) is None


def _attr(tag: str, name: str) -> str:
    return _first(rf'{name}="([^"]*)"', tag).replace("&amp;", "&")


def _link(html: str, marker: str) -> str:
    """The opening ``<a …>`` tag carrying ``marker``."""
    return _first(rf"(<a [^>]*{marker}[^>]*>)", html)


@pytest.mark.parametrize(
    ("query", "edge_id", "endpoint"),
    [
        (f"focus={PLAN}&depth=2&min_weight=0.65&provenance=inferred", REFINES, LEGACY),
        ("namespace=influx&min_weight=0.65", CONTRADICTION, CAPACITY),
        ("type=contradicts&namespace=influx&min_weight=0.75", RESOLVED, PLAN),
    ],
)
def test_the_links_the_page_emits_carry_its_scope_and_filters(
    lithos_lens_config_env: Path, query: str, edge_id: str, endpoint: str
) -> None:
    """Follow the page's own hx-get, not a hand-built URL: the fragment it
    names is the page's panel for the matching href, scope and filters
    carried — then the same for the panel's Node details link."""
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?{query}")
        edge_link = _link(
            _first(rf'data-kgraph-edge="{edge_id}">(.*?)</li>', page),
            'class="kgraph-edge-link"',
        )
        render_id = _render_id(page)
        href, hx_get = _attr(edge_link, "href"), _attr(edge_link, "hx-get")
        assert href == f"{ROUTE}?{query}&edge={edge_id}"
        assert hx_get == f"{PANEL}?{query}&edge={edge_id}&render={render_id}"
        assert _attr(edge_link, "hx-push-url") == href
        edge_fragment = _get(client, hx_get).strip()
        assert _unrendered(edge_fragment) == _unrendered(_host(_get(client, href)))

        details = _link(_card(edge_fragment, endpoint), "data-kgraph-node-details")
        node_href, node_get = _attr(details, "href"), _attr(details, "hx-get")
        node_query = f"{query}&selected={endpoint}&pin={edge_id}"
        assert node_href == f"{ROUTE}?{node_query}"
        assert node_get == f"{PANEL}?{node_query}&render={render_id}"
        node_fragment = _get(client, node_get).strip()
        assert _unrendered(node_fragment) == _unrendered(_host(_get(client, node_href)))
        unfiltered = _get(
            client, f"{PANEL}?focus={endpoint}&depth=2&min_weight=0&selected={endpoint}"
        )

    # The filter changes what is drawn: fewer relations than unfiltered.
    drawn = sum(len(ids) for ids in _relations(node_fragment).values())
    assert 0 < drawn < sum(len(ids) for ids in _relations(unfiltered).values())


def test_text_baseline_edge_links_fetch_their_panel_with_htmx(
    lithos_lens_config_env: Path,
) -> None:
    html = _page(lithos_lens_config_env, f"{ROUTE}?focus={PLAN}&selected={PLAN}")

    entry = _first(rf'data-kgraph-edge="{REFINES}">(.*?)</li>', html)
    # The href stays the no-JS baseline; edge= replaces the page's selected=.
    assert f'href="{ROUTE}?focus={PLAN}&amp;edge={REFINES}"' in entry
    render_id = _render_id(html)
    assert (
        f'hx-get="{PANEL}?focus={PLAN}&amp;edge={REFINES}&amp;render={render_id}"'
        in (entry)
    )
    assert 'hx-target="#kgraph-panel" hx-swap="innerHTML"' in entry
    # One sync owner for every panel link: a later click aborts the request
    # still in flight from any other link (f-001).
    assert 'hx-sync="#kgraph-panel:replace"' in entry
    assert f'hx-push-url="{ROUTE}?focus={PLAN}&amp;edge={REFINES}"' in entry
    queue = _page(lithos_lens_config_env, f"{ROUTE}?type=contradicts")
    queue_entry = _first(rf'data-kgraph-edge="{CONTRADICTION}">(.*?)</li>', queue)
    queue_get = f"{PANEL}?type=contradicts&amp;edge={CONTRADICTION}"
    assert f'hx-get="{queue_get}&amp;render={_render_id(queue)}"' in queue_entry


# ── telemetry ──────────────────────────────────────────────────────────


def _opens(reader: InMemoryMetricReader, kind: str, source: str) -> int:
    labels = {"kind": kind, "source": source}
    return sum(
        int(point.value)
        for point in metric_points(reader, "lens_knowledge_graph_panel_opens_total")
        if dict(point.attributes or {}) == labels
    )


def test_panel_opens_are_counted_by_kind_and_source(
    lithos_lens_config_env: Path, metric_reader: InMemoryMetricReader
) -> None:
    with _client(lithos_lens_config_env) as client:
        _get(client, f"{ROUTE}?focus={PLAN}&selected={PLAN}")
        _get(client, f"{ROUTE}?focus={PLAN}&edge={REFINES}")
        _get(client, f"{PANEL}?focus={PLAN}&edge={REFINES}")
        _get(client, f"{PANEL}?focus={PLAN}&edge={SUPPORTS}")
        _get(client, f"{PANEL}?focus={PLAN}&selected={CAPACITY}")
        # Not in this view: not an open.
        _get(client, f"{ROUTE}?focus={PLAN}&selected=zzsecretzz")
        _get(client, f"{PANEL}?focus={PLAN}&edge=zzsecretzz")

    assert _opens(metric_reader, "node", "url") == 1
    assert _opens(metric_reader, "edge", "url") == 1
    assert _opens(metric_reader, "edge", "fragment") == 2
    assert _opens(metric_reader, "node", "fragment") == 1
    points = metric_points(metric_reader, "lens_knowledge_graph_panel_opens_total")
    assert all(
        set(dict(point.attributes or {})) == {"kind", "source"} for point in points
    )
    # The fragment is not a page render.
    renders = snapshot_value(
        metric_snapshot(metric_reader),
        "lens_knowledge_graph_renders_total",
        mode="focus",
        outcome="rendered",
    )
    assert renders.value == 3
    data = metric_reader.get_metrics_data()
    assert data is not None
    labels = str(
        [
            dict(point.attributes or {})
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            for point in metric.data.data_points
        ]
    )
    assert "zzsecretzz" not in labels
    assert PLAN not in labels and REFINES not in labels
