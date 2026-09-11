"""The `/tasks/graph` route and its instrumentation (T2-A3).

Its own module for the reason `knowledge_routes.py` is: the task routes in
`web.py` are already at the 800-line ceiling, and this page's assembly — one
master read, one scope, one cycle-signal fan-out, one fold — shares no state
with them. Registered as a closure over app/state/templates, the shape
`create_app` already uses.

Registration ORDER matters and is the one thing a reader must not rearrange:
`/tasks/graph` has to be attached before `/tasks/{task_id}`, or Starlette
matches the dynamic route first and the graph page becomes a task detail for a
task called "graph".
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from lithos_lens import metrics
from lithos_lens.graph_page import (
    EDGE_LEGEND,
    SCOPE_EPIC,
    GraphPageParams,
    GraphPageView,
    graph_url,
    load_graph_page,
    observed_projects,
    open_epics,
    parse_graph_params,
)
from lithos_lens.graph_scope import GraphScopeLimits
from lithos_lens.state import AppState
from lithos_lens.tasks import TaskRecord, default_since
from lithos_lens.telemetry import get_current_span

logger = logging.getLogger(__name__)


def register_graph_routes(
    app: FastAPI, state: AppState, templates: Jinja2Templates
) -> None:
    """Attach `GET /tasks/graph` (scope picker + one scope's graph)."""

    # The graph page's own URL vocabulary: its query state is not the `/tasks`
    # filter set, so it does not travel through `request_filters`.
    templates.env.globals["graph_url"] = graph_url
    templates.env.globals["edge_legend"] = EDGE_LEGEND

    @app.get("/tasks/graph", response_class=HTMLResponse)
    async def tasks_graph(request: Request) -> HTMLResponse:
        """The dependency graph of one scope, or the picker when none is given.

        The no-JS baseline is the WHOLE page here (D3): layers, callout,
        chain, hierarchy and the embedded payload are all server-rendered, and
        A4's Cytoscape layer is enhancement over the same payload.
        """
        params = parse_graph_params(dict(request.query_params))
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
        }
        if snapshot.lithos != "ok":
            _record(params, outcome="offline")
            return templates.TemplateResponse(request, "tasks/graph.html", context)

        tasks_config = state.config.tasks
        try:
            master = await _master_rows(state, params)
        except Exception:
            logger.warning("graph page master read failed", exc_info=True)
            context["error"] = "Task data is unavailable. Lens will retry on refresh."
            _record(params, outcome="error")
            return templates.TemplateResponse(request, "tasks/graph.html", context)

        if not params.scoped:
            context["projects"] = observed_projects(
                master, tag_key=tasks_config.project_tag_key
            )
            context["epics"] = open_epics(master)
            _record(params, outcome="picker")
            return templates.TemplateResponse(request, "tasks/graph.html", context)

        cache = state.graph_cache
        before = (cache.hits, cache.misses)
        try:
            view = await load_graph_page(
                state.lithos_client,
                params=params,
                master=master,
                cache=cache,
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
            _record(params, outcome="error")
            return templates.TemplateResponse(request, "tasks/graph.html", context)

        context["view"] = view
        _record(
            params,
            outcome="refused" if view.refused else "rendered",
            view=view,
            cache_hits=cache.hits - before[0],
            cache_misses=cache.misses - before[1],
        )
        return templates.TemplateResponse(request, "tasks/graph.html", context)


async def _master_rows(state: AppState, params: GraphPageParams) -> list[TaskRecord]:
    """The snapshot the page needs — open always, resolved only when it counts.

    The open list is what makes an OPEN far endpoint cost no `task_get` (D5)
    and what the picker enumerates. Resolved rows are fetched only for a
    project scope asking to include them: an epic's closed children come from
    `task_children`, and ghost resolution reads its own endpoints, so loading
    two more windows for every epic page would be work no branch consumes.
    """
    rows = list(await state.lithos_client.list_tasks(status="open", with_claims=True))
    if params.kind == SCOPE_EPIC or not params.include_resolved:
        return rows
    since = default_since(state.config.tasks.default_time_range_days)
    for status in ("completed", "cancelled"):
        rows.extend(
            await state.lithos_client.list_tasks(status=status, resolved_since=since)
        )
    return rows


def _record(
    params: GraphPageParams,
    *,
    outcome: str,
    view: GraphPageView | None = None,
    cache_hits: int = 0,
    cache_misses: int = 0,
) -> None:
    """`lens.tasks.graph`: span attributes for one page, counter for the fleet.

    The scope KEY (a project slug or an epic id) is deliberately a span
    attribute and never a metric label — one series per project is exactly the
    unbounded cardinality `metrics.py` forbids, while on a span it is what
    makes a slow render findable.
    """
    span = get_current_span()
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
        span.set_attribute("lens.graph.cache_hits", cache_hits)
        span.set_attribute("lens.graph.cache_misses", cache_misses)
        span.set_attribute("lens.graph.fanout", cache_misses)
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
        # a fleet question, and `outcome` is a three-value enum.
        for read_outcome, count in (
            ("ok", view.reads_ok),
            ("truncated", view.reads_truncated),
            ("failed", view.reads_failed),
        ):
            if count:
                metrics.tasks_graph_cycle_reads().add(count, {"outcome": read_outcome})
