"""Expansion on the canvas (K2 S9, PRD D16): the script's half.

The harness of ``tests/test_knowledge_graph_js.py`` — the real script and the
SHIPPED Cytoscape bundle, headless in Node — with ``fetch`` held until a case
answers it from a page the real route rendered for the very URL fetched. So
"the double click asks for what the panel's link names" and "the swap
commits what the server answered" are both asked of the server's own output.

What headless cannot see — measured title bounds, a real pointer's double
click, the keyboard and touch — is the browser suite's
(``e2e/tests/knowledge-graph-canvas.spec.ts``,
``e2e/tests/knowledge-graph-full-page.spec.ts``).

The demo set at ``min_weight=0.7`` around the rollback note draws it and the
plan; showing the plan's neighbours adds the capacity and legacy notes, and
then showing the capacity note's adds only the capacity–legacy edge.
"""

from __future__ import annotations

import copy
import html as html_lib
import json
import math
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_knowledge_graph_js import (
    CAPACITY,
    EVENTS,
    LEGACY,
    NODE,
    PLAN,
    ROLLBACK,
    ROUTE,
    TEXT_SELECTORS,
    UNDRAWN,
    Page,
    _frame,
    _lens,
    _page,
    _parts,
    _rgb,
    _run,
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

BASE = f"{ROUTE}?focus={ROLLBACK}&min_weight=0.7"
#: The view with the plan's neighbours shown, as the server's link writes it.
ONE = f"{ROUTE}?focus={ROLLBACK}&expand={PLAN}&min_weight=0.7&selected={PLAN}"
#: …and then the capacity note's: one edge more, no note.
TWO = (
    f"{ROUTE}?focus={ROLLBACK}&expand={PLAN}&expand={CAPACITY}"
    f"&min_weight=0.7&selected={CAPACITY}"
)
#: The plan collapsed from TWO: the base again, the plan still selected.
COLLAPSED = f"{ROUTE}?focus={ROLLBACK}&min_weight=0.7&selected={PLAN}"
SIZE_MAX = 42


def _href(html: str, marker: str) -> str:
    """The href of the first ``<a>`` carrying ``marker``, unescaped."""
    tag = re.search(rf"(<a [^>]*{marker}[^>]*>)", html)
    assert tag is not None, marker
    href = re.search(r'href="([^"]*)"', tag.group(1))
    assert href is not None
    return html_lib.unescape(href.group(1))


def _response(client: TestClient, url: str) -> dict[str, Any]:
    response = client.get(url)
    return {"status": response.status_code, "parts": _parts(response.text)}


def _with_payload(response: dict[str, Any], change: Any) -> dict[str, Any]:
    """``response`` with its payload as ``change`` rewrites it."""
    changed = copy.deepcopy(response)
    part = changed["parts"]["[data-knowledge-graph-payload]"]
    payload = json.loads(part["inner"])
    change(payload)
    part["inner"] = json.dumps(payload)
    return changed


def _payload_of(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["parts"]["[data-knowledge-graph-payload]"]["inner"])


def _render_of(response: dict[str, Any]) -> str:
    return response["parts"]["#kgraph-panel"]["attrs"]["data-kgraph-render"]


def _positions(mark: dict) -> dict[str, dict[str, float]]:
    return dict((pid, at) for pid, at in mark["positions"])


def _states(result: dict) -> list[dict]:
    return [mark for mark in result["marks"] if "busy" in mark]


def _expand(page_html: str) -> str:
    return _href(page_html, "data-kgraph-expand")


def _go(page: Page, responses: dict[str, Any], actions: list[str], **dom: Any) -> dict:
    return _run(page, actions, dom={"responses": responses, **dom})


# ── The double click and the links: what they ask for, when they push ──


@pytest.mark.parametrize("canvas", ["", "&canvas=full"], ids=["inline", "full-page"])
def test_a_double_click_fetches_the_panels_own_expansion_and_pushes_after_the_swap(
    lithos_lens_config_env: Path, canvas: str
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE + canvas)
        # The node's panel, as the server renders it: its Show its
        # neighbours link is the address a double click must ask for.
        href = _expand(client.get(f"{BASE}&selected={PLAN}{canvas}").text)
        answer = _response(client, href)
    assert href == ONE + canvas

    full = {"presentation": "full"} if canvas else {}
    result = _go(
        page,
        {href: answer},
        # Request 1 is the second tap's: htmx's `hx-sync` replace has
        # already aborted the first tap's request 0 for it.
        [
            "mark",
            "tap-node:" + PLAN,
            "dbltap-node:" + PLAN,
            "state",
            "swap:1",
            "respond",
            "swap:1",
            "mark",
        ],
        **full,
    )

    assert result["fetches"] == [href]
    during = _states(result)[0]
    assert during["busy"] and during["toggle"] and during["slider"]
    assert during["href"].endswith(BASE + canvas), "nothing pushed before the swap"
    # The taps' panel request was retired: aborted, and its answer — before
    # or after the expansion's — never lands.
    assert result["aborted"] == [1]
    panel = answer["parts"]["#kgraph-panel"]["inner"]
    assert result["panel"] == panel
    assert result["pushes"] == [{"url": href, "panel": panel}]
    assert result["href"].endswith(href)
    payload = _payload_of(answer)
    assert sorted(n["id"] for n in result["nodes"]) == sorted(
        n["id"] for n in payload["nodes"]
    )
    assert sorted(e["id"] for e in result["edges"]) == sorted(
        e["id"] for e in payload["edges"]
    )
    before, after = _positions(result["marks"][0]), _positions(result["marks"][-1])
    assert {pid: after[pid] for pid in before} == before
    assert result["render"] == _render_of(answer)
    assert result["controls"]["busy"] is False
    assert result["controls"]["toggle"] is False
    assert PLAN in result["lit"] and result["picked"] == [PLAN]


def test_the_double_click_url_is_the_servers_from_a_selected_and_pinned_address(
    lithos_lens_config_env: Path,
) -> None:
    """From an address with an earlier request for the note, another note
    selected and an edge drawn for: the request moves last, the note is
    selected, the edge becomes the pin — the panel link's very spelling."""
    contradicts = "edge_e1f4a8c27b90"
    start = f"{ROUTE}?focus={ROLLBACK}&expand={PLAN}&min_weight=0.7&edge={contradicts}"
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, start)
        node = next(n for n in page.payload["nodes"] if n["id"] == LEGACY)
        assert node["expansion"]["state"] == "available"
        panel = client.get(
            f"{ROUTE}?focus={ROLLBACK}&expand={PLAN}&min_weight=0.7"
            f"&selected={LEGACY}&pin={contradicts}"
        ).text
        href = _expand(panel)

    result = _go(page, {}, ["tap-node:" + LEGACY, "dbltap-node:" + LEGACY])

    assert result["fetches"] == [href]


