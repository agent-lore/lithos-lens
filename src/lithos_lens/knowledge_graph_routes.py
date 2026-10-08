"""The `/knowledge/graph` page: picker, focus and scoped-global text (K2 S3),
and its node and edge panels (S5).

Its own module for the reason `graph_routes.py` is: the page's assembly — the
edge-table snapshot, one ``lithos_related``, the gated facts fan-out — lives in
:mod:`lithos_lens.knowledge_graph` and shares nothing with the K1 routes in
`knowledge_routes.py`. What is here is the page's own vocabulary and wiring:

- **One query grammar** (:func:`parse_knowledge_graph_params`) and **one URL
  builder** (:func:`knowledge_graph_url`, a template global), so every link
  into the graph — this page's, and the panels, canvas and entry points that
  follow it — is spelled one way.
- **Three modes.** No ``focus``, ``type`` or ``namespace``: the scope picker,
  from the snapshot's facets alone. ``focus=``: the ego graph. ``type=`` and/or
  ``namespace=``: the scoped-global graph. A refusal (over a cap, the table over
  its bound, the table unreadable) renders the "narrow your scope" panel and
  nothing degraded.
- **The text is the page** (D12): the template renders the view in the PRD's
  order and embeds the same view as the JSON payload the canvas (S4) draws.
- **One panel at a time** (D10): ``selected=`` a node's, ``edge=`` an edge's,
  rendered in the page on a full request and as a fragment from
  ``/knowledge/graph/panel`` — one assembly (:func:`load_knowledge_graph`)
  and one partial behind both. Every drawn view is kept under a render id
  (:class:`RenderedViews`) that the page's panel links carry, so a click's
  fragment is drawn from the very view its page showed.
- **Telemetry**: ``lens.knowledge.graph.*`` attributes on the request span and
  one counter by mode and outcome; panel opens by kind and source; the scope
  and the selection are never labels.

Registered as a closure over app, state and templates, the shape `create_app`
already uses.
"""

from __future__ import annotations

import math
import secrets
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from lithos_lens import metrics
from lithos_lens.knowledge_edge_types import is_conflict_resolved
from lithos_lens.knowledge_edges import (
    EdgeFacets,
    EdgeTable,
    EdgeTableRefusal,
    EdgeTableSnapshot,
)
from lithos_lens.knowledge_facts import NoteFactsTally
from lithos_lens.knowledge_graph import assemble_focus_graph, assemble_global_graph
from lithos_lens.knowledge_graph_view import (
    MAX_DEPTH,
    PROVENANCE_GROUPS,
    HiddenEdgeCounts,
    KnowledgeEdgePanel,
    KnowledgeGraphFilters,
    KnowledgeGraphView,
    KnowledgeNodePanel,
    edge_entry,
    edge_sections,
    graph_panel,
    graph_payload,
    node_metadata,
)
from lithos_lens.state import AppState, HealthSnapshot
from lithos_lens.telemetry import get_current_span

KNOWLEDGE_GRAPH_PATH = "/knowledge/graph"
KNOWLEDGE_GRAPH_PANEL_PATH = "/knowledge/graph/panel"

#: The picker's namespace table shows this many rows; the rest are behind a
#: disclosure (PRD D11).
PICKER_TOP_NAMESPACES = 20

GraphMode = Literal["picker", "focus", "global"]

#: The counter's outcome label: what the page did.
RenderOutcome = Literal["rendered", "refused", "unavailable", "offline"]

# The query keys the page reads, in the order the URL builder writes them.
_URL_KEYS = (
    "focus",
    "depth",
    "type",
    "namespace",
    "min_weight",
    "provenance",
    "selected",
    "edge",
)


