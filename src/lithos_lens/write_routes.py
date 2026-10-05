"""The curated-write route group (§5C.7).

Registered like the graph and knowledge groups: a closure over app, state and
templates, attached by ``create_app`` BEFORE the dynamic ``/tasks/{task_id}``
route, because every later slice's routes land here and a static write path
attached after it would be matched as a task id.

What this module owns, and what every later slice reuses:

- ``GET``/``POST /operator`` — the identity page, the one surface that states
  the trusted-network boundary (REQUIREMENTS §5C.1).
- :func:`request_identity`, how a request's acting identity is resolved from
  the cookie and the configured default, and the chrome's "Acting as …" chip.
- the process's :class:`~lithos_lens.operator.OperatorRegistry` — the
  guard-and-register-once ledger — and its
  :class:`~lithos_lens.receipts.ReceiptStore`, both held by the one
  :class:`~lithos_lens.write_funnel.WriteFunnel` every write goes through.
- ``POST /tasks/{task_id}/approve`` (T3-W4) — complete a gate. The handler
  parses its form and describes the action; the funnel does the rest. The
  Origin check (``write_funnel.origin_refusal``) is re-exported here because
  ``POST /operator`` calls it too.
- ``GET /tasks/{task_id}/approve`` (T3-W4b) — the Proceed anyway confirm page
  for a gate a machine resolves (``timer``, ``ci``, ``pr``, or a type Lens does
  not know). Its form is the only one that carries the confirmation; a POST
  for such a gate without it is sent here rather than performed.
- ``POST /tasks/{task_id}/reopen`` (T3-W5) — reopen a completed or cancelled
  task (D8). No confirm page: each case's copy is rendered beside the button.
- ``GET``/``POST /tasks/{task_id}/cancel`` (T3-W6) — cancel with its
  consequences stated first, in ``cancel_routes``, registered from here with
  this group's funnel.

There is deliberately NO posture switch (D2): the routes are always registered
and the affordances are part of the page. What decides whether an affordance
renders is the task's state and whether an identity resolves — never config.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from typing import NamedTuple
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from lithos_lens.cancel_routes import register_cancel_routes
from lithos_lens.gate_completion import (
    MAX_NOTE_LENGTH,
    PROCEED_ANYWAY_CONFIRMATION,
    completes_directly,
    default_outcome,
    is_override,
    proceeds_anyway,
    refusal_for,
)
from lithos_lens.gate_override import GateOverride, load_gate_override
from lithos_lens.lithos_client import LithosClientProtocol, LithosToolError
from lithos_lens.operator import (
    OPERATOR_COOKIE_MAX_AGE_S,
    OPERATOR_COOKIE_NAME,
    OPERATOR_ID_RULE,
    IdentityCheck,
    OperatorIdentity,
    OperatorRegistry,
    resolve_operator,
)
from lithos_lens.receipts import RECEIPT_KEY, ReceiptStore, ReceiptTask, WriteReceipt
from lithos_lens.request_filters import filter_query_oversized
from lithos_lens.state import AppState, HealthSnapshot
from lithos_lens.task_links import (
    BLOCKER_EDGE_TYPES,
    LINK_READ_TIMEOUT_S,
    LinkTarget,
    gate_type_of,
    load_link_page,
    outgoing_targets,
)
from lithos_lens.tasks import MAX_FILTER_QUERY_BYTES, TaskRecord, task_detail_path
from lithos_lens.write_funnel import (
    TaskWrite,
    WriteDone,
    WriteForm,
    WriteFunnel,
    origin_refusal,
)
from lithos_lens.write_guards import safe_next

logger = logging.getLogger(__name__)

#: The operator page, and the default destination of its own form.
OPERATOR_PATH = "/operator"

#: The query key that carries where to return the operator afterwards. One
#: spelling, shared with W4's post-write redirect, because both read it from
#: the same untrusted place through ``write_guards.safe_next``.
NEXT_KEY = "next"


def request_identity(request: Request, *, default_operator: str) -> OperatorIdentity:
    """The identity this request acts under: cookie → default → none (D3)."""
    return resolve_operator(
        cookie=request.cookies.get(OPERATOR_COOKIE_NAME),
        default_operator=default_operator,
    )


def operator_page_url(request: Request) -> str:
    """The operator page, carrying this request's page as its return trip.

    Built server-side for the same reason every other generated URL here is:
    the encoding has one definition. The value is re-checked by ``safe_next``
    when it comes back, so a crafted ``?next=`` arriving in the link is no more
    trusted than one typed by hand.

    The query rides along only when the page is willing to re-emit it. This
    chip is in the chrome of EVERY page — including the one that refuses an
    over-budget filter query rather than reflecting it
    (``MAX_FILTER_QUERY_BYTES``) — and a link back to a page that would be
    refused is not a return trip anyway. Over budget, or over that bound for
    any other reason, the path goes back alone.
    """
    here = request.url.path
    query = request.url.query
    if (
        query
        and len(query.encode()) <= MAX_FILTER_QUERY_BYTES
        and not filter_query_oversized(request)
    ):
        here = f"{here}?{query}"
    return f"{OPERATOR_PATH}?{NEXT_KEY}={quote(here, safe='')}"


def operator_page_for(next_url: str) -> str:
    """The operator page, returning the operator to ``next_url`` afterwards.

    For a write POST with no identity (D3, clarification 8): the return trip is
    the form's ``next`` or the task's page — never the POST's own path, which
    has no GET route to come back to.
    """
    return f"{OPERATOR_PATH}?{NEXT_KEY}={quote(next_url, safe='')}"


def write_return_path(request: Request) -> str:
    """This page, as a write form's ``next``: where its 303 brings them back.

    Bounded exactly like the identity chip's return trip
    (:func:`operator_page_url`), for the same reason — the value is re-emitted
    into a page — and re-checked by ``safe_next`` when it comes back.
    """
    here = request.url.path
    query = request.url.query
    if (
        query
        and len(query.encode()) <= MAX_FILTER_QUERY_BYTES
        and not filter_query_oversized(request)
    ):
        here = f"{here}?{query}"
    return here


def complete_gate_path(task_id: str) -> str:
    """The Complete action's POST target, the id as ONE encoded segment."""
    return f"/tasks/{quote(task_id, safe='')}/approve"


