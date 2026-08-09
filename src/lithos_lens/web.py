"""FastAPI application factory."""

from __future__ import annotations

import logging
from asyncio import CancelledError
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lithos_lens.config import LithosLensConfig
from lithos_lens.events import LensEvent
from lithos_lens.fake_lithos import (
    FakeEventHub,
    FakeLithosClient,
    fake_lithos_enabled,
)
from lithos_lens.frontier import load_dashboard
from lithos_lens.knowledge import (
    ResolveOutcome,
    load_produced_by,
    load_related_panel,
    render_markdown,
    resolve_wiki_link,
)
from lithos_lens.knowledge_metadata import build_note_metadata
from lithos_lens.lithos_client import (
    LithosClient,
    LithosClientProtocol,
    LithosToolError,
)
from lithos_lens.state import AppState
from lithos_lens.tasks import (
    default_since,
    find_task,
    format_display_date,
    format_tag,
    load_task_detail,
    parse_filters,
)
from lithos_lens.telemetry import install_request_middleware

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"
logger = logging.getLogger(__name__)

# Cap on the recently-updated list shown by /knowledge with no query. A local
# constant, not config: K1-S6 introduces the [knowledge].recent_limit dial when
# it builds out the full landing page.
_KNOWLEDGE_RECENT_LIMIT = 20

LithosClientFactory = Callable[[LithosLensConfig], LithosClientProtocol]


def _default_lithos_client(config: LithosLensConfig) -> LithosClientProtocol:
    """Pick the real client, or the in-memory fake when fake-Lithos mode is on.

    ``LITHOS_LENS_FAKE_LITHOS=1`` boots the app against
    :class:`~lithos_lens.fake_lithos.FakeLithosClient` so the whole UI is
    browsable with no Lithos server behind it (used by the ``e2e/`` Playwright
    smoke suite and for offline demos).
    """
    if fake_lithos_enabled():
        # Loud on purpose: fake mode fabricates task/note data and pins /health
        # to "ok", so a real Lithos outage would report healthy. Never enable it
        # against a real deployment; the warning makes an accidental/leaked flag
        # visible in the logs rather than silently masking a backend.
        logger.warning(
            "fake-Lithos app mode is ENABLED (LITHOS_LENS_FAKE_LITHOS): serving "
            "in-memory demo fixtures and reporting health as ok — do NOT use "
            "against a real Lithos deployment; it masks backend outages."
        )
        return FakeLithosClient(config.lithos)
    return LithosClient(config.lithos)


