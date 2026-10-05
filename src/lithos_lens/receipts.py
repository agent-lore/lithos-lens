"""Write receipts: what a write returned, shown once on the page it lands on.

A redirect loses the response body, and the body is where a write's news is —
``unblocked[]`` on a completion, ``reblocked[]`` on a reopen. So the funnel
mints a :class:`WriteReceipt`, files it here under a random id, and the 303
carries ``?receipt=<id>``; the target page's chrome takes it and renders it as
a banner above its content (T3 D5, REQUIREMENTS §5C.3).

**Feedback, not state.** A receipt makes no claim the page below it does not
re-derive from fresh reads, and a restart loses them all without making
anything wrong. That is the difference from the Lens-side trackers the ROADMAP
rejects (a claim ledger, a lifecycle tracker): nothing here is believed after
it went stale, because nothing here is read twice —

- **shown once**: :meth:`ReceiptStore.take` consumes a receipt as it renders,
  so a reload, a bookmark or a shared link of the redirect target shows the
  page and no banner;
- **bounded**: :data:`MAX_RECEIPTS` and :data:`RECEIPT_TTL_S` are the backstop
  for receipts that never render (a closed tab, a redirect never followed).
  Module constants, not config — the ``[writes]`` section is exactly
  ``default_operator`` and ``confirm_cancel``.

An unknown or expired id is not an error: the page renders without a banner.
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "CancelFacts",
    "CreateFacts",
    "EdgeFacts",
    "MAX_RECEIPTS",
    "MAX_TITLED_RELEASES",
    "RECEIPT_KEY",
    "RECEIPT_TTL_S",
    "ReceiptStore",
    "ReceiptTask",
    "WriteReceipt",
    "receipt_url",
]

#: The query key a redirect carries the receipt id under. Any read page
#: accepts it: the chrome takes it, whichever page that is.
RECEIPT_KEY = "receipt"

#: How many receipts the store holds at once. A receipt lives from its write to
#: the redirect that follows it — under a second for a browser that follows the
#: 303 — so this only has to cover writes whose redirect was never followed.
#: Past it the OLDEST goes first, which is the one least likely to be followed.
MAX_RECEIPTS = 64

#: How long an unrendered receipt is kept. Long enough for a slow redirect over
#: a congested network, short enough that a stale receipt cannot be shown as
#: news about a board that has moved on.
RECEIPT_TTL_S = 300.0

#: How many released tasks a receipt names by title. The funnel reads each one
#: (``lithos_task_get``) when it mints the receipt, so this is also the bound
#: on those reads; the rest are counted as "and N more".
MAX_TITLED_RELEASES = 5

#: Length of the random part of a receipt id, in bytes before encoding. The id
#: is not a credential — Lens has no authentication — it only has to be
#: unguessable enough that one tab cannot stumble onto another's receipt.
_ID_BYTES = 16


@dataclass(frozen=True)
class ReceiptTask:
    """A task a receipt names: its id, and its title when one could be read.

    An empty title means the read for it failed or was over the bound; the
    banner then shows the short id alone rather than inventing a name.
    """

    task_id: str
    title: str = ""


@dataclass(frozen=True)
class CancelFacts:
    """What a cancel strands, releases and keeps, in plain receipt terms (T3-W6).

    Filled in by the cancel route module from its consequence read, because
    this module may not import the TaskGraph walk that computes it; the same
    record feeds the confirm page and the receipt, through one partial, so the
    two cannot state the facts differently.

    ``stated`` is false on the receipt of a cancel the operator confirmed on
    the confirm page: that page already stated the facts (REQUIREMENTS: the
    receipt carries them "instead", only when there was no confirm page).
    ``reason_given`` says whether the cancel sent a reason — the receipt
    repeats that it is recorded in the event stream only — never the reason.

    ``stranded`` / ``behind`` are the first few by title, with their totals.
    ``exact`` is false when the totals are lower bounds ("≥ N"), and
    ``bound_reasons`` says why; ``unavailable`` is set when the consequence
    could not be computed at all. The ``*_unread`` flags mark a claims or
    children read that failed — never stated as "none". ``claims`` are
    ``(agent, aspects)`` pairs.
    """

    stated: bool = True
    reason_given: bool = False
    stranded: tuple[ReceiptTask, ...] = ()
    stranded_total: int = 0
    behind: tuple[ReceiptTask, ...] = ()
    behind_total: int = 0
    exact: bool = True
    bound_reasons: tuple[str, ...] = ()
    unavailable: str = ""
    claims: tuple[tuple[str, tuple[str, ...]], ...] = ()
    claims_unread: bool = False
    children: tuple[ReceiptTask, ...] = ()
    children_total: int = 0
    children_unread: bool = False
    gate: bool = False

    @property
    def claims_total(self) -> int:
        return sum(len(aspects) for _, aspects in self.claims)

    @property
    def stranded_more(self) -> int:
        return max(self.stranded_total - len(self.stranded), 0)

    @property
    def behind_more(self) -> int:
        return max(self.behind_total - len(self.behind), 0)

    @property
    def children_more(self) -> int:
        return max(self.children_total - len(self.children), 0)


@dataclass(frozen=True)
class CreateFacts:
    """What a create made, in receipt terms (T3-W7).

    ``task_type`` and ``project`` are as the form asked for them. ``repeated``
    is true when this submit made no call of its own — it carried a request id
    whose create had already landed (or was landing), so Lens put the operator
    on that task rather than create a second one (T3 D10).
    """

    task_type: str = "task"
    project: str = ""
    repeated: bool = False


@dataclass(frozen=True)
class EdgeFacts:
    """What an add-dependency did, in receipt terms (T3-W8, D11).

    ``source`` and ``target`` are the edge's ``from`` and ``to`` tasks, titled
    as Lithos (or a read) named them; ``edge_type`` is the edge's type, which
    the banner words. ``already_exists`` is true when Lens's fresh read found
    the relation before the call and so wrote nothing (D5); ``created_by`` and
    ``created_at`` are then the existing edge's stamps — empty on an edge
    Lithos recorded without them, when the banner drops them.
    """

    source: ReceiptTask
    target: ReceiptTask
    edge_type: str
    already_exists: bool = False
    created_by: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class WriteReceipt:
    """One write's outcome, as the banner states it.

    ``action`` is the write's name as the span spells it (``complete`` …).
    ``task`` is the task the write acted on, as Lithos named it in its answer.
    ``outcome`` is what the write recorded on the task (a completion's note,
    or the default that names the operator). ``released`` are the tasks the
    write freed — titled up to :data:`MAX_TITLED_RELEASES` — and
    ``released_total`` is how many Lithos reported, so the banner can say
    "and N more" without the store holding ids it never shows.

    A reopen (T3-W5) fills the same fields: ``released`` are the dependents it
    names — the ``reblocked`` ids for a completed task, the open dependents now
    waiting on it again for a cancelled one — and ``prior_status`` says which
    case the banner words. ``checked_status`` is the status the pre-check read;
    it differs from ``prior_status`` only when Lithos's answer proved that read
    stale (a non-empty ``reblocked`` for a task read as cancelled), and the
    banner then says so. ``released_exact`` is false when that count is a
    lower bound (a truncated or partly unread dependents page), and
    ``released_unread`` true when the dependents could not be read at all.
    ``released_waiting`` says which of the two a reopen's ``released`` are:
    true for the dependents read after the write (Lithos re-blocked no one),
    false for Lithos's own ``reblocked`` ids.
    ``back_to`` is the page the write returned to, already checked by
    ``safe_next``: a follow-up form on the receipt (Reopen gate) posts it as its
    ``next``, because an HTMX receipt is rendered against the POST, whose own
    path is no page to come back to.
    ``cancel`` is a cancel's :class:`CancelFacts` (T3-W6), ``created`` a
    create's :class:`CreateFacts` (T3-W7), and ``edge`` an add-dependency's
    :class:`EdgeFacts` (T3-W8); each is None for every other action.
    """

    action: str
    task: ReceiptTask
    operator: str
    outcome: str = ""
    released: tuple[ReceiptTask, ...] = ()
    released_total: int = 0
    prior_status: str = ""
    checked_status: str = ""
    released_exact: bool = True
    released_unread: bool = False
    released_waiting: bool = False
    back_to: str = ""
    cancel: CancelFacts | None = None
    created: CreateFacts | None = None
    edge: EdgeFacts | None = None

    @property
    def released_more(self) -> int:
        """How many released tasks the banner counts but does not name."""
        return max(self.released_total - len(self.released), 0)


class ReceiptStore:
    """The process's receipts, bounded in count and age (T3 D5).

    In memory and per process, like everything else Lens holds: the single
    operator's browser posts to this process and follows its redirect back to
    it. ``clock`` is injectable so the TTL is tested without sleeping; it is a
    monotonic clock by default, because a receipt's age must not jump with the
    wall clock.
    """

    def __init__(
        self,
        *,
        max_receipts: int = MAX_RECEIPTS,
        ttl_s: float = RECEIPT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_receipts
        self._ttl_s = ttl_s
        self._clock = clock
        # Insertion order IS age order, so the oldest is always first — both
        # for expiry and for eviction past the count bound.
        self._receipts: OrderedDict[str, tuple[float, WriteReceipt]] = OrderedDict()

    def put(self, receipt: WriteReceipt) -> str:
        """File ``receipt`` and return the id its redirect carries."""
        self._expire()
        receipt_id = secrets.token_urlsafe(_ID_BYTES)
        self._receipts[receipt_id] = (self._clock(), receipt)
        while len(self._receipts) > self._max:
            self._receipts.popitem(last=False)
        return receipt_id

    def take(self, receipt_id: str | None) -> WriteReceipt | None:
        """The receipt filed under ``receipt_id``, removed — or None.

        Consumed on the first read, so a receipt is shown once (§5C.3). An
        unknown, expired or empty id answers None, which renders nothing.
        """
        self._expire()
        if not receipt_id:
            return None
        entry = self._receipts.pop(receipt_id, None)
        return entry[1] if entry is not None else None

    def __len__(self) -> int:
        return len(self._receipts)

    def _expire(self) -> None:
        cutoff = self._clock() - self._ttl_s
        while self._receipts:
            filed_at, _ = next(iter(self._receipts.values()))
            if filed_at > cutoff:
                return
            self._receipts.popitem(last=False)


def receipt_url(next_url: str, receipt_id: str) -> str:
    """``next_url`` with ``?receipt=<receipt_id>`` merged into its query.

    Parsed and rebuilt rather than concatenated: the destination may already
    carry a query (the board's filters) or a fragment (a section anchor), and
    may carry a ``receipt=`` from an earlier write — which is REPLACED, so a
    page never names two receipts and never re-shows a consumed one.
    """
    parts = urlsplit(next_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != RECEIPT_KEY
    ]
    query.append((RECEIPT_KEY, receipt_id))
    return urlunsplit(parts._replace(query=urlencode(query)))
