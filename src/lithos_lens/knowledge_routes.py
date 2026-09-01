"""The three knowledge routes and their instrumentation.

Extracted from :mod:`lithos_lens.web` when instrumenting them pushed that
module past the 800-line ceiling — extraction over budget-raising, the repo
convention (`request_filters.py`, `knowledge_produced_by.py` in #40,
`mcp_transport.py`). The seam is the natural one: these three are the whole
knowledge surface, they share no state with the task routes, and they are what
the K1 telemetry points describe.

Registered as a closure over the app, state and templates, which is the shape
`create_app` already uses — not an ``APIRouter``, which would need its own
dependency wiring for the same two objects.
"""

from __future__ import annotations

import time
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lithos_lens import metrics
from lithos_lens.knowledge import RelatedPanel, load_related_panel
from lithos_lens.knowledge_metadata import build_note_metadata
from lithos_lens.knowledge_produced_by import load_produced_by
from lithos_lens.knowledge_resolver import ResolveOutcome, resolve_wiki_link
from lithos_lens.lithos_client import LithosClientProtocol, LithosToolError
from lithos_lens.state import AppState
from lithos_lens.telemetry import get_current_span, get_tracer


async def _traced_related_panel(
    client: LithosClientProtocol, knowledge_id: str, *, cap: int
) -> RelatedPanel:
    """Load a note's related panel inside its own span.

    The one knowledge point that keeps a named span. It is a PHASE within the
    note render rather than the render itself — its own backend calls, its own
    failure mode — so it does not nest 1:1 with the request the way the three
    route-level points do, and "which half of the note page was slow" is a
    question the server span alone cannot answer. See
    `telemetry.get_current_span` for the rule this is the exception to.
    """
    with get_tracer().start_as_current_span("lens.knowledge.related") as span:
        panel = await load_related_panel(client, knowledge_id, title_fanout_cap=cap)
        span.set_attribute("lens.related.fanout", panel.fanout)
        span.set_attribute("lens.related.state", panel.state.value)
        return panel