def create_app(
    config: LithosLensConfig,
    *,
    lithos_client_factory: LithosClientFactory | None = None,
) -> FastAPI:
    """Create the Lithos Lens ASGI app."""

    factory = lithos_client_factory or _default_lithos_client
    # Fake mode must be hermetic: swapping only the client would still leave
    # the real EventHub dialing the configured Lithos /events URL, so the
    # in-process hub is injected alongside the fake client.
    events = (
        FakeEventHub(config.events, config.lithos) if fake_lithos_enabled() else None
    )
    state = AppState(config, factory(config), events=events)
    templates = Jinja2Templates(directory=TEMPLATE_DIR)
    templates.env.filters["format_tag"] = format_tag
    templates.env.filters["display_date"] = format_display_date
    templates.env.filters["render_markdown"] = render_markdown
    templates.env.globals["task_tag_url"] = task_tag_url
    templates.env.globals["task_detail_url"] = task_detail_url
    templates.env.globals["tasks_url"] = tasks_url
    templates.env.globals["tag_chip_class"] = tag_chip_class
    templates.env.globals["knowledge_tag_url"] = knowledge_tag_url

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="Lithos Lens", lifespan=lifespan)
    app.state.lens = state
    install_request_middleware(app, config.telemetry)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/health")
    async def health() -> dict[str, str]:
        snapshot = await state.refresh_health()
        return {
            "status": snapshot.status,
            "lithos": snapshot.lithos,
            "events": snapshot.events,
            "llm": snapshot.llm,
        }

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> HTMLResponse:
        return await _render_tasks(request, templates, state)

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks(request: Request) -> HTMLResponse:
        return await _render_tasks(request, templates, state)

    @app.get("/tasks/events")
    async def task_events() -> StreamingResponse:
        queue = state.events.subscribe()

        async def stream():
            try:
                yield 'event: lens.status\ndata: {"status":"connected"}\n\n'
                while True:
                    event = await queue.get()
                    yield event.as_sse()
            except CancelledError:
                raise
            finally:
                state.events.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    if fake_lithos_enabled():
        # Fake-mode-only harness seam: lets the browser suite drive the REAL
        # SSE path (publish -> hub -> /tasks/events -> EventSource ->
        # tasks.js) without a Lithos server. Never registered outside fake
        # mode, so production has no injection surface.
        @app.post("/tasks/events/publish")
        async def publish_test_event(request: Request) -> JSONResponse:
            data = await request.json()
            event = LensEvent(
                id=str(data.get("id") or ""),
                type=str(data.get("type") or "task.created"),
                task_id=str(data.get("task_id") or ""),
                payload=dict(data.get("payload") or {}),
                requires_refresh=bool(data.get("requires_refresh", True)),
            )
            await state.events.publish(event)
            return JSONResponse({"published": True}, status_code=202)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: str) -> HTMLResponse:
        snapshot = await state.refresh_health()
        if snapshot.lithos != "ok":
            return templates.TemplateResponse(
                request,
                "tasks/detail.html",
                {
                    "config": state.config,
                    "health": snapshot,
                    "active_view": "tasks",
                    "detail": None,
                    "offline": True,
                },
            )
        detail = await load_task_detail(state.lithos_client, task_id)
        return templates.TemplateResponse(
            request,
            "tasks/detail.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "tasks",
                "detail": detail,
                "offline": False,
            },
        )

    @app.get("/tasks/{task_id}/findings", response_class=HTMLResponse)
    async def task_findings(request: Request, task_id: str) -> HTMLResponse:
        snapshot = await state.refresh_health()
        if snapshot.lithos != "ok":
            return templates.TemplateResponse(
                request,
                "tasks/findings.html",
                {
                    "config": state.config,
                    "health": snapshot,
                    "active_view": "tasks",
                    "detail": None,
                    "offline": True,
                },
            )
        detail = await load_task_detail(state.lithos_client, task_id)
        return templates.TemplateResponse(
            request,
            "tasks/findings.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "tasks",
                "detail": detail,
                "offline": False,
            },
        )

    @app.get("/knowledge", response_class=HTMLResponse)
    async def knowledge(request: Request) -> HTMLResponse:
        """Knowledge landing: a title search and a recently-updated browse list.

        K1-S2 needs this so the resolver's "unresolved link" page can offer a
        working ``/knowledge?q=…`` search instead of a dead 404. It is the
        minimal functional surface — a ``lithos_list`` title/tag search plus a
        recent list; hybrid ``lithos_search`` cards, snippets, and the nav search
        box are K1-S6.
        """
        query = request.query_params.get("q", "").strip()
        tag = request.query_params.get("tag", "").strip()
        snapshot = await state.refresh_health()
        results = None
        error = ""
        if snapshot.lithos != "ok":
            error = "Lithos is offline or degraded. Knowledge search is unavailable."
        else:
            try:
                # Every branch is capped: a broad ``?q=a`` / ``?tag=`` must not
                # be able to materialize and render an unbounded result set (the
                # resolver caps candidates for the same reason).
                if query:
                    results = await state.lithos_client.list_notes(
                        title_contains=query, limit=_KNOWLEDGE_RECENT_LIMIT
                    )
                elif tag:
                    results = await state.lithos_client.list_notes(
                        tags=[tag], limit=_KNOWLEDGE_RECENT_LIMIT
                    )
                else:
                    results = await state.lithos_client.list_notes(
                        limit=_KNOWLEDGE_RECENT_LIMIT
                    )
            except Exception:
                error = "Knowledge search is currently unavailable."
        return templates.TemplateResponse(
            request,
            "knowledge/landing.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "knowledge",
                "query": query,
                "tag": tag,
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
                kind="unresolved", target=target, search_query=target
            )
        else:
            outcome = await resolve_wiki_link(state.lithos_client, target, from_id)
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
        if snapshot.lithos != "ok":
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
            if note_record is not None:
                note_meta = build_note_metadata(note_record)
                related = await load_related_panel(
                    state.lithos_client,
                    knowledge_id,
                    title_fanout_cap=state.config.knowledge.related_title_fanout_cap,
                )
                produced_by = await load_produced_by(state.lithos_client, note_record)
            task_id = request.query_params.get("task", "")
            if task_id:
                try:
                    task = await find_task(state.lithos_client, task_id)
                except Exception:
                    task = None
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

    return app


