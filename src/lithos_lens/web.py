"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from asyncio import CancelledError
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
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
from lithos_lens.frontier import AttentionPolicy, load_dashboard
from lithos_lens.knowledge import (
    ResolveOutcome,
    load_related_panel,
    render_markdown,
    resolve_wiki_link,
)
from lithos_lens.knowledge_metadata import build_note_metadata
from lithos_lens.knowledge_produced_by import load_produced_by
from lithos_lens.lithos_client import (
    LithosClient,
    LithosClientProtocol,
    LithosToolError,
)
from lithos_lens.request_filters import (
    board_is_filtered,
    epic_scope_url,
    filter_query_oversized,
    knowledge_tag_url,
    note_url,
    tag_chip_class,
    task_card_url,
    task_detail_url,
    task_tag_clear_url,
    task_tag_url,
    tasks_url,
)
from lithos_lens.state import AppState
from lithos_lens.task_detail import load_task_detail
from lithos_lens.tasks import (
    MAX_FILTER_QUERY_BYTES,
    MAX_FILTER_TAG_CHIPS,
    default_since,
    format_display_date,
    format_tag,
    parse_filters,
)
from lithos_lens.telemetry import install_request_middleware

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"
logger = logging.getLogger(__name__)

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


# How many concurrent RENDERS this process admits before refusing. The
# expensive half of a request is the Lithos fan-out and the template render,
# both finished before the response object exists, so this brackets the work
# rather than the socket. Past it Lens answers 503 immediately — a fast, honest
# refusal in front of a saturated backend, rather than a queue that grows until
# the process does. Lens takes unauthenticated requests across the
# trusted-network boundary, so the arrival rate is not Lens's to choose.
MAX_CONCURRENT_RENDERS = 128

# What admission control deliberately does NOT meter. Each of these would be
# made WORSE by refusing it under load, not better:
#
# ``/tasks/events`` — an SSE connection is not a render. It does no Lithos work
# and is held open for as long as a tab is. Metering it spends the render
# budget on parked browsers and refuses real requests while the backend sits
# idle, with N open tabs consuming N slots permanently. This is why the bound
# lives here rather than in uvicorn's ``limit_concurrency``, which counts
# connections and cannot tell the two apart.
#
# ``/health`` — REQUIREMENTS §4 makes this the container health check. A 503
# under load tells the orchestrator the container is unhealthy, so it restarts
# a process that was merely busy: a load spike becomes a restart loop, and the
# saturation the cap exists to survive is converted into an outage. The probe
# must be able to say "busy but alive", which it cannot do if it never runs.
#
# ``/static/*`` — served from disk, no Lithos call, and needed BY the pages
# that were admitted. Refusing assets to a page whose HTML got through renders
# it unstyled and inert, spending a slot to produce a broken result.
_UNMETERED_EXACT = frozenset({"/health", "/tasks/events"})
_UNMETERED_PREFIXES = ("/static/",)


def _is_metered(path: str) -> bool:
    """Whether admission control applies to ``path``.

    Default-metered: a new route is bounded unless it is deliberately listed
    above, which is the safe direction for a bound whose job is to survive
    saturation.
    """
    if path in _UNMETERED_EXACT:
        return False
    return not path.startswith(_UNMETERED_PREFIXES)


# How long the event stream waits for an event before emitting a comment frame.
# The stream otherwise blocks on ``queue.get()`` forever and only discovers a
# departed client when it next WRITES — which, in a quiet period, is never. A
# slept laptop or a dropped NAT mapping would then park a subscriber and its
# queue for the life of the process. The keepalive is what makes a dead peer
# surface: the write fails, the generator unwinds, and ``unsubscribe`` runs.
SSE_KEEPALIVE_S = 20.0


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
    templates.env.globals["task_tag_clear_url"] = task_tag_clear_url
    templates.env.globals["task_detail_url"] = task_detail_url
    templates.env.globals["tasks_url"] = tasks_url
    templates.env.globals["epic_scope_url"] = epic_scope_url
    templates.env.globals["task_card_url"] = task_card_url
    templates.env.globals["tag_chip_class"] = tag_chip_class
    templates.env.globals["knowledge_tag_url"] = knowledge_tag_url
    templates.env.globals["note_url"] = note_url
    templates.env.globals["board_is_filtered"] = board_is_filtered

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="Lithos Lens", lifespan=lifespan)

    render_gate = asyncio.Semaphore(MAX_CONCURRENT_RENDERS)

    @app.middleware("http")
    async def limit_concurrent_renders(request: Request, call_next):
        """Refuse past :data:`MAX_CONCURRENT_RENDERS`, never queue.

        ``Semaphore.acquire`` takes a fast path when a slot is free and does
        not yield, so nothing interleaves between the check and the acquire.
        The slot is released when ``call_next`` returns, which is after the
        reads and the template render and before the body is streamed — the
        expensive half, which is the half worth bounding.
        """
        if not _is_metered(request.url.path):
            return await call_next(request)
        if render_gate.locked():
            return PlainTextResponse(
                "Lens is at capacity. Retry shortly.", status_code=503
            )
        async with render_gate:
            return await call_next(request)

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
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=SSE_KEEPALIVE_S
                        )
                    except TimeoutError:
                        # A comment frame: ignored by EventSource, but a WRITE,
                        # which is the only way this end learns the peer left.
                        yield ": keepalive\n\n"
                        continue
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
        if filter_query_oversized(request):
            return await _reject_oversized_filters(
                request, templates, state, "tasks/detail.html"
            )
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
        if filter_query_oversized(request):
            return await _reject_oversized_filters(
                request, templates, state, "tasks/findings.html"
            )
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
        if snapshot.lithos != "ok":
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
                    # Addressed directly, like the detail page since T1-S7:
                    # the three-list scan `find_task` did is gone. A dead
                    # ?task= link answers task_not_found and drops the
                    # back-link rather than failing the document render.
                    task = await state.lithos_client.task_get(task_id)
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


