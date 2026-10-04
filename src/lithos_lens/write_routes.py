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
- ``POST /tasks/{task_id}/approve`` (T3-W4) — complete a person-resolved gate.
  The handler parses its form and describes the action; the funnel does the
  rest. The Origin check (``write_funnel.origin_refusal``) is re-exported here
  because ``POST /operator`` calls it too.

There is deliberately NO posture switch (D2): the routes are always registered
and the affordances are part of the page. What decides whether an affordance
renders is the task's state and whether an identity resolves — never config.
"""

from __future__ import annotations

import logging
from functools import partial
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from lithos_lens.gate_completion import (
    MAX_NOTE_LENGTH,
    completes_directly,
    default_outcome,
    refusal_for,
)
from lithos_lens.lithos_client import LithosClientProtocol
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
from lithos_lens.state import AppState
from lithos_lens.task_links import gate_type_of
from lithos_lens.tasks import MAX_FILTER_QUERY_BYTES, TaskRecord
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


def offers_complete(task: TaskRecord | None) -> bool:
    """Whether a surface showing ``task`` offers the direct Complete action.

    The ONE helper every surface asks (the shared partial calls it): the gate
    row, the promoted row in Needs attention, the side panel and the detail
    page. Whether an identity resolves is the partial's other question.
    """
    if task is None:
        return False
    return completes_directly(task.task_type, task.status, gate_type_of(task))


def _complete_gate(note: str) -> TaskWrite:
    """The complete action, as the funnel drives it (T3 D7, direct path)."""

    async def perform(
        client: LithosClientProtocol, task: TaskRecord, operator: str
    ) -> WriteDone:
        outcome = note or default_outcome(operator)
        result = await client.task_complete(task.id, agent=operator, outcome=outcome)
        return WriteDone(
            task=ReceiptTask(
                task_id=result.task_id or task.id, title=result.title or task.title
            ),
            outcome=outcome,
            released=result.unblocked,
        )

    def admits(task: TaskRecord) -> tuple[str, str] | None:
        return refusal_for(task.task_type, gate_type_of(task))

    def describe(task: TaskRecord) -> dict[str, str | bool]:
        # `override` is always False until W4b's proceed-anyway path exists;
        # it is recorded now so the attribute means the same from day one.
        return {"gate_type": gate_type_of(task), "override": False}

    return TaskWrite(
        action="complete", perform=perform, admits=admits, describe=describe
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
    templates.env.globals["complete_gate_path"] = complete_gate_path
    templates.env.globals["write_return_path"] = write_return_path
    templates.env.globals["complete_note_max"] = MAX_NOTE_LENGTH

    def take_receipt(request: Request) -> WriteReceipt | None:
        """The receipt this page's ``?receipt=`` names, consumed — or None.

        A global, read by the chrome's receipt slot, so EVERY page that
        extends the base layout accepts ``?receipt=`` without its handler
        knowing (D5). Consumed on render: a receipt is shown once (§5C.3).
        """
        return receipts.take(request.query_params.get(RECEIPT_KEY))

    templates.env.globals["take_receipt"] = take_receipt

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

    @app.post("/tasks/{task_id}/approve")
    async def complete_gate(request: Request, task_id: str) -> Response:
        """Complete an open human or external-task gate (T3 D7, direct path).

        The path keeps §5C.7's name; the action says "Complete". Every other
        gate type, and every task that is not a gate, is refused by the
        funnel's pre-check through :func:`_complete_gate`'s ``admits`` until
        the proceed-anyway step (T3-W4b) exists. The note is ONE line, bounded
        and stripped: it is stored as the outcome and echoed on every surface
        that shows one.
        """
        form = await request.form()
        note = " ".join(str(form.get("note") or "").split())[:MAX_NOTE_LENGTH]
        return await funnel.submit(
            request,
            WriteForm(
                task_id=task_id,
                expected_status=str(form.get("expected_status") or ""),
                next_url=str(form.get(NEXT_KEY) or ""),
                # Ids, types, lengths — never the note's text (§5C.6).
                arguments={"task_id": task_id, "note_chars": len(note)},
            ),
            _complete_gate(note),
        )
