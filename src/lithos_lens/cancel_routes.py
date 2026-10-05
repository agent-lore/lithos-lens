"""Cancel, with its consequences stated first (T3-W6, D9; REQUIREMENTS §5C.2).

A cancelled predecessor blocks its dependents forever, so the cancel is a
decision only when the operator can see what it strands. This module owns the
action's routes and the shapes the surfaces render:

- ``GET /tasks/{task_id}/cancel`` — the confirm page, server-rendered and
  complete without JavaScript. It states what cancelling strands directly and
  behind (``cancel_consequences``, the bounded downstream walk), the active
  claims it releases by agent, that the task's open children are NOT
  cancelled with it, and for a gate that its waiters become unsatisfiable and
  completing it is how they proceed — then offers the one form that carries
  the confirmation, with an optional reason. A task that is not open is W2's
  conflict page and states no consequence: there is nothing in its future.
- ``POST /tasks/{task_id}/cancel`` — one ``lithos_task_cancel`` through the
  write funnel. With ``[writes].confirm_cancel = true`` (default) a POST
  without the confirmation is sent to the confirm page and writes nothing
  (PRD user story 25: the step cannot be skipped by a disabled script). With
  it ``false`` the affordance posts directly, and the facts are read inside
  ``perform``, just before the call — the only moment the claims still exist —
  and carried on the receipt instead.

Registered by the write route group (``write_routes.register_write_routes``),
which owns the funnel; split from it because the group had no room for a
second confirm page, not because cancel is written differently — it is a
``TaskWrite`` like the others, and the funnel is still the only way to Lithos.

The facts travel as :class:`~lithos_lens.receipts.CancelFacts`, built here from
the walk's records: the receipt store may not import the TaskGraph walk, and
one record for the page and the receipt keeps the two from stating different
facts. The reason's TEXT goes to Lithos and nowhere else — the audit line and
the span carry its length (§5C.6) — and both surfaces say it is not stored on
the task (ROADMAP ledger #6).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from functools import partial
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from lithos_lens.cancel_consequences import CancelConsequences, load_cancel_consequences
from lithos_lens.lithos_client import LithosClientProtocol, LithosToolError
from lithos_lens.receipts import MAX_TITLED_RELEASES, CancelFacts, ReceiptTask
from lithos_lens.state import AppState, HealthSnapshot
from lithos_lens.task_links import GATE_TASK_TYPE
from lithos_lens.tasks import TaskRecord, task_detail_path
from lithos_lens.write_errors import (
    CONFLICT,
    CONFLICT_STATUS,
    TaskRef,
    WriteProblem,
    funnel_problem,
)
from lithos_lens.write_funnel import TaskWrite, WriteDone, WriteForm, WriteFunnel
from lithos_lens.write_guards import safe_next

logger = logging.getLogger(__name__)

__all__ = [
    "CANCEL_CONFIRMATION",
    "MAX_REASON_LENGTH",
    "REASON_NOT_STORED",
    "cancel_facts",
    "cancel_path",
    "cancel_url",
    "offers_cancel",
    "register_cancel_routes",
]

#: The value only the confirm page's form posts as ``confirm``.
CANCEL_CONFIRMATION = "cancel"

#: The reason, folded to one line, is bounded like a completion note: it is
#: sent upstream and lands in the ``task.cancelled`` event payload.
MAX_REASON_LENGTH = 500

#: What the reason field says about where the reason goes (ledger #6).
REASON_NOT_STORED = "recorded in the event stream only — not stored on the task"

#: The page a not-open task gets (D7): W2's conflict page, as the funnel's.
CONFLICT_PAGE = "writes/conflict.html"
CONFIRM_PAGE = "writes/cancel_confirm.html"


def cancel_path(task_id: str) -> str:
    """The cancel action's path, the id as ONE encoded segment."""
    return f"/tasks/{quote(task_id, safe='')}/cancel"


def cancel_url(task_id: str, next_url: str = "") -> str:
    """The confirm page, returning the operator to ``next_url`` afterwards."""
    path = cancel_path(task_id)
    return f"{path}?{urlencode({'next': next_url})}" if next_url else path


