"""FastAPI application factory."""

from __future__ import annotations

import logging
from asyncio import CancelledError
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
from lithos_lens.state import AppState
from lithos_lens.task_detail import (
    find_task,
    load_task_detail,
)
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
        if _filter_query_oversized(request):
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
        if _filter_query_oversized(request):
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
    if _filter_query_oversized(request):
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
_PRESERVED_FILTER_KEYS = ("status", "project", "agent", "since", "tag", "epic")


@dataclass(frozen=True)
class _RequestFilters:
    """This request's preserved filters, parsed once (see ``_request_filters``)."""

    oversized: bool
    params: tuple[tuple[str, str], ...]


# Where the parsed filters hang off the request. The URL helpers below run once
# per generated link — one per row, one per tag per row, plus the summary cards
# and the epic chips — and each scan is O(every param on the request), not
# O(the six preserved ones. Recomputing per link cost O(J x L) for J params and
# L links, and J is bounded only by the URL length limit: an unrecognised key
# scores nothing against the byte budget but still has to be walked. Parsed
# once, a request costs O(J + L).
_FILTER_STATE_ATTR = "lens_preserved_filters"


def _parse_preserved_filters(request: Request) -> _RequestFilters:
    """Scan the query string once: the preserved filters, and their emitted size.

    The size is the length of the exact string that will go into the links —
    ``urlencode`` run over the very pairs this returns — rather than any
    estimate of it. Both ways of estimating have now been wrong: counting code
    points ignored percent-encoding, where one character can cost 12 bytes (an
    astral character becomes ``%F0%9F%98%80``), and letting a value at the
    budget emit 12x it; and adding two separator bytes per pair over-counted by
    one, because ``urlencode`` writes "=" per pair but "&" only BETWEEN them,
    so a query of exactly ``MAX_FILTER_QUERY_BYTES`` was refused by a ceiling
    documented — and displayed to the operator — as rejecting only what is
    larger. Measuring the real thing cannot drift from it.

    Empty-valued keys cost nothing because they are not emitted: this is a
    bound on what the response reflects, not on what the request said.
    """
    params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key in _PRESERVED_FILTER_KEYS and value
    ]
    if len(urlencode(params)) > MAX_FILTER_QUERY_BYTES:
        # An oversized request preserves NOTHING, so the guarantee holds at the
        # choke point rather than per template: the refusal page still renders
        # its own chrome (the "back to tasks" link), and every URL on it must
        # come out unfiltered — otherwise the page refusing to reflect the
        # value reflects it anyway, once per link.
        return _RequestFilters(oversized=True, params=())
    return _RequestFilters(oversized=False, params=tuple(params))


def _request_filters(request: Request) -> _RequestFilters:
    """This request's parsed filters, computed on first use and then reused."""
    cached = getattr(request.state, _FILTER_STATE_ATTR, None)
    if cached is None:
        cached = _parse_preserved_filters(request)
        setattr(request.state, _FILTER_STATE_ATTR, cached)
    return cached


def _filter_query_oversized(request: Request) -> bool:
    """True when this request's filters exceed ``MAX_FILTER_QUERY_BYTES``.

    Measured over the preserved keys only — they are the ones re-emitted into
    every generated URL, and so the ones whose size the response multiplies.
    An oversized request is answered explicitly rather than served as though
    the offending filter had not been sent: see the constant for why trimming
    is not an option.
    """
    return _request_filters(request).oversized


def _preserved_filter_params(
    request: Request, *, exclude: str = ""
) -> list[tuple[str, str]]:
    """The live filters of this request, ready to re-emit into a generated URL.

    Echoed verbatim, every key alike — the routes refuse an over-budget query
    before rendering, so there is nothing left here to trim, and trimming per
    key was the wrong shape anyway: it dropped filter terms the board had been
    asked for.
    """
    return [
        (key, value)
        for key, value in _request_filters(request).params
        if key != exclude
    ]


def task_tag_url(request: Request, tag: str) -> str:
    params = _preserved_filter_params(request, exclude="tag")
    params.append(("tag", tag))
    return f"/tasks?{urlencode(params)}"


def task_tag_clear_url(request: Request, tags: Sequence[str], tag: str) -> str:
    """Link an active-filter chip to the same board WITHOUT that one tag.

    The chip is the only place the ``?tag=`` scope is named: a cross-project
    tag (the monthly-roadmap convention) belongs to no project, so nothing else
    on the board says what the slice is — the row chips name each row's own
    tags, not the filter. Clearing one chip keeps the other tags rather than
    dropping the whole filter.

    ``tags`` is the HONOURED list (``TaskFilters.tags``) and NOT the raw query
    string: the two spellings (repeated and comma-joined) both fold into it, so
    rebuilding from the request would emit a tag twice or miss a duplicate it
    had collapsed. It also keeps the link truthful — clearing one chip cannot
    resurrect a tag the board is not filtering by.

    One repeated ``tag`` pair per remaining tag — the same and only spelling the
    request itself uses, now that tags are literal. That is what keeps the link
    inside ``MAX_FILTER_QUERY_BYTES``, which matters because a chip that renders
    but 400s when clicked is worse than no chip: encoding the output exactly as
    the input was encoded makes this link a strict subset of the request that
    was already accepted, so it cannot overflow a budget the request cleared.

    Round 6 comma-joined them to solve that budget problem, which worked only
    because tags were being split on commas — the very thing that made the real
    tag ``customer,2`` unnameable. With literal tags the two are the same
    representation and the budget property falls out for free.
    """
    params = _preserved_filter_params(request, exclude="tag")
    params.extend(("tag", other) for other in tags if other != tag)
    return f"/tasks?{urlencode(params)}" if params else "/tasks"


def task_detail_url(request: Request, task_id: str) -> str:
    params = _preserved_filter_params(request)
    suffix = f"?{urlencode(params)}" if params else ""
    return f"/tasks/{quote(task_id)}{suffix}"


def epic_scope_url(request: Request, epic_id: str) -> str:
    """Link an epic chip to the dashboard scoped to that epic — or unscoped.

    An empty ``epic_id`` clears the scope, which is what the SELECTED chip
    links to: clicking the active epic toggles its scope back off. Only one
    epic scopes the board at a time, so the incoming ``epic`` param is replaced
    rather than appended.
    """
    params = _preserved_filter_params(request, exclude="epic")
    if epic_id:
        params.append(("epic", epic_id))
    return f"/tasks?{urlencode(params)}" if params else "/tasks"


def task_card_url(request: Request, status: str, since: str, anchor: str = "") -> str:
    """Link a summary card to the board it actually counts.

    The card's number is computed over the ACTIVE filters, so the link has to
    carry them: project/tag/agent — and the epic scope — ride along (rebuilt
    from the request through the same allowlist as every other generated tasks
    URL), the card supplies
    the status it counts, and ``since`` is the resolved window this page is
    showing rather than whatever the request did or did not say. Dropping the
    filters made the card a lie by one click: the count described the filtered
    board, the destination showed the unfiltered one.
    """
    params = [
        (key, value)
        for key, value in _preserved_filter_params(request)
        if key not in {"status", "since"}
    ]
    params.append(("status", status))
    params.append(("since", since))
    return f"/tasks?{urlencode(params)}{anchor}"


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
