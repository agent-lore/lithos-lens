"""Create a task, epic or gate (T3-W7, D10; REQUIREMENTS §5C.2 "Create").

The routes and the surfaces of the create action:

- ``GET /tasks/new`` — the one form for a task, an epic or a gate, complete
  without JavaScript: the gate fieldset is shown to every type, labelled "only
  for gates", and a small script (``create_form.js``) hides it for the others.
  ``?project=`` (the board's single selected project) and ``?parent=`` (an
  epic's **Add child**) pre-fill it, and the server mints the request id the
  form carries. Its one Lithos read is the open tasks the project datalist is
  derived from — the board filter's own derivation — and a failed read renders
  the form without the list; a POST's re-render makes no read. With no
  operator identity the page renders only
  the "choose an operator" link, so nothing is typed that a redirect would
  lose (D3).
- ``POST /tasks/new`` — one create through the write funnel
  (``WriteFunnel.submit_create``), de-duplicated on the request id by this
  process's :class:`~lithos_lens.create_coordinator.CreateCoordinator`. A
  refusal, Lens's or Lithos's, re-renders the form with the input kept and the
  message on its field; success is a 303 to the new task's page with its
  receipt; an unknown outcome is create's own "not visible yet" page.
- ``POST /tasks/new`` with ``intent=restart`` — **Start again** from that
  page: the form again, input kept, under a NEW request id. No call, and not
  an attempt.

Registered by the write route group, which owns the funnel, like cancel.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from lithos_lens.create_coordinator import CreateCoordinator
from lithos_lens.create_form import (
    CREATABLE_GATE_TYPES,
    CREATABLE_TASK_TYPES,
    CreateInput,
    FieldError,
    place_problem,
)
from lithos_lens.operator import OPERATOR_COOKIE_NAME, resolve_operator
from lithos_lens.state import AppState
from lithos_lens.task_filtering import project_universe
from lithos_lens.tasks import TaskFilters, TaskRecord
from lithos_lens.write_errors import WriteProblem
from lithos_lens.write_funnel import CreateWrite, WriteFunnel, origin_refusal

logger = logging.getLogger(__name__)

__all__ = [
    "GATE_TYPE_LABELS",
    "NEW_TASK_PATH",
    "PROJECT_READ_TIMEOUT_S",
    "new_task_url",
    "offers_add_child",
    "register_create_routes",
]

#: The create form's path — ``new`` is a reserved task-path segment (§5C.7).
NEW_TASK_PATH = "/tasks/new"

#: The value Start again posts as ``intent``.
RESTART_INTENT = "restart"

#: How long the form waits for its project list before rendering without it.
#: The list is an enhancement; creating must not wait on a slow Lithos (D13).
PROJECT_READ_TIMEOUT_S = 3.0

#: How the form names each gate type a person may create.
GATE_TYPE_LABELS: Mapping[str, str] = {
    "human": "Human — a person decides",
    "external_task": "External task — something outside Lithos finishes",
    "timer": "Timer — ready at a set time",
}

FORM_PAGE = "writes/create_form.html"
UNKNOWN_PAGE = "writes/create_unknown.html"


def new_task_url(*, project: str = "", parent: str = "") -> str:
    """The create form, pre-filled with a project and/or a parent."""
    query = {
        key: value for key, value in (("project", project), ("parent", parent)) if value
    }
    return f"{NEW_TASK_PATH}?{urlencode(query)}" if query else NEW_TASK_PATH


def offers_add_child(task: TaskRecord | None) -> bool:
    """Whether a page showing ``task`` offers **Add child**: an OPEN epic (D12).

    Whether an identity resolves is the partial's other question.
    """
    return task is not None and task.task_type == "epic" and task.status == "open"


def register_create_routes(
    app: FastAPI,
    state: AppState,
    templates: Jinja2Templates,
    funnel: WriteFunnel,
) -> None:
    """Attach the create routes and their template globals."""

    # One per process, like the registry and the receipt store: the request
    # ids this Lens has seen and what each came to. A restart empties it.
    coordinator = CreateCoordinator()
    tag_key = state.config.tasks.project_tag_key
    default_operator = state.config.writes.default_operator

    templates.env.globals["new_task_url"] = new_task_url
    templates.env.globals["offers_add_child"] = offers_add_child

    async def known_projects() -> tuple[str, ...]:
        """The project datalist: the open tasks' projects, both conventions."""
        try:
            tasks = await asyncio.wait_for(
                state.lithos_client.list_tasks(status="open"), PROJECT_READ_TIMEOUT_S
            )
        except Exception:
            logger.warning("create form project read failed", exc_info=True)
            return ()
        filters = TaskFilters(
            statuses=(), tags=(), agent="", since="", project_tag_key=tag_key
        )
        return project_universe(tasks, filters)

    def resolved(request: Request) -> bool:
        return resolve_operator(
            cookie=request.cookies.get(OPERATOR_COOKIE_NAME),
            default_operator=default_operator,
        ).resolved

    async def render_form(
        request: Request,
        typed: CreateInput,
        problem: WriteProblem | None = None,
        errors: Mapping[str, FieldError] | None = None,
        status_code: int = 200,
        *,
        projects: tuple[str, ...] = (),
    ) -> Response:
        """The form. ``projects`` feeds the datalist and is read only by the GET.

        A POST's re-render — a refusal, Lens's or Lithos's, and Start again —
        makes no Lithos call of its own: those answers are promised to make
        none, and must not wait on a slow Lithos for an enhancement. The
        operator's typed project is kept either way.
        """
        placed = dict(errors or {})
        if problem is not None:
            problem, upstream = place_problem(typed, problem)
            placed.update(upstream)
        return templates.TemplateResponse(
            request,
            FORM_PAGE,
            {
                "config": state.config,
                "active_view": "tasks",
                "typed": typed,
                "problem": problem,
                "errors": placed,
                "projects": projects,
                "task_types": CREATABLE_TASK_TYPES,
                "gate_types": CREATABLE_GATE_TYPES,
                "gate_type_labels": GATE_TYPE_LABELS,
                "project_tag_key": tag_key,
                "restart_intent": RESTART_INTENT,
            },
            status_code=status_code,
        )

    async def render_unknown(request: Request, typed: CreateInput) -> Response:
        """Create's own page for a create Lens never heard back about (D7)."""
        board = "/tasks"
        if typed.project_slug:
            board = f"/tasks?{urlencode({'project': typed.project_slug})}"
        return templates.TemplateResponse(
            request,
            UNKNOWN_PAGE,
            {
                "config": state.config,
                "active_view": "tasks",
                "typed": typed,
                "board_url": board,
                "restart_intent": RESTART_INTENT,
            },
        )

    @app.get(NEW_TASK_PATH, response_class=HTMLResponse)
    async def new_task_form(request: Request) -> Response:
        """The create form, pre-filled from where it was opened."""
        typed = CreateInput.prefilled(
            project=request.query_params.get("project", ""),
            parent=request.query_params.get("parent", ""),
        )
        # Only an operator who can submit gets the read behind the list.
        projects = await known_projects() if resolved(request) else ()
        return await render_form(request, typed, projects=projects)

    @app.post(NEW_TASK_PATH)
    async def create_task(request: Request) -> Response:
        """Create one task, epic or gate — or, with ``intent=restart``, the
        form again under a new request id."""
        form = await request.form()
        typed = CreateInput.from_form(form)
        if form.get("intent") == RESTART_INTENT:
            refused = origin_refusal(request)
            if refused is not None:
                return refused
            return await render_form(request, typed.restarted())
        return await funnel.submit_create(
            request,
            CreateWrite(
                typed=typed,
                coordinator=coordinator,
                render_form=render_form,
                render_unknown=render_unknown,
                return_to=new_task_url(
                    project=typed.project_slug, parent=typed.parent.strip()
                ),
            ),
        )
