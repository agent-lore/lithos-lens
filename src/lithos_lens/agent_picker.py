"""The Agent filter picker: last activity, its ordering, and the dead-row window.

``lithos_agent_list`` is a registration LOG, not a roster: it accumulates one
row per probe, per session and per host, so the production list is 59 rows of
which about fifteen are live identities (T2 UX pass, 2026-09-16) — thirteen
test/probe leftovers, and one actor registered many times over (eight
``Claude Code (…)`` variants, three ``Lithos Lens`` rows sharing one name).
Rendered verbatim into the ``agents`` datalist, the registrations that matter
were crowded out by the ones nobody can tell apart.

Dedupe is upstream's to fix (filed on Lithos: ``agent_register`` has none, and
the skill's guidance registers per session). What Lens can do without a Lithos
change is say WHEN each registration was last active, order by it, and keep the
ones that have done nothing inside ``[tasks] agent_inactive_days`` out of the
default list. A ``<datalist>`` cannot fade an option, so hiding is the
behaviour, and ``?all_agents=1`` brings them back — a query parameter, so the
choice survives a reload and works with no JavaScript.

Activity is derived from rows the dashboard ALREADY loaded: ``created_by`` /
``created_at`` over the open snapshot and the resolved windows, the inline
claims that came with the open read, and ``last_seen_at`` off the registration
itself as the fallback for an agent that never did any of it. Never a per-agent
call — the picker stays ONE ``lithos_agent_list`` read rather than a fan-out
over 59 registrations. "Last finding posted" is deliberately not part of it:
findings are not in the snapshot, and reading them would buy one more signal at
exactly that cost.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from lithos_lens.tasks import AgentRecord, TaskRecord, humanize_age, parse_timestamp

__all__ = [
    "DEFAULT_AGENT_INACTIVE_DAYS",
    "SHOW_ALL_AGENTS_KEY",
    "AgentOption",
    "agent_options",
    "show_all_agents",
]

# Default window for "still around": a month of no task, no claim and no
# registration touch. The real value comes from ``[tasks] agent_inactive_days``;
# this literal is the module's own default for the same reason
# ``AttentionPolicy`` carries the Needs-attention thresholds' — Foundation does
# not import Config.
DEFAULT_AGENT_INACTIVE_DAYS = 30

# The "show all agents" toggle. Deliberately NOT one of request_filters'
# preserved keys: it narrows nothing, so carrying it into every generated link
# would make ``board_is_filtered`` call an unfiltered board filtered. The filter
# form re-emits it as a hidden input instead, so applying a filter keeps it.
SHOW_ALL_AGENTS_KEY = "all_agents"

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentOption:
    """One registration as the picker renders it: who, when, and whether to show.

    ``age`` is measured from :attr:`last_active_at`, the newest DATED signal —
    a task this agent created, or its registration stamp. A held claim carries
    no date upstream (``lithos_task_list``'s inline claims have an expiry, not a
    start), so it rides as :attr:`holds_claim` rather than being back-dated to
    an instant nothing observed.
    """

    agent: AgentRecord
    last_active_at: str = ""
    age: timedelta | None = None
    # True when the only stamp is the registration record: the agent exists,
    # but nothing on this board was ever created or claimed by it. Said in the
    # label, because "registered 2h ago" and "created something 2h ago" are
    # very different answers to "is this identity live?".
    registered_only: bool = False
    holds_claim: bool = False
    # Inside the window (or holding a claim): rendered in the default datalist.
    active: bool = False
    # Another registration shares this one's ``name``, so the label shows the id
    # too — the picker's whole failure mode is three identical ``Lithos Lens``
    # rows the operator has to guess between.
    ambiguous_name: bool = False

    @property
    def id(self) -> str:
        """The option's VALUE — what the ``agent`` filter matches on, unchanged."""
        return self.agent.id

    @property
    def activity(self) -> str:
        """When this agent was last active, in the board's own age style.

        ``humanize_age`` is the dashboard's one relative-time formatter (the
        Needs-attention details and the reconciliation badge state their ages
        with it); a second rounding here would read as a contradiction against
        the rows below.
        """
        if self.age is None:
            return "claim held" if self.holds_claim else "no activity"
        text = f"{humanize_age(self.age)} ago"
        if self.registered_only:
            text = f"registered {text}"
        return f"{text} · claim held" if self.holds_claim else text

    @property
    def label(self) -> str:
        """The ``<option>`` text: who, disambiguated when it has to be, and when."""
        name = self.agent.name or self.agent.id
        if self.ambiguous_name:
            name = f"{name} ({self.agent.id})"
        return f"{name} · {self.activity}"


