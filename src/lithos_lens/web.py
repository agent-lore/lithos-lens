"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lithos_lens.config import LithosLensConfig
from lithos_lens.lithos_client import LithosClient, LithosClientProtocol
from lithos_lens.state import AppState
from lithos_lens.tasks import (
    default_since,
    find_task,
    format_tag,
    load_dashboard,
    load_task_detail,
    parse_filters,
)
from lithos_lens.telemetry import install_request_middleware

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"
logger = logging.getLogger(__name__)

LithosClientFactory = Callable[[LithosLensConfig], LithosClientProtocol]


def create_app(
    config: LithosLensConfig,
    *,
    lithos_client_factory: LithosClientFactory | None = None,
) -> FastAPI:
    """Create the Lithos Lens ASGI app."""

    factory = lithos_client_factory or (lambda cfg: LithosClient(cfg.lithos))
    state = AppState(config, factory(config))
    templates = Jinja2Templates(directory=TEMPLATE_DIR)
    templates.env.filters["format_tag"] = format_tag

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

    @app.get("/note/{knowledge_id}", response_class=HTMLResponse)
    async def note(request: Request, knowledge_id: str) -> HTMLResponse:
        snapshot = await state.refresh_health()
        note_record = None
        task = None
        error = ""
        if snapshot.lithos != "ok":
            error = "Lithos is offline or degraded. The note cannot be loaded."
        else:
            try:
                note_record = await state.lithos_client.read_note(knowledge_id)
            except Exception:
                error = "Could not load this document from Lithos."
            if note_record is None and not error:
                error = "Document not found."
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
                "task": task,
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
                "claimed_state": filters.claimed_state,
                "tags": list(filters.tags),
                "agent": filters.agent,
                "since": filters.since,
                "visible_cap": state.config.tasks.visible_cap,
            },
        )
        dashboard = await load_dashboard(
            state.lithos_client,
            filters=filters,
            visible_cap=state.config.tasks.visible_cap,
        )
        logger.debug(
            "tasks dashboard loaded",
            extra={
                "lens_route": str(request.url.path),
                "statuses": list(filters.statuses),
                "claimed_state": filters.claimed_state,
                "tags": list(filters.tags),
                "agent": filters.agent,
                "since": filters.since,
                "visible_cap": dashboard.visible_cap,
                "open_total": dashboard.open_total,
                "group_counts": {
                    status: len(rows) for status, rows in dashboard.groups.items()
                },
                "claim_cap_exceeded": dashboard.claim_cap_exceeded,
                "claim_filter_limited": dashboard.claim_filter_limited,
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