def test_an_intercepted_panel_link_fetches_its_own_href_and_a_modified_click_does_not(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
        answer = _response(client, href)

    plain = _go(
        page, {href: answer}, [f"graph-link:data-kgraph-expand|{href}", "respond"]
    )
    modified = _go(page, {}, [f"graph-link-ctrl:data-kgraph-expand|{href}"])

    assert plain["marks"][0] == {"prevented": True}
    assert plain["fetches"] == [href]
    assert [push["url"] for push in plain["pushes"]] == [href]
    assert modified["marks"][0] == {"prevented": False}
    assert modified["fetches"] == [] and modified["pushes"] == []


# ── One request at a time ──────────────────────────────────────────────


def test_while_a_request_is_out_every_graph_panel_and_full_page_action_is_inert(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
        answer = _response(client, href)
        collapse = _href(
            answer["parts"]["#kgraph-panel"]["inner"], "data-kgraph-collapse"
        )

    result = _go(
        page,
        {href: answer},
        [
            f"graph-link:data-kgraph-expand|{href}",
            # Rapid clicks: the same link, a collapse, a remove, a double click.
            f"graph-link:data-kgraph-expand|{href}",
            f"graph-link:data-kgraph-collapse|{collapse}",
            f"graph-link:data-kgraph-expansion-remove|{collapse}",
            "tap-node:" + PLAN,
            "dbltap-node:" + PLAN,
            # A text panel link: htmx would send it; the page cancels it.
            f"text-click:{BASE}&selected={ROLLBACK}",
            "escape",
            "state",
            "respond",
            "state",
            # Given back: the next link is a request of its own.
            f"graph-link:data-kgraph-collapse|{collapse}",
        ],
    )

    assert [m["prevented"] for m in result["marks"] if "prevented" in m] == [
        True,
        True,
        True,
        True,
        False,
        True,
    ]
    assert result["fetches"] == [href, collapse]
    assert result["ajax"] == [], "no canvas click opened a panel"
    assert result["cancelled"] == [0], "the text link's request never left"
    busy, idle = _states(result)
    assert busy["busy"] and busy["request"] == "busy"
    assert busy["toggle"] and busy["slider"]
    assert all(busy["depth"]) and all(busy["colour"]) and all(busy["provenance"])
    assert not idle["busy"] and idle["request"] == "idle"
    assert not idle["toggle"] and not idle["slider"]
    assert not any(idle["depth"]) and not any(idle["colour"])
    assert [push["url"] for push in result["pushes"]] == [href]


def test_back_retires_the_request_and_its_late_answer_and_any_panel_redirect(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        href = _expand(client.get(f"{BASE}&selected={PLAN}").text)
        answer = _response(client, href)

    result = _go(
        page,
        {href: answer},
        [
            # One entry of this page's own, by a canvas click.
            "tap-node:" + PLAN,
            "swap:0",
            "mark",
            # A second click's panel request, then the expansion.
            "tap-node:" + ROLLBACK,
            "dbltap-node:" + ROLLBACK,
            f"graph-link:data-kgraph-expand|{href}",
            "back",
            # Too late: the expansion's answer and the panel's redirect.
            "respond:0",
            "redirect:2",
            "swap:2",
            "mark",
        ],
    )

    assert result["fetches"] == [href]
    assert result["fetchAborted"] == [0]
    assert result["reloads"] == [f"http://lens.test{BASE}"]
    assert result["redirects"] == [] and 2 in result["aborted"]
    assert [push["url"] for push in result["pushes"]] == [f"{BASE}&selected={PLAN}"]
    assert result["render"] == page.dom["render"]
    assert json.loads(result["payloadText"]) == page.payload
    assert _positions(result["marks"][0]) == _positions(result["marks"][-1])
    assert result["controls"]["busy"] is False


def test_a_toggle_during_a_request_retires_it(lithos_lens_config_env: Path) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
        answer = _response(client, href)

    result = _go(
        page,
        {href: answer},
        [f"graph-link:data-kgraph-expand|{href}", "toggle", "respond:0"],
    )

    assert result["fetchAborted"] == [0]
    assert [push["url"] for push in result["pushes"]] == [
        f"http://lens.test{BASE}&selected={PLAN}&canvas=full"
    ]
    assert len(result["nodes"]) == len(page.payload["nodes"])
    assert result["controls"]["busy"] is False


# ── Failures leave the view; a per-expansion refusal commits ───────────


def _refused(config: Path, url: str) -> dict[str, Any]:
    _cap(config, 1)
    with _lens(config) as client:
        answer = _response(client, url)
    assert _payload_of(answer)["refusal"] is not None
    return answer


def _cap(config: Path, cap: int) -> None:
    text = config.read_text(encoding="utf-8")
    line = f"graph_focus_max_nodes = {cap}"
    text = re.sub(r"\ngraph_focus_max_nodes = \d+", "", text)
    config.write_text(
        text.replace("[lithos-lens.knowledge]", f"[lithos-lens.knowledge]\n{line}", 1)
        if "[lithos-lens.knowledge]" in text
        else f"{text}\n[lithos-lens.knowledge]\n{line}\n",
        encoding="utf-8",
    )


FAILURES = {
    "render-cap": ("status:0=503", "Lens is drawing too many views at once."),
    "server-error": ("status:0=500", "the server answered 500."),
    "network": ("fail:0", "the request did not complete."),
    "offline": ("respond:0=offline", "no graph came back (Lithos may be offline)."),
    "refused": ("respond:0=refused", "that view was refused."),
    "unreadable": ("respond:0=unreadable", "the answer could not be read."),
    "no-render": ("respond:0=no-render", "the answer could not be read."),
    "empty": ("respond:0=empty", "the answer drew no graph."),
}


@pytest.mark.parametrize("case", FAILURES.values(), ids=FAILURES.keys())
def test_a_failed_invalid_or_refused_answer_leaves_the_view_and_says_why(
    lithos_lens_config_env: Path, case: tuple[str, str]
) -> None:
    action, reason = case
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
        answer = _response(client, href)
    parts = answer["parts"]
    offline = {
        "status": 200,
        "parts": {k: v for k, v in parts.items() if "payload" not in k},
    }
    unreadable = copy.deepcopy(answer)
    unreadable["parts"]["[data-knowledge-graph-payload]"]["inner"] = "{not json"
    no_render = copy.deepcopy(answer)
    no_render["parts"]["#kgraph-panel"]["attrs"]["data-kgraph-render"] = ""
    responses = {
        "offline": offline,
        "refused": _refused(lithos_lens_config_env, href),
        "unreadable": unreadable,
        "no-render": no_render,
        "empty": _with_payload(answer, lambda p: p.update(nodes=[])),
        href: answer,
    }

    result = _go(
        page,
        responses,
        ["mark", f"graph-link:data-kgraph-expand|{href}", action, "mark"],
    )

    assert result["pushes"] == []
    assert result["href"].endswith(f"{BASE}&selected={PLAN}")
    assert _positions(result["marks"][0]) == _positions(result["marks"][-1])
    assert sorted(n["id"] for n in result["nodes"]) == sorted(
        n["id"] for n in page.payload["nodes"]
    )
    assert result["render"] == page.dom["render"]
    for selector in TEXT_SELECTORS:
        part = page.dom["parts"].get(selector)
        assert result["text"][selector] == (part["outer"] if part else None), selector
    controls = result["controls"]
    assert controls["busy"] is False and controls["toggle"] is False
    assert controls["status"]["hidden"] is False
    assert reason in controls["status"]["text"]
    assert controls["status"]["href"] == href


def test_the_next_request_after_a_failure_clears_the_reason(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
        answer = _response(client, href)

    link = f"graph-link:data-kgraph-expand|{href}"
    result = _go(page, {href: answer}, [link, "status:0=503", link, "respond:1"])

    assert result["controls"]["status"]["hidden"] is True
    assert [push["url"] for push in result["pushes"]] == [href]


def test_an_expansion_the_server_refuses_on_its_own_commits_the_valid_view(
    lithos_lens_config_env: Path,
) -> None:
    """Eligibility on screen was stale: the server rechecks, refuses that
    one request over the cap and draws the rest — a valid view, committed."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
    _cap(lithos_lens_config_env, 3)
    with _lens(lithos_lens_config_env) as client:
        answer = _response(client, href)
    payload = _payload_of(answer)
    assert payload["refusal"] is None
    assert [step["state"] for step in payload["expansions"]] == ["refused"]

    result = _go(
        page, {href: answer}, [f"graph-link:data-kgraph-expand|{href}", "respond"]
    )

    assert [push["url"] for push in result["pushes"]] == [href]
    assert (
        result["text"]["[data-kgraph-hidden]"]
        == answer["parts"]["[data-kgraph-hidden]"]["outer"]
    )
    assert (
        'data-kgraph-expansion-state="refused"'
        in result["text"]["[data-kgraph-hidden]"]
    )
    assert result["controls"]["status"]["hidden"] is True


# ── The server's eligibility is the preflight ──────────────────────────


def _restate(page: Page, pid: str, **expansion: Any) -> dict[str, Any]:
    payload = copy.deepcopy(page.payload)
    for node in payload["nodes"]:
        if node["id"] == pid:
            node["expansion"].update(expansion)
    return payload


def test_only_a_note_the_server_says_can_expand_sends_a_request(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
    actions = ["tap-node:" + PLAN, "dbltap-node:" + PLAN]

    over = _run(page, actions, _restate(page, PLAN, state="over_cap", would_count=300))
    for state in ("expanded", "complete"):
        assert _run(page, actions, _restate(page, PLAN, state=state))["fetches"] == []
    focus = _run(page, ["tap-node:" + ROLLBACK, "dbltap-node:" + ROLLBACK])
    # Layer-only notes make the drawn count exceed the cap the server
    # counts against; its `available` is still what decides.
    layered = _restate(page, PLAN, state="available", cap=1)
    layered["nodes"][0]["layer_only"] = True
    crowded = _run(page, actions, layered)

    assert over["fetches"] == [] and over["assigns"] == []
    assert focus["fetches"] == []
    assert len(layered["nodes"]) > 1
    assert crowded["fetches"] == [ONE]


def test_a_pair_of_taps_on_two_targets_is_the_second_ones_single_click(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)

    result = _run(page, ["tap-node:" + ROLLBACK, "dbltap-node:" + PLAN])

    assert result["fetches"] == []
    assert [
        request.split("selected=")[1].split("&")[0] for request in result["requests"]
    ] == [ROLLBACK, PLAN]


# ── The marks ──────────────────────────────────────────────────────────


def test_a_note_shows_what_its_expansion_adds_and_the_key_explains_it(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        base = _page(client, BASE)
        one = _page(client, ONE)

    before, after = _run(base), _run(one)

    assert before["marked"][PLAN]["classes"] == ["adds-notes"]
    assert "%2B2" in before["marked"][PLAN]["badge"], "+2 on the note"
    assert before["marked"][ROLLBACK]["classes"] == []
    assert "adds-notes" in before["keyMarks"]
    assert any("adds N notes" in text for text in before["keyMarkText"])
    # Expanded: no mark; each new note adds only an edge between drawn notes.
    assert after["marked"][PLAN]["classes"] == []
    assert after["marked"][CAPACITY]["classes"] == ["adds-edges"]
    assert after["marked"][LEGACY]["classes"] == ["adds-edges"]
    assert "adds-edges" in after["keyMarks"] and "adds-notes" not in after["keyMarks"]
    assert any(
        "only edges between notes already drawn" in t for t in after["keyMarkText"]
    )


# ── Positions: survivors stay, new notes beside their anchor ───────────


def test_every_surviving_note_keeps_its_place_through_expansion_and_collapse(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        one = _response(client, ONE)
        two = _response(client, TWO)
        collapse = _href(
            two["parts"]["[data-kgraph-scope]"]["inner"],
            f'data-kgraph-expansion-remove="{PLAN}"',
        )
        shared = _href(
            two["parts"]["[data-kgraph-scope]"]["inner"],
            f'data-kgraph-expansion-remove="{CAPACITY}"',
        )
        collapsed = _response(client, collapse)
        unshared = _response(client, shared)
    # The capacity note it selects goes with the plan: the selection too.
    assert collapse == BASE

    result = _go(
        page,
        {ONE: one, TWO: two, collapse: collapsed, shared: unshared},
        [
            "mark",
            "tap-node:" + PLAN,
            "dbltap-node:" + PLAN,
            "respond",
            "mark",
            # Edge-only: nothing new to place, nothing moves.
            f"graph-link:data-kgraph-expand|{TWO}",
            "respond",
            "mark",
            # The shared branch: the legacy note is still the plan's.
            f"graph-link:data-kgraph-expansion-remove|{shared}",
            "respond",
            "mark",
            f"graph-link:data-kgraph-expand|{TWO}",
            "respond",
            "mark",
            f"graph-link:data-kgraph-expansion-remove|{collapse}",
            "respond",
            "mark",
        ],
    )

    marks = [_positions(m) for m in result["marks"] if "positions" in m]
    base, expanded, edged, unshared_, again, back = marks
    assert set(expanded) == {ROLLBACK, PLAN, CAPACITY, LEGACY}
    assert {pid: expanded[pid] for pid in base} == base
    assert edged == expanded
    assert unshared_ == expanded, "the shared note stays, where it was"
    assert again == expanded
    assert back == base, "collapsed: the survivors where they were"
    assert [push["url"] for push in result["pushes"]] == [
        ONE,
        TWO,
        shared,
        TWO,
        collapse,
    ]
    assert result["picked"] == [] and result["lit"] == []


def _beside(positions: dict[str, dict[str, float]], anchor: str, centre: dict) -> None:
    at = positions[anchor]
    for pid in (CAPACITY, LEGACY):
        new = positions[pid]
        outward = (at["x"] - centre["x"], at["y"] - centre["y"])
        offset = (new["x"] - at["x"], new["y"] - at["y"])
        assert outward[0] * offset[0] + outward[1] * offset[1] > 0, f"{pid} faces away"
        assert math.dist((new["x"], new["y"]), (at["x"], at["y"])) < math.dist(
            (new["x"], new["y"]), (positions[ROLLBACK]["x"], positions[ROLLBACK]["y"])
        )
    for a in positions:
        for b in positions:
            if a < b:
                gap = math.dist(
                    (positions[a]["x"], positions[a]["y"]),
                    (positions[b]["x"], positions[b]["y"]),
                )
                assert gap > SIZE_MAX, f"{a} × {b}"


def test_new_notes_go_beside_the_note_expanded_facing_away_from_the_graph(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        fresh = _page(client, ONE)
        one = _response(client, ONE)

    live = _go(
        page, {ONE: one}, ["tap-node:" + PLAN, "dbltap-node:" + PLAN, "respond", "mark"]
    )
    loaded = _run(fresh, ["mark"])

    for result in (live, loaded):
        positions = _positions(result["marks"][-1])
        base = [positions[ROLLBACK], positions[PLAN]]
        centre = {
            "x": (base[0]["x"] + base[1]["x"]) / 2,
            "y": (base[0]["y"] + base[1]["y"]) / 2,
        }
        _beside(positions, PLAN, centre)


def test_fresh_loads_of_one_address_are_deterministic(
    lithos_lens_config_env: Path,
) -> None:
    """The same data, address, faces and canvas lay out and colour the same
    way. Not that a live picture equals a fresh load: degrees, and so the
    sizes the base layout spaces by, differ once an expansion is in."""
    with _lens(lithos_lens_config_env) as client:
        two = _page(client, TWO)

    first, second = _run(two, ["mark"]), _run(two, ["mark"])

    assert first["marks"] == second["marks"]
    assert first["layouts"] == ["concentric"], "the base laid out once; the rest placed"
    assert [(n["id"], n["colour"]) for n in first["nodes"]] == [
        (n["id"], n["colour"]) for n in second["nodes"]
    ]


# ── Colour slots ───────────────────────────────────────────────────────


def _colours(result: dict) -> dict[str, str]:
    return {node["id"]: node["colour"] for node in result["nodes"]}


def test_colour_slots_are_kept_through_a_collapse_and_new_values_take_unused_ones(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        fresh = _page(client, ONE)
        one = _response(client, ONE)
        collapsed = _response(client, COLLAPSED)

    actions = ["tap-node:" + PLAN, "dbltap-node:" + PLAN, "respond"]
    once = _go(page, {ONE: one}, actions)
    twice = _go(
        page,
        {ONE: one, COLLAPSED: collapsed},
        [
            *actions,
            f"graph-link:data-kgraph-collapse|{COLLAPSED}",
            "respond",
            "tap-node:" + PLAN,
            "dbltap-node:" + PLAN,
            "respond",
        ],
    )
    loaded = _run(fresh)

    palette = once["palettes"]["nodes"]
    assert _colours(once)[PLAN] == _rgb(palette[0])
    assert _colours(once)[CAPACITY] == _rgb(palette[1]), (
        "the new value takes the next slot"
    )
    assert _colours(twice) == _colours(once)
    assert _colours(loaded) == _colours(once)
    assert ["reports", palette[1]] in once["keyNodes"]


def test_past_the_palette_new_values_share_the_neutral_and_changed_values_recolour(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        one = _response(client, ONE)

    def crowd(payload: dict) -> None:
        template = next(n for n in payload["nodes"] if n["id"] == CAPACITY)
        for index in range(10):
            extra = copy.deepcopy(template)
            extra.update(
                id=f"note-extra-{index}", label=f"Extra {index}", namespace=f"ns{index}"
            )
            payload["nodes"].append(extra)
        # A refreshed namespace: the plan's changed upstream.
        next(n for n in payload["nodes"] if n["id"] == PLAN)["namespace"] = "ns0"

    crowded = _with_payload(one, crowd)
    result = _go(
        page, {ONE: crowded}, ["tap-node:" + PLAN, "dbltap-node:" + PLAN, "respond"]
    )

    palette = [_rgb(colour) for colour in result["palettes"]["nodes"][:-1]]
    neutral = _rgb(result["palettes"]["nodes"][-1])
    colours = _colours(result)
    # Kept: `plans` slot 0 (no longer drawn). New, in order: ns0, ns1, …,
    # `reports` last by name — seven slots left, then the neutral.
    taken = ["ns0", "ns1", "ns2", "ns3", "ns4", "ns5", "ns6"]
    for index, value in enumerate(taken):
        assert colours[f"note-extra-{index}"] == palette[index + 1], value
    for index in (7, 8, 9):
        assert colours[f"note-extra-{index}"] == neutral
    assert colours[CAPACITY] == neutral, "reports came after the palette ran out"
    assert colours[PLAN] == palette[1], "its value changed: its colour follows"
    assert colours[ROLLBACK] == neutral


# ── Everything the view says follows the swap ──────────────────────────


def test_text_counts_search_key_panel_render_ids_and_upstream_changes_follow_the_swap(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        one = _response(client, ONE)
    wiki = f"wiki_link:{PLAN}->{ROLLBACK}"

    def upstream(payload: dict) -> None:
        # Between the two renders: the focus retitled, a wiki-link gone.
        next(n for n in payload["nodes"] if n["id"] == ROLLBACK)["label"] = (
            "Rollback, retitled"
        )
        payload["edges"] = [e for e in payload["edges"] if e["id"] != wiki]

    changed = _with_payload(one, upstream)
    render = _render_of(one)
    result = _go(
        page,
        {ONE: changed},
        [
            "search:capacity",
            "mark",
            "tap-node:" + PLAN,
            "dbltap-node:" + PLAN,
            "respond",
            "mark",
            "tap-node:" + CAPACITY,
        ],
    )

    payload = _payload_of(changed)
    for selector in TEXT_SELECTORS:
        part = changed["parts"].get(selector)
        assert result["text"][selector] == (part["outer"] if part else None), selector
    assert f"render={render}" in result["text"]["[data-kgraph-edges]"]
    assert json.loads(result["payloadText"]) == payload
    assert result["exported"] == [n["id"] for n in payload["nodes"]]
    assert result["render"] == render
    # A canvas click after the swap asks for the new render.
    assert f"render={render}" in result["ajax"][-1]["path"]
    assert result["searchCount"] == "1 match" and result["matched"] == [CAPACITY]
    assert result["keyEdges"] == [line["type"] for line in payload["legend"]]
    assert result["canvas"]["dataset"]["canvasNodes"] == str(len(payload["nodes"]))
    assert result["canvas"]["dataset"]["canvasEdges"] == str(len(payload["edges"]))
    assert result["hiddenCount"].startswith(f"{payload['hidden']['by_weight']} edge")
    labels = {n["id"]: n["label"] for n in result["nodes"]}
    assert labels[ROLLBACK] == "Rollback, retitled"
    assert wiki not in {e["id"] for e in result["edges"]}
    before, after = _positions(result["marks"][0]), _positions(result["marks"][1])
    assert {pid: after[pid] for pid in before} == before


# ── The pill follows the swap and keeps what came in during it ─────────


def test_a_frame_or_a_reconnect_during_a_request_keeps_the_pill_up_after_the_swap(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        one = _response(client, ONE)
    start = ["tap-node:" + PLAN, "dbltap-node:" + PLAN]

    undrawn = _go(
        page,
        {ONE: one},
        [*start, _frame("note.updated", id=UNDRAWN), "respond"],
        **EVENTS,
    )
    reconnect = _go(
        page, {ONE: one}, [*start, "sse:error", "sse:open", "respond"], **EVENTS
    )
    outside = _go(
        page,
        {ONE: one},
        [*start, "respond", _frame("note.updated", id=UNDRAWN)],
        **EVENTS,
    )
    newly = _go(
        page,
        {ONE: one},
        [*start, "respond", _frame("note.updated", id=CAPACITY)],
        **EVENTS,
    )
    quiet = _go(page, {ONE: one}, [_frame("note.updated", id=CAPACITY)], **EVENTS)

    assert undrawn["pill"]["hidden"] is False
    assert undrawn["pill"]["href"].endswith(ONE)
    assert reconnect["pill"]["hidden"] is False
    assert outside["pill"]["hidden"] is True
    assert newly["pill"]["hidden"] is False, "the drawn set is the swapped payload's"
    assert quiet["pill"]["hidden"] is True, (
        "before the swap the capacity note was not drawn"
    )