def show_all_agents(value: str) -> bool:
    """Whether ``?all_agents=`` asks for the out-of-window registrations too."""
    return value.strip().lower() in _TRUTHY


def agent_options(
    agents_result: list[AgentRecord] | BaseException,
    tasks: Sequence[TaskRecord],
    errors: list[str],
    *,
    days: int = DEFAULT_AGENT_INACTIVE_DAYS,
    now: datetime | None = None,
) -> tuple[AgentOption, ...]:
    """Order every registration by last activity, newest first, and window it.

    ``tasks`` is the load's UNFILTERED rows (open snapshot + both resolved
    windows, deduped): the picker lists who you can switch to, so filtering the
    board by one agent must not collapse the evidence for the others — the same
    rule ``project_universe`` follows for the project datalist.

    Every option is returned, in-window or not, each carrying
    :attr:`AgentOption.active`; the template renders the inactive ones only
    under the show-all toggle. Assembling the WHOLE list once keeps the toggle
    a pure presentation choice and the count on the situation card (which is
    every registration) consistent with what it counts.

    A failed agent read appends the load error and answers with no options —
    the picker is one input on a board the rest of which is perfectly fine.
    """
    if isinstance(agents_result, BaseException):
        errors.append("Could not load agent list.")
        return ()
    agents = cast("list[AgentRecord]", agents_result)
    evaluated_at = now or datetime.now(UTC)
    window = timedelta(days=days)
    worked_at = _worked_at(tasks)
    claimants = _claimants(tasks)
    shared_names = {
        name
        for name, count in Counter(a.name for a in agents if a.name).items()
        if count > 1
    }
    options = []
    for agent in agents:
        # The registration stamp is a FALLBACK, never a competitor: an agent
        # that did something is described by that, even when Lithos touched
        # last_seen_at more recently than the work happened.
        stamp = worked_at.get(agent.id) or parse_timestamp(agent.last_seen_at)
        age = None if stamp is None else evaluated_at - stamp
        holds_claim = agent.id in claimants
        options.append(
            AgentOption(
                agent=agent,
                last_active_at="" if stamp is None else stamp.isoformat(),
                age=age,
                registered_only=agent.id not in worked_at and stamp is not None,
                holds_claim=holds_claim,
                # A claim is live state, so its holder is active whatever the
                # dates say — a long-running claim on a task created before the
                # window would otherwise hide the one agent working right now.
                active=holds_claim or (age is not None and age <= window),
                ambiguous_name=agent.name in shared_names,
            )
        )
    return tuple(sorted(options, key=_recency))


def _recency(option: AgentOption) -> tuple[bool, timedelta, str, str]:
    """Sort key: claim-holders, then newest activity, then a stable tie-break.

    A held claim outranks every date because it is the only signal about NOW;
    ages sort ascending (a smaller age is more recent), and an agent with no
    stamp at all sorts last rather than being dropped — the show-all toggle
    still has to list it somewhere.
    """
    return (
        not option.holds_claim,
        option.age if option.age is not None else timedelta.max,
        option.agent.name,
        option.agent.id,
    )


def _worked_at(tasks: Sequence[TaskRecord]) -> dict[str, datetime]:
    """Newest ``created_at`` per creating agent, over the loaded rows.

    Unreadable stamps are skipped rather than guessed at: the picker would
    otherwise date an agent by a row it could not read, and the window decides
    whether that agent is shown at all.
    """
    newest: dict[str, datetime] = {}
    for task in tasks:
        created_at = parse_timestamp(task.created_at)
        if not task.created_by or created_at is None:
            continue
        current = newest.get(task.created_by)
        if current is None or created_at > current:
            newest[task.created_by] = created_at
    return newest


def _claimants(tasks: Sequence[TaskRecord]) -> frozenset[str]:
    """Every agent holding an inline claim on a loaded row.

    ``claims is None`` means the read did not ask for them (the resolved
    windows, unless the agent filter needs them), not "no claimer" — so it
    contributes nothing here rather than evidence of inactivity.
    """
    return frozenset(
        claim.agent for task in tasks for claim in task.claims or () if claim.agent
    )
