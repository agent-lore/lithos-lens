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


# ── Round 2: the gaps the review found ─────────────────────────────────


def _note(payload: dict, like: str, **changes: Any) -> dict:
    """A note shaped as the payload's ``like`` is, with ``changes``."""
    note = copy.deepcopy(next(n for n in payload["nodes"] if n["id"] == like))
    note.update(changes)
    return note


def _gap(a: dict[str, float], b: dict[str, float]) -> float:
    return math.dist((a["x"], a["y"]), (b["x"], b["y"]))


def test_while_a_request_is_out_every_other_graph_link_is_inert_but_note_links_are_not(
    lithos_lens_config_env: Path,
) -> None:
    """Centre on this, a remedy or depth link, the pill: each loads a view
    built from the one being replaced. A note page and a `#` jump do not."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
        centre = _href(page.html, "data-kgraph-centre")
        answer = _response(client, href)
    note = "/knowledge/notes/" + PLAN

    result = _go(
        page,
        {href: answer},
        [
            f"graph-link:data-kgraph-expand|{href}",
            f"graph-link:data-kgraph-centre|{centre}",
            f"graph-link:data-kgraph-refresh-pill|{BASE}&selected={PLAN}",
            # The pill as an event during the request shows it: its href is
            # the page's full address (`syncRefresh`).
            _frame("note.updated", id=UNDRAWN),
            f"graph-link:data-kgraph-refresh-pill|http://lens.test{BASE}&selected={PLAN}",
            f"graph-link:data-kgraph-node|{note}",
            "graph-link:data-kgraph-text-link|#kgraph-text",
            f"graph-link-ctrl:data-kgraph-centre|{centre}",
            "respond",
            f"graph-link:data-kgraph-centre|{centre}",
        ],
        **EVENTS,
    )

    assert result["pill"]["hidden"] is False
    # …and once the view commits, it follows the expanded address.
    assert result["pill"]["href"] == f"http://lens.test{href}"
    assert [m["prevented"] for m in result["marks"]] == [
        True,
        True,
        True,
        True,
        False,
        False,
        False,
        False,
    ]
    assert result["fetches"] == [href]


def test_the_double_click_url_takes_the_servers_key_order_from_any_spelling(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{ROUTE}?min_weight=0.7&focus={ROLLBACK}")

    result = _run(page, ["tap-node:" + PLAN, "dbltap-node:" + PLAN])

    assert result["fetches"] == [ONE]


def test_an_expansion_brings_an_operator_zoomed_past_the_floor_back_to_readable(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
        answer = _response(client, href)

    result = _go(
        page,
        {href: answer},
        ["zoom:0.2", f"graph-link:data-kgraph-expand|{href}", "respond"],
    )

    assert result["zoom"] >= 11 / 12, "MIN_READABLE_ZOOM: 11px over a 12px label"


def test_a_late_answer_whose_body_lands_after_retirement_changes_nothing(
    lithos_lens_config_env: Path,
) -> None:
    """Retirement is the page's own guard, not the abort: the response has
    landed, the request is retired while its body is still coming, then the
    body arrives — and a newer request's busy state is not released by it."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
        full = f"{href}&canvas=full"
        answer, answer_full = _response(client, href), _response(client, full)

    toggled = _go(
        page,
        {href: answer, full: answer_full},
        [
            f"graph-link:data-kgraph-expand|{href}",
            "headers:0",
            "toggle",
            f"graph-link:data-kgraph-expand|{full}",
            "body:0",
            "state",
            "mark",
            "respond:1",
        ],
    )
    travelled = _go(
        page,
        {href: answer},
        [
            "hash:top",
            f"graph-link:data-kgraph-expand|{href}",
            "headers:0",
            "back",
            "body:0",
            "state",
        ],
    )

    after_late = _states(toggled)[0]
    assert after_late["busy"] is True, "the newer request is still out"
    assert after_late["toggle"] is True
    assert len(toggled["marks"][-1]["positions"]) == 2, "the late body drew nothing"
    assert [push["url"] for push in toggled["pushes"]] == [
        f"http://lens.test{BASE}&selected={PLAN}&canvas=full",
        full,
    ]
    assert toggled["render"] == _render_of(answer_full)

    assert travelled["reloads"] == [], "Back onto the same address: no reload"
    assert travelled["pushes"] == []
    assert travelled["render"] == page.dom["render"]
    assert len(travelled["nodes"]) == 2
    assert _states(travelled)[0]["busy"] is False


