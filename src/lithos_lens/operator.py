"""Operator identity: the name a curated write is attributed to (§5C.5).

Lens has one service agent (``[lithos].agent_id``, type ``web-ui``) and it is
deliberately NOT what a write is recorded under: an audit trail has to tell
"Lens the process" apart from "the human driving it". So every write carries a
named human operator, resolved here from the ``lens_operator`` cookie, else
``[writes].default_operator``, else nothing — and registered upstream as
``type="human"`` before its first write.

Deep and browser-free on purpose (PRD D13): the resolution, the id rule, the
impersonation guard and the register-once ledger are plain functions and one
small in-memory object, so every case this module exists for — an archived
agent's id, a lookup that fails, a second write — is a unit test rather than a
request. The route group that reads the cookie and renders the page is
``lithos_lens.write_routes``; the write funnel W4 adds calls
:meth:`OperatorRegistry.ensure_registered` and nothing else.

The cookie is an ATTRIBUTION LABEL, not a credential (REQUIREMENTS §5C.1):
Lens has no authentication, the boundary is the trusted network, and anyone who
can reach the port can set any identity here. That is why the id rule below is
about what is a legible name and what is safe to render, not about proving who
sent the request.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from lithos_lens.tasks import AgentRecord

logger = logging.getLogger(__name__)

__all__ = [
    "HUMAN_AGENT_TYPE",
    "MAX_OPERATOR_ID_LENGTH",
    "OPERATOR_COOKIE_MAX_AGE_S",
    "OPERATOR_COOKIE_NAME",
    "OPERATOR_ID_RULE",
    "IdentityCheck",
    "OperatorIdentity",
    "OperatorLithosClient",
    "OperatorRegistry",
    "resolve_operator",
    "valid_operator_id",
]

#: The cookie that carries the chosen identity. Set with ``HttpOnly`` and
#: ``SameSite=Lax`` and NO ``Secure`` attribute — Lens serves plain HTTP on the
#: trusted network, so ``Secure`` would stop the browser storing it at all, and
#: the cookie is an attribution label rather than a credential (§5C.1).
OPERATOR_COOKIE_NAME = "lens_operator"

#: One year. The identity is chosen once per browser and shown on every page,
#: so an expiry short enough to matter would only re-prompt an operator who had
#: not changed anything.
OPERATOR_COOKIE_MAX_AGE_S = 365 * 24 * 60 * 60

#: Upstream agent type an operator identity must be registered under. Also the
#: ONE type the impersonation guard accepts on an existing id.
HUMAN_AGENT_TYPE = "human"

#: Longest operator id accepted. The id is rendered on every surface and sent
#: as the ``agent`` argument of every write; it is bounded because the cookie
#: is unvalidated user input and an unbounded one would be echoed into pages
#: and into Lithos's registry.
MAX_OPERATOR_ID_LENGTH = 63

#: The id rule, in one place for all three of its readers: the cookie (an
#: invalid value is treated as absent), ``[writes].default_operator`` (an
#: invalid value fails the load) and the operator page's form field (an invalid
#: value re-renders the form). A lowercase slug — the spelling agent ids
#: already use upstream — so the name in an audit line is the name a person
#: types, with no case or whitespace variants of one identity.
_OPERATOR_ID_PATTERN = re.compile(
    rf"^[a-z0-9][a-z0-9-]{{0,{MAX_OPERATOR_ID_LENGTH - 1}}}$"
)

#: The rule as a sentence, for the messages that have to state it.
OPERATOR_ID_RULE = (
    "lowercase letters, digits and dashes, starting with a letter or digit, "
    f"at most {MAX_OPERATOR_ID_LENGTH} characters"
)

OperatorSource = Literal["cookie", "default", "none"]

#: What each source is CALLED on the operator page. The page has to say where
#: the identity came from, because "switch" means different things for a cookie
#: this browser holds and for a value the deployment configured.
SOURCE_LABELS: dict[OperatorSource, str] = {
    "cookie": "chosen in this browser",
    "default": "the deployment's configured default ([writes].default_operator)",
    "none": "not set",
}


def valid_operator_id(value: str) -> bool:
    """Whether ``value`` is a legible operator id (:data:`OPERATOR_ID_RULE`)."""
    return bool(_OPERATOR_ID_PATTERN.fullmatch(value))


@dataclass(frozen=True)
class OperatorIdentity:
    """The identity a request acts under, and where it came from."""

    id: str = ""
    source: OperatorSource = "none"

    @property
    def resolved(self) -> bool:
        return bool(self.id)

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS[self.source]


#: The one value every "no identity" path answers with.
NO_IDENTITY = OperatorIdentity()


def resolve_operator(*, cookie: str | None, default_operator: str) -> OperatorIdentity:
    """Resolve the acting identity: cookie → configured default → none (D3).

    The cookie is user input and is validated on EVERY read rather than once
    when it was set — nothing stops a client sending any value — and an invalid
    one is treated as absent, which is what makes the configured default still
    apply to a browser holding junk.

    ``default_operator`` is validated at load (``config._parse_writes``), so an
    invalid one cannot reach here; it is re-checked anyway, because this
    function is the one place that decides what a request acts as and a silent
    fallthrough would attribute writes to an id no surface could render.
    """
    if cookie and valid_operator_id(cookie):
        return OperatorIdentity(id=cookie, source="cookie")
    if default_operator and valid_operator_id(default_operator):
        return OperatorIdentity(id=default_operator, source="default")
    return NO_IDENTITY


# ── the impersonation guard and the register-once ledger ───────────────

#: Why an identity was refused, and the sentence the operator reads. The codes
#: are for the log line and the tests; the copy is what a page renders.
REFUSAL_INVALID_ID = f"That is not a usable operator id — use {OPERATOR_ID_RULE}."
REFUSAL_SERVICE_AGENT = (
    "That id is Lens's own service agent. Writes name the person driving "
    "Lens, never Lens itself — choose a name of your own."
)
REFUSAL_BELONGS_TO_AGENT = (
    "That id belongs to an agent. Registering it as a human would overwrite "
    "the agent's registry entry, so Lens will not use it — choose another id."
)
REFUSAL_LOOKUP_FAILED = (
    "Lens could not check that id with Lithos, so it was not accepted; "
    "nothing was changed. Try again when Lithos answers."
)
#: §5C.5's wording, kept verbatim: a refused write says first that nothing was
#: changed.
REFUSAL_REGISTRATION_FAILED = (
    "Could not register the operator identity; nothing was changed."
)


@dataclass(frozen=True)
class IdentityCheck:
    """The verdict on an identity: usable, or refused with a reason."""

    ok: bool
    code: str = ""
    reason: str = ""


_ACCEPTED = IdentityCheck(ok=True)


class OperatorLithosClient(Protocol):
    """The two client calls the identity module makes.

    Structural, not the concrete client: the layering contract forbids
    Foundation importing Core (the same reason ``frontier.FrontierLithosClient``
    and ``task_detail.TaskDetailClient`` exist).
    """

    async def agent_info(self, agent_id: str) -> AgentRecord | None: ...

    async def register_operator(self, operator_id: str) -> bool: ...


class OperatorRegistry:
    """Guards an identity, then registers it once per process (D3).

    One in-memory set, process-lifetime: the ids REGISTERED as ``type="human"``
    here. That set is what makes "registration precedes the first write" cost
    one upstream call per identity instead of one per write, and what keeps an
    operator already writing in this session working when Lithos goes away.

    What it deliberately does NOT hold is a set of ids some earlier check
    merely approved. The guard's whole value is that the lookup happens
    immediately before the registration that could overwrite an agent's type,
    and an acceptance cached when the operator page was visited would suppress
    exactly that lookup minutes later — a far wider window than the
    lookup-to-register one clarification 3 accepts, and one Lens can close by
    simply not caching. So :meth:`check` is a probe with no memory, and
    :meth:`ensure_registered` always does its own lookup before registering.

    Losing the set to a restart is harmless: the next write rebuilds it by
    doing the lookup and the registration again, and re-registering an id
    ALREADY typed ``human`` leaves it typed ``human``.
    """

    def __init__(self, *, service_agent_id: str) -> None:
        self._service_agent_id = service_agent_id.strip().lower()
        self._registered: set[str] = set()
        # Serialises the whole ensure-registered operation (lookup, register,
        # record), so "exactly one registration per identity per process" holds
        # for the concurrent first writes the operational model names — a
        # double submit, or two tabs. Without it the check-then-act between the
        # `_registered` test and its `add` lets both callers look up and both
        # register.
        #
        # ONE lock for the registry rather than one per identity: a
        # registration happens once per identity per process, so the
        # contention it serialises is nil, and a per-id map would be a dict
        # keyed by values that arrive in a request — unbounded for no gain.
        # Created lazily because an OperatorRegistry is built by
        # `register_write_routes`, which may run outside a running loop.
        self._registration_lock: asyncio.Lock | None = None

    async def check(
        self, client: OperatorLithosClient, operator_id: str
    ) -> IdentityCheck:
        """The impersonation guard: may Lens act as ``operator_id``, right now?

        ``lithos_agent_register`` has no "create only" form — re-registering an
        existing id with a type OVERWRITES that agent's stored type and
        un-archives it — so an id is looked up EXACTLY first, with
        ``lithos_agent_info``. Not through ``lithos_agent_list``, which hides
        archived agents by default: an archived agent's id would read as new
        and be silently re-typed into a human.

        Accepted when the lookup finds nothing, or finds an agent already typed
        ``human`` (archived or not — the type is the whole question). Refused
        for Lens's own service agent, and for any id that exists with another
        type or with none. A lookup that FAILS refuses: Lens cannot tell
        "absent" from "unreadable", and the safe answer is the one that changes
        nothing upstream.

        A PROBE, with no memory: it answers for the moment it runs and records
        nothing, so a yes here never stands in for the lookup
        :meth:`ensure_registered` does before it registers. ``POST /operator``
        calls this so a refusal is immediate rather than discovered at the
        first write; the answer that binds is the seam's.
        """
        if not valid_operator_id(operator_id):
            return IdentityCheck(False, "invalid_id", REFUSAL_INVALID_ID)
        if operator_id == self._service_agent_id:
            return IdentityCheck(False, "service_agent", REFUSAL_SERVICE_AGENT)
        try:
            agent = await client.agent_info(operator_id)
        except Exception:
            logger.warning(
                "operator identity lookup failed", extra={"operator": operator_id}
            )
            return IdentityCheck(False, "lookup_failed", REFUSAL_LOOKUP_FAILED)
        if agent is not None and agent.type != HUMAN_AGENT_TYPE:
            return IdentityCheck(False, "belongs_to_agent", REFUSAL_BELONGS_TO_AGENT)
        return _ACCEPTED

    async def ensure_registered(
        self, client: OperatorLithosClient, operator_id: str
    ) -> IdentityCheck:
        """Guard ``operator_id`` and register it, once per process (the W4 seam).

        Every write goes through here, whatever the identity's source — so the
        guard covers the cookie, ``[writes].default_operator`` (which never
        passes through the operator page at all) and a cookie chosen before its
        id became an agent's. This is where the guard BINDS: the lookup is
        this call's own, never one an earlier page visit did.

        Lithos auto-registers an unknown ``agent`` on any write, untyped, which
        is what this call exists to pre-empt: the agent pickers and the Planning
        View's definition of a human read the registry's type.

        Already registered in this process → no call at all, which is both the
        cost bound (one registration per identity, not one per write) and why
        an operator mid-session survives a Lithos outage.

        KNOWN LIMIT, deliberately not closed (PRD clarification 3, Dave
        2026-10-03): between the lookup below and the registration after it,
        another writer could register the same id with a type, and this call
        would then overwrite it to ``human``. ``lithos_agent_register`` has no
        conditional form, so nothing Lens can do here closes that window —
        narrowing it is the most a pre-check can do, exactly as D4's
        ``expected_status`` pre-check narrows the write race. Recorded in
        docs/SPECIFICATION.md beside the guard; a finding asking Lens to close
        it is out of scope until upstream grows a conditional registration.
        """
        if operator_id in self._registered:
            return _ACCEPTED
        if self._registration_lock is None:
            self._registration_lock = asyncio.Lock()
        async with self._registration_lock:
            # Re-read inside the lock: the caller that held it may have been
            # the first write for this very identity, in which case this one
            # takes its result and makes no call of its own.
            if operator_id in self._registered:
                return _ACCEPTED
            checked = await self.check(client, operator_id)
            if not checked.ok:
                return checked
            try:
                registered = await client.register_operator(operator_id)
            except Exception:
                logger.warning(
                    "operator registration failed", extra={"operator": operator_id}
                )
                registered = False
            if not registered:
                # Not remembered, so the next write tries again rather than
                # writing under an identity Lithos never typed.
                return IdentityCheck(
                    False, "registration_failed", REFUSAL_REGISTRATION_FAILED
                )
            self._registered.add(operator_id)
            return _ACCEPTED
