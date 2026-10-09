"""Browser-side behaviour of the knowledge graph's canvas (knowledge_graph.js).

The pattern of ``tests/test_tasks_js.py``: the real static file and the
SHIPPED Cytoscape bundle run inside Node with a stub DOM, headless. What it
pins has no server half — how each control turns into a URL, what the canvas
draws and how, what a click fetches and pushes — so it can only be asked of
the script itself.

Every case starts from a page the real route rendered (TestClient over the
fake's demo knowledge dataset): its embedded payload, the controls its
toolbar offers and its render id. So "the drawn sets equal the payload's
after each filter" (S4 D3) is asked of the server's own payload for each
control's URL, and the URL a control builds is handed back to the server to
read.

Node is the same runtime the ``e2e/`` suite needs; the tests skip without it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.web import create_app

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

STATIC = Path(__file__).resolve().parents[1] / "src/lithos_lens/static"
KNOWLEDGE_GRAPH_JS = STATIC / "knowledge_graph.js"
CYTOSCAPE_JS = STATIC / "vendor/cytoscape.min.js"

PLAN = "note-influx-plan"
CAPACITY = "note-influx-capacity"
LEGACY = "note-influx-legacy-ingest"
ROLLBACK = "note-influx-rollback"
ROUTE = "/knowledge/graph"
#: legacy → plan ``related_to`` at weight 0.03: below the default 0.1, so
#: only its ``edge=`` exemption draws it (S6 D13).
FAINT_EDGE = "edge_15d0c3e8f972"
UNRESOLVED = "edge_e1f4a8c27b90"
RESOLVED = "edge_b6e0f27d4c18"

_PAYLOAD = re.compile(
    r'<script type="application/json" data-knowledge-graph-payload>(.*?)</script>',
    re.S,
)

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const [scriptPath, cytoscapePath, href, payloadRaw, domRaw, actionsRaw] =
  process.argv.slice(1);
const dom = JSON.parse(domRaw);
const actions = JSON.parse(actionsRaw);
const url = new URL(href, "http://lens.test");
const assigns = [];
const ajax = [];
const layouts = [];

function el(extra) {
  return Object.assign({
    dataset: {},
    attributes: {},
    style: {},
    hidden: false,
    textContent: "",
    value: "",
    checked: false,
    disabled: false,
    className: "",
    children: [],
    listeners: {},
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    fire(type) {
      (this.listeners[type] || []).forEach((fn) => fn({ target: this }));
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) {
      return name in this.attributes ? this.attributes[name] : null;
    },
    replaceChildren(...kids) { this.children = kids; },
    appendChild(kid) { this.children.push(kid); return kid; },
  }, extra || {});
}

// The toolbar as the TEMPLATE rendered it: which provenance groups, depth
// levels and colour modes it offers comes from the page's own HTML.
const provenance = dom.provenance.map((group) =>
  el({ dataset: { kgraphProvenance: group }, value: group }));
const depths = dom.depths.map((level) => el({ value: String(level) }));
const depthCounts = dom.depths.map((level) =>
  el({ dataset: { kgraphDepthCount: String(level) } }));
const colours = dom.colours.map((mode) => el({ value: mode }));
const single = {
  "[data-kgraph-canvas]": el({ hidden: true }),
  "[data-knowledge-graph-payload]": el({ textContent: payloadRaw }),
  "[data-kgraph-toolbar]": el({ hidden: true }),
  "[data-kgraph-key]": el({ hidden: true }),
  "[data-kgraph-key-edges]": el(),
  "[data-kgraph-key-nodes]": el(),
  "[data-kgraph-key-marks]": el(),
  "[data-kgraph-min-weight]": el(),
  "[data-kgraph-min-weight-value]": el(),
  "[data-kgraph-weight-hidden]": el(),
  "[data-kgraph-search]": el(),
  "[data-kgraph-search-count]": el(),
  "[data-kgraph-panel-source]": el({ attributes: { "hx-sync": dom.sync } }),
  "#kgraph-panel": el({ dataset: { kgraphRender: dom.render } }),
};
const many = {
  "[data-kgraph-provenance]": provenance,
  "[data-kgraph-depth]": depths,
  "[data-kgraph-depth-count]": depthCounts,
  "[data-kgraph-colour]": colours,
};
const document = {
  readyState: "interactive",
  querySelector(selector) { return single[selector] || null; },
  querySelectorAll(selector) { return many[selector] || []; },
  createElement(tag) { return el({ tagName: tag }); },
  addEventListener() {},
};

// Timers never fire: the headless renderer's animation loop asks for one,
// and nothing this file does waits on a timer.
const sandbox = {
  document, console, URL, URLSearchParams,
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
};
sandbox.window = {
  location: {
    pathname: url.pathname,
    search: url.search,
    href: url.href,
    assign(target) { assigns.push(target); },
  },
  htmx: {
    ajax(verb, path, context) {
      ajax.push({
        verb,
        path,
        target: context.target,
        swap: context.swap,
        push: context.source.getAttribute("hx-push-url"),
        sync: context.source.getAttribute("hx-sync"),
      });
      return Promise.resolve();
    },
  },
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(cytoscapePath, "utf8"), sandbox);
sandbox.__recordLayout = (name) => layouts.push(name);
vm.runInContext(`
  window.cytoscape = function (options) {
    var cy = cytoscape(Object.assign({}, options, {
      container: null, headless: true, styleEnabled: true
    }));
    var layout = cy.layout.bind(cy);
    cy.layout = function (opts) { __recordLayout(opts.name); return layout(opts); };
    return cy;
  };
`, sandbox);
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), sandbox);

const graph = sandbox.window.LithosLensKnowledgeGraph;
const cy = graph.cy;
const byPid = (collection, pid) => collection.filter((e) => e.data("pid") === pid);
const pidsWith = (name) => cy.elements("." + name).map((e) => e.data("pid")).sort();

for (const action of actions) {
  const [kind, ...rest] = action.split(":");
  const arg = rest.join(":");
  if (kind === "slider-input" || kind === "slider-change") {
    const slider = single["[data-kgraph-min-weight]"];
    slider.value = arg;
    slider.fire(kind === "slider-input" ? "input" : "change");
  } else if (kind === "provenance") {
    const [group, state] = arg.split("=");
    const toggle = provenance.find((t) => t.dataset.kgraphProvenance === group);
    toggle.checked = state === "on";
    toggle.fire("change");
  } else if (kind === "depth" || kind === "colour") {
    const radios = kind === "depth" ? depths : colours;
    radios.forEach((radio) => { radio.checked = radio.value === arg; });
    radios.find((radio) => radio.value === arg).fire("change");
  } else if (kind === "search") {
    const search = single["[data-kgraph-search]"];
    search.value = arg;
    search.fire("input");
  } else if (kind === "tap-node") {
    byPid(cy.nodes(), arg).emit("tap");
  } else if (kind === "tap-edge") {
    byPid(cy.edges(), arg).emit("tap");
  } else if (kind === "tap-background") {
    cy.emit("tap");
  } else {
    throw new Error("unknown action " + action);
  }
}

const style = (e, name) => e.pstyle(name).strValue;
const canvas = single["[data-kgraph-canvas]"];
const textOf = (node) => node.children.length
  ? node.children.map(textOf).join("")
  : node.textContent;
console.log(JSON.stringify({
  nodes: cy.nodes().map((n) => ({
    id: n.data("pid"),
    label: n.data("label"),
    size: n.data("size"),
    colour: style(n, "background-color"),
    borderColour: style(n, "border-color"),
    borderStyle: style(n, "border-style"),
    borderWidth: n.pstyle("border-width").pfValue,
    opacity: n.pstyle("opacity").value,
    position: n.position(),
  })),
  edges: cy.edges().map((e) => ({
    id: e.data("pid"),
    colour: style(e, "line-color"),
    lineStyle: style(e, "line-style"),
    arrow: style(e, "target-arrow-shape"),
    width: e.pstyle("width").pfValue,
    label: style(e, "label"),
    opacity: e.pstyle("opacity").value,
  })),
  lit: pidsWith("lit"),
  dimmed: pidsWith("dimmed"),
  matched: pidsWith("match"),
  layouts,
  assigns,
  ajax,
  canvas: { hidden: canvas.hidden, dataset: canvas.dataset },
  toolbarHidden: single["[data-kgraph-toolbar]"].hidden,
  keyHidden: single["[data-kgraph-key]"].hidden,
  slider: single["[data-kgraph-min-weight]"].value,
  sliderValue: single["[data-kgraph-min-weight-value]"].textContent,
  hiddenCount: single["[data-kgraph-weight-hidden]"].textContent,
  searchCount: single["[data-kgraph-search-count]"].textContent,
  provenance: Object.fromEntries(provenance.map((t) =>
    [t.dataset.kgraphProvenance, { checked: t.checked, disabled: t.disabled }])),
  depth: depths.filter((r) => r.checked).map((r) => r.value),
  depthCounts: Object.fromEntries(depthCounts.map((c) =>
    [c.dataset.kgraphDepthCount, c.textContent])),
  colour: colours.filter((r) => r.checked).map((r) => r.value),
  keyEdges: single["[data-kgraph-key-edges]"].children.map((li) => li.dataset.keyType),
  keyNodes: single["[data-kgraph-key-nodes]"].children
    .filter((li) => li.dataset.keyValue !== undefined)
    .map((li) => [li.dataset.keyValue, li.children[0].style.backgroundColor]),
  keyNodeText: single["[data-kgraph-key-nodes]"].children.map(textOf),
  keyMarks: single["[data-kgraph-key-marks]"].children.map((li) => li.dataset.keyMark),
  palettes: {
    edges: Object.values(graph.edgeColours).concat([graph.resolvedColour]),
    nodes: graph.nodePalette.concat([graph.nodeNeutral]),
  },
}));
"""


