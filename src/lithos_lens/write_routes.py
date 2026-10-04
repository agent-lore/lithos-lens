"""The curated-write route group (§5C.7) — W1: operator identity.

Registered like the graph and knowledge groups: a closure over app, state and
templates, attached by ``create_app`` BEFORE the dynamic ``/tasks/{task_id}``
route, because every later slice's routes land here and a static write path
attached after it would be matched as a task id.

What this module owns, and what every later slice reuses:

- ``GET``/``POST /operator`` — the identity page, the one surface that states
  the trusted-network boundary (REQUIREMENTS §5C.1).
- :func:`origin_refusal`, the 403 answer for a POST that did not come from a
  page this Lens served, applied before any Lithos call.
- :func:`request_identity`, how a request's acting identity is resolved from
  the cookie and the configured default, and the chrome's "Acting as …" chip.
- the process's :class:`~lithos_lens.operator.OperatorRegistry` — the
  guard-and-register-once ledger, held in this closure because it is the write
  surface's own memory and the write funnel W4 adds is registered here too.

There is deliberately NO posture switch (D2): the routes are always registered
and the affordances are part of the page. What decides whether an affordance
renders is the task's state and whether an identity resolves — never config.
"""

from __future__ import annotations

import logging
from functools import partial
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lithos_lens.operator import (
    OPERATOR_COOKIE_MAX_AGE_S,
    OPERATOR_COOKIE_NAME,
    OPERATOR_ID_RULE,
    IdentityCheck,
    OperatorIdentity,
    OperatorRegistry,
    resolve_operator,
)
from lithos_lens.request_filters import filter_query_oversized
from lithos_lens.state import AppState
from lithos_lens.tasks import MAX_FILTER_QUERY_BYTES
from lithos_lens.write_guards import (
    ORIGIN_REFUSAL_MESSAGE,
    safe_next,
    same_origin,
)

logger = logging.getLogger(__name__)

#: The operator page, and the default destination of its own form.
OPERATOR_PATH = "/operator"

#: The query key that carries where to return the operator afterwards. One
#: spelling, shared with W4's post-write redirect, because both read it from
#: the same untrusted place through ``write_guards.safe_next``.
NEXT_KEY = "next"


def origin_refusal(request: Request) -> PlainTextResponse | None:
    """403 for a cross-origin POST, or ``None`` to let the handler run.

    The milestone's ONE Origin check (§5C.6): every write POST calls this
    first, so the refusal happens before any Lithos call and cannot be
    forgotten per route. CSRF hygiene, not authentication — it stops another
    tab driving Lens with the operator's browser and nothing else.
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
    return PlainTextResponse(ORIGIN_REFUSAL_MESSAGE, status_code=403)


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


def register_write_routes(
    app: FastAPI, state: AppState, templates: Jinja2Templates
) -> None:
    """Attach the write route group and its template globals."""

    default_operator = state.config.writes.default_operator
    # One registry per process: the ids the impersonation guard has accepted
    # and the ids registered as type="human". Held here rather than on
    # AppState because nothing outside the write surface reads it, and the
    # write funnel (W4) is registered in this same group.
    registry = OperatorRegistry(service_agent_id=state.config.lithos.agent_id)

    templates.env.globals["operator_identity"] = partial(
        request_identity, default_operator=default_operator
    )
    templates.env.globals["operator_page_url"] = operator_page_url
    templates.env.globals["operator_path"] = OPERATOR_PATH

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