def proceed_anyway_url(task_id: str, next_url: str = "") -> str:
    """The Proceed anyway confirm page, returning the operator to ``next_url``.

    The same path as the POST it confirms (§5C.7's ``/approve``), read with
    GET. ``next_url`` rides along so the confirm page's form returns the
    operator where they started; it is re-checked by ``safe_next`` there.
    """
    path = complete_gate_path(task_id)
    return f"{path}?{urlencode({NEXT_KEY: next_url})}" if next_url else path


def offers_proceed_anyway(task: TaskRecord | None) -> bool:
    """Whether a surface showing ``task`` offers the Proceed anyway link.

    The counterpart of :func:`offers_complete`, asked by the same partial: an
    open gate that a machine resolves, or whose type Lens does not know. A
    task gets one or the other, never both.
    """
    if task is None:
        return False
    return proceeds_anyway(task.task_type, task.status, gate_type_of(task))


def offers_complete(task: TaskRecord | None) -> bool:
    """Whether a surface showing ``task`` offers the direct Complete action.

    The ONE helper every surface asks (the shared partial calls it): the gate
    row, the promoted row in Needs attention, the side panel and the detail
    page. Whether an identity resolves is the partial's other question.
    """
    if task is None:
        return False
    return completes_directly(task.task_type, task.status, gate_type_of(task))


#: The statuses Reopen is offered on: the two terminal ones (D8).
REOPENABLE_STATUSES = frozenset({"completed", "cancelled"})


def reopen_path(task_id: str) -> str:
    """The Reopen action's POST target, the id as ONE encoded segment."""
    return f"/tasks/{quote(task_id, safe='')}/reopen"


