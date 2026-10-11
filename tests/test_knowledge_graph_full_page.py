"""K2 slice 10 — the full-page canvas's server half (PRD D17).

``canvas=full`` is presentation: parsed in every mode, carried by every link
the URL builder writes (the picker's and its typed-in scope form included),
left out of the drawn view's identity so a page toggled in place keeps
answering panel clicks from the view it drew, recorded on the request span,
and — without the script — invisible. Every panel answer leads with a close
control that clears the selection and keeps the edge whose exemption the
drawing needs as ``pin=``. The script's half is ``test_knowledge_graph_js.py``
and the browser's ``e2e/tests/knowledge-graph-full-page.spec.ts``; expansion
links carrying it are in ``test_knowledge_graph_expansion_page.py``.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lithos_lens.knowledge_graph_routes import (
    KnowledgeGraphParams,
    RenderedViews,
    knowledge_graph_panel_url,
    knowledge_graph_url,
    parse_knowledge_graph_params,
)
from lithos_lens.knowledge_graph_view import KnowledgeGraphFilters, KnowledgeGraphView
from tests.test_knowledge_graph_panels import (
    _client,
    _get,
    _host,
    _Offline,
    _ReadRecorder,
    _render_id,
    _set_knowledge,
)

PLAN = "note-influx-plan"
CAPACITY = "note-influx-capacity"
LEGACY = "note-influx-legacy-ingest"
ROUTE = "/knowledge/graph"
PANEL = "/knowledge/graph/panel"
SUPPORTS = "edge_4c1e9a7b20d3"  # capacity supports plan
REFINES = "edge_a07c5f3e18b2"  # plan refines legacy
#: legacy → plan at 0.03: below the default 0.1, drawn only by its exemption.
FAINT_EDGE = "edge_15d0c3e8f972"

_CLOSE = re.compile(r'<a class="kgraph-panel-close" href="([^"]*)"')


def _close_href(fragment: str) -> str:
    return html_lib.unescape(_CLOSE.findall(fragment)[0])


def _graph_links(html: str) -> list[str]:
    """Every server-written address into the graph: hrefs, ``hx-get`` and
    ``hx-push-url`` values on ``/knowledge/graph`` and its panel."""
    values = re.findall(r'(?:href|hx-get|hx-push-url)="(/knowledge/graph[^"]*)"', html)
    return [html_lib.unescape(value) for value in values]


# ── the grammar and the URL builder ────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("full", "full"),
        (" full ", "full"),
        ("inline", "inline"),
        ("FULL", "inline"),
        ("", "inline"),
        ("yes", "inline"),
    ],
)
@pytest.mark.parametrize("scope", ["", f"focus={PLAN}&", "type=contradicts&"])
def test_canvas_is_full_or_else_inline_in_every_mode(
    raw: str, expected: str, scope: str
) -> None:
    query = dict(
        pair.split("=", 1) for pair in f"{scope}canvas={raw}".split("&") if pair
    )
    assert parse_knowledge_graph_params(query).canvas == expected


def test_the_url_builder_writes_canvas_last_and_only_when_full() -> None:
    full = KnowledgeGraphParams(focus=PLAN, depth=2, canvas="full")

    assert (
        knowledge_graph_url(KnowledgeGraphParams(focus=PLAN)) == f"{ROUTE}?focus={PLAN}"
    )
    # Last, after a selection and its pin: where `URLSearchParams.set`
    # appends it, so the script and the server spell one address alike.
    assert knowledge_graph_url(full, selected=CAPACITY) == (
        f"{ROUTE}?focus={PLAN}&depth=2&selected={CAPACITY}&canvas=full"
    )
    assert knowledge_graph_url(full, edge=SUPPORTS) == (
        f"{ROUTE}?focus={PLAN}&depth=2&edge={SUPPORTS}&canvas=full"
    )
    # Kept across a new focus and a new scope, which drop pin and expand.
    assert knowledge_graph_url(full, focus=CAPACITY) == (
        f"{ROUTE}?focus={CAPACITY}&depth=2&canvas=full"
    )
    assert knowledge_graph_url(full, focus="", type="contradicts") == (
        f"{ROUTE}?type=contradicts&canvas=full"
    )
    # The panel fragment's request: canvas, then the render id last.
    assert knowledge_graph_panel_url(full, render="r1", edge=SUPPORTS) == (
        f"{PANEL}?focus={PLAN}&depth=2&edge={SUPPORTS}&canvas=full&render=r1"
    )
    assert knowledge_graph_url(full, canvas="inline") == f"{ROUTE}?focus={PLAN}&depth=2"


def test_a_view_kept_inline_answers_its_full_page_requests() -> None:
    """``canvas`` is not part of a drawn view's identity (D17): the same
    render id finds the view whichever presentation asks."""
    views = RenderedViews()
    view = KnowledgeGraphView(
        mode="focus", filters=KnowledgeGraphFilters(), focus_id=PLAN
    )
    render = views.keep(KnowledgeGraphParams(focus=PLAN), view)

    assert views.get(render, KnowledgeGraphParams(focus=PLAN, canvas="full")) is view
    assert views.get(render, KnowledgeGraphParams(focus=PLAN, depth=2)) is None


# ── the page and its panels ────────────────────────────────────────────


def test_a_toggled_pages_next_click_is_answered_from_the_view_it_drew(
    lithos_lens_config_env: Path,
) -> None:
    """Render inline, then ask for a panel as a page toggled to full page
    asks: the held view answers — no redirect and no read — and its links
    carry the new presentation."""
    fake = _ReadRecorder()
    with _client(lithos_lens_config_env, fake) as client:
        page = _get(client, f"{ROUTE}?focus={PLAN}&selected={PLAN}")
        render = _render_id(page)
        fake.data_reads.clear()
        response = client.get(
            f"{PANEL}?focus={PLAN}&selected={CAPACITY}&canvas=full&render={render}"
        )
        fragment = response.text

    assert response.status_code == 200
    assert "HX-Redirect" not in response.headers
    assert fake.data_reads == []
    assert f'data-kgraph-panel-id="{CAPACITY}"' in fragment
    assert f"render={render}" in fragment
    links = _graph_links(fragment)
    assert links and all("canvas=full" in link for link in links), links


def test_every_graph_link_on_a_full_page_carries_it(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?focus={PLAN}&depth=2&edge={SUPPORTS}&canvas=full")
        inline = _get(client, f"{ROUTE}?focus={PLAN}&depth=2&edge={SUPPORTS}")

    links = _graph_links(page)
    # The pill, the edge and node links, "Not shown", the panel's links.
    assert len(links) > 20
    missing = [
        link for link in links if not re.search(r"[?&]canvas=full(&render=|$)", link)
    ]
    assert missing == []
    assert not [link for link in _graph_links(inline) if "canvas=" in link]
    assert 'data-kgraph-presentation="full"' in page
    assert 'data-kgraph-presentation="inline"' in inline


def test_the_picker_and_its_scope_form_keep_a_full_page_session_full(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env) as client:
        full = _get(client, f"{ROUTE}?canvas=full&selected={PLAN}")
        inline = _get(client, ROUTE)

    links = _graph_links(full)
    assert f"{ROUTE}?type=contradicts&canvas=full" in links
    assert f"{ROUTE}?namespace=influx&canvas=full" in links
    # A scope link opens no selection the picker happened to carry.
    assert not [link for link in links if "selected=" in link]
    assert all(link.endswith("canvas=full") for link in links)
    form = re.search(r"<form[^>]*data-kgraph-scope-form.*?</form>", full, re.S)
    assert form is not None
    assert '<input type="hidden" name="canvas" value="full">' in form.group(0)
    assert f"{ROUTE}?type=contradicts" in _graph_links(inline)
    assert 'name="canvas"' not in inline


def test_a_refusals_scope_form_keeps_it_too(lithos_lens_config_env: Path) -> None:
    # A table over its bound refuses a focus graph with a typed-in scope.
    _set_knowledge(lithos_lens_config_env, "graph_edge_table_max_edges = 3")
    with _client(lithos_lens_config_env) as client:
        page = _get(client, f"{ROUTE}?focus={PLAN}&canvas=full")

    assert 'data-kgraph-refusal="table_refused"' in page
    form = re.search(r"<form[^>]*data-kgraph-scope-form.*?</form>", page, re.S)
    assert form is not None, "no typed-in scope offered"
    assert '<input type="hidden" name="canvas" value="full">' in form.group(0)


_PRESENTATION = re.compile(
    # The links, the operator chooser's `next=` back here, and the mark.
    r'(?:&amp;|\?)canvas=full|(?:%26|%3F)canvas%3Dfull|data-kgraph-presentation="[a-z]+"'
)
_PAYLOAD = re.compile(r"<script [^>]*data-knowledge-graph-payload>(.*?)</script>", re.S)
_RENDER = re.compile(r"(render=|data-kgraph-render=\")[A-Za-z0-9_-]+")


@pytest.mark.parametrize(
    "query",
    [f"focus={PLAN}&depth=2&selected={CAPACITY}", "type=contradicts", ""],
)
def test_without_the_script_canvas_full_changes_nothing_but_its_links(
    lithos_lens_config_env: Path, query: str
) -> None:
    """The no-JS baseline (D17): the server renders the same page — the
    canvas hidden, the text the page — save the mode its links carry and
    the mark the script reads."""
    with _client(lithos_lens_config_env) as client:
        inline = _get(client, f"{ROUTE}?{query}")
        full = _get(client, f"{ROUTE}?{query}{'&' if query else ''}canvas=full")

    def bare(html: str) -> str:
        html = html.replace('\n  <input type="hidden" name="canvas" value="full">', "")
        return _RENDER.sub(r"\1-", _PRESENTATION.sub("", _PAYLOAD.sub("", html)))

    def payload(html: str) -> dict[str, Any]:
        match = _PAYLOAD.search(html)
        drawn = json.loads(match.group(1)) if match else {}
        drawn.pop("facts", None)  # the second request's facts are cache hits
        return drawn

    assert bare(full) == bare(inline)
    assert payload(full) == payload(inline)
    # The class every full-page rule keys to is only ever the script's.
    assert not re.search(r'class="[^"]*\bkgraph-full\b(?!-)', full)
    # The canvas, its toolbar (the toggle in it) and its key stay hidden.
    for hook in ("data-kgraph-toolbar", "data-kgraph-canvas", "data-kgraph-key"):
        assert re.search(
            rf"<div [^>]*{hook} [^>]*hidden>|<div [^>]*{hook} hidden>", full
        ) or (hook not in full), hook


# ── closing a panel ────────────────────────────────────────────────────


def test_every_panel_answer_leads_with_a_close_that_clears_the_selection(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env) as client:
        node = _get(
            client, f"{ROUTE}?focus={PLAN}&depth=2&selected={CAPACITY}&canvas=full"
        )
        edge = _get(client, f"{ROUTE}?focus={PLAN}&edge={SUPPORTS}")
        notice = _get(client, f"{PANEL}?focus={PLAN}&selected=note-not-drawn")

    assert _close_href(_host(node)) == f"{ROUTE}?focus={PLAN}&depth=2&canvas=full"
    # An edge drawn at its own weight: nothing to keep.
    assert _close_href(_host(edge)) == f"{ROUTE}?focus={PLAN}"
    assert 'data-kgraph-panel="none"' in notice
    assert _close_href(notice) == f"{ROUTE}?focus={PLAN}"
    for host in (_host(node), _host(edge)):
        assert host.index("data-kgraph-panel-close") < host.index(
            "data-kgraph-panel-heading"
        )


def test_an_offline_panel_answer_closes_too(lithos_lens_config_env: Path) -> None:
    with _client(lithos_lens_config_env, _Offline()) as client:
        fragment = _get(client, f"{PANEL}?focus={PLAN}&selected={PLAN}&canvas=full")

    assert _close_href(fragment) == f"{ROUTE}?focus={PLAN}&canvas=full"


def test_closing_keeps_the_exemption_the_drawing_needs_as_its_pin(
    lithos_lens_config_env: Path,
) -> None:
    """The view an exempted edge drew: closing its panel (or a node panel
    opened from it) keeps that edge as ``pin=``, and the next click after
    the close is answered from the same render id, nothing read."""
    fake = _ReadRecorder()
    with _client(lithos_lens_config_env, fake) as client:
        page = _get(client, f"{ROUTE}?focus={PLAN}&edge={FAINT_EDGE}&canvas=full")
        render = _render_id(page)
        node = _get(
            client,
            f"{PANEL}?focus={PLAN}&selected={LEGACY}&pin={FAINT_EDGE}&canvas=full"
            f"&render={render}",
        )
        closed = _close_href(_host(page))
        fake.data_reads.clear()
        after = client.get(
            f"{PANEL}?focus={PLAN}&selected={CAPACITY}&pin={FAINT_EDGE}&canvas=full"
            f"&render={render}"
        )
        after_reads = list(fake.data_reads)
        reloaded = _get(client, closed)

    expected = f"{ROUTE}?focus={PLAN}&pin={FAINT_EDGE}&canvas=full"
    assert closed == expected
    assert _close_href(node) == expected
    assert "HX-Redirect" not in after.headers
    assert f'data-kgraph-panel-id="{CAPACITY}"' in after.text
    assert after_reads == []
    # The close's address draws the same picture, with no panel.
    assert FAINT_EDGE in reloaded and _host(reloaded) == ""


# ── telemetry ──────────────────────────────────────────────────────────


def _canvas_attributes(spans: InMemorySpanExporter, route: str) -> list[Any]:
    return [
        (span.attributes or {}).get("lens.knowledge.graph.canvas")
        for span in spans.get_finished_spans()
        if (span.attributes or {}).get("http.route") == route
    ]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (f"?focus={PLAN}&canvas=full", "full"),
        (f"?focus={PLAN}", "inline"),
        ("?canvas=full", "full"),
        ("?type=contradicts&canvas=nope", "inline"),
    ],
)
def test_the_request_span_says_which_presentation_was_asked_for(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    query: str,
    expected: str,
) -> None:
    with _client(lithos_lens_config_env) as client:
        _get(client, f"{ROUTE}{query}")
        _get(client, f"{PANEL}{query}&selected={PLAN}")

    assert _canvas_attributes(spans, ROUTE) == [expected]
    # The fragment is not a page render: no presentation recorded on it.
    assert _canvas_attributes(spans, PANEL) == [None]


def test_an_offline_page_records_its_presentation(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    with _client(lithos_lens_config_env, _Offline()) as client:
        _get(client, f"{ROUTE}?focus={PLAN}&canvas=full")

    assert _canvas_attributes(spans, ROUTE) == ["full"]
