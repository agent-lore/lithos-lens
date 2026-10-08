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

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from lithos_lens.config import load_config
from lithos_lens.fake_knowledge_dataset import DANGLING_NOTE_ID, knowledge_edge_rows
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge import RelatedNeighborhood
from lithos_lens.knowledge_edges import KnowledgeEdge, normalize_edge_list
from lithos_lens.knowledge_graph_routes import (
    KnowledgeGraphParams,
    knowledge_graph_panel_url,
    knowledge_graph_url,
    parse_knowledge_graph_params,
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
    r'<div id="kgraph-panel" class="kgraph-panel-host" data-kgraph-panel-host>'
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
    return match.group(1).strip()


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
    assert knowledge_graph_url(on_edge, selected=CAPACITY) == (
        f"{ROUTE}?focus={PLAN}&selected={CAPACITY}"
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
    assert f'hx-get="{PANEL}?focus={PLAN}&amp;edge={SUPPORTS}"' in host


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
        host = _host(_get(client, f"{ROUTE}?{query}"))
        fragment = _get(client, f"{PANEL}?{query}")

    assert "Facts pending a re-read." in host
    # Its last-known facts: the title the event set, marked, and the chips and
    # lede read before the event — not its id.
    assert _plain(_first(r"<h2>(.*?)</h2>", host)) == "v2 facts pending"
    assert _facts(_node_head(host)) == PLAN_FACTS
    assert "data-kgraph-full-id" not in host
    assert "Facts not read for this view." not in host
    assert fragment.strip() == host


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
    assert f'hx-get="{PANEL}?focus={PLAN}&amp;selected={LEGACY}"' in host


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
        f"focus={PLAN}&edge=edge_nope",
        f"focus={PLAN}&selected=note-not-drawn",
        # A layer pair's synthetic id is not a typed edge.
        f"focus={PLAN}&edge=wiki_link:{PLAN}->note-influx-runbook",
        # The weight filter hides it: not in this view.
        f"focus={PLAN}&edge=edge_15d0c3e8f972",
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
        fragment = _get(client, f"{PANEL}?{query}")

    assert "data-kgraph-panel=" in fragment
    assert "<html" not in fragment  # extends no layout
    assert _host(page) == fragment.strip()


def test_the_fragment_reads_nothing_the_page_does_not(
    lithos_lens_config_env: Path,
) -> None:
    """On a warm cache the fragment's reads are the view's own: the one
    ``lithos_related`` a focus draw makes, and no facts or table read."""
    fake = _ReadRecorder()
    with _client(lithos_lens_config_env, fake) as client:
        _get(client, f"{ROUTE}?focus={PLAN}")
        before = len(fake.data_reads)
        _get(client, f"{PANEL}?focus={PLAN}&edge={REFINES}")
        focus_reads = fake.data_reads[before:]
        _get(client, f"{ROUTE}?type=contradicts")
        before = len(fake.data_reads)
        _get(client, f"{PANEL}?type=contradicts&edge={CONTRADICTION}")
        global_reads = fake.data_reads[before:]

    assert focus_reads == ["related"]
    assert global_reads == []


def test_the_fragment_equals_the_page_under_the_facts_cap(
    lithos_lens_config_env: Path,
) -> None:
    """f-002: each render spends its own facts cap, so a fragment that read
    facts again would draw a node the page left unread. The fragment reads no
    facts: it shows what the page's render left in the cache."""
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    query = f"focus={PLAN}&selected={ROLLBACK}"
    edge_query = f"focus={PLAN}&edge={BARE_CONTRADICTION}"  # plan ↔ rollback
    with _client(lithos_lens_config_env) as client:
        page = _host(_get(client, f"{ROUTE}?{query}"))
        fragment = _get(client, f"{PANEL}?{query}").strip()
        edge_fragment = _get(client, f"{PANEL}?{edge_query}").strip()
        edge_page = _host(_get(client, f"{ROUTE}?{edge_query}"))

    # The page read only the focus: the rollback is unread, in both.
    assert "Facts not read for this view." in page
    assert f"data-kgraph-full-id>{ROLLBACK}</p>" in page
    assert fragment == page
    # The edge fragment, before the page's own next render reads the next
    # node, still shows the rollback card unread, as the page last drew it.
    assert "Facts not read for this view." in _card(edge_fragment, ROLLBACK)
    # That next render reads it (the cap's next read): page and fragment for
    # one render are compared, not two different renders.
    assert "Facts not read for this view." not in _card(edge_page, ROLLBACK)


def test_an_unread_selection_costs_no_read_on_the_page_or_the_fragment(
    lithos_lens_config_env: Path,
) -> None:
    """D1: no facts lookup for an unread node. From a cold, capped cache the
    page reads exactly its cap (the focus) and the fragment reads no note."""
    _set_knowledge(lithos_lens_config_env, "graph_title_fanout_cap = 1")
    fake = _ReadRecorder()
    query = f"focus={PLAN}&selected={CAPACITY}"
    with _client(lithos_lens_config_env, fake) as client:
        cold_fragment = _get(client, f"{PANEL}?{query}")
        cold_fragment_reads = list(fake.data_reads)
        fake.data_reads.clear()
        page = _host(_get(client, f"{ROUTE}?{query}"))
        page_reads = list(fake.data_reads)
        fake.data_reads.clear()
        fragment = _get(client, f"{PANEL}?{query}")
        fragment_reads = list(fake.data_reads)

    # Cold: the snapshot and the focus's related read, and no note at all.
    assert cold_fragment_reads == ["edge_list", "related"]
    assert "Facts not read for this view." in cold_fragment
    # The page: the cap's one read, the focus, by lithos_read(max_length=1).
    assert page_reads == ["related", f"read_note:{PLAN}:1"]
    assert "Facts not read for this view." in page
    # The fragment after it: the view's related read only.
    assert fragment_reads == ["related"]
    assert fragment.strip() == page


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
        href, hx_get = _attr(edge_link, "href"), _attr(edge_link, "hx-get")
        assert href == f"{ROUTE}?{query}&edge={edge_id}"
        assert hx_get == f"{PANEL}?{query}&edge={edge_id}"
        assert _attr(edge_link, "hx-push-url") == href
        edge_fragment = _get(client, hx_get).strip()
        assert edge_fragment == _host(_get(client, href))

        details = _link(_card(edge_fragment, endpoint), "data-kgraph-node-details")
        node_href, node_get = _attr(details, "href"), _attr(details, "hx-get")
        assert node_href == f"{ROUTE}?{query}&selected={endpoint}"
        assert node_get == f"{PANEL}?{query}&selected={endpoint}"
        node_fragment = _get(client, node_get).strip()
        assert node_fragment == _host(_get(client, node_href))
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
    assert f'hx-get="{PANEL}?focus={PLAN}&amp;edge={REFINES}"' in entry
    assert 'hx-target="#kgraph-panel" hx-swap="innerHTML"' in entry
    # One sync owner for every panel link: a later click aborts the request
    # still in flight from any other link (f-001).
    assert 'hx-sync="#kgraph-panel:replace"' in entry
    assert f'hx-push-url="{ROUTE}?focus={PLAN}&amp;edge={REFINES}"' in entry
    queue = _page(lithos_lens_config_env, f"{ROUTE}?type=contradicts")
    queue_entry = _first(rf'data-kgraph-edge="{CONTRADICTION}">(.*?)</li>', queue)
    assert f'hx-get="{PANEL}?type=contradicts&amp;edge={CONTRADICTION}"' in queue_entry


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