def offers_reopen(task: TaskRecord | None) -> bool:
    """Whether a surface showing ``task`` offers Reopen (T3 D8).

    Any completed or cancelled task — task, epic or gate alike. Asked by the
    detail page's partial; whether an identity resolves is its other question.
    The completion receipt's Reopen gate needs no helper: the gate it names was
    just completed, and the pre-check binds if it has moved since.
    """
    return task is not None and task.status in REOPENABLE_STATUSES


class _Dependents(NamedTuple):
    """The open dependents waiting on a reopened task, as one bounded read saw
    them."""

    waiting: tuple[str, ...] = ()
    exact: bool = True
    unread: bool = False


async def _waiting_dependents(
    client: LithosClientProtocol, task_id: str
) -> _Dependents:
    """The open tasks a reopened CANCELLED task now holds back again.

    Lithos's ``reblocked`` is empty by design in this case — those dependents
    were stranded, not ready — so the receipt's count comes from the task's own
    outgoing ``blocks`` / ``waits_on_gate`` edges, through the same bounded
    reader the detail page's Blocks line uses. The targets are deduplicated by
    task id first, keeping the first edge: a task may depend on this one by
    both a ``blocks`` and a ``waits_on_gate`` edge, and the receipt counts
    TASKS, so a link count would name one twice and push "at least N" past the
    number of tasks there are. A truncated page, or a dependent whose read
    failed, makes the count a lower bound. The read catches its own errors: it
    runs after a reopen that APPLIED, and anything raised from ``perform``
    would be classified as the write's own failure.
    """
    try:
        edges = await asyncio.wait_for(
            client.task_edge_list(task_id, direction="outgoing"),
            LINK_READ_TIMEOUT_S,
        )
        distinct: dict[str, LinkTarget] = {}
        for target in outgoing_targets(task_id, edges, BLOCKER_EDGE_TYPES):
            distinct.setdefault(target.task_id, target)
        page = await load_link_page(client, tuple(distinct.values()))
    except Exception:
        logger.warning("reopen dependents read failed", extra={"task_id": task_id})
        return _Dependents(exact=False, unread=True)
    return _Dependents(
        waiting=tuple(link.task_id for link in page.links if link.status == "open"),
        exact=not page.tail.truncated
        and not any(link.unresolved for link in page.links),
    )


def _reopen_task() -> TaskWrite:
    """The reopen action, as the funnel drives it (T3 D8).

    The receipt names what the write did. A non-empty ``reblocked`` is proof:
    Lithos returns ids only for a task that was COMPLETED when the reopen
    applied, so those are named as re-blocked whatever the pre-check read — an
    agent that resolved the task again in the window after it cannot make the
    receipt drop them. An EMPTY ``reblocked`` proves neither case: a completed
    task nothing had become ready behind, or a cancelled one (reblocked is
    empty by design there) — including one an agent cancelled after the
    pre-check read it as completed. So then the receipt states what is true
    in both: Lithos re-blocked no one, and the open dependents read after the
    write are waiting on it again. Nothing else says what the task was when
    Lithos reopened it (the ``[Reopened]`` finding is free text any client can
    post, ``tasks.REOPENED_FINDING_PREFIX``), so the status and outcome are
    worded as what Lens READ before the write, and no outcome is quoted once
    the answer shows that read was stale.
    """

    async def perform(
        client: LithosClientProtocol, task: TaskRecord, operator: str
    ) -> WriteDone:
        result = await client.task_reopen(task.id, agent=operator)
        prior_status = "completed" if result.reblocked else task.status
        dependents = (
            _Dependents(waiting=result.reblocked)
            if result.reblocked
            else await _waiting_dependents(client, task.id)
        )
        return WriteDone(
            task=ReceiptTask(
                task_id=result.task_id or task.id, title=result.title or task.title
            ),
            outcome=task.outcome if prior_status == task.status else "",
            released=dependents.waiting,
            answer={**asdict(result), "reblocked": list(result.reblocked)},
            prior_status=prior_status,
            checked_status=task.status,
            released_exact=dependents.exact,
            released_unread=dependents.unread,
            released_waiting=not result.reblocked,
        )

    return TaskWrite(action="reopen", perform=perform)