def offers_cancel(task: TaskRecord | None) -> bool:
    """Whether a surface showing ``task`` offers Cancel: any OPEN task.

    Task, epic or gate alike. Whether an identity resolves is the partial's
    other question. A resolved task is offered nothing — no future-tense
    consequence is shown for it, and the pre-check would refuse it anyway.
    """
    return task is not None and task.status == "open"


def _titled(tasks: tuple[TaskRecord, ...]) -> tuple[ReceiptTask, ...]:
    return tuple(
        ReceiptTask(task_id=task.id, title=task.title)
        for task in tasks[:MAX_TITLED_RELEASES]
    )


def cancel_facts(
    consequences: CancelConsequences, *, stated: bool = True, reason_given: bool = False
) -> CancelFacts:
    """The walk's answer as the page and the receipt state it."""
    walk = consequences.walk
    return CancelFacts(
        stated=stated,
        reason_given=reason_given,
        stranded=_titled(walk.direct) if walk else (),
        stranded_total=len(walk.direct) if walk else 0,
        behind=_titled(walk.behind) if walk else (),
        behind_total=len(walk.behind) if walk else 0,
        exact=walk.exact if walk else False,
        bound_reasons=walk.bound_reasons if walk else (),
        unavailable=consequences.unavailable,
        claims=tuple((holder.agent, holder.aspects) for holder in consequences.claims),
        claims_unread=consequences.claims_unread,
        children=_titled(consequences.open_children),
        children_total=len(consequences.open_children),
        children_unread=consequences.children_unread,
        gate=consequences.is_gate,
    )


