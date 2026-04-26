"""FastAPI application factory."""

from __future__ import annotations

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
from lithos_lens.telemetry import install_request_middleware

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"

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

    return app


async def _render_tasks(
    request: Request,
    templates: Jinja2Templates,
    state: AppState,
) -> HTMLResponse:
    snapshot = await state.refresh_health()
    return templates.TemplateResponse(
        request,
        "tasks/dashboard.html",
        {
            "config": state.config,
            "health": snapshot,
            "active_view": "tasks",
        },
    )