@dataclass(frozen=True)
class KnowledgeGraphParams:
    """The page's query, parsed. ``None`` and ``""`` mean "not given".

    ``depth`` and ``min_weight`` stay ``None`` when absent or unreadable, so
    the configured defaults apply at render time and links do not pin them.
    ``provenance`` is empty when the request names no known group: all shown.
    """

    focus: str = ""
    type: str | None = None
    namespace: str | None = None
    depth: int | None = None
    min_weight: float | None = None
    provenance: tuple[str, ...] = ()
    edge: str = ""
    selected: str = ""

    @property
    def mode(self) -> GraphMode:
        if self.focus:
            return "focus"
        if self.type is not None or self.namespace is not None:
            return "global"
        return "picker"

    def depth_or(self, default: int) -> int:
        return self.depth if self.depth is not None else default

    def filters(self, default_min_weight: float) -> KnowledgeGraphFilters:
        weight = self.min_weight if self.min_weight is not None else default_min_weight
        groups = frozenset(self.provenance or PROVENANCE_GROUPS)
        return KnowledgeGraphFilters(min_weight=weight, provenance=groups)


def _value(query: Mapping[str, str], key: str) -> str:
    """A query value exactly as sent; ``""`` when absent or wholly blank.

    Not trimmed: a type or namespace is matched as stored upstream (a
    namespace ``" influx "`` is a different namespace from ``"influx"``, and
    the picker's own facet links send it so), and ``focus`` / ``edge`` /
    ``selected`` are ids carried unchanged. Only the numeric and provenance
    grammar below strips what it parses.
    """
    value = query.get(key) or ""
    return value if value.strip() else ""


_DEPTHS = {str(level): level for level in range(1, MAX_DEPTH + 1)}


def _depth(raw: str) -> int | None:
    return _DEPTHS.get(raw)


def _weight(raw: str) -> float | None:
    try:
        weight = float(raw)
    except ValueError:
        return None
    if not math.isfinite(weight):
        return None
    return min(max(weight, 0.0), 1.0)


def _provenance(raw: str) -> tuple[str, ...]:
    named = {part.strip() for part in raw.split(",")}
    return tuple(group for group in PROVENANCE_GROUPS if group in named)


def parse_knowledge_graph_params(query: Mapping[str, str]) -> KnowledgeGraphParams:
    """The page's query grammar (S3 D3), in one place.

    A blank value is absent. A non-blank ``focus`` is focus mode and the
    ``type``/``namespace`` scope is dropped; otherwise either of those (exact
    strings — a type or namespace is matched as stored) is global mode; with
    none, the picker. ``depth`` is 1 or 2, anything else the default, and
    is kept in focus mode only — elsewhere it draws nothing, the URL builder
    leaves it out, and so must every panel built from the request (a node
    panel's "Centre on this" reads it), or a page and its own fragment would
    word that link differently;
    ``min_weight`` is clamped to [0, 1], unreadable is the default;
    ``provenance`` is a comma list of known groups, unknown names dropped.
    ``edge`` and ``selected`` are ids kept as given — one at a time: a
    request carrying both is read as ``edge`` alone, whatever their order
    (no link the page writes carries both; :func:`knowledge_graph_url`
    clears one when it sets the other).
    """
    focus = _value(query, "focus")
    edge_type = _value(query, "type") or None
    namespace = _value(query, "namespace") or None
    if focus:
        edge_type = namespace = None
    edge = _value(query, "edge")
    return KnowledgeGraphParams(
        focus=focus,
        type=edge_type,
        namespace=namespace,
        depth=_depth(_value(query, "depth").strip()) if focus else None,
        min_weight=_weight(_value(query, "min_weight").strip()),
        provenance=_provenance(_value(query, "provenance")),
        edge=edge,
        selected="" if edge else _value(query, "selected"),
    )


def _url_value(key: str, params: KnowledgeGraphParams) -> str:
    if key == "depth":
        return "" if params.depth is None or not params.focus else str(params.depth)
    if key == "min_weight":
        # repr round-trips: a link must name the very threshold the page drew
        # with, or following it changes the scope (0.7000001 is not 0.7).
        return "" if params.min_weight is None else repr(params.min_weight)
    if key == "provenance":
        return ",".join(params.provenance)
    if key in ("type", "namespace") and params.focus:
        return ""
    value = getattr(params, key)
    return value or ""


def _graph_url(
    path: str, params: KnowledgeGraphParams | None, changes: dict[str, Any]
) -> str:
    # One selection per link (D10): setting one clears the other.
    if changes.get("edge"):
        changes["selected"] = ""
    elif changes.get("selected"):
        changes["edge"] = ""
    merged = replace(params or KnowledgeGraphParams(), **changes)
    pairs = [(key, _url_value(key, merged)) for key in _URL_KEYS]
    query = urlencode([(key, value) for key, value in pairs if value])
    return f"{path}?{query}" if query else path