async def _render_tasks(
    request: Request,
    templates: Jinja2Templates,
    state: AppState,
) -> HTMLResponse:
    snapshot = await state.refresh_health()
    dashboard = None
    if snapshot.lithos == "ok":
        query_items = list(request.query_params.multi_items())
        filters = parse_filters(
            query_items,
            state.config.tasks.default_time_range_days,
            state.config.tasks.default_status_groups,
        )
        logger.debug(
            "tasks dashboard filters parsed",
            extra={
                "lens_route": str(request.url.path),
                "query_items": query_items,
                "statuses": list(filters.statuses),
                "tags": list(filters.tags),
                "agent": filters.agent,
                "since": filters.since,
                "frontier_limit": state.config.tasks.frontier_limit,
            },
        )
        dashboard = await load_dashboard(
            state.lithos_client,
            filters=filters,
            frontier_limit=state.config.tasks.frontier_limit,
        )
        logger.debug(
            "tasks dashboard loaded",
            extra={
                "lens_route": str(request.url.path),
                "statuses": list(filters.statuses),
                "tags": list(filters.tags),
                "agent": filters.agent,
                "since": filters.since,
                "frontier_limit": dashboard.frontier_limit,
                "open_total": dashboard.open_total,
                "section_counts": {
                    section: len(rows) for section, rows in dashboard.sections.items()
                },
                "truncated": dashboard.truncated,
                "errors": list(dashboard.errors),
            },
        )
    return templates.TemplateResponse(
        request,
        "tasks/dashboard.html",
        {
            "config": state.config,
            "health": snapshot,
            "active_view": "tasks",
            "dashboard": dashboard,
            "default_since": default_since(state.config.tasks.default_time_range_days),
        },
    )


# The live /tasks filter vocabulary. Every generated tasks URL rebuilds its
# query from this allowlist, so a retired param (e.g. the pre-T1
# ``claimed_state``) carried by a legacy bookmark degrades on arrival instead
# of propagating through tag / detail / back-link navigation forever.
_PRESERVED_FILTER_KEYS = ("status", "agent", "since", "tag")


def _preserved_filter_params(
    request: Request, *, exclude: str = ""
) -> list[tuple[str, str]]:
    return [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key in _PRESERVED_FILTER_KEYS and key != exclude and value
    ]


def task_tag_url(request: Request, tag: str) -> str:
    params = _preserved_filter_params(request, exclude="tag")
    params.append(("tag", tag))
    return f"/tasks?{urlencode(params)}"


def task_detail_url(request: Request, task_id: str) -> str:
    params = _preserved_filter_params(request)
    suffix = f"?{urlencode(params)}" if params else ""
    return f"/tasks/{quote(task_id)}{suffix}"


def tasks_url(request: Request) -> str:
    params = _preserved_filter_params(request)
    return f"/tasks?{urlencode(params)}" if params else "/tasks"


def knowledge_tag_url(tag: str) -> str:
    """Link a note-page tag chip to the ``/knowledge`` list filtered by it (§6.4)."""
    return f"/knowledge?{urlencode({'tag': tag})}"


def tag_chip_class(tag: str) -> str:
    classes = ["tag-chip"]
    if tag.startswith("project:"):
        classes.append("tag-chip-project")
    return " ".join(classes)