def _complete_gate(note: str, *, confirmed: bool, next_url: str) -> TaskWrite:
    """The complete action, as the funnel drives it (T3 D7).

    Direct for a person-resolved gate. For any other gate it is an override,
    performed only when the form ``confirmed`` it — which only the Proceed
    anyway page's form does; otherwise the funnel sends the operator to that
    page (carrying ``next_url``) and writes nothing. Both are decided on the
    gate type the pre-check READ, not on what the posting page showed.
    """

    async def perform(
        client: LithosClientProtocol, task: TaskRecord, operator: str
    ) -> WriteDone:
        outcome = note or default_outcome(operator, gate_type_of(task))
        result = await client.task_complete(task.id, agent=operator, outcome=outcome)
        return WriteDone(
            task=ReceiptTask(
                task_id=result.task_id or task.id, title=result.title or task.title
            ),
            outcome=outcome,
            released=result.unblocked,
            # The canonical answer, whole, for the audit line's envelope.
            answer={**asdict(result), "unblocked": list(result.unblocked)},
        )

    def admits(task: TaskRecord) -> tuple[str, str] | None:
        return refusal_for(task.task_type)

    def describe(task: TaskRecord) -> dict[str, str | bool]:
        return {
            "gate_type": gate_type_of(task),
            "override": is_override(task.task_type, gate_type_of(task)),
        }

    def confirm_page(task: TaskRecord) -> str:
        if confirmed or not is_override(task.task_type, gate_type_of(task)):
            return ""
        return proceed_anyway_url(
            task.id, safe_next(next_url, default=task_detail_path(task.id))
        )

    return TaskWrite(
        action="complete",
        perform=perform,
        admits=admits,
        describe=describe,
        confirm_page=confirm_page,
    )