def knowledge_graph_url(
    params: KnowledgeGraphParams | None = None, **changes: Any
) -> str:
    """A link into the knowledge graph: ``params`` with ``changes`` applied.

    The one URL builder (a template global): the picker's facet links,
    the refusal's remedies, every edge entry's ``edge=`` link, and the entry
    points S6 adds all come through here. Defaults are left out, so a link
    does not pin today's configured depth or weight; ``type``/``namespace``
    are dropped from a focus link, as the parser drops them. A change that
    sets ``edge`` clears ``selected`` and one that sets ``selected`` clears
    ``edge``, so a link carries one selection — the later click's.
    """
    return _graph_url(KNOWLEDGE_GRAPH_PATH, params, changes)


def knowledge_graph_edge_url(
    focus: str, edge: str, weight: float | None = None, *, floor: float
) -> str:
    """An entry point's link to ``edge``'s panel, focused on ``focus``.

    ``edge=`` opens a panel only for an edge the page draws, and a focus
    hides edges below the weight ``floor`` (``graph_min_weight_default``) —
    so an edge known to weigh less carries its own weight as ``min_weight``,
    and the page it lands on draws it.
    """
    if weight is not None and weight < floor:
        return knowledge_graph_url(focus=focus, edge=edge, min_weight=weight)
    return knowledge_graph_url(focus=focus, edge=edge)


def knowledge_graph_panel_url(
    params: KnowledgeGraphParams | None = None, render: str = "", **changes: Any
) -> str:
    """The same link to the panel fragment: what a click's ``hx-get`` fetches,
    with the scope and filters carried, and ``render`` — the id of the view
    the page drew (:class:`RenderedViews`) — last when there is one."""
    url = _graph_url(KNOWLEDGE_GRAPH_PANEL_PATH, params, changes)
    if not render:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode([(RENDER_KEY, render)])}"


# ── the views a click answers from ─────────────────────────────────────

#: The panel fragment's query key naming the view its page drew.
RENDER_KEY = "render"

#: How many drawn views are kept for their panel clicks (most recent first).
#: A view past it — or after a restart — is assembled afresh, as a reload of
#: the page would be.
RENDERED_VIEWS_KEPT = 32


def _scope(params: KnowledgeGraphParams) -> str:
    """The scope and filters a view is drawn for, as the URL builder writes
    them: one canonical spelling, so what the page's own links leave out (a
    ``depth`` outside focus mode) cannot make its view unfindable."""
    return knowledge_graph_url(params, selected="", edge="")


class RenderedViews:
    """The views recent renders drew, each under a fresh render id (S5).

    A page's facts depend on when it was drawn: each render spends its own
    facts cap from a process-wide cache that other tabs, the TTL and note
    events keep changing. So a panel click does not re-assemble its page's
    view — it names it, by the render id the page's panel links carry, and
    its fragment is drawn from the view the page showed, facts and all. A
    view is found only for the scope and filters it was drawn under.
    """

    def __init__(self, size: int = RENDERED_VIEWS_KEPT) -> None:
        self._size = size
        self._views: OrderedDict[str, tuple[str, KnowledgeGraphView]] = OrderedDict()

    def keep(self, params: KnowledgeGraphParams, view: KnowledgeGraphView) -> str:
        """Keep ``view``, drawn for ``params``; its new render id."""
        render_id = secrets.token_urlsafe(9)
        self._views[render_id] = (_scope(params), view)
        while len(self._views) > self._size:
            self._views.popitem(last=False)
        return render_id

    def get(
        self, render_id: str, params: KnowledgeGraphParams
    ) -> KnowledgeGraphView | None:
        """The view kept under ``render_id`` for ``params``' scope, if any."""
        kept = self._views.get(render_id) if render_id else None
        if kept is None or kept[0] != _scope(params):
            return None
        self._views.move_to_end(render_id)
        return kept[1]


