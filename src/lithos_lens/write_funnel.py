"""The write funnel: the ONE path a curated write takes to Lithos (T3 D4).

Every action in the milestone goes through :meth:`WriteFunnel.submit`, in one
order, with one shape:

1. **Origin** — a POST that did not come from a page this Lens served is
   refused before anything else (:func:`origin_refusal`, §5C.6).
2. **Operator** — resolved from the cookie or the configured default; with
   none, the operator is sent to the operator page and the write is NOT
   replayed (D3).
3. **Form** — the ``expected_status`` the operator saw must be a status.
4. **Registration** — the identity is guarded and registered once
   (``OperatorRegistry.ensure_registered``, W1).
5. **Pre-check** — the task is re-read; a status other than the one the
   operator saw is the conflict page and no write (§5C.6). The action then
   says whether it applies to the task as read, and whether it needs a
   confirmation the form did not carry — in which case the operator is sent
   to the action's confirm page and nothing is written (T3-W4b).
6. **The single Lithos call.**
7. **Classification** — an answer with an envelope is a refusal, mapped by
   ``write_errors``; NO answer (a timeout, a dead session, an empty envelope)
   is the unknown-outcome page with what a re-read shows now.
8. **Record** — exactly one audit line, one span and one counter increment per
   attempt, refusals included.
9. **Receipt** — the write's news, minted with the released tasks' titles.
10. **Answer** — a plain form POST gets ``303 See Other`` to a page rendered
    from fresh reads, carrying ``?receipt=``; an HTMX POST (row actions only)
    gets the receipt or refusal fragment, always ``200`` (htmx swaps nothing
    else), with ``HX-Trigger: lens:reconcile`` so ``tasks.js`` re-renders the
    board through the reconcile every event already drives. The one exception
    is no identity, which answers ``HX-Redirect`` to the operator page — and,
    the same way, an attempt that still needs its confirmation, which is sent
    to the confirm page.

A create has no task to pre-check, so it enters through
:meth:`WriteFunnel.submit_create` (T3-W7): the same Origin check, operator,
registration, record and receipt, with Lens's form validation in place of the
pre-check and the call made through the create coordinator, which runs at most
one create per request id. Its refusals re-render the create form, input kept.

Route handlers parse their form, describe their action as a :class:`TaskWrite`
(or a :class:`CreateWrite`) and hand both over; they never call a write method
themselves. A second path to
Lithos is the defect this module exists to prevent — the audit line and the
pre-check are guarantees only because nothing can go around them.

**Never optimistic.** A write's result informs the receipt and nothing else:
no row is hand-assembled from it, and every page after a write is rendered from
fresh reads. A step for the synthetic-event publish the edge slice (W8) needs
sits between the classification and the receipt; W4 publishes nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from fastapi import Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from opentelemetry.trace import Span

from lithos_lens import metrics
from lithos_lens.create_coordinator import (
    CreateCoordinator,
    Created,
    CreateOutcome,
    OutcomeUnknown,
    Refused,
)
from lithos_lens.create_form import (
    CreateInput,
    CreateRequest,
    FieldError,
    is_request_id,
    validate,
)
from lithos_lens.lithos_client import LithosClientProtocol
from lithos_lens.mcp_transport import LithosToolError
from lithos_lens.operator import (
    OPERATOR_COOKIE_NAME,
    REFUSAL_REGISTRATION_FAILED,
    OperatorRegistry,
    resolve_operator,
)
from lithos_lens.receipts import (
    MAX_TITLED_RELEASES,
    CancelFacts,
    CreateFacts,
    ReceiptStore,
    ReceiptTask,
    WriteReceipt,
    receipt_url,
)
from lithos_lens.state import AppState
from lithos_lens.tasks import TASK_STATUSES, TaskRecord, task_detail_path
from lithos_lens.telemetry import get_tracer
from lithos_lens.write_errors import (
    CONFLICT,
    CONFLICT_STATUS,
    NO_SUBJECT,
    NOT_READ,
    REFUSED,
    REFUSED_STATUS,
    REREAD_FAILED,
    TASK_ABSENT,
    TaskRef,
    TaskReread,
    WriteAction,
    WriteProblem,
    funnel_problem,
    map_write_error,
)
from lithos_lens.write_guards import ORIGIN_REFUSAL_MESSAGE, safe_next, same_origin

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_EVENT",
    "CONFIRMATION_REQUIRED",
    "INVALID_FORM",
    "RECONCILE_TRIGGER",
    "CreateWrite",
    "TaskWrite",
    "WriteDone",
    "WriteForm",
    "WriteFunnel",
    "origin_refusal",
]

#: The ``lens_event`` marker on the one structured audit line per attempt. The
#: warnings the Origin check and the operator registry log beside it are
#: diagnostics, not audit lines, and carry no such marker.
AUDIT_EVENT = "lens.writes.audit"

#: The event an HTMX write answer asks the page to fire. ``tasks.js`` listens
#: for it on ``document`` and runs its immediate, coalesced reconcile — so the
#: board re-renders from fresh reads rather than from anything the write said.
RECONCILE_TRIGGER = "lens:reconcile"

#: The page a ``refused`` problem answers a plain POST with. ``write_errors``
#: gives that kind no page of its own because a form re-render is its usual
#: home; a lifecycle action has no form to re-render, so it gets this one.
REFUSED_PAGE = "writes/refused.html"

#: The fragment an HTMX POST is answered with when the write did not apply —
#: the same copy block the pages carry, so "Nothing was changed." is said the
#: same way in both modes.
OUTCOME_FRAGMENT = "writes/outcome.html"

#: The fragment an HTMX POST is answered with when the write applied.
RECEIPT_FRAGMENT = "writes/receipt.html"

#: How each attempt ended, as the span and the counter spell it (§5C.6).
WriteResult = Literal[
    "ok", "conflict", "rejected", "unknown", "refused_origin", "no_operator"
]

#: The code an attempt is recorded with when it was sent to its action's
#: confirm page rather than performed. Recorded as ``rejected``: nothing was
#: written, and the bounded result set has no other word for "not performed".
CONFIRMATION_REQUIRED = "confirmation_required"

#: The code a create refused by Lens's own form validation is recorded with.
INVALID_FORM = "invalid_form"

#: The funnel's own refusals, each with the status a plain POST answers with.
BAD_CREATE_FORM_COPY = (
    "The form didn't carry the request id Lens gave it — reload the form and try again."
)
INVALID_FORM_COPY = "Lens didn't send this — fix the fields marked below."
BAD_FORM_COPY = "The form didn't say what status you saw — reload and try again."
PRECHECK_FAILED_COPY = "Lens couldn't read the task, so it didn't try the write."
#: An HTMX attempt the Origin check refused. A plain POST keeps W1's 403 text;
#: an HTMX one is answered with the same notice every other refusal carries,
#: because htmx swaps no 4xx body and the operator would otherwise see nothing.
ORIGIN_REFUSED_COPY = "This request didn't come from a page this Lens served."
_BAD_REQUEST = 400
_FORBIDDEN = 403
_UNAVAILABLE = 503
_OK = 200

#: The registry's codes that mean "Lens could not ask or tell Lithos", which
#: §5C.5 answers with the registration-failed copy, as opposed to an identity
#: Lens will not write under.
_REGISTRATION_UNAVAILABLE = frozenset({"lookup_failed", "registration_failed"})


def origin_refusal(request: Request) -> PlainTextResponse | None:
    """403 for a cross-origin POST, or ``None`` to let the handler run.

    The milestone's ONE Origin check (§5C.6): the funnel runs it first, so a
    refused attempt is still recorded, and ``POST /operator`` calls it too.
    CSRF hygiene, not authentication — it stops another tab driving Lens with
    the operator's browser and nothing else.
    """
    if same_origin(
        # No default: ``None`` is how the guard tells an ABSENT header (Referer
        # fallback allowed) from a present empty one (a mismatch).
        origin=request.headers.get("origin"),
        referer=request.headers.get("referer"),
        host=request.headers.get("host", ""),
        scheme=request.url.scheme,
    ):
        return None
    logger.warning(
        "refused a cross-origin write POST",
        extra={
            "path": request.url.path,
            "origin": request.headers.get("origin", ""),
            "host": request.headers.get("host", ""),
        },
    )
    return PlainTextResponse(ORIGIN_REFUSAL_MESSAGE, status_code=_FORBIDDEN)


@dataclass(frozen=True)
class WriteForm:
    """What a route parsed out of its form: the funnel's whole input from it.

    ``expected_status`` and ``next_url`` are as submitted — the funnel judges
    both. ``arguments`` is the audit line's argument summary, built by the
    route because only it knows its fields: ids, types and LENGTHS, never free
    text (a note is "note_chars: 41", not the note).
    """

    task_id: str
    expected_status: str
    next_url: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WriteDone:
    """What a write that applied reported: the receipt's raw material.

    ``released`` are task IDS, in upstream's order — the funnel resolves the
    first few to titles when it mints the receipt. ``answer`` is the canonical
    success result as Lithos returned it, for the audit line's result
    envelope (§5C.6): a success is recorded with what Lithos said, exactly
    as a refusal is. ``prior_status``, ``checked_status``, ``released_exact``,
    ``released_unread``, ``released_waiting`` and a cancel's ``cancel`` facts
    are carried to the receipt as they are (see
    :class:`~lithos_lens.receipts.WriteReceipt`).
    """

    task: ReceiptTask
    outcome: str = ""
    released: tuple[str, ...] = ()
    answer: Mapping[str, Any] = field(default_factory=dict)
    prior_status: str = ""
    checked_status: str = ""
    released_exact: bool = True
    released_unread: bool = False
    released_waiting: bool = False
    cancel: CancelFacts | None = None


def _applies(task: TaskRecord) -> tuple[str, str] | None:
    return None


def _no_attributes(task: TaskRecord) -> Mapping[str, str | bool]:
    return {}


def _no_confirmation(task: TaskRecord) -> str:
    return ""


@dataclass(frozen=True)
class _Unconfirmed:
    """An attempt the action sends to its confirm page instead of performing."""

    page: str


@dataclass(frozen=True)
class TaskWrite:
    """One action on an existing task, as the funnel drives it.

    - ``perform`` places the single Lithos call for the task as read, under
      the operator's id, and reports what it did;
    - ``admits`` answers, for a task that passed the status pre-check, the
      ``(code, sentence)`` refusal when the action does not apply to it;
    - ``describe`` names the span attributes the task contributes (a
      completion's gate type, whether it was an override);
    - ``confirm_page`` answers, for a task the action applies to, the URL of
      the page that must confirm it first — empty when the task needs no
      confirmation or the form already carried it. Decided on the task AS
      READ, so the answer binds whatever the page that posted showed.
    """

    action: WriteAction
    perform: Callable[[LithosClientProtocol, TaskRecord, str], Awaitable[WriteDone]]
    admits: Callable[[TaskRecord], tuple[str, str] | None] = _applies
    describe: Callable[[TaskRecord], Mapping[str, str | bool]] = _no_attributes
    confirm_page: Callable[[TaskRecord], str] = _no_confirmation


#: Re-renders the create form: the input as typed, the problem that stopped it
#: (for the form-level notice), the per-field errors, and the status.
RenderCreateForm = Callable[
    [Request, CreateInput, WriteProblem | None, Mapping[str, FieldError], int],
    Awaitable[Response],
]


@dataclass(frozen=True)
class CreateWrite:
    """A create, as the funnel drives it (T3-W7, D1).

    ``typed`` is the form as posted. ``coordinator`` is the create routes'
    process-wide :class:`~lithos_lens.create_coordinator.CreateCoordinator`.
    ``render_form`` re-renders the form, input kept; ``render_unknown`` is
    create's own "not visible yet" page. ``return_to`` is where the operator
    page sends an operator who had no identity: the form, pre-filled again.
    """

    typed: CreateInput
    coordinator: CreateCoordinator
    render_form: RenderCreateForm
    render_unknown: Callable[[Request, CreateInput], Awaitable[Response]]
    return_to: str


@dataclass
class _Ledger:
    """One attempt's audit fields, filled in as the funnel learns them."""

    action: WriteAction
    task_id: str
    expected_status: str
    arguments: Mapping[str, Any]
    result: WriteResult = "unknown"
    code: str = ""
    operator: str = ""
    observed_status: str = ""
    envelope: Mapping[str, Any] = field(default_factory=dict)
    attributes: dict[str, str | bool] = field(default_factory=dict)


class WriteFunnel:
    """The one function every curated write goes through (D4).

    Held by the write route group for the process: the operator registry and
    the receipt store are its memory, and both are in-process by design (one
    Lens process, Lithos the only shared state).
    """

    def __init__(
        self,
        state: AppState,
        templates: Jinja2Templates,
        *,
        registry: OperatorRegistry,
        receipts: ReceiptStore,
        operator_page_for: Callable[[str], str],
    ) -> None:
        self._state = state
        self._templates = templates
        self._registry = registry
        self._receipts = receipts
        # Where an attempt with no identity is sent, given where the operator
        # should come back to. Passed in by the route group, which owns the
        # operator page's path.
        self._operator_page_for = operator_page_for

    async def submit(
        self, request: Request, form: WriteForm, write: TaskWrite
    ) -> Response:
        """Run one attempt end to end and answer it. Always recorded once."""
        ledger = _Ledger(
            action=write.action,
            task_id=form.task_id,
            expected_status=form.expected_status,
            arguments=form.arguments,
        )
        with get_tracer().start_as_current_span(f"lens.writes.{write.action}") as span:
            try:
                return await self._run(request, form, write, ledger)
            finally:
                # In a `finally`, so an attempt that raised out of the funnel —
                # a Lens defect — still leaves its line, with the result it had
                # reached.
                _record(span, ledger)

    async def _run(
        self, request: Request, form: WriteForm, write: TaskWrite, ledger: _Ledger
    ) -> Response:
        """Steps 1, 2 and 10: who may act, and how the attempt is answered."""
        htmx = request.headers.get("hx-request") == "true"
        back_to = safe_next(form.next_url, default=task_detail_path(form.task_id))

        refused = origin_refusal(request)
        if refused is not None:
            ledger.result = "refused_origin"
            if htmx:
                # 200 and the notice, like every HTMX outcome (clarification
                # 3) — the plain POST keeps its 403. Still no Lithos call.
                return self._fragment(
                    request,
                    funnel_problem(
                        write.action,
                        REFUSED,
                        ORIGIN_REFUSED_COPY,
                        code="",
                        status_code=_FORBIDDEN,
                        subject=TaskRef(task_id=form.task_id),
                    ),
                )
            return refused

        identity = resolve_operator(
            cookie=request.cookies.get(OPERATOR_COOKIE_NAME),
            default_operator=self._state.config.writes.default_operator,
        )
        if not identity.resolved:
            # Not replayed (D3): the operator repeats the action once they
            # have a name, so Lens never makes a write nobody was named for.
            ledger.result = "no_operator"
            target = self._operator_page_for(back_to)
            if htmx:
                return Response(status_code=_OK, headers={"HX-Redirect": target})
            return RedirectResponse(target, status_code=303)
        ledger.operator = identity.id

        outcome = await self._attempt(form, write, identity.id, ledger)
        if isinstance(outcome, WriteProblem):
            return self._refuse(request, ledger, outcome, htmx)
        if isinstance(outcome, _Unconfirmed):
            # Not performed: the confirm page states what the override does
            # and carries the confirmation. Answered like no identity — a
            # redirect in either mode, since an HTMX swap cannot hold a page.
            ledger.result = "rejected"
            ledger.code = CONFIRMATION_REQUIRED
            if htmx:
                return Response(status_code=_OK, headers={"HX-Redirect": outcome.page})
            return RedirectResponse(outcome.page, status_code=303)
        ledger.result = "ok"
        # Where this write returns the operator, for a follow-up form on the
        # receipt (Reopen gate) — the same checked value the 303 uses.
        outcome = replace(outcome, back_to=back_to)
        if htmx:
            return self._templates.TemplateResponse(
                request,
                RECEIPT_FRAGMENT,
                {"receipt": outcome},
                headers={"HX-Trigger": RECONCILE_TRIGGER},
            )
        receipt_id = self._receipts.put(outcome)
        return RedirectResponse(receipt_url(back_to, receipt_id), status_code=303)

    async def _attempt(
        self, form: WriteForm, write: TaskWrite, operator: str, ledger: _Ledger
    ) -> WriteReceipt | WriteProblem | _Unconfirmed:
        """Steps 3-9, for an attempt with an operator: a receipt, or why not."""
        action = write.action
        client = self._state.lithos_client
        subject = TaskRef(task_id=form.task_id)

        if form.expected_status not in TASK_STATUSES:
            return funnel_problem(
                action,
                REFUSED,
                BAD_FORM_COPY,
                code="bad_form",
                status_code=_BAD_REQUEST,
                subject=subject,
            )

        unregistered = await self._register(action, operator, subject)
        if unregistered is not None:
            return unregistered

        try:
            task = await client.task_get(form.task_id)
        except Exception as exc:
            if isinstance(exc, LithosToolError) and exc.code == "task_not_found":
                ledger.observed_status = "absent"
                return funnel_problem(
                    action,
                    CONFLICT,
                    "This task no longer exists.",
                    code="task_not_found",
                    status_code=CONFLICT_STATUS,
                    subject=subject,
                    hint="It may have been removed since you loaded the page.",
                )
            logger.warning(
                "write pre-check read failed",
                extra={"action": action, "task_id": form.task_id},
            )
            return funnel_problem(
                action,
                REFUSED,
                PRECHECK_FAILED_COPY,
                code="precheck_failed",
                status_code=_UNAVAILABLE,
                subject=subject,
                hint="Reload once Lithos is reachable.",
            )
        subject = TaskRef(task_id=task.id, title=task.title)
        ledger.observed_status = task.status
        ledger.attributes.update(write.describe(task))

        if task.status != form.expected_status:
            return funnel_problem(
                action,
                CONFLICT,
                f"This task is now {task.status}.",
                code="stale_status",
                status_code=CONFLICT_STATUS,
                subject=subject,
                hint="Reload to see the task as it is now.",
            )
        not_applicable = write.admits(task)
        if not_applicable is not None:
            code, sentence = not_applicable
            return funnel_problem(
                action,
                REFUSED,
                sentence,
                code=code,
                status_code=CONFLICT_STATUS,
                subject=subject,
            )
        confirm_page = write.confirm_page(task)
        if confirm_page:
            return _Unconfirmed(page=confirm_page)

        try:
            done = await write.perform(client, task, operator)
        except Exception as exc:
            # Only a NON-EMPTY envelope is Lithos answering. A timeout, a dead
            # session, an `isError` result and an unparseable body all raise
            # with an empty one — and any of them may have followed a write
            # that applied — so they are the unknown outcome, never a refusal.
            envelope = dict(exc.envelope) if isinstance(exc, LithosToolError) else {}
            if not envelope:
                logger.warning(
                    "write outcome unknown",
                    extra={"action": action, "task_id": task.id},
                    exc_info=True,
                )
            reread = await self._reread(
                task.id, needed=not envelope or _split(envelope)
            )
            if reread.found:
                ledger.observed_status = reread.status
            ledger.envelope = envelope
            return map_write_error(
                action, envelope or None, reread=reread, subject=subject
            )

        ledger.envelope = done.answer
        # (W8's synthetic-event publish lands here: after the call, before the
        # receipt. W4 publishes nothing — upstream emits complete's event.)
        return await self._mint(action, done, operator, client)

    async def submit_create(self, request: Request, write: CreateWrite) -> Response:
        """Run one create attempt end to end and answer it (D1). Recorded once.

        Plain POST only. No ``expected_status``: there is no task yet, so
        §5C.6's pre-check does not apply (D15) — Lens's validation stands in
        its place, and the call goes through the coordinator.
        """
        ledger = _Ledger(action="create", task_id="", expected_status="", arguments={})
        ledger.attributes["request_id"] = write.typed.request_id
        with get_tracer().start_as_current_span("lens.writes.create") as span:
            try:
                return await self._run_create(request, write, ledger)
            finally:
                _record(span, ledger)

    async def _run_create(
        self, request: Request, write: CreateWrite, ledger: _Ledger
    ) -> Response:
        typed = write.typed
        refused = origin_refusal(request)
        if refused is not None:
            ledger.result = "refused_origin"
            return refused
        identity = resolve_operator(
            cookie=request.cookies.get(OPERATOR_COOKIE_NAME),
            default_operator=self._state.config.writes.default_operator,
        )
        if not identity.resolved:
            # Not replayed (D3): the form comes back pre-filled, not submitted.
            ledger.result = "no_operator"
            return RedirectResponse(
                self._operator_page_for(write.return_to), status_code=303
            )
        ledger.operator = identity.id

        if not is_request_id(typed.request_id):
            return self._refuse(
                request,
                ledger,
                funnel_problem(
                    "create",
                    REFUSED,
                    BAD_CREATE_FORM_COPY,
                    code="bad_form",
                    status_code=_BAD_REQUEST,
                ),
                htmx=False,
            )
        validated = validate(
            typed, project_tag_key=self._state.config.tasks.project_tag_key
        )
        if validated.request is None:
            ledger.result = "rejected"
            ledger.code = INVALID_FORM
            problem = funnel_problem(
                "create",
                REFUSED,
                INVALID_FORM_COPY,
                code=INVALID_FORM,
                status_code=REFUSED_STATUS,
            )
            return await write.render_form(
                request, typed, problem, validated.errors, REFUSED_STATUS
            )
        create = validated.request
        ledger.arguments = create.arguments()

        unregistered = await self._register("create", identity.id, NO_SUBJECT)
        if unregistered is not None:
            ledger.result = "rejected"
            ledger.code = unregistered.code
            return await write.render_form(
                request, typed, unregistered, {}, unregistered.status_code
            )

        settled = await write.coordinator.submit(
            typed.request_id, lambda: self._create_once(create, identity.id)
        )
        if settled.dedup:
            ledger.attributes["dedup"] = settled.dedup
        outcome = settled.outcome
        if isinstance(outcome, Refused):
            ledger.result = "rejected"
            ledger.envelope = dict(outcome.envelope)
            problem = map_write_error("create", outcome.envelope)
            ledger.code = problem.code
            return await write.render_form(
                request, typed, problem, {}, problem.status_code
            )
        if isinstance(outcome, OutcomeUnknown):
            ledger.result = "unknown"
            return await write.render_unknown(request, typed)
        ledger.result = "ok"
        ledger.task_id = outcome.task_id
        ledger.envelope = dict(outcome.answer)
        # Each submit mints its OWN receipt — a joined or remembered one too —
        # because a receipt is consumed by the page that shows it.
        receipt = WriteReceipt(
            action="create",
            task=ReceiptTask(task_id=outcome.task_id, title=outcome.title),
            # The facts of the create that RAN — its creator too — not of this
            # submit's form or identity, which a resubmit under the same id
            # may have changed. The attempt itself is recorded under the
            # submitting operator (`ledger.operator`).
            operator=outcome.operator,
            created=CreateFacts(
                task_type=outcome.task_type,
                project=outcome.project,
                repeated=bool(settled.dedup),
            ),
        )
        receipt_id = self._receipts.put(receipt)
        return RedirectResponse(
            receipt_url(task_detail_path(outcome.task_id), receipt_id),
            status_code=303,
        )

    async def _register(
        self, action: WriteAction, operator: str, subject: TaskRef
    ) -> WriteProblem | None:
        """Step 4: guard and register the identity once, or why it can't write."""
        checked = await self._registry.ensure_registered(
            self._state.lithos_client, operator
        )
        if checked.ok:
            return None
        unavailable = checked.code in _REGISTRATION_UNAVAILABLE
        return funnel_problem(
            action,
            REFUSED,
            REFUSAL_REGISTRATION_FAILED if unavailable else checked.reason,
            code="registration_failed" if unavailable else "identity_refused",
            status_code=_UNAVAILABLE if unavailable else _FORBIDDEN,
            subject=subject,
        )

    async def _create_once(self, create: CreateRequest, operator: str) -> CreateOutcome:
        """The single ``lithos_task_create``, and how it ended.

        Run by the coordinator at most once per request id. An answer with an
        envelope is a refusal; anything else that raised — a timeout, a dead
        session, an unparseable answer — is no answer, and the create may
        still be landing upstream.
        """
        try:
            result = await create.send(self._state.lithos_client, agent=operator)
        except Exception as exc:
            envelope = dict(exc.envelope) if isinstance(exc, LithosToolError) else {}
            if envelope:
                return Refused(envelope=envelope)
            logger.warning(
                "create outcome unknown",
                extra={"request_id": create.request_id},
                exc_info=True,
            )
            return OutcomeUnknown()
        if not result.task_id:
            logger.warning(
                "create answered without a task id",
                extra={"request_id": create.request_id},
            )
            return OutcomeUnknown()
        # A task event evicts only the NEW id (`task.created`), so the parent's
        # and the predecessors' cached edges are dropped here, and their pages
        # show the new edge at once (D14).
        for linked in (result.parent_task_id, *result.depends_on):
            if linked:
                self._state.graph_cache.evict(linked)
        return Created(
            task_id=result.task_id,
            title=result.title or create.title,
            answer={**asdict(result), "depends_on": list(result.depends_on)},
            task_type=create.task_type,
            project=create.project,
            operator=operator,
        )

    def _refuse(
        self, request: Request, ledger: _Ledger, problem: WriteProblem, htmx: bool
    ) -> Response:
        """Record a write that did not apply, and answer it in the right mode."""
        if problem.kind == "unknown_outcome":
            ledger.result = "unknown"
        elif problem.kind == CONFLICT:
            ledger.result = "conflict"
        else:
            ledger.result = "rejected"
        ledger.code = problem.code
        if htmx:
            return self._fragment(request, problem)
        return self._templates.TemplateResponse(
            request,
            problem.page or REFUSED_PAGE,
            {
                "config": self._state.config,
                "active_view": "tasks",
                "problem": problem,
            },
            status_code=problem.status_code,
        )

    def _fragment(self, request: Request, problem: WriteProblem) -> Response:
        """An HTMX attempt that did not apply: its copy, as a 200 fragment.

        200 whatever the outcome: htmx 2 swaps no 4xx/5xx body, and a refusal
        that showed the operator nothing would be the worst answer there is.
        The trigger rides along so the board re-reads either way.
        """
        return self._templates.TemplateResponse(
            request,
            OUTCOME_FRAGMENT,
            {"problem": problem},
            headers={"HX-Trigger": RECONCILE_TRIGGER},
        )

    async def _reread(self, task_id: str, *, needed: bool) -> TaskReread:
        """What a fresh read shows of the task, for the rows that need one."""
        if not needed:
            return NOT_READ
        try:
            task = await self._state.lithos_client.task_get(task_id)
        except LithosToolError as exc:
            return TASK_ABSENT if exc.code == "task_not_found" else REREAD_FAILED
        except Exception:
            return REREAD_FAILED
        return TaskReread(outcome="exists", status=task.status)

    async def _mint(
        self,
        action: WriteAction,
        done: WriteDone,
        operator: str,
        client: LithosClientProtocol,
    ) -> WriteReceipt:
        """The receipt, with the first few released tasks named by title.

        Read now, at mint time, because the pages a receipt lands on hold no
        task index to resolve them from (the knowledge and operator pages take
        ``?receipt=`` too). Bounded at :data:`MAX_TITLED_RELEASES` reads; a read
        that fails leaves that task's short id standing alone.
        """
        named = done.released[:MAX_TITLED_RELEASES]
        reads = await asyncio.gather(
            *(client.task_get(task_id) for task_id in named), return_exceptions=True
        )
        released = tuple(
            ReceiptTask(
                task_id=task_id,
                title=read.title if isinstance(read, TaskRecord) else "",
            )
            for task_id, read in zip(named, reads, strict=True)
        )
        return WriteReceipt(
            action=action,
            task=done.task,
            operator=operator,
            outcome=done.outcome,
            released=released,
            released_total=len(done.released),
            prior_status=done.prior_status,
            checked_status=done.checked_status,
            released_exact=done.released_exact,
            released_unread=done.released_unread,
            released_waiting=done.released_waiting,
            cancel=done.cancel,
        )


def _split(envelope: Mapping[str, Any]) -> bool:
    """Whether an upstream refusal needs the re-read to be answered.

    ``task_not_found`` is one code for two facts — gone, or no longer open —
    and only a fresh read can tell the operator which (``write_errors``).
    """
    return envelope.get("code") == "task_not_found"


def _record(span: Span, ledger: _Ledger) -> None:
    """One attempt's span attributes, counter increment and audit line.

    The operator, the task and a refusal's code are span attributes and audit
    fields, never metric labels (``metrics.writes``).
    """
    span.set_attribute("lens.write.action", ledger.action)
    span.set_attribute("lens.write.result", ledger.result)
    span.set_attribute("lens.write.task_id", ledger.task_id)
    span.set_attribute("lens.write.operator", ledger.operator)
    if ledger.code:
        span.set_attribute("lens.write.code", ledger.code)
    for key, value in ledger.attributes.items():
        span.set_attribute(f"lens.write.{key}", value)
    metrics.writes().add(1, {"action": ledger.action, "result": ledger.result})
    logger.info(
        "write attempt",
        extra={
            "lens_event": AUDIT_EVENT,
            "operator": ledger.operator,
            "action": ledger.action,
            "task_id": ledger.task_id,
            "arguments": dict(ledger.arguments),
            "expected_status": ledger.expected_status,
            "observed_status": ledger.observed_status,
            "result": ledger.result,
            "code": ledger.code,
            "envelope": dict(ledger.envelope),
            **{f"write_{key}": value for key, value in ledger.attributes.items()},
        },
    )
