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
class WriteReceipt:
    """One write's outcome, as the banner states it.

    ``action`` is the write's name as the span spells it (``complete`` …).
    ``task`` is the task the write acted on, as Lithos named it in its answer.
    ``outcome`` is what the write recorded on the task (a completion's note,
    or the default that names the operator). ``released`` are the tasks the
    write freed — titled up to :data:`MAX_TITLED_RELEASES` — and
    ``released_total`` is how many Lithos reported, so the banner can say
    "and N more" without the store holding ids it never shows.
    """

    action: str
    task: ReceiptTask
    operator: str
    outcome: str = ""
    released: tuple[ReceiptTask, ...] = ()
    released_total: int = 0

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