@dataclass(frozen=True)
class Page:
    """One page the route rendered: what the harness is handed."""

    url: str
    html: str
    payload: dict[str, Any]
    dom: dict[str, Any]


def _page(client: TestClient, url: str) -> Page:
    response = client.get(url)
    assert response.status_code == 200
    html = response.text
    match = _PAYLOAD.search(html)
    assert match is not None
    render = re.search(r'data-kgraph-render="([^"]*)"', html)
    sync = re.search(r'data-kgraph-panel-source hx-sync="([^"]*)"', html)
    assert render is not None and sync is not None
    dom = {
        "provenance": re.findall(r'data-kgraph-provenance="([^"]+)"', html),
        "depths": [
            int(v) for v in re.findall(r'value="(\d)" data-kgraph-depth[ >]', html)
        ],
        "colours": re.findall(r'value="([a-z]+)" data-kgraph-colour', html),
        "render": render.group(1),
        "sync": sync.group(1),
    }
    return Page(url, html, json.loads(match.group(1)), dom)


@contextmanager
def _lens(config_path: Path) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            load_config(config_path),
            lithos_client_factory=lambda _: FakeLithosClient(),
        )
    ) as client:
        yield client


def _run(page: Page, actions: Sequence[str] = (), payload: dict | None = None) -> dict:
    assert NODE is not None
    result = subprocess.run(
        [
            NODE,
            "-e",
            HARNESS,
            "--",
            str(KNOWLEDGE_GRAPH_JS),
            str(CYTOSCAPE_JS),
            page.url,
            json.dumps(payload or page.payload),
            json.dumps(page.dom),
            json.dumps(list(actions)),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    # The minified bundle's own line is in a stack trace's excerpt: left out.
    assert result.returncode == 0, "\n".join(
        line for line in result.stderr.splitlines() if len(line) < 300
    )
    # The LAST line: the library may print its own warnings first.
    return json.loads(result.stdout.strip().splitlines()[-1])


def _query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _rgb(hex_colour: str) -> str:
    value = hex_colour.lstrip("#")
    return "rgb({},{},{})".format(*(int(value[i : i + 2], 16) for i in (0, 2, 4)))


def _by_id(items: list[dict]) -> dict[str, dict]:
    return {item["id"]: item for item in items}


# ── What is drawn: exactly the payload, after every filter ─────────────

DRAWN_URLS = [
    f"{ROUTE}?focus={PLAN}",
    f"{ROUTE}?focus={PLAN}&depth=2",
    f"{ROUTE}?focus={PLAN}&depth=2&min_weight=0",
    f"{ROUTE}?focus={PLAN}&depth=2&min_weight=0.7",
    f"{ROUTE}?focus={PLAN}&depth=2&provenance=inferred",
    f"{ROUTE}?focus={PLAN}&depth=2&provenance=reinforced,other",
    f"{ROUTE}?focus={PLAN}&depth=2&colour=type",
    f"{ROUTE}?focus={PLAN}&edge={FAINT_EDGE}",
    f"{ROUTE}?type=contradicts",
    f"{ROUTE}?namespace=influx&min_weight=0.65",
]


@pytest.mark.parametrize("url", DRAWN_URLS)
def test_the_canvas_draws_exactly_the_payloads_nodes_and_edges(
    lithos_lens_config_env: Path, url: str
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, url)
    result = _run(page)

    assert sorted(n["id"] for n in result["nodes"]) == sorted(
        n["id"] for n in page.payload["nodes"]
    )
    assert sorted(e["id"] for e in result["edges"]) == sorted(
        e["id"] for e in page.payload["edges"]
    )
    assert result["canvas"]["dataset"]["canvasState"] == "ready"
    assert result["canvas"]["dataset"]["canvasNodes"] == str(len(page.payload["nodes"]))
    assert result["canvas"]["dataset"]["canvasEdges"] == str(len(page.payload["edges"]))
    assert not result["canvas"]["hidden"]
    assert not result["toolbarHidden"] and not result["keyHidden"]


def test_an_edge_only_its_exemption_draws_stays_drawn_under_the_slider(
    lithos_lens_config_env: Path,
) -> None:
    """S6's exemption: ``edge=`` draws its edge below the threshold, and the
    canvas does not second-guess the payload by hiding it client-side."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&edge={FAINT_EDGE}")
    result = _run(page)

    assert page.payload["filters"]["min_weight"] == 0.1
    assert FAINT_EDGE in {edge["id"] for edge in result["edges"]}
    assert result["slider"] == "0.1"


# ── Each control: one key of the URL, read back by the server ──────────


@dataclass(frozen=True)
class ControlCase:
    start: str
    actions: tuple[str, ...]
    expected: dict[str, str | None]  # key → value written (None: removed)


CONTROL_CASES = {
    "min-weight": ControlCase(
        f"?focus={PLAN}&selected={CAPACITY}",
        ("slider-change:0.35",),
        {"min_weight": "0.35", "selected": CAPACITY},
    ),
    "min-weight-zero": ControlCase(
        f"?focus={PLAN}", ("slider-change:0",), {"min_weight": "0"}
    ),
    "provenance-off": ControlCase(
        f"?focus={PLAN}&depth=2",
        ("provenance:reinforced=off",),
        {"provenance": "inferred,declared,other", "depth": "2"},
    ),
    "provenance-back-on": ControlCase(
        f"?focus={PLAN}&depth=2&provenance=inferred,declared,other",
        ("provenance:reinforced=on",),
        {"provenance": None, "depth": "2"},
    ),
    "provenance-add": ControlCase(
        f"?focus={PLAN}&depth=2&provenance=inferred",
        ("provenance:other=on",),
        {"provenance": "inferred,other"},
    ),
    "depth-2": ControlCase(f"?focus={PLAN}", ("depth:2",), {"depth": "2"}),
    "depth-1": ControlCase(f"?focus={PLAN}&depth=2", ("depth:1",), {"depth": "1"}),
    "colour-type": ControlCase(
        f"?focus={PLAN}&edge={UNRESOLVED}",
        ("colour:type",),
        {"colour": "type", "edge": UNRESOLVED},
    ),
    "colour-namespace": ControlCase(
        "?type=contradicts&colour=type", ("colour:namespace",), {"colour": None}
    ),
    "keeps-edge": ControlCase(
        f"?focus={PLAN}&edge={FAINT_EDGE}",
        ("slider-change:0.5",),
        {"min_weight": "0.5", "edge": FAINT_EDGE},
    ),
    "keeps-pin": ControlCase(
        f"?focus={PLAN}&selected={LEGACY}&pin={FAINT_EDGE}",
        ("depth:2",),
        {"depth": "2", "selected": LEGACY, "pin": FAINT_EDGE},
    ),
}


def _control_state(result: dict) -> dict[str, Any]:
    """What the toolbar shows, in the payload's own terms."""
    return {
        "min_weight": float(result["slider"]),
        "provenance": sorted(
            g for g, s in result["provenance"].items() if s["checked"]
        ),
        "depth": result["depth"],
        "colour": result["colour"],
    }


@pytest.mark.parametrize("case", CONTROL_CASES.values(), ids=CONTROL_CASES.keys())
def test_each_control_round_trips_through_the_url(
    lithos_lens_config_env: Path, case: ControlCase
) -> None:
    """State → URL: the control writes its one key and keeps every other —
    the selection and pin included (S4 D15). URL → state: the server reads
    that URL into the payload, and the toolbar drawn from it shows the very
    value the control was set to; the canvas draws that payload exactly."""
    with _lens(lithos_lens_config_env) as client:
        start = _page(client, f"{ROUTE}{case.start}")
        result = _run(start, list(case.actions))

        assert len(result["assigns"]) == 1, "a control navigates, once"
        target = result["assigns"][0]
        assert urlsplit(target).path == ROUTE
        before, after = _query(start.url), _query(target)
        for key, value in case.expected.items():
            if value is None:
                assert key not in after
            else:
                assert after[key] == value
        unchanged = {k: v for k, v in before.items() if k not in case.expected}
        assert {k: after.get(k) for k in unchanged} == unchanged

        landed = _page(client, target)
    shown = _run(landed)
    state = _control_state(shown)
    filters = landed.payload["filters"]

    assert state["min_weight"] == filters["min_weight"]
    if "min_weight" in case.expected:
        assert filters["min_weight"] == float(case.expected["min_weight"] or 0)
    offered = set(landed.dom["provenance"])
    assert state["provenance"] == sorted(offered & set(filters["provenance"]))
    if landed.payload["mode"] == "focus":
        assert state["depth"] == [str(landed.payload["depth"])]
        if "depth" in case.expected:
            assert landed.payload["depth"] == int(case.expected["depth"] or 0)
    assert state["colour"] == [landed.payload["colour"]]
    if "colour" in case.expected:
        assert landed.payload["colour"] == (case.expected["colour"] or "namespace")
    assert sorted(n["id"] for n in shown["nodes"]) == sorted(
        n["id"] for n in landed.payload["nodes"]
    )
    assert sorted(e["id"] for e in shown["edges"]) == sorted(
        e["id"] for e in landed.payload["edges"]
    )


def test_the_slider_shows_its_value_while_dragged_and_navigates_on_release(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}")

    dragging = _run(page, ["slider-input:0.45"])
    released = _run(page, ["slider-input:0.45", "slider-change:0.45"])

    assert dragging["sliderValue"] == "0.45"
    assert dragging["assigns"] == []
    assert released["assigns"] == [f"{ROUTE}?focus={PLAN}&min_weight=0.45"]


@pytest.mark.parametrize("weight", ["0.1", "0", "0.7", "1"])
def test_the_hidden_count_is_the_servers_in_the_servers_words(
    lithos_lens_config_env: Path, weight: str
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&depth=2&min_weight={weight}")
    result = _run(page)

    hidden = page.payload["hidden"]["by_weight"]
    line = re.search(r"<li data-hidden-weight>([^<]*?)(?: — |</li>)", page.html)
    assert line is not None
    assert result["hiddenCount"] == line.group(1).strip()
    assert result["hiddenCount"].startswith(f"{hidden} edge")
    assert result["sliderValue"] == result["hiddenCount"].split(" below ")[1].split()[0]


def test_the_last_provenance_group_on_cannot_be_turned_off(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&depth=2&provenance=inferred")
    result = _run(page)

    assert result["provenance"]["inferred"] == {"checked": True, "disabled": True}
    for group in ("reinforced", "declared", "other"):
        assert result["provenance"][group] == {"checked": False, "disabled": False}


def test_each_depth_not_drawn_states_its_node_count_before_it_is_asked_for(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        one = _page(client, f"{ROUTE}?focus={PLAN}")
        two = _page(client, f"{ROUTE}?focus={PLAN}&depth=2")

    at_one, at_two = _run(one), _run(two)
    would_be = one.payload["would_be_nodes"]

    assert at_one["depthCounts"] == {"1": "", "2": f": {would_be['2']} notes"}
    assert at_two["depthCounts"] == {
        "1": f": {two.payload['would_be_nodes']['1']} notes",
        "2": "",
    }
    # …and it is the count the server draws there.
    assert len(two.payload["nodes"]) == would_be["2"]


# ── How it is drawn ────────────────────────────────────────────────────


def test_each_edge_draws_the_way_the_legend_says(lithos_lens_config_env: Path) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&depth=2")
    result = _run(page)
    edges = _by_id(result["edges"])
    payload_edges = _by_id(page.payload["edges"])
    colours = result["palettes"]["edges"]

    # Arrowheads only on directed types (and as stored for an unknown one).
    for edge_id, edge in edges.items():
        directed = payload_edges[edge_id]["direction"] != "symmetric"
        assert edge["arrow"] == ("triangle" if directed else "none"), edge_id
    assert edges["edge_7d2c0e95b463"]["lineStyle"] == "dotted"  # derived_from
    # Unresolved: red, dashed, wider than its weight alone draws.
    unresolved = edges[UNRESOLVED]
    assert unresolved["colour"] == _rgb("#bd4f2b")
    assert unresolved["lineStyle"] == "dashed"
    assert unresolved["width"] > edges["edge_4c1e9a7b20d3"]["width"]  # 0.8 vs 0.82
    # Resolved: muted, dashed, labelled with its resolution.
    resolved = edges[RESOLVED]
    assert resolved["colour"] != unresolved["colour"]
    assert resolved["lineStyle"] == "dashed" and resolved["label"] == "superseded"
    # Unknown: neutral grey, labelled with the raw type.
    unknown = edges["edge_f29d84a6130c"]
    assert unknown["label"] == "assesses" and unknown["arrow"] == "triangle"
    # A wiki-link: thin grey solid.
    wiki = [e for e in page.payload["edges"] if e["kind"] == "wiki_link"]
    assert wiki
    for edge in wiki:
        drawn = edges[edge["id"]]
        assert (drawn["lineStyle"], drawn["width"]) == ("solid", 1)
    # One colour per known type: no two typed classes share one.
    typed_colours = {
        payload_edges[i]["style"]["class"]: e["colour"]
        for i, e in edges.items()
        if payload_edges[i]["kind"] == "typed"
    }
    assert len(set(typed_colours.values())) == len(typed_colours)
    assert all(colour.startswith("#") for colour in colours)


def test_weight_maps_linearly_to_width_and_no_weight_draws_thin(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?type=supports")
    payload = page.payload
    base = payload["edges"][0]
    weights = [None, 0.0, 0.1, 0.55, 1.0]
    payload = {
        **payload,
        "edges": [
            {**base, "id": f"w{i}", "weight": weight}
            for i, weight in enumerate(weights)
        ],
    }
    widths = [
        e["width"]
        for e in sorted(_run(page, payload=payload)["edges"], key=lambda e: e["id"])
    ]

    assert widths == pytest.approx([1, 1, 1, 3.5, 6])


def test_node_marks_size_by_degree_and_status(lithos_lens_config_env: Path) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&depth=2")
    nodes = page.payload["nodes"]
    # No fixture note is archived or pending: two nodes are made so here.
    marked = [
        {**node, "status": "archived"}
        if node["id"] == ROLLBACK
        else {**node, "facts_state": "pending"}
        if node["id"] == CAPACITY
        else node
        for node in nodes
    ]
    result = _run(page, payload={**page.payload, "nodes": marked})
    drawn = _by_id(result["nodes"])
    ghost = next(node["id"] for node in nodes if node["ghost"])

    by_degree = sorted(nodes, key=lambda node: node["degree"])
    assert drawn[by_degree[-1]["id"]]["size"] == 42
    assert drawn[by_degree[0]["id"]]["size"] == 18
    assert drawn[ghost]["borderStyle"] == "dashed"
    assert drawn[ghost]["label"] == next(n["label"] for n in nodes if n["ghost"])
    assert drawn[LEGACY]["borderColour"] == _rgb("#bd4f2b")  # quarantined
    assert drawn[LEGACY]["borderWidth"] == 3
    assert drawn[CAPACITY]["borderStyle"] == "double"  # pending, not dashed
    assert drawn[ROLLBACK]["opacity"] < 1  # archived: greyed
    assert result["keyMarks"] == ["ghost", "pending", "quarantined", "archived"]


@pytest.mark.parametrize("mode", ["namespace", "type"])
def test_nodes_colour_by_the_payloads_mode_from_a_palette_apart_from_the_edges(
    lithos_lens_config_env: Path, mode: str
) -> None:
    colour = "&colour=type" if mode == "type" else ""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&depth=2{colour}")
    result = _run(page)
    drawn = _by_id(result["nodes"])
    field = "note_type" if mode == "type" else "namespace"
    palette = result["palettes"]["nodes"]

    assert not set(map(str.lower, palette)) & set(
        map(str.lower, result["palettes"]["edges"])
    )
    by_value: dict[str | None, set[str]] = {}
    for node in page.payload["nodes"]:
        by_value.setdefault(node[field] or None, set()).add(drawn[node["id"]]["colour"])
    # One colour per value; a node with none (the ghost) is the neutral.
    assert all(len(colours) == 1 for colours in by_value.values())
    named = {value: colours.pop() for value, colours in by_value.items()}
    assert named[None] == _rgb(palette[-1])
    values = [value for value in named if value is not None]
    assert len({named[value] for value in values}) == len(values)
    # The key lists the values present, most nodes first, with their colours.
    counts = {
        value: sum(1 for n in page.payload["nodes"] if (n[field] or None) == value)
        for value in values
    }
    expected = sorted(values, key=lambda value: (-counts[value], value))
    assert [value for value, _ in result["keyNodes"] if value] == expected
    assert result["keyNodeText"][0] == (
        "Colour: note type" if mode == "type" else "Colour: namespace"
    )
    assert "Size: connections in this view" in result["keyNodeText"]


def test_the_canvas_key_lists_the_text_legends_types_in_its_order(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&depth=2")
    result = _run(page)

    text_legend = re.findall(r'data-legend-(?:type|layer)="([^"]+)"', page.html)
    assert result["keyEdges"] == [line["type"] for line in page.payload["legend"]]
    assert result["keyEdges"] == text_legend


@pytest.mark.parametrize(
    ("url", "layouts"),
    [
        (f"{ROUTE}?focus={PLAN}&depth=2", ["concentric"]),
        (f"{ROUTE}?type=contradicts", ["circle", "cose"]),
    ],
)
def test_the_layout_runs_once_concentric_around_a_focus_force_directed_otherwise(
    lithos_lens_config_env: Path, url: str, layouts: list[str]
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, url)
    result = _run(
        page, ["tap-node:" + page.payload["nodes"][0]["id"], "tap-background"]
    )

    assert result["layouts"] == layouts
    if page.payload["mode"] == "focus":
        positions = {node["id"]: node["position"] for node in result["nodes"]}
        hops = {node["id"]: node["hop"] for node in page.payload["nodes"]}
        centre = positions[PLAN]
        radius = {
            node_id: ((p["x"] - centre["x"]) ** 2 + (p["y"] - centre["y"]) ** 2) ** 0.5
            for node_id, p in positions.items()
        }
        # Each hop sits on a ring further out than the one before.
        for near in hops:
            for far in hops:
                if hops[near] < hops[far]:
                    assert radius[near] < radius[far]


# ── Clicks, lit and dimmed, search ─────────────────────────────────────


def test_a_node_click_opens_its_panel_through_htmx_and_pushes_the_page_url(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&colour=type")
        result = _run(page, [f"tap-node:{CAPACITY}"])

        assert result["assigns"] == []
        [call] = result["ajax"]
        render = page.dom["render"]
        assert (call["verb"], call["target"], call["swap"]) == (
            "GET",
            "#kgraph-panel",
            "innerHTML",
        )
        assert call["sync"] == "#kgraph-panel:replace"
        assert call["path"] == (
            f"{ROUTE}/panel?focus={PLAN}&colour=type&selected={CAPACITY}&render={render}"
        )
        assert call["push"] == f"{ROUTE}?focus={PLAN}&colour=type&selected={CAPACITY}"
        fragment = client.get(call["path"])

    assert "HX-Redirect" not in fragment.headers
    assert 'data-kgraph-panel="node"' in fragment.text
    assert f'data-kgraph-node="{CAPACITY}"' in fragment.text
    assert sorted(result["lit"]) == sorted(
        {CAPACITY, PLAN}
        | {e["id"] for e in page.payload["edges"] if CAPACITY in (e["from"], e["to"])}
        | {
            other
            for e in page.payload["edges"]
            if CAPACITY in (e["from"], e["to"])
            for other in (e["from"], e["to"])
        }
    )


def test_a_node_click_on_a_view_drawn_for_an_edge_pins_it(
    lithos_lens_config_env: Path,
) -> None:
    """S4 D14: the click builds ``pin`` as ``knowledge_graph_url`` does, so the
    view only that edge's exemption draws answers the fragment — no
    ``HX-Redirect`` to a page that would draw something else."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&edge={FAINT_EDGE}")
        [call] = _run(page, [f"tap-node:{LEGACY}"])["ajax"]
        query = _query(call["path"])

        assert query["pin"] == FAINT_EDGE
        assert "edge" not in query
        assert query["selected"] == LEGACY
        assert _query(call["push"]) == {k: v for k, v in query.items() if k != "render"}
        fragment = client.get(call["path"])

    assert fragment.status_code == 200
    assert "HX-Redirect" not in fragment.headers
    assert 'data-kgraph-panel="node"' in fragment.text


def test_an_edge_click_selects_the_edge_and_drops_the_node_and_pin(
    lithos_lens_config_env: Path,
) -> None:
    """``edge=`` with ``selected`` and ``pin`` removed, as the text's edge
    links build it: on a plain view the fragment is that edge's panel; on a
    view a pin drew, the edge's own view may draw otherwise, and the server
    sends the browser to that page (S6 D13) — exactly as for a text link."""
    with _lens(lithos_lens_config_env) as client:
        plain = _page(client, f"{ROUTE}?focus={PLAN}&selected={LEGACY}")
        [call] = _run(plain, [f"tap-edge:{RESOLVED}"])["ajax"]
        fragment = client.get(call["path"])
        pinned = _page(
            client, f"{ROUTE}?focus={PLAN}&selected={LEGACY}&pin={FAINT_EDGE}"
        )
        [from_pinned] = _run(pinned, [f"tap-edge:{RESOLVED}"])["ajax"]
        redirected = client.get(from_pinned["path"])

    assert _query(call["push"]) == {"focus": PLAN, "edge": RESOLVED}
    assert 'data-kgraph-panel="edge"' in fragment.text
    assert "HX-Redirect" not in fragment.headers
    assert _query(from_pinned["push"]) == {"focus": PLAN, "edge": RESOLVED}
    assert redirected.headers["HX-Redirect"] == from_pinned["push"]


def test_a_layer_edge_has_no_panel_and_its_click_does_nothing(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}")
    layer = next(e["id"] for e in page.payload["edges"] if e["kind"] != "typed")
    result = _run(page, [f"tap-edge:{layer}"])

    assert result["ajax"] == [] and result["assigns"] == []
    assert result["dimmed"] == []


@pytest.mark.parametrize(
    ("query", "lit"),
    [
        (f"&selected={ROLLBACK}", "node"),
        (f"&edge={UNRESOLVED}", "edge"),
        (f"&edge={UNRESOLVED}&selected={ROLLBACK}", "edge"),  # edge wins
        ("&edge=edge_not_in_the_snapshot", None),
        (f"&selected={LEGACY}&pin={FAINT_EDGE}", "node"),
    ],
)
def test_the_url_selection_lights_on_load_and_a_background_tap_clears_it(
    lithos_lens_config_env: Path, query: str, lit: str | None
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}{query}")
    loaded = _run(page)
    cleared = _run(page, ["tap-background"])
    edges = page.payload["edges"]
    every = {n["id"] for n in page.payload["nodes"]} | {e["id"] for e in edges}

    if lit is None:
        assert loaded["lit"] == [] and loaded["dimmed"] == []
    else:
        if lit == "edge":
            edge = next(e for e in edges if e["id"] == UNRESOLVED)
            expected = {UNRESOLVED, edge["from"], edge["to"]}
        else:
            node = _query(page.url)["selected"]
            at = [e for e in edges if node in (e["from"], e["to"])]
            expected = (
                {node}
                | {e["id"] for e in at}
                | {x for e in at for x in (e["from"], e["to"])}
            )
        assert set(loaded["lit"]) == expected
        assert set(loaded["dimmed"]) == every - expected
    assert cleared["lit"] == [] and cleared["dimmed"] == []
    assert cleared["assigns"] == [] and cleared["ajax"] == []


def test_search_highlights_the_titles_it_matches_and_clears(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?focus={PLAN}&depth=2")
    found = _run(page, ["search:  INFLUX "])
    none = _run(page, ["search:no such words"])
    cleared = _run(page, ["search:influx", "search:"])
    expected = sorted(
        n["id"] for n in page.payload["nodes"] if "influx" in n["label"].lower()
    )

    assert expected and found["matched"] == expected
    assert found["searchCount"] == f"{len(expected)} matches"
    assert none["matched"] == [] and none["searchCount"] == "0 matches"
    assert cleared["matched"] == [] and cleared["searchCount"] == ""
    # Search is not a URL key (S4 D11).
    assert found["assigns"] == [] and found["ajax"] == []
