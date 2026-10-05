"""What the Proceed anyway confirm page states about a gate (T3-W4b, D7).

A ``timer``, ``ci`` or ``pr`` gate — or one whose type Lens does not know — is
resolved by whatever watches it, so completing it by hand overrides that wait.
The confirm page makes the override a decision by stating, from what Lens can
read and nothing more:

- what would otherwise resolve the gate: a timer's ``ready_at`` (and whether it
  has already passed, when the gate no longer blocks anything and completing
  only closes it), a PR gate's PR link, and for CI and unknown types the gate's
  own description, which the template renders;
- the waiters completing it releases, from the SAME read the gate row's waiter
  list uses (``gates.attach_gate_waiters`` over the whole open list and the
  blocked frontier), so the "at least N" and "unverified" labels carry over
  unchanged.

It deliberately says nothing about how the gate's author reacts. A loom ``pr``
gate's waiter is the story itself, so proceeding makes the story ready again
while its PR is still open; what loom's watcher then does is not in its
specification, and the page does not guess.

Web rather than Writes: the waiter read is the Gates section's, which the
Foundation write rules may not import, and the route group that renders the
page already reads through the client.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast
from urllib.parse import urlsplit

from lithos_lens.gates import (
    GateRow,
    GateWaiterState,
    attach_gate_waiters,
    collect_gates,
)
from lithos_lens.lithos_client import LithosClientProtocol
from lithos_lens.pr_reconciliation import PR_GATE_TYPE, PR_URL_KEY
from lithos_lens.task_graph import BlockedTaskRecord
from lithos_lens.tasks import TaskRecord, parse_timestamp

__all__ = ["GateOverride", "load_gate_override"]

#: How much of a PR url that is not a link the page shows as text. Peer-written
#: metadata, echoed into a page, so bounded like the Gates row's chips.
_PR_TEXT_CAP = 200

#: The schemes a PR url may carry to be rendered as a link. Anything else (a
#: ``javascript:`` url above all) is shown as text and never made clickable.
_LINK_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class GateOverride:
    """One open machine-owned gate, as its Proceed anyway page states it.

    ``gate`` is the Gates section's own row for the task — its ``ready_at``
    normalised and its waiters attached by the same read the board uses, with
    that read's ``waiters_state`` — so the page and the row cannot describe
    the gate differently. ``lapsed`` is a timer whose ``ready_at`` parsed and
    has passed: it no longer blocks anything, and completing it only closes
    it. ``pr_url`` is a PR gate's ``metadata.pr_url`` when it is an http(s)
    link; ``pr_text`` is the bounded raw value when it is present but not one.
    """

    gate: GateRow
    lapsed: bool = False
    pr_url: str = ""
    pr_text: str = ""

    @property
    def task(self) -> TaskRecord:
        return self.gate.task

    @property
    def is_pr(self) -> bool:
        return self.gate.gate_type == PR_GATE_TYPE


async def load_gate_override(
    client: LithosClientProtocol,
    task: TaskRecord,
    *,
    frontier_limit: int,
    now: datetime,
) -> GateOverride:
    """Read what the confirm page states about ``task``, an open gate.

    Two reads, both the dashboard's: the whole open list (the index waiter ids
    resolve against — never narrowed, so the count is the row's) and the
    blocked frontier at the board's limit. A truncated or failed blocked read
    takes the row's degraded path, the gate's own ``waits_on_gate`` edges,
    labelled unverified; a failed open list leaves nothing to resolve waiters
    against, so the count is unavailable rather than a confident zero.
    """
    (gate,) = collect_gates((task,), now=now)
    open_read, blocked_read = await asyncio.gather(
        client.list_tasks(status="open"),
        client.task_blocked(limit=frontier_limit),
        return_exceptions=True,
    )
    if isinstance(open_read, BaseException):
        gate = replace(gate, waiters=(), waiters_state=GateWaiterState.UNKNOWN)
    else:
        blocked_ok = not isinstance(blocked_read, BaseException)
        blocked = cast(list[BlockedTaskRecord], blocked_read) if blocked_ok else []
        (gate,) = await attach_gate_waiters(
            client,
            (gate,),
            index={row.id: row for row in open_read},
            blocked=blocked,
            blocked_available=blocked_ok,
            blocked_truncated=len(blocked) >= frontier_limit,
        )
    ready = parse_timestamp(gate.ready_instant)
    pr_url, pr_text = _pr_link(task) if gate.gate_type == PR_GATE_TYPE else ("", "")
    return GateOverride(
        gate=gate,
        lapsed=gate.is_timer and ready is not None and ready <= now,
        pr_url=pr_url,
        pr_text=pr_text,
    )


def _pr_link(task: TaskRecord) -> tuple[str, str]:
    """``(link, text)`` for a PR gate's ``metadata.pr_url``.

    A link only for an absolute http(s) url; any other non-empty value is
    shown as bounded text, so the operator still sees what the gate names
    without Lens making peer-written text clickable.
    """
    value = task.metadata.get(PR_URL_KEY)
    if not isinstance(value, str) or not value.strip():
        return "", ""
    try:
        parts = urlsplit(value)
    except ValueError:
        parts = None
    if parts is not None and parts.scheme in _LINK_SCHEMES and parts.netloc:
        return value, ""
    text = value.strip()
    if len(text) > _PR_TEXT_CAP:
        text = text[: _PR_TEXT_CAP - 1] + "…"
    return "", text