def register_knowledge_routes(
    app: FastAPI, state: AppState, templates: Jinja2Templates
) -> None:
    """Attach the knowledge landing, wiki-link resolver and note routes."""

    @app.get("/knowledge", response_class=HTMLResponse)
    async def knowledge(request: Request) -> HTMLResponse:
        """Knowledge landing: hybrid search, tag browse, and recently-updated.

        Three branches (§7.1): a ``?q=`` query runs ``lithos_search`` and
        renders hybrid-search result cards (title, escaped snippet, updated);
        a ``?tag=`` filter and the bare landing both run ``lithos_list`` for a
        lightweight note list (tagged, or recently updated). Every branch is
        capped from config so a broad ``?q=a`` / ``?tag=`` cannot materialize
        an unbounded result set (the resolver caps candidates for the same
        reason).
        """
        query = request.query_params.get("q", "").strip()
        tag = request.query_params.get("tag", "").strip()
        snapshot = await state.refresh_health()
        search_results = None
        results = None
        error = ""
        mode = "search" if query else "browse"
        if snapshot.lithos != "ok":
            mode = "offline"
            error = "Lithos is offline or degraded. Knowledge search is unavailable."
        else:
            try:
                if query:
                    search_results = await state.lithos_client.search_notes(
                        query,
                        tags=[tag] if tag else None,
                        limit=state.config.knowledge.search_limit,
                    )
                else:
                    # Both browse branches (tagged and bare) are recency
                    # lists: recent_notes owns the newest-first ordering
                    # lithos_list cannot provide (upstream task e0e31654).
                    results = await state.lithos_client.recent_notes(
                        tags=[tag] if tag else None,
                        limit=state.config.knowledge.recent_limit,
                    )
            except Exception:
                mode = "error"
                error = "Knowledge search is currently unavailable."
        span = get_current_span()
        # The query is deliberately absent from the metric LABEL: one series
        # per distinct search is unbounded cardinality from unauthenticated
        # input. It does reach the SPAN, via the instrumentation's own
        # `http.target`, where it costs no series and helps read a trace -- and
        # is bounded there by `MAX_LOGGED_VALUE_CHARS` (telemetry.py).
        span.set_attribute("lens.mode", mode)
        span.set_attribute("lens.result_count", len(search_results or results or ()))
        span.set_attribute("lens.has_tag", bool(tag))
        metrics.knowledge_searches().add(1, {"mode": mode})
        return templates.TemplateResponse(
            request,
            "knowledge/landing.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "knowledge",
                "query": query,
                "tag": tag,
                "search_results": search_results,
                "results": results,
                "error": error,
            },
        )

    @app.get("/knowledge/resolve")
    async def knowledge_resolve(request: Request):
        """Resolve a clicked ``[[wiki-link]]`` per §6.3 and redirect or explain.

        A confident resolution 302-redirects to the note page; an ambiguous one
        renders a disambiguation page listing candidates; an unresolvable one
        renders an unresolved page offering a search. When Lithos is offline the
        link can't be resolved, so the unresolved page is shown directly.
        """
        target = request.query_params.get("target", "").strip()
        from_id = request.query_params.get("from", "").strip()
        snapshot = await state.refresh_health()
        offline = snapshot.lithos != "ok"
        if offline:
            outcome = ResolveOutcome(
                kind="unresolved", via="offline", target=target, search_query=target
            )
        else:
            outcome = await resolve_wiki_link(state.lithos_client, target, from_id)
        # Recorded BEFORE the redirect returns: the confident resolutions are
        # the ones that leave early, so measuring after the branch would count
        # only the failures and make resolution look broken. `via`, not `kind`
        # — the latter answers "redirect" for the uuid, path and single-title
        # arms alike (see ResolveOutcome).
        span = get_current_span()
        span.set_attribute("lens.outcome", outcome.via)
        span.set_attribute("lens.candidate_count", outcome.candidate_count)
        metrics.knowledge_resolves().add(1, {"outcome": outcome.via})
        if outcome.kind == "redirect":
            return RedirectResponse(
                f"/note/{quote(outcome.target_id)}", status_code=302
            )
        return templates.TemplateResponse(
            request,
            "knowledge/resolve.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "knowledge",
                "outcome": outcome,
                "offline": offline,
            },
        )

    @app.get("/note/{knowledge_id}", response_class=HTMLResponse)
    async def note(request: Request, knowledge_id: str) -> HTMLResponse:
        snapshot = await state.refresh_health()
        note_record = None
        note_meta = None
        task = None
        related = None
        produced_by = None
        error = ""
        outcome = "rendered"
        related_seconds = 0.0
        if snapshot.lithos != "ok":
            outcome = "offline"
            error = "Lithos is offline or degraded. The note cannot be loaded."
        else:
            not_found = False
            try:
                note_record = await state.lithos_client.read_note(knowledge_id)
            except LithosToolError as exc:
                # Lithos answers a missing document with a coded error envelope
                # (doc_not_found) rather than an empty success, so this — not
                # the None fallback below — is the production not-found path.
                if exc.code == "doc_not_found":
                    not_found = True
                else:
                    error = "Could not load this document from Lithos."
            except Exception:
                error = "Could not load this document from Lithos."
            if not_found or (note_record is None and not error):
                error = "Document not found."
                outcome = "not_found"
            elif error:
                outcome = "error"
            if note_record is not None:
                note_meta = build_note_metadata(note_record)
                started = time.perf_counter()
                related = await _traced_related_panel(
                    state.lithos_client,
                    knowledge_id,
                    cap=state.config.knowledge.related_title_fanout_cap,
                )
                related_seconds = time.perf_counter() - started
                produced_by = await load_produced_by(state.lithos_client, note_record)
            task_id = request.query_params.get("task", "")
            if task_id:
                try:
                    # Addressed directly, like the detail page since T1-S7:
                    # the three-list scan `find_task` did is gone. A dead
                    # ?task= link answers task_not_found and drops the
                    # back-link rather than failing the document render.
                    task = await state.lithos_client.task_get(task_id)
                except Exception:
                    task = None
        span = get_current_span()
        span.set_attribute("lens.outcome", outcome)
        metrics.knowledge_note_renders().add(1, {"outcome": outcome})
        if related is not None:
            # Only when the panel was actually loaded. Recording a zero for the
            # offline and not-found paths would drag the latency distribution
            # toward nothing and make a slow panel look fast on average.
            span.set_attribute("lens.related.duration_ms", related_seconds * 1000)
            span.set_attribute("lens.related.fanout", related.fanout)
            span.set_attribute("lens.related.state", related.state.value)
            metrics.knowledge_related_duration().record(related_seconds)
            metrics.knowledge_related_fanout().record(related.fanout)
        return templates.TemplateResponse(
            request,
            "note.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "knowledge",
                "note": note_record,
                "note_meta": note_meta,
                "task": task,
                "related": related,
                "produced_by": produced_by,
                "error": error,
            },
        )