def test_the_swapped_text_is_processed_and_its_new_provenance_toggles_navigate(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, f"{BASE}&selected={PLAN}")
        href = _expand(page.html)
        answer = _response(client, href)
    assert "[data-kgraph-provenance-group]" in answer["parts"]

    result = _go(
        page,
        {href: answer},
        [f"graph-link:data-kgraph-expand|{href}", "respond", "provenance:inferred=off"],
    )

    inserted = [s for s in TEXT_SELECTORS if s in answer["parts"]] + ["#kgraph-panel"]
    assert sorted(k for k in result["processed"] if isinstance(k, str)) == sorted(
        inserted
    )
    # The answer's own toggle, bound as it arrived: it navigates from the
    # expanded address.
    assert len(result["assigns"]) == 1
    assert f"expand={PLAN}" in result["assigns"][0]
    assert "provenance=reinforced%2Cdeclared%2Cother" in result["assigns"][0]


def test_a_surviving_notes_size_follows_its_degree_through_expansion_and_collapse(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        one = _response(client, ONE)
        collapsed = _response(client, COLLAPSED)
    expand = ["mark", "tap-node:" + PLAN, "dbltap-node:" + PLAN, "respond", "mark"]

    before = _run(page)
    expanded = _go(page, {ONE: one}, expand)
    folded = _go(
        page,
        {ONE: one, COLLAPSED: collapsed},
        [*expand, f"graph-link:data-kgraph-collapse|{COLLAPSED}", "respond", "mark"],
    )

    sized = lambda result: {n["id"]: (n["size"], n["width"]) for n in result["nodes"]}  # noqa: E731
    assert sized(before)[PLAN] == sized(before)[ROLLBACK] == (30, 30)
    # Degrees 7 (plan), 3 (rollback), 2 and 2: the hub at the top of the
    # range, the focus a fifth of the way up it.
    assert sized(expanded)[PLAN] == (42, 42)
    assert sized(expanded)[ROLLBACK][0] == pytest.approx(18 + 24 / 5)
    assert sized(expanded)[ROLLBACK][1] == pytest.approx(18 + 24 / 5)
    assert sized(folded)[PLAN] == sized(folded)[ROLLBACK] == (30, 30)
    marks = [_positions(m) for m in folded["marks"] if "positions" in m]
    assert {pid: marks[1][pid] for pid in marks[0]} == marks[0] == marks[2]


def test_a_fresh_load_places_and_colours_each_expansion_in_url_order(
    lithos_lens_config_env: Path,
) -> None:
    """Two steps that add notes, the second anchored on a note the first
    added; their new values in an order opposite to their names, and the
    payload's arrays in an order opposite to the URL's."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, ONE)
    payload = copy.deepcopy(page.payload)
    extra = _note(
        payload,
        CAPACITY,
        id="note-x",
        label="X note",
        via=LEGACY,
        namespace="alpha",
        hop=3,
    )
    edge = copy.deepcopy(next(e for e in payload["edges"] if e["to"] == LEGACY))
    edge.update(id="edge-x", **{"from": LEGACY, "to": "note-x"})
    payload["nodes"].insert(0, extra)
    payload["edges"].append(edge)
    payload["expansions"].append(
        {"id": LEGACY, "state": "applied", "added_nodes": 1, "added_edges": 1}
    )

    result = _run(page, ["mark"], payload)

    at = _positions(result["marks"][0])
    assert _gap(at["note-x"], at[LEGACY]) < _gap(at["note-x"], at[PLAN])
    assert _gap(at["note-x"], at[LEGACY]) < _gap(at["note-x"], at[ROLLBACK])
    assert _gap(at[CAPACITY], at[PLAN]) < _gap(at[CAPACITY], at[ROLLBACK])
    palette = [_rgb(c) for c in result["palettes"]["nodes"]]
    colours = _colours(result)
    assert colours[PLAN] == palette[0], "the base's value"
    assert colours[CAPACITY] == palette[1], "the first step's, though named after"
    assert colours["note-x"] == palette[2], "the second step's"


def test_a_slot_given_before_a_collapse_is_not_given_again_after_it(
    lithos_lens_config_env: Path,
) -> None:
    """Expand (`reports` takes slot 1), collapse, a new base value arrives
    (`alpha` takes slot 2, not the freed-looking 1), expand again: each
    keeps its slot — unlike a fresh load of the same view."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        one = _response(client, ONE)
        collapsed = _response(client, COLLAPSED)
    fresh = _note(
        _payload_of(collapsed),
        PLAN,
        id="note-fresh-alpha",
        label="Fresh alpha note",
        namespace="alpha",
        via=None,
    )

    def add(payload: dict) -> None:
        payload["nodes"].append(copy.deepcopy(fresh))

    later = _with_payload(collapsed, add)
    again = _with_payload(one, add)
    result = _go(
        page,
        {ONE: one, COLLAPSED: later, "again": again},
        [
            "tap-node:" + PLAN,
            "dbltap-node:" + PLAN,
            "respond",
            f"graph-link:data-kgraph-collapse|{COLLAPSED}",
            "respond",
            f"graph-link:data-kgraph-expand|{ONE}",
            "respond:2=again",
        ],
    )
    loaded = _run(page, [], _payload_of(again))

    palette = [_rgb(c) for c in result["palettes"]["nodes"]]
    live, reloaded = _colours(result), _colours(loaded)
    assert (live[PLAN], live[CAPACITY], live["note-fresh-alpha"]) == (
        palette[0],
        palette[1],
        palette[2],
    )
    # A fresh load ranks the base's two values by name: alpha, plans, then
    # the expansion's reports — not the slots this page kept.
    assert (reloaded["note-fresh-alpha"], reloaded[PLAN], reloaded[CAPACITY]) == (
        palette[0],
        palette[1],
        palette[2],
    )


def test_unrelated_refreshed_notes_go_beside_their_own_anchor_and_removals_apply(
    lithos_lens_config_env: Path,
) -> None:
    """Expanding the capacity note, the answer also brings a new base note
    (beside the focus), a new note of the plan's earlier expansion (beside
    the plan) and drops the legacy note — none of them the click's."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, ONE)
        two = _response(client, TWO)

    def refresh(payload: dict) -> None:
        payload["nodes"] = [n for n in payload["nodes"] if n["id"] != LEGACY]
        payload["edges"] = [
            e for e in payload["edges"] if LEGACY not in (e["from"], e["to"])
        ]
        payload["nodes"] += [
            _note(
                payload,
                PLAN,
                id="note-fresh-base",
                label="Freshly linked note",
                via=None,
                hop=1,
            ),
            _note(
                payload,
                CAPACITY,
                id="note-fresh-branch",
                label="Branch note",
                via=PLAN,
                hop=2,
            ),
        ]

    changed = _with_payload(two, refresh)
    result = _go(
        page,
        {TWO: changed},
        [
            "search:fresh",
            "mark",
            f"graph-link:data-kgraph-expand|{TWO}",
            "respond",
            "mark",
        ],
    )

    before, after = [_positions(m) for m in result["marks"] if "positions" in m]
    assert set(after) == {
        ROLLBACK,
        PLAN,
        CAPACITY,
        "note-fresh-base",
        "note-fresh-branch",
    }
    assert {pid: after[pid] for pid in (ROLLBACK, PLAN, CAPACITY)} == {
        pid: before[pid] for pid in (ROLLBACK, PLAN, CAPACITY)
    }
    base, branch = after["note-fresh-base"], after["note-fresh-branch"]
    assert _gap(base, after[ROLLBACK]) < _gap(base, after[PLAN])
    assert _gap(base, after[ROLLBACK]) < _gap(base, after[CAPACITY])
    assert _gap(branch, after[PLAN]) < _gap(branch, after[ROLLBACK])
    # On a ring around its own anchor: the first is the base layout's
    # spacing and a note at its largest out.
    assert _gap(branch, after[PLAN]) == pytest.approx(140 + SIZE_MAX)
    assert _gap(base, after[ROLLBACK]) == pytest.approx(140 + SIZE_MAX)
    assert result["matched"] == ["note-fresh-base"]
    assert result["canvas"]["dataset"]["canvasNodes"] == "5"


def _title_box(label: str, at: dict[str, float]) -> tuple[float, float, float, float]:
    """A note's body at the largest size and its title, as the script
    estimates a title without a renderer (`titleSize`)."""
    length = len(label) * 13 * 0.6
    lines = max(1, math.ceil(length / 140))
    width, height = min(length, 140) + 4, lines * 13 * 1.3 + 4
    half = max(SIZE_MAX, width) / 2
    return (
        at["x"] - half,
        at["x"] + half,
        at["y"] - SIZE_MAX / 2,
        at["y"] + SIZE_MAX / 2 + 4 + height,
    )


def test_many_new_notes_with_long_titles_each_get_clear_ground(
    lithos_lens_config_env: Path,
) -> None:
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        one = _response(client, ONE)
    long = "Detailed design considerations and operational requirements " * 3

    def crowd(payload: dict) -> None:
        payload["nodes"] += [
            _note(
                payload,
                CAPACITY,
                id=f"note-n{index:03}",
                label=f"Expansion note {index} {long}",
            )
            for index in range(60)
        ]

    result = _go(
        page,
        {ONE: _with_payload(one, crowd)},
        ["tap-node:" + PLAN, "dbltap-node:" + PLAN, "respond", "mark"],
    )

    at = _positions(result["marks"][0])
    labels = {n["id"]: n["label"] for n in result["nodes"]}
    new = [pid for pid in at if pid not in (ROLLBACK, PLAN)]
    assert len(new) == 62
    boxes = {pid: _title_box(labels[pid], at[pid]) for pid in at}
    overlaps = [
        (a, b)
        for a in new
        for b in boxes
        if a < b or b in (ROLLBACK, PLAN)
        if a != b
        and not (
            boxes[a][1] <= boxes[b][0]
            or boxes[b][1] <= boxes[a][0]
            or boxes[a][3] <= boxes[b][2]
            or boxes[b][3] <= boxes[a][2]
        )
    ]
    assert overlaps == []


def _outward(positions: dict[str, dict[str, float]], anchor: str, centre: dict) -> list:
    """The new notes NOT on the side of ``anchor`` facing away from ``centre``."""
    at = positions[anchor]
    out = (at["x"] - centre["x"], at["y"] - centre["y"])
    return [
        pid
        for pid, p in positions.items()
        if pid not in (ROLLBACK, PLAN)
        and out[0] * (p["x"] - at["x"]) + out[1] * (p["y"] - at["y"]) <= 0
    ]


@pytest.mark.parametrize("count", [3, 62])
def test_new_notes_with_long_titles_stay_on_the_side_facing_away(
    lithos_lens_config_env: Path, count: int
) -> None:
    """Too wide for one ring's outward side, they go further out on it —
    never round towards the focus."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)
        one = _response(client, ONE)
    long = "Detailed design considerations and operational requirements " * 3

    def crowd(payload: dict) -> None:
        payload["nodes"] = [n for n in payload["nodes"] if n["via"] is None]
        payload["nodes"] += [
            _note(
                payload,
                PLAN,
                id=f"note-n{index:03}",
                via=PLAN,
                hop=2,
                label=f"Expansion note {index} {long}",
            )
            for index in range(count)
        ]
        drawn = {n["id"] for n in payload["nodes"]}
        payload["edges"] = [
            e for e in payload["edges"] if e["from"] in drawn and e["to"] in drawn
        ]

    result = _go(
        page,
        {ONE: _with_payload(one, crowd)},
        ["mark", "tap-node:" + PLAN, "dbltap-node:" + PLAN, "respond", "mark"],
    )

    before = _positions(result["marks"][0])
    after = _positions(result["marks"][-1])
    centre = {
        "x": (before[ROLLBACK]["x"] + before[PLAN]["x"]) / 2,
        "y": (before[ROLLBACK]["y"] + before[PLAN]["y"]) / 2,
    }
    assert len(after) == count + 2
    assert _outward(after, PLAN, centre) == []


def test_a_taps_panel_lets_the_pointer_through_until_the_pair_is_over(
    lithos_lens_config_env: Path,
) -> None:
    """In full page a drawer or sheet opens over the canvas on the first
    tap: until the double-click window ends it takes no pointer, so the
    second press reaches the note beneath it."""
    with _lens(lithos_lens_config_env) as client:
        page = _page(client, BASE)

    tapped = _run(page, ["tap-node:" + PLAN, "swap"], dom={"presentation": "full"})
    paired = _run(
        page,
        ["tap-node:" + PLAN, "swap", "dbltap-node:" + PLAN],
        dom={"presentation": "full"},
    )

    assert tapped["panelPointer"] == "none"
    assert paired["panelPointer"] == ""