def _route_template(request: Request) -> str:
    """The matched route's template (``/tasks/{task_id}``), not the concrete path.

    ``task_id`` is a free path segment bounded only by the URL length limit and
    NOT by ``MAX_FILTER_QUERY_BYTES``, which measures the query string. Logging
    the concrete path let a 50 KB request write a 50 KB log line — a persistent
    sink for a transient input, on a container whose json-file driver has no
    size cap — while the response itself stayed correctly bounded at ~1.5 KB.

    The template is also what the ``lens_route`` field name implies, and what
    aggregates across requests.
    """
    template = getattr(request.scope.get("route"), "path", "")
    return template if isinstance(template, str) and template else "<unmatched>"


async def _reject_oversized_filters(
    request: Request,
    templates: Jinja2Templates,
    state: AppState,
    template: str,
) -> HTMLResponse:
    """Answer an over-budget filter query explicitly, before any Lithos read.

    Shared by every route that re-emits the preserved filters into generated
    URLs — the board and the detail pages alike — because the amplification
    lives in that shared helper, not in one route.

    The response deliberately does NOT echo the offending value: reflecting it
    is the whole problem. And it refuses rather than trimming, because a
    trimmed filter renders a WIDER board than the one requested, with chrome
    claiming a scope that is not applied.
    """
    logger.warning(
        "filter query rejected as oversized",
        extra={
            "lens_route": _route_template(request),
            "query_bytes": len(request.url.query or ""),
            "max_filter_query_bytes": MAX_FILTER_QUERY_BYTES,
        },
    )
    return templates.TemplateResponse(
        request,
        template,
        {
            "config": state.config,
            "health": await state.refresh_health(),
            "active_view": "tasks",
            "dashboard": None,
            "detail": None,
            "offline": False,
            "filter_query_rejected": True,
            "max_filter_query_bytes": MAX_FILTER_QUERY_BYTES,
            "default_since": default_since(state.config.tasks.default_time_range_days),
        },
        status_code=400,
    )


async def _render_tasks(
    request: Request,
    templates: Jinja2Templates,
    state: AppState,
) -> HTMLResponse:
    if filter_query_oversized(request):
        return await _reject_oversized_filters(
            request, templates, state, "tasks/dashboard.html"
        )
    snapshot = await state.refresh_health()
    dashboard = None
    if snapshot.lithos == "ok":
        query_items = list(request.query_params.multi_items())
        filters = parse_filters(
            query_items,
            state.config.tasks.default_time_range_days,
            state.config.tasks.default_status_groups,
            project_convention=state.config.tasks.project_convention,
            project_tag_key=state.config.tasks.project_tag_key,
        )
        logger.debug(
            "tasks dashboard filters parsed",
            extra={
                "lens_route": _route_template(request),
                # Only the COUNT of raw pairs: params outside
                # _PRESERVED_FILTER_KEYS score zero against
                # MAX_FILTER_QUERY_BYTES, so the raw list is unbounded
                # attacker-controlled data (a 47 KB junk query wrote 72 KB of
                # log). Under the container's size-capped rotation that buys
                # cheap eviction of the log history, which on a service with no
                # authentication is the only forensic record there is. The
                # parsed filters below carry the diagnostic value; the count
                # keeps the "was there junk on this request?" signal.
                "query_param_count": len(query_items),
                "statuses": list(filters.statuses),
                "projects": list(filters.projects),
                "tags": list(filters.tags),
                "agent": filters.agent,
                "since": filters.since,
                "epic": filters.epic,
                "frontier_limit": state.config.tasks.frontier_limit,
            },
        )
        tasks_config = state.config.tasks
        dashboard = await load_dashboard(
            state.lithos_client,
            filters=filters,
            frontier_limit=tasks_config.frontier_limit,
            attention=AttentionPolicy(
                gate_waiting_attention_hours=tasks_config.gate_waiting_attention_hours,
                claim_expiring_soon_minutes=tasks_config.claim_expiring_soon_minutes,
                stale_open_age_days=tasks_config.stale_open_age_days,
                unclaimed_ready_age_minutes=tasks_config.unclaimed_ready_age_minutes,
            ),
        )
        logger.debug(
            "tasks dashboard loaded",
            extra={
                "lens_route": _route_template(request),
                "statuses": list(filters.statuses),
                "projects": list(filters.projects),
                "tags": list(filters.tags),
                "agent": filters.agent,
                "since": filters.since,
                "epic_scope": dashboard.epic_scope,
                "frontier_limit": dashboard.frontier_limit,
                "open_total": dashboard.open_total,
                "attention": dashboard.summary.attention,
                "section_counts": {
                    section: len(rows) for section, rows in dashboard.sections.items()
                },
                "truncated": dashboard.truncated,
                "nothing_to_show": dashboard.nothing_to_show,
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
            "max_tag_chips": MAX_FILTER_TAG_CHIPS,
            "default_since": default_since(state.config.tasks.default_time_range_days),
        },
    )


# The live /tasks filter vocabulary. Every generated tasks URL rebuilds its
# query from this allowlist, so a retired param (e.g. the pre-T1
# ``claimed_state``) carried by a legacy bookmark degrades on arrival instead
# of propagating through tag / detail / back-link navigation forever.