def register_cancel_routes(
    app: FastAPI,
    state: AppState,
    templates: Jinja2Templates,
    funnel: WriteFunnel,
) -> None:
    """Attach the cancel routes and their template globals."""

    confirm_cancel = state.config.writes.confirm_cancel

    templates.env.globals["offers_cancel"] = offers_cancel
    templates.env.globals["cancel_url"] = cancel_url
    templates.env.globals["cancel_path"] = cancel_path
    templates.env.globals["confirm_cancel"] = confirm_cancel
    templates.env.globals["cancel_reason_max"] = MAX_REASON_LENGTH
    templates.env.globals["cancel_reason_note"] = REASON_NOT_STORED

    async def read_facts(
        client: LithosClientProtocol, task: TaskRecord, *, reason_given: bool = False
    ) -> CancelFacts:
        """The consequence read, never raising: a failure degrades the facts."""
        try:
            consequences = await load_cancel_consequences(
                client,
                task,
                cache=state.graph_cache,
                fetch_concurrency=state.config.graph.fetch_concurrency,
                max_nodes=state.config.graph.max_tasks,
            )
        except Exception:
            logger.warning("cancel consequence read failed", exc_info=True)
            return CancelFacts(
                reason_given=reason_given,
                exact=False,
                unavailable="Lens couldn't read what depends on it",
                claims_unread=True,
                children_unread=True,
                gate=task.task_type == GATE_TASK_TYPE,
            )
        return cancel_facts(consequences, reason_given=reason_given)

    def cancel_task(reason: str, *, confirmed: bool, next_url: str) -> TaskWrite:
        """The cancel action, as the funnel drives it (T3 D9)."""

        async def perform(
            client: LithosClientProtocol, task: TaskRecord, operator: str
        ) -> WriteDone:
            if confirm_cancel:
                # The confirm page stated the facts; the receipt says only
                # that the task was cancelled (and where a reason went).
                facts = CancelFacts(
                    stated=False,
                    reason_given=bool(reason),
                    gate=task.task_type == GATE_TASK_TYPE,
                )
            else:
                # Read BEFORE the call: the claims are released by it.
                facts = await read_facts(client, task, reason_given=bool(reason))
            result = await client.task_cancel(task.id, agent=operator, reason=reason)
            return WriteDone(
                task=ReceiptTask(
                    task_id=result.task_id or task.id, title=result.title or task.title
                ),
                answer=asdict(result),
                cancel=facts,
            )

        def admits(task: TaskRecord) -> tuple[str, str] | None:
            # A form that claimed the status the task already has — cancelled
            # on a cancelled task — passes the pre-check; it makes no call.
            if offers_cancel(task):
                return None
            return (
                "not_open",
                f"Only an open task can be cancelled; this one is {task.status}.",
            )

        def confirm_page(task: TaskRecord) -> str:
            if not confirm_cancel or confirmed:
                return ""
            return cancel_url(
                task.id, safe_next(next_url, default=task_detail_path(task.id))
            )

        return TaskWrite(
            action="cancel", perform=perform, admits=admits, confirm_page=confirm_page
        )

    def render_confirm(
        request: Request,
        *,
        next_url: str,
        health: HealthSnapshot,
        task: TaskRecord | None = None,
        facts: CancelFacts | None = None,
        notice: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            CONFIRM_PAGE,
            {
                "config": state.config,
                "health": health,
                "active_view": "tasks",
                "task": task,
                "facts": facts,
                "notice": notice,
                "next_url": next_url,
                "confirmation": CANCEL_CONFIRMATION,
            },
            status_code=status_code,
        )

    def render_conflict(
        request: Request, problem: WriteProblem, health: HealthSnapshot
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            CONFLICT_PAGE,
            {
                "config": state.config,
                "health": health,
                "active_view": "tasks",
                "problem": problem,
            },
            status_code=problem.status_code,
        )

    @app.get("/tasks/{task_id}/cancel", response_class=HTMLResponse)
    async def cancel_page(request: Request, task_id: str) -> Response:
        """The consequence confirm page (T3 D9). A read, in either mode."""
        back_to = safe_next(
            request.query_params.get("next"), default=task_detail_path(task_id)
        )
        render = partial(render_confirm, request, next_url=back_to)
        snapshot = await state.refresh_health()
        if snapshot.lithos != "ok":
            return render(
                notice=(
                    "Lithos is unreachable, so Lens can't show what this cancel "
                    "would do."
                ),
                status_code=503,
                health=snapshot,
            )
        client = state.lithos_client
        try:
            task = await client.task_get(task_id)
        except Exception as exc:
            if isinstance(exc, LithosToolError) and exc.code == "task_not_found":
                problem = funnel_problem(
                    "cancel",
                    CONFLICT,
                    "This task no longer exists.",
                    code="task_not_found",
                    status_code=404,
                    subject=TaskRef(task_id=task_id),
                    hint="It may have been removed since you loaded the page.",
                )
                return render_conflict(request, problem, snapshot)
            logger.warning("cancel confirm read failed", extra={"task_id": task_id})
            return render(
                notice="Lens couldn't read this task. Reload once Lithos is reachable.",
                status_code=503,
                health=snapshot,
            )
        if not offers_cancel(task):
            # D7: no future-tense consequence for a task that is not open.
            problem = funnel_problem(
                "cancel",
                CONFLICT,
                f"This task is now {task.status}.",
                code="stale_status",
                status_code=CONFLICT_STATUS,
                subject=TaskRef(task_id=task.id, title=task.title),
                hint="Reload to see the task as it is now.",
            )
            return render_conflict(request, problem, snapshot)
        facts = await read_facts(client, task)
        return render(task=task, facts=facts, health=snapshot)

    @app.post("/tasks/{task_id}/cancel")
    async def cancel(request: Request, task_id: str) -> Response:
        """Cancel an open task (T3 D9): one ``lithos_task_cancel``, via the funnel.

        The reason is folded to one line and bounded; its text is sent and
        recorded nowhere else (its length is the audit argument, D10).
        """
        form = await request.form()
        reason = " ".join(str(form.get("reason") or "").split())[:MAX_REASON_LENGTH]
        next_url = str(form.get("next") or "")
        return await funnel.submit(
            request,
            WriteForm(
                task_id=task_id,
                expected_status=str(form.get("expected_status") or ""),
                next_url=next_url,
                arguments={"task_id": task_id, "reason_chars": len(reason)},
            ),
            cancel_task(
                reason,
                confirmed=form.get("confirm") == CANCEL_CONFIRMATION,
                next_url=next_url,
            ),
        )