def utc_minute(moment: datetime) -> str:
    """``as_of`` as the scope line states it: UTC, to the minute."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# ── the picker ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KnowledgeGraphPicker:
    """What the unscoped page offers (PRD D11, story 17).

    ``facets`` is ``None`` when the snapshot gave none: the table over its
    bound (``refused``) or unreadable (``unavailable``). The page then offers
    only a typed-in ``type=`` / ``namespace=`` scope.
    """

    facets: EdgeFacets | None = None
    as_of: datetime | None = None
    stale: bool = False
    refused: EdgeTableRefusal | None = None
    unavailable: bool = False

    @property
    def top_namespaces(self) -> tuple[tuple[str, int], ...]:
        rows = tuple(self.facets.namespaces.items()) if self.facets else ()
        return rows[:PICKER_TOP_NAMESPACES]

    @property
    def more_namespaces(self) -> tuple[tuple[str, int], ...]:
        rows = tuple(self.facets.namespaces.items()) if self.facets else ()
        return rows[PICKER_TOP_NAMESPACES:]


async def load_picker(table: EdgeTable) -> KnowledgeGraphPicker:
    """The picker from the snapshot: its facets, or why there are none."""
    try:
        state = await table.read()
    except Exception:
        return KnowledgeGraphPicker(unavailable=True)
    if isinstance(state, EdgeTableSnapshot):
        return KnowledgeGraphPicker(state.facets, state.as_of, state.stale)
    return KnowledgeGraphPicker(refused=state, as_of=state.as_of)


# ── telemetry ──────────────────────────────────────────────────────────


def _outcome(
    view: KnowledgeGraphView | None, picker: KnowledgeGraphPicker | None
) -> tuple[RenderOutcome, str]:
    """The counter's outcome and the span's refusal reason ("" for none)."""
    if picker is not None:
        if picker.unavailable:
            return "unavailable", "unavailable"
        if picker.refused is not None:
            return "refused", "table_refused"
        return "rendered", ""
    if view is not None and view.refusal is not None:
        reason = view.refusal.reason
        return ("unavailable" if reason == "unavailable" else "refused"), reason
    return "rendered", ""


def _record(
    mode: GraphMode,
    outcome: RenderOutcome,
    *,
    snapshot_age_s: float,
    refusal: str = "",
    view: KnowledgeGraphView | None = None,
    depth: int | None = None,
) -> None:
    """``lens.knowledge.graph``: attributes on the request span, one counter.

    Counts and the refusal reason go on the span, where a per-request value
    costs no series; the counter carries only the two bounded enums. Every
    mode sets every count — zero where nothing was drawn (the picker, an
    offline page) — so a query over the span never mistakes absent for 0;
    ``depth`` is set in focus mode only (the drawn one, else the requested
    or configured one), ``refusal`` only on a refusal. The
    focus id, type and namespace are on neither: ``http.target`` already
    carries the query on the span, bounded (``telemetry.py``).
    """
    span = get_current_span()
    prefix = "lens.knowledge.graph"
    span.set_attribute(f"{prefix}.mode", mode)
    span.set_attribute(f"{prefix}.outcome", outcome)
    if refusal:
        span.set_attribute(f"{prefix}.refusal", refusal)
    span.set_attribute(f"{prefix}.snapshot_age_s", snapshot_age_s)
    if view is not None and view.mode == "focus":
        depth = view.depth
    if mode == "focus" and depth is not None:
        span.set_attribute(f"{prefix}.depth", depth)
    hidden = view.hidden if view is not None else HiddenEdgeCounts()
    tally = view.facts_tally if view is not None else NoteFactsTally()
    span.set_attribute(f"{prefix}.nodes", len(view.nodes) if view else 0)
    span.set_attribute(f"{prefix}.edges", len(view.edges) if view else 0)
    span.set_attribute(f"{prefix}.hidden_by_weight", hidden.by_weight)
    span.set_attribute(f"{prefix}.hidden_by_provenance", hidden.by_provenance)
    span.set_attribute(f"{prefix}.hidden_total", hidden.total)
    span.set_attribute(f"{prefix}.facts.hits", tally.hits)
    span.set_attribute(f"{prefix}.facts.reads", tally.reads)
    span.set_attribute(f"{prefix}.facts.missing", tally.missing)
    span.set_attribute(f"{prefix}.facts.capped", tally.capped)
    span.set_attribute(f"{prefix}.facts.failed", tally.failed)
    metrics.knowledge_graph_renders().add(1, {"mode": mode, "outcome": outcome})


def _record_panel(
    panel: KnowledgeNodePanel | KnowledgeEdgePanel | None,
    source: Literal["url", "fragment"],
) -> None:
    """One panel open by kind and source; nothing when no panel rendered."""
    if panel is not None:
        metrics.knowledge_graph_panel_opens().add(
            1, {"kind": panel.kind, "source": source}
        )


# ── the assembly ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class KnowledgeGraphLoad:
    """What one request read: offline (nothing), the picker, or a view."""

    health: HealthSnapshot
    offline: bool = False
    picker: KnowledgeGraphPicker | None = None
    view: KnowledgeGraphView | None = None


async def load_knowledge_graph(
    state: AppState, params: KnowledgeGraphParams, *, picker: bool = True
) -> KnowledgeGraphLoad:
    """The page's reads for ``params``, shared by the page and its panel
    fragment so the two draw the same view (S5 D1).

    Offline, nothing is read. The picker reads only the snapshot (and not at
    all with ``picker=False``: no panel opens on it). Focus and global modes
    run the S2 orchestrators, which never raise for a table failure: they
    answer a refused view instead.
    """
    health = await state.refresh_health()
    if health.lithos != "ok":
        return KnowledgeGraphLoad(health, offline=True)
    table = state.edge_table
    if params.mode == "picker":
        return KnowledgeGraphLoad(
            health, picker=await load_picker(table) if picker else None
        )
    knowledge = state.config.knowledge
    filters = params.filters(knowledge.graph_min_weight_default)
    if params.mode == "focus":
        view = await assemble_focus_graph(
            table,
            state.lithos_client.related,
            state.note_facts,
            params.focus,
            depth=params.depth_or(knowledge.graph_default_depth),
            filters=filters,
            max_nodes=knowledge.graph_focus_max_nodes,
            fanout_cap=knowledge.graph_title_fanout_cap,
        )
    else:
        view = await assemble_global_graph(
            table,
            state.note_facts,
            type=params.type,
            namespace=params.namespace,
            filters=filters,
            max_nodes=knowledge.graph_global_max_nodes,
            fanout_cap=knowledge.graph_title_fanout_cap,
        )
    return KnowledgeGraphLoad(health, view=view)


# ── the routes ─────────────────────────────────────────────────────────


def register_knowledge_graph_routes(
    app: FastAPI, state: AppState, templates: Jinja2Templates
) -> None:
    """Attach `GET /knowledge/graph` (the picker, focus and scoped-global)
    and `GET /knowledge/graph/panel` (its node and edge panels)."""

    templates.env.globals["knowledge_graph_url"] = knowledge_graph_url
    templates.env.globals["knowledge_graph_edge_url"] = partial(
        knowledge_graph_edge_url, floor=state.config.knowledge.graph_min_weight_default
    )
    templates.env.globals["knowledge_graph_panel_url"] = knowledge_graph_panel_url
    templates.env.globals["edge_entry"] = edge_entry
    templates.env.filters["utc_minute"] = utc_minute
    templates.env.filters["is_conflict_resolved"] = is_conflict_resolved
    views = RenderedViews()

    @app.get(KNOWLEDGE_GRAPH_PATH, response_class=HTMLResponse)
    async def knowledge_graph(request: Request) -> HTMLResponse:
        """The knowledge graph as text, with its payload for the canvas.

        Offline, the page says so and reads nothing. A refused view renders
        its message and remedy in place of the graph. A drawn view renders
        the panel ``selected=`` / ``edge=`` names when it draws that node or
        typed edge, and no panel otherwise.
        """
        params = parse_knowledge_graph_params(request.query_params)
        mode = params.mode
        knowledge = state.config.knowledge
        load = await load_knowledge_graph(state, params)
        context: dict[str, Any] = {
            "config": state.config,
            "health": load.health,
            "active_view": "knowledge",
            "params": params,
            "mode": mode,
            "offline": load.offline,
            "picker": None,
            "view": None,
            "panel": None,
            "render_id": "",
        }
        table = state.edge_table
        if load.offline:
            # The age is the holder's own clock: no read is started for it.
            _record(
                mode,
                "offline",
                snapshot_age_s=table.age_seconds(),
                depth=params.depth_or(knowledge.graph_default_depth),
            )
            return templates.TemplateResponse(request, "knowledge/graph.html", context)
        view = load.view
        if view is None:  # the picker
            context["picker"] = load.picker
            outcome, reason = _outcome(None, load.picker)
            _record(mode, outcome, refusal=reason, snapshot_age_s=table.age_seconds())
            return templates.TemplateResponse(request, "knowledge/graph.html", context)
        focus_node = view.node(view.focus_id) if view.mode == "focus" else None
        panel = graph_panel(view, selected=params.selected, edge=params.edge)
        context.update(
            view=view,
            sections=edge_sections(view),
            focus_node=focus_node,
            focus_meta=node_metadata(focus_node),
            payload=graph_payload(view),
            panel=panel,
            render_id=views.keep(params, view) if view.refusal is None else "",
        )
        _record_panel(panel, "url")
        outcome, reason = _outcome(view, None)
        _record(
            mode,
            outcome,
            refusal=reason,
            view=view,
            snapshot_age_s=table.age_seconds(),
        )
        return templates.TemplateResponse(request, "knowledge/graph.html", context)

    @app.get(KNOWLEDGE_GRAPH_PANEL_PATH, response_class=HTMLResponse)
    async def knowledge_graph_panel(request: Request) -> HTMLResponse:
        """The panel ``selected=`` / ``edge=`` names, as a fragment.

        Offline first, as the page: "Lithos is offline", nothing read, no
        panel — whatever the request names. Then ``render=`` names the view
        the page drew (:class:`RenderedViews`): the panel is drawn from it
        with nothing read, so it equals the page's own panel whatever other
        tabs or the facts TTL did since. A ``render=`` no longer held —
        evicted, or lost to a restart — does not swap in a panel from a
        different view beside the page's old graph: it answers
        ``HX-Redirect`` to the full page with that selection, which draws
        graph and panel afresh together (and, for a client that does not
        follow it, a one-line notice linking there). With no ``render=`` at
        all (a hand-made or cold request) the page's own assembly runs — the
        reads a full request makes — and its view is kept for the panel's
        own links. Anything that is not a drawn node or typed edge — the
        picker, a refused view, an id the view does not draw — answers 200
        with a one-line "Not in this view" panel: htmx swaps a 200 and would
        drop a 4xx. Not a page render: the renders counter is not touched,
        and only a panel that renders counts as an open.
        """
        params = parse_knowledge_graph_params(request.query_params)
        render_id = request.query_params.get(RENDER_KEY) or ""
        context: dict[str, Any] = {
            "config": state.config,
            "params": params,
            "panel": None,
            "panel_notice": "Not in this view.",
            "render_id": render_id,
        }
        health = await state.refresh_health()
        if health.lithos != "ok":
            context["panel_notice"] = "Lithos is offline."
            return templates.TemplateResponse(
                request, "knowledge/graph_panel.html", context
            )
        if render_id:
            view = views.get(render_id, params)
            if view is None:
                page_url = knowledge_graph_url(params)
                context.update(
                    panel_notice="This view is no longer held.",
                    panel_reload=page_url,
                    render_id="",
                )
                response = templates.TemplateResponse(
                    request, "knowledge/graph_panel.html", context
                )
                response.headers["HX-Redirect"] = page_url
                return response
        else:
            load = await load_knowledge_graph(state, params, picker=False)
            if load.offline:
                # The assembly's own probe may be the one that saw the outage.
                context["panel_notice"] = "Lithos is offline."
                return templates.TemplateResponse(
                    request, "knowledge/graph_panel.html", context
                )
            view = load.view
            if view is not None and view.refusal is None:
                context["render_id"] = views.keep(params, view)
        if view is not None:
            context["panel"] = graph_panel(
                view, selected=params.selected, edge=params.edge
            )
        _record_panel(context["panel"], "fragment")
        return templates.TemplateResponse(
            request, "knowledge/graph_panel.html", context
        )
