"""The `/tasks/graph` route and its instrumentation (T2-A3).

Its own module for the reason `knowledge_routes.py` is: the task routes in
`web.py` are already at the 800-line ceiling, and this page's assembly — one
master read, one scope, one cycle-signal fan-out, one fold — shares no state
with them. The side panel's downstream-impact line (:func:`panel_impact`) is
here for the same two reasons: it is the graph feature's, and it is assembled
the same way, from the same scope and the same cache. The route itself is
registered as a closure over app/state/templates, the shape `create_app`
already uses.

Registration ORDER matters and is the one thing a reader must not rearrange:
`/tasks/graph` has to be attached before `/tasks/{task_id}`, or Starlette
matches the dynamic route first and the graph page becomes a task detail for a
task called "graph".
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from opentelemetry.trace import Span

from lithos_lens import metrics
from lithos_lens.graph_impact import (
    ImpactScope,
    load_impact,
    parse_impact_scope,
    reconciled_impact,
    with_canvas_notes,
)
from lithos_lens.graph_mini import (
    MiniGraphLimits,
    MiniGraphView,
    load_mini_graph,
)
from lithos_lens.graph_page import (
    EDGE_LEGEND,
    SCOPE_PROJECT,
    GraphPageParams,
    GraphPageView,
    graph_url,
    load_graph_page,
    observed_projects,
    open_epics,
    parse_graph_params,
    scope_param,
)
from lithos_lens.graph_scope import GraphScopeLimits
from lithos_lens.graph_snapshot import CanvasNotes, parse_canvas_notes
from lithos_lens.graph_view import DownstreamImpact
from lithos_lens.state import AppState
from lithos_lens.task_detail import TaskDetailData, load_task_detail
from lithos_lens.tasks import (
    GRAPH_SELECTION_KEY,
    PANEL_BOUND_KEY,
    PANEL_CHAIN_KEY,
    PANEL_SCOPE_KEY,
    PANEL_SELECTION_KEY,
    PANEL_SNAPSHOT_KEY,
    TaskRecord,
    default_since,
)
from lithos_lens.telemetry import get_tracer

logger = logging.getLogger(__name__)

#: The assembly span every graph render opens (the PRD's telemetry point).
GRAPH_SPAN = "lens.tasks.graph"

#: The same, for the detail page's mini-graph fragment (D11).
MINI_GRAPH_SPAN = "lens.tasks.minigraph"


def register_graph_routes(
    app: FastAPI, state: AppState, templates: Jinja2Templates
) -> None:
    """Attach `GET /tasks/graph` (scope picker + one scope's graph)."""

    # The graph page's own URL vocabulary: its query state is not the `/tasks`
    # filter set, so it does not travel through `request_filters`.
    templates.env.globals["graph_url"] = graph_url
    templates.env.globals["edge_legend"] = EDGE_LEGEND
    # The page's single selection parameter, handed to `tasks.js` so the one
    # panel implementation is told which key this host uses rather than
    # carrying a copy of both pages' vocabularies (D9).
    templates.env.globals["graph_selection_key"] = GRAPH_SELECTION_KEY
    # The scope a panel opened from this page counts its impact over (D10):
    # the page hands it to both halves — the URL the server writes onto the
    # panel host, and the one `tasks.js` builds for a node click.
    templates.env.globals["graph_scope_param"] = scope_param

    @app.get("/tasks/graph", response_class=HTMLResponse)
    async def tasks_graph(request: Request) -> Response:
        """The dependency graph of one scope, or the picker when none is given.

        The no-JS baseline is the WHOLE page here (D3): layers, callout,
        chain, hierarchy and the embedded payload are all server-rendered, and
        A4's Cytoscape layer is enhancement over the same payload.

        The whole assembly runs inside one ``lens.tasks.graph`` span. That is
        the exception `telemetry.get_current_span` describes rather than a
        violation of it: this handler is not one unit of work but a multi-phase
        fan-out — the master read, one ``edge_list`` per node through the
        shared cache, the ghost ``task_get``s, the coverage set's blocked reads
        — and "which phase was slow, and how much did this page actually
        fetch?" is a question the server span cannot answer.
        """
        params = parse_graph_params(dict(request.query_params))
        # `selected=` is the DASHBOARD's selection parameter, accepted here as a
        # compatibility alias (D8). Canonicalising it means REPLACING it, not
        # merely reading it: this page has one selection parameter, and both
        # clients read `focus`. A page served under the alias would render the
        # panel while no node was lit, Escape would be inert, and closing would
        # push a URL that still carried `selected=` — so the next reload, or the
        # next Back, would reopen the panel the operator just closed. Redirected
        # before any read, because the answer costs nothing to compute.
        if PANEL_SELECTION_KEY in request.query_params:
            return RedirectResponse(graph_url(params), status_code=307)
        snapshot = await state.refresh_health()
        context: dict[str, object] = {
            "config": state.config,
            "health": snapshot,
            "active_view": "graph",
            "params": params,
            "view": None,
            "projects": (),
            "epics": (),
            "error": "",
            "offline": snapshot.lithos != "ok",
            # The side panel's three hooks, filled in below when this render
            # actually draws a graph (D9's no-JS baseline for `focus=`).
            "panel": None,
            "selected_id": params.focus,
            "panel_close_url": graph_url(params, focus=""),
            # D10's line, computed by the render that already holds this
            # scope and its cycle signal — so the no-JS baseline states the
            # impact without a second assembly (``graph_impact.load_impact``
            # is for the panel fetched on its own).
            "impact": None,
        }
        with get_tracer().start_as_current_span(GRAPH_SPAN) as span:
            if snapshot.lithos != "ok":
                _record(span, params, outcome="offline")
                return templates.TemplateResponse(request, "tasks/graph.html", context)

            tasks_config = state.config.tasks
            try:
                master = await _master_rows(state, params)
            except Exception:
                logger.warning("graph page master read failed", exc_info=True)
                context["error"] = (
                    "Task data is unavailable. Lens will retry on refresh."
                )
                _record(span, params, outcome="error")
                return templates.TemplateResponse(request, "tasks/graph.html", context)

            if not params.scoped:
                context["projects"] = observed_projects(
                    master, tag_key=tasks_config.project_tag_key
                )
                context["epics"] = open_epics(master)
                _record(span, params, outcome="picker")
                return templates.TemplateResponse(request, "tasks/graph.html", context)

            try:
                view = await load_graph_page(
                    state.lithos_client,
                    params=params,
                    master=master,
                    cache=state.graph_cache,
                    limits=GraphScopeLimits(
                        max_tasks=state.config.graph.max_tasks,
                        fetch_concurrency=state.config.graph.fetch_concurrency,
                    ),
                    frontier_limit=tasks_config.frontier_limit,
                    convention=tasks_config.project_convention,
                    tag_key=tasks_config.project_tag_key,
                )
            except Exception:
                # An epic scope reads its anchor up front, so a deep link to a
                # deleted epic arrives here as a coded error rather than as an
                # empty graph. Either way the page says so instead of 500ing.
                logger.warning(
                    "graph page scope failed",
                    exc_info=True,
                    extra={"lens_scope_kind": params.kind},
                )
                context["error"] = "This scope could not be loaded from Lithos."
                _record(span, params, outcome="error")
                return templates.TemplateResponse(request, "tasks/graph.html", context)

            context["view"] = view
            # Not gated on the NODE COUNT, only on the refusal: a scope that
            # holds nothing to draw still renders this page's panel host, and
            # `focus=` owes that panel whatever the canvas has to show (D9) —
            # a project whose last task completed answers "nothing to draw"
            # AND "this task is completed; no pending impact", never silence
            # (round-5 correctness f-002). A REFUSED scope renders neither: it
            # has no host to put a panel in.
            if not view.refused:
                panel = await _focused_panel(state, params)
                context["panel"] = panel
                # Reconciled against the panel's OWN read of the focal task,
                # not handed over whole: the impact is this render's arithmetic
                # over the graph, the badge beside it is a later `task_get`,
                # and a task that completed between the two must not carry a
                # future-tense number under a `completed` badge (round-2
                # correctness f-002).
                context["impact"] = reconciled_impact(
                    view.impact,
                    panel.task if panel else None,
                    # This render IS the scope, so a focus that resolved out of
                    # it still gets D10's wording for that (round-4 f-002).
                    scoped=True,
                )
            _record(
                span,
                params,
                outcome="refused" if view.refused else "rendered",
                view=view,
            )
            return templates.TemplateResponse(request, "tasks/graph.html", context)

    @app.get("/tasks/{task_id}/minigraph", response_class=HTMLResponse)
    async def task_minigraph(request: Request, task_id: str) -> Response:
        """The detail page's mini-graph, as an HTMX fragment (D11, T2-A5).

        Its own request for the reason `/tasks/{task_id}/findings` is: the
        detail page's own reads answer "why can't this run" already, and this
        neighbourhood costs a fan-out of its own — one `edge_list` per drawn
        blocker plus a `task_get` per neighbour the open snapshot does not
        carry. Loaded after the page rather than inside it, so the text
        baseline is never waiting on the picture drawn above it.

        The whole assembly runs inside one ``lens.tasks.minigraph`` span for
        the reason the graph page's does: it is a multi-phase fan-out, and
        "what did this fragment actually fetch?" is not a question the server
        span can answer.
        """
        snapshot = await state.refresh_health()
        context: dict[str, object] = {
            "config": state.config,
            "health": snapshot,
            "active_view": "tasks",
            "view": None,
            "offline": snapshot.lithos != "ok",
            "error": "",
        }
        with get_tracer().start_as_current_span(MINI_GRAPH_SPAN) as span:
            span.set_attribute("lens.minigraph.task_id", task_id)
            if snapshot.lithos != "ok":
                _record_minigraph(span, outcome="offline")
                return templates.TemplateResponse(
                    request, "tasks/minigraph.html", context
                )
            tasks_config = state.config.tasks
            try:
                # The open snapshot, for the reason the graph page reads it: an
                # OPEN neighbour is already on it, so the common blocker costs
                # no `task_get` at all. A list read that fails is not fatal
                # here — every neighbour is readable on its own — so it
                # degrades to an empty snapshot and more reads, not to no
                # picture.
                master = list(
                    await state.lithos_client.list_tasks(
                        status="open", with_claims=True
                    )
                )
            except Exception:
                logger.warning("mini-graph master read failed", exc_info=True)
                master = []
            try:
                view = await load_mini_graph(
                    state.lithos_client,
                    task_id,
                    master=master,
                    cache=state.graph_cache,
                    limits=MiniGraphLimits(
                        max_nodes=state.config.graph.mini_graph_max_nodes,
                        fetch_concurrency=state.config.graph.fetch_concurrency,
                    ),
                    convention=tasks_config.project_convention,
                    tag_key=tasks_config.project_tag_key,
                )
            except Exception:
                # Including the focal `task_get`: a task Lens cannot read has
                # no neighbourhood to draw, and the fragment says so rather
                # than rendering an empty canvas that reads as "nothing here".
                logger.warning("mini-graph assembly failed", exc_info=True)
                context["error"] = (
                    "The mini-graph could not be loaded. "
                    "The blocker chain below is unaffected."
                )
                _record_minigraph(span, outcome="error")
                return templates.TemplateResponse(
                    request, "tasks/minigraph.html", context
                )
            context["view"] = view
            _record_minigraph(
                span, outcome="capped" if view.capped else "rendered", view=view
            )
            return templates.TemplateResponse(request, "tasks/minigraph.html", context)


def _record_minigraph(
    span: Span, *, outcome: str, view: MiniGraphView | None = None
) -> None:
    """`lens.tasks.minigraph`: the PRD's telemetry point for this fragment.

    The node count and the cap are span attributes and the outcome is the
    counter's label, the same split `_record` makes above: a per-task
    distribution belongs on a span, and a four-value enum is what a fleet
    counter can carry.
    """
    span.set_attribute("lens.minigraph.outcome", outcome)
    if view is not None:
        span.set_attribute("lens.minigraph.nodes", view.node_count)
        span.set_attribute("lens.minigraph.capped", view.capped)
        span.set_attribute("lens.minigraph.not_shown", view.tail.remaining)
        # Empty when the hierarchy tier is settled; a reason when D11's node
        # could not be decided, which is the half of that promise a node count
        # cannot show.
        span.set_attribute(
            "lens.minigraph.parent_epic_unknown", view.parent_epic_unknown
        )
        span.set_attribute("lens.minigraph.cache_hits", view.cache_hits)
        span.set_attribute("lens.minigraph.cache_misses", view.cache_misses)
        span.set_attribute("lens.minigraph.ghost_reads", view.ghost_reads)
    metrics.tasks_minigraph_renders().add(1, {"outcome": outcome})


async def _focused_panel(
    state: AppState, params: GraphPageParams
) -> TaskDetailData | None:
    """The panel `?focus=<id>` asks for, server-rendered (D9, T2-A4).

    `focus` is this page's single selection parameter and it opens the SAME
    panel a dashboard row does — so, like `?selected=` there, a deep link or a
    shared URL has to arrive with the panel already up. The client layer is
    enhancement over this, not the thing that creates it: with scripting off
    there is no canvas to click and the panel is the only way the focused task
    says anything at all.

    Read AFTER the graph rather than beside it. The scope assembly is this
    page's long pole and holds the fan-out semaphore for its whole duration, so
    a concurrent panel read would contend with it for the one MCP session
    rather than overlap it — and a failure here must cost the panel, never the
    graph, which is why it degrades to `None` (an empty host, the same as no
    focus) instead of reaching the route's scope-error branch.
    """
    if not params.focus:
        return None
    tasks_config = state.config.tasks
    try:
        detail = await load_task_detail(
            state.lithos_client,
            params.focus,
            convention=tasks_config.project_convention,
            tag_key=tasks_config.project_tag_key,
        )
    except Exception:
        logger.warning("graph page focus panel failed", exc_info=True)
        return None
    metrics.tasks_panel_opens().add(1, {"source": "url"})
    return detail


async def _master_rows(state: AppState, params: GraphPageParams) -> list[TaskRecord]:
    """The snapshot the page needs — open always, resolved when a branch reads it.

    The open list is what makes an OPEN far endpoint cost no `task_get` (D5)
    and what the picker enumerates. The two bounded `resolved_since` windows
    are added for exactly two branches:

    - the **picker**, because §5B.1's project universe is open tasks PLUS the
      resolved window, and a project whose last task finished yesterday is a
      project the operator can still want a graph of;
    - a **project scope** asking to include resolved tasks, which is where
      those rows become nodes.

    An epic scope needs neither: its closed children come from
    `task_children`, and ghost classification reads its own endpoints, so two
    more list calls per epic page would be work no branch consumes.
    """
    rows = list(await state.lithos_client.list_tasks(status="open", with_claims=True))
    wants_resolved = not params.scoped or (
        params.kind == SCOPE_PROJECT and params.include_resolved
    )
    if not wants_resolved:
        return rows
    since = default_since(state.config.tasks.default_time_range_days)
    for status in ("completed", "cancelled"):
        rows.extend(
            await state.lithos_client.list_tasks(status=status, resolved_since=since)
        )
    return rows


def _record(
    span: Span,
    params: GraphPageParams,
    *,
    outcome: str,
    view: GraphPageView | None = None,
) -> None:
    """`lens.tasks.graph`: span attributes for one page, counter for the fleet.

    The scope KEY (a project slug or an epic id) is deliberately a span
    attribute and never a metric label — one series per project is exactly the
    unbounded cardinality `metrics.py` forbids, while on a span it is what
    makes a slow render findable.

    Every count comes off the VIEW, which carries what this render did. The
    earlier version subtracted the process-wide cache counters around the call
    and therefore attributed a concurrent page's misses to this one.
    """
    span.set_attribute("lens.graph.scope_kind", params.kind or "none")
    span.set_attribute("lens.graph.scope_key", params.key)
    span.set_attribute("lens.graph.outcome", outcome)
    span.set_attribute("lens.graph.include_resolved", params.include_resolved)
    if view is not None:
        span.set_attribute("lens.graph.nodes", len(view.nodes))
        span.set_attribute("lens.graph.edges", view.edge_count)
        span.set_attribute("lens.graph.ghosts", len(view.ghosts))
        span.set_attribute("lens.graph.cycles", view.cycle_count)
        span.set_attribute("lens.graph.isolated", len(view.isolated))
        span.set_attribute("lens.graph.chain_length", view.chain.length)
        span.set_attribute("lens.graph.chain_exact", view.chain.exact)
        span.set_attribute("lens.graph.cache_hits", view.cache_hits)
        span.set_attribute("lens.graph.cache_misses", view.cache_misses)
        span.set_attribute("lens.graph.ghost_reads", view.ghost_reads)
        span.set_attribute("lens.graph.fanout", view.fanout)
        # Planned-but-unissued reads get a span field rather than a counter
        # bucket: `tasks_graph_cycle_reads` counts calls that reached Lithos,
        # so an outcome for calls that did not would break the one thing that
        # counter is for.
        span.set_attribute("lens.graph.cycle_reads_unmade", view.reads_unmade)
        span.set_attribute(
            "lens.graph.cycle_signal_incomplete",
            any(banner.id.startswith("cycle-") for banner in view.banners),
        )
        if view.refusal is not None:
            span.set_attribute("lens.graph.refusal_reason", view.refusal.reason)
            span.set_attribute("lens.graph.refusal_count", view.refusal.count)
    metrics.tasks_graph_renders().add(
        1, {"scope": params.kind or "none", "outcome": outcome}
    )
    if view is not None:
        # The coverage reads get their own counter rather than a span field
        # only: "how often is the cycle signal partial in this deployment?" is
        # a fleet question, and `outcome` is a three-value enum. Its total is
        # the number of scoped blocked calls ISSUED — the three outcomes below
        # are exhaustive over those, and a read the phase deadline left
        # unissued is not one of them (see `lens.graph.cycle_reads_unmade`).
        for read_outcome, count in (
            ("ok", view.reads_ok),
            ("truncated", view.reads_truncated),
            ("failed", view.reads_failed),
        ):
            if count:
                metrics.tasks_graph_cycle_reads().add(count, {"outcome": read_outcome})


def panel_canvas(request: Request, task_id: str) -> DownstreamImpact | None:
    """What a panel can state with NO Lithos read at all (D7/D8, T2-A7).

    The offline path's line, and the reason it has one: D7's position and D8's
    lower bound describe the picture the page is still showing, they arrived in
    this request (:class:`~lithos_lens.graph_snapshot.CanvasNotes`), and a
    health probe that went red since the page loaded changes neither. The panel
    beside them still says Lithos is unavailable — this adds no task detail and
    no D10 figures, which are counted over an assembly this request cannot make
    (round-5 correctness f-008).

    ``None`` for every panel that described no canvas, which is every panel but
    the graph page's.
    """
    return with_canvas_notes(None, _canvas_notes(request), focus=task_id)


def _canvas_notes(request: Request) -> CanvasNotes:
    """The client's account of what it is drawing, off the panel request."""
    return parse_canvas_notes(
        request.query_params.get(PANEL_BOUND_KEY),
        request.query_params.get(PANEL_CHAIN_KEY),
    )


async def panel_impact(
    request: Request, state: AppState, task_id: str, detail: TaskDetailData
) -> DownstreamImpact | None:
    """D10's impact line for a panel fetched with a `scope=` (T2-A7).

    The graph page's own render passes its already-assembled answer through
    (`graph_routes`); this is the path a CLICKED panel takes, where there is no
    page assembly to borrow and the scope has to be rebuilt — affordably, because
    the per-task edge cache is warm for the graph the operator is looking at.

    Degrades to the LINE, never to an error: the impact sits in a panel whose
    other sections are already loaded, so a scope that fails, is refused, or no
    longer holds this task costs it and nothing else — and the rebuild is
    trusted only as far as ``snapshot=`` says (``graph_impact.load_impact``).
    Reconciled here, where the scope is parsed: "was an impact asked for at
    all?" decides whether a panel with no assembly still states D10's resolved
    wording (``graph_impact.reconciled_impact``).
    """
    scope = parse_impact_scope(
        request.query_params.get(PANEL_SCOPE_KEY),
        request.query_params.get("include_resolved"),
        request.query_params.get(PANEL_SNAPSHOT_KEY),
    )
    if not scope.scoped:
        return None
    # What the canvas beside this panel is DRAWING (D7/D8). Stated by the client
    # because this rebuild is a later read of a graph the page is deliberately
    # still showing: it can hold a different picture, or — refused, failed, or
    # no longer holding the focus — none at all, while the operator is looking
    # at a focused node with its neighbourhood lit (round-4 correctness f-006).
    notes = _canvas_notes(request)
    tasks_config = state.config.tasks
    impact = None
    try:
        master = await _impact_master(state, scope)
        impact = await load_impact(
            state.lithos_client,
            scope=scope,
            focus=task_id,
            master=master,
            cache=state.graph_cache,
            limits=GraphScopeLimits(
                max_tasks=state.config.graph.max_tasks,
                fetch_concurrency=state.config.graph.fetch_concurrency,
            ),
            frontier_limit=tasks_config.frontier_limit,
            convention=tasks_config.project_convention,
            tag_key=tasks_config.project_tag_key,
        )
    except Exception:
        logger.warning("panel impact assembly failed", exc_info=True)
    return with_canvas_notes(
        reconciled_impact(impact, detail.task, scoped=True), notes, focus=task_id
    )


async def _impact_master(state: AppState, scope: ImpactScope) -> list[TaskRecord]:
    """The snapshot one impact scope needs — the graph page's own rule.

    Open always; the two bounded ``resolved_since`` windows only for a project
    scope that asked to include resolved tasks, which is the one branch that
    turns those rows into nodes. An epic's closed children come from
    ``task_children``, so they cost no list call here.
    """
    rows = list(await state.lithos_client.list_tasks(status="open"))
    if not (scope.include_resolved and scope.kind == SCOPE_PROJECT):
        return rows
    since = default_since(state.config.tasks.default_time_range_days)
    for status in ("completed", "cancelled"):
        rows.extend(
            await state.lithos_client.list_tasks(status=status, resolved_since=since)
        )
    return rows