def register_write_routes(
    app: FastAPI, state: AppState, templates: Jinja2Templates
) -> None:
    """Attach the write route group and its template globals."""

    default_operator = state.config.writes.default_operator
    # One registry per process: the ids registered as type="human". Held here
    # rather than on AppState because nothing outside the write surface reads
    # it; the funnel below is its one writer.
    registry = OperatorRegistry(service_agent_id=state.config.lithos.agent_id)
    # One receipt store per process, for the same reason: a receipt is minted
    # by the funnel and taken by the chrome of the page its redirect lands on.
    receipts = ReceiptStore()
    funnel = WriteFunnel(
        state,
        templates,
        registry=registry,
        receipts=receipts,
        operator_page_for=operator_page_for,
    )

    templates.env.globals["operator_identity"] = partial(
        request_identity, default_operator=default_operator
    )
    templates.env.globals["operator_page_url"] = operator_page_url
    templates.env.globals["operator_path"] = OPERATOR_PATH
    templates.env.globals["offers_complete"] = offers_complete
    templates.env.globals["offers_proceed_anyway"] = offers_proceed_anyway
    templates.env.globals["proceed_anyway_url"] = proceed_anyway_url
    templates.env.globals["complete_gate_path"] = complete_gate_path
    templates.env.globals["write_return_path"] = write_return_path
    templates.env.globals["complete_note_max"] = MAX_NOTE_LENGTH
    templates.env.globals["offers_reopen"] = offers_reopen
    templates.env.globals["reopen_path"] = reopen_path

    def take_receipt(request: Request) -> WriteReceipt | None:
        """The receipt this page's ``?receipt=`` names, consumed — or None.

        A global, read by the chrome's receipt slot, so EVERY page that
        extends the base layout accepts ``?receipt=`` without its handler
        knowing (D5). Consumed on render: a receipt is shown once (§5C.3).
        """
        return receipts.take(request.query_params.get(RECEIPT_KEY))

    templates.env.globals["take_receipt"] = take_receipt

    # Cancel's confirm page and action (T3-W6), through this group's funnel.
    register_cancel_routes(app, state, templates, funnel)

    def render(
        request: Request,
        identity: OperatorIdentity,
        *,
        next_url: str,
        problem: str = "",
        typed: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "writes/operator.html",
            {
                "config": state.config,
                # No `health`: this page makes no Lithos call, so it has no
                # fresh snapshot to show and must not print a stale one.
                "active_view": "operator",
                "identity": identity,
                "default_operator": default_operator,
                "operator_id_rule": OPERATOR_ID_RULE,
                # The form re-render keeps what was typed, so a refusal is
                # corrected rather than retyped.
                "typed": typed,
                "problem": problem,
                # Already sanitised by the caller. It is passed IN rather than
                # re-derived here because a refusal re-renders a POST, whose
                # destination arrived in the FORM — re-reading the query there
                # would silently drop it and send a corrected submission to
                # /operator instead of back where the operator came from.
                "next_url": next_url,
            },
            status_code=status_code,
        )

    @app.get(OPERATOR_PATH, response_class=HTMLResponse)
    async def operator_page(request: Request) -> HTMLResponse:
        """Show the acting identity, where it came from, and the boundary.

        A read: no health probe gate and no Lithos call, so the page that
        explains the write surface still renders while Lithos is down — which
        is exactly when an operator comes looking for it.
        """
        return render(
            request,
            request_identity(request, default_operator=default_operator),
            next_url=safe_next(request.query_params.get(NEXT_KEY), default=""),
        )

    @app.post(OPERATOR_PATH)
    async def set_operator(request: Request):
        """Set or switch the identity, then return the operator where they were.

        The impersonation guard runs HERE as well as at the write seam
        (``OperatorRegistry.ensure_registered``) so a refusal is immediate
        rather than discovered at the first write. It is not the only place:
        ``[writes].default_operator`` never passes through this page, so the
        seam is what covers every identity source.
        """
        refusal = origin_refusal(request)
        if refusal is not None:
            return refusal
        form = await request.form()
        # NOT stripped: the rule is that the submitted value itself matches the
        # id pattern (clarification 6). Trimming first would accept " dave " by
        # silently turning it into a DIFFERENT value than the one submitted —
        # the one case where "be liberal in what you accept" writes an identity
        # the operator did not type.
        typed = str(form.get("operator") or "")
        # The return trip rides in the FORM, so it is read (and sanitised) ONCE
        # here and carried by every exit below — the redirect and each refusal
        # re-render alike. Empty means "nowhere in particular", which the
        # redirect reads as this page and the form as no hidden field.
        returning_to = safe_next(str(form.get(NEXT_KEY) or ""), default="")
        identity = request_identity(request, default_operator=default_operator)
        checked: IdentityCheck = await registry.check(state.lithos_client, typed)
        if not checked.ok:
            logger.info(
                "operator identity refused",
                extra={"operator": typed, "reason": checked.code},
            )
            return render(
                request,
                identity,
                # The sanitised destination, kept across the refusal:
                # correcting the id must still land the operator where they
                # came from.
                next_url=returning_to,
                problem=checked.reason,
                typed=typed,
                status_code=400,
            )
        response = RedirectResponse(returning_to or OPERATOR_PATH, status_code=303)
        # No `secure`: Lens serves plain HTTP on the trusted network, so a
        # Secure cookie would never be stored at all. This is an attribution
        # label, not a credential (§5C.1) — HttpOnly keeps page scripts out of
        # it and SameSite=Lax keeps another site from setting it through a
        # top-level POST.
        response.set_cookie(
            OPERATOR_COOKIE_NAME,
            typed,
            max_age=OPERATOR_COOKIE_MAX_AGE_S,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    def render_proceed_anyway(
        request: Request,
        *,
        next_url: str,
        health: HealthSnapshot,
        override: GateOverride | None = None,
        task: TaskRecord | None = None,
        notice: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "writes/proceed_anyway.html",
            {
                "config": state.config,
                "health": health,
                "active_view": "tasks",
                "override": override,
                "task": task,
                "notice": notice,
                "next_url": next_url,
                "confirmation": PROCEED_ANYWAY_CONFIRMATION,
            },
            status_code=status_code,
        )

    @app.get("/tasks/{task_id}/approve", response_class=HTMLResponse)
    async def proceed_anyway_page(request: Request, task_id: str) -> Response:
        """The Proceed anyway confirm page for a machine-owned gate (T3-W4b).

        Server-rendered and complete without JavaScript, like the cancel
        confirm. A read: it states what would otherwise resolve the gate and
        which waiters completing it releases, then offers the one form that
        carries the confirmation. A person-resolved gate has nothing to
        confirm, so it is sent to its own page, where the direct action is;
        anything else that cannot be completed says why, and offers no form.
        """
        back_to = safe_next(
            request.query_params.get(NEXT_KEY), default=task_detail_path(task_id)
        )
        render = partial(render_proceed_anyway, request, next_url=back_to)
        snapshot = await state.refresh_health()
        if snapshot.lithos != "ok":
            return render(
                notice="Lithos is unreachable, so Lens can't show this gate.",
                status_code=503,
                health=snapshot,
            )
        client = state.lithos_client
        try:
            task = await client.task_get(task_id)
        except Exception as exc:
            if isinstance(exc, LithosToolError) and exc.code == "task_not_found":
                return render(
                    notice="This task no longer exists.",
                    status_code=404,
                    health=snapshot,
                )
            logger.warning("proceed-anyway read failed", extra={"task_id": task_id})
            return render(
                notice="Lens couldn't read this task. Reload once Lithos is reachable.",
                status_code=503,
                health=snapshot,
            )
        if completes_directly(task.task_type, task.status, gate_type_of(task)):
            return RedirectResponse(task_detail_path(task.id), status_code=303)
        if not proceeds_anyway(task.task_type, task.status, gate_type_of(task)):
            refused = refusal_for(task.task_type)
            return render(
                task=task,
                notice=refused[1]
                if refused
                else f"This gate is now {task.status} — there is nothing to complete.",
                status_code=409,
                health=snapshot,
            )
        override = await load_gate_override(
            client,
            task,
            frontier_limit=state.config.tasks.frontier_limit,
            now=datetime.now(UTC),
        )
        return render(task=task, override=override, health=snapshot)

    @app.post("/tasks/{task_id}/approve")
    async def complete_gate(request: Request, task_id: str) -> Response:
        """Complete an open gate (T3 D7).

        The path keeps §5C.7's name; the action says "Complete". A human or
        external-task gate completes directly. Any other gate is an override
        and completes only when the form carries the confirmation, which only
        the Proceed anyway page's form does; without it the funnel sends the
        operator to that page and writes nothing. A task that is not a gate is
        refused. The note is ONE line, bounded and stripped: it is stored as
        the outcome and echoed on every surface that shows one.
        """
        form = await request.form()
        note = " ".join(str(form.get("note") or "").split())[:MAX_NOTE_LENGTH]
        next_url = str(form.get(NEXT_KEY) or "")
        return await funnel.submit(
            request,
            WriteForm(
                task_id=task_id,
                expected_status=str(form.get("expected_status") or ""),
                next_url=next_url,
                # Ids, types, lengths — never the note's text (§5C.6).
                arguments={"task_id": task_id, "note_chars": len(note)},
            ),
            _complete_gate(
                note,
                confirmed=form.get("confirm") == PROCEED_ANYWAY_CONFIRMATION,
                next_url=next_url,
            ),
        )

    @app.post("/tasks/{task_id}/reopen")
    async def reopen_task(request: Request, task_id: str) -> Response:
        """Reopen a completed or cancelled task (T3 D8).

        One ``lithos_task_reopen`` through the funnel. An already-open task is
        the conflict page either way: the pre-check catches a stale form with
        no call, and Lithos refuses one reopened in the window after it with
        ``task_not_resolved`` (§5.14).
        """
        form = await request.form()
        return await funnel.submit(
            request,
            WriteForm(
                task_id=task_id,
                expected_status=str(form.get("expected_status") or ""),
                next_url=str(form.get(NEXT_KEY) or ""),
                arguments={"task_id": task_id},
            ),
            _reopen_task(),
        )
