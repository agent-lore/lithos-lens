"""One create per request id: Lens's in-process de-duplication (T3 D10, D5).

Lithos gives a create no help here: ``lithos_task_create`` mints a new id and
inserts, with nothing unique about any metadata key. So the create form carries
a random request id, and this coordinator — one per process, held by the
create routes — remembers what each id came to:

- **no entry**: this submit makes the call. It runs as its own task, shielded,
  so a waiter whose browser goes away cannot cancel the create the others are
  waiting on (the ``graph_cache`` single-flight pattern);
- **in flight**: the submit waits for that call and receives ITS outcome, as a
  value — it never makes a call of its own;
- **created**: the submit lands on that task, with no call;
- **outcome unknown**: never called again under that id. After a timeout the
  original call may still be landing upstream, so nothing Lens could do would
  prove it did not; the page says "not visible yet" and Start again issues a
  NEW id, the operator's explicit decision to risk a duplicate;
- **refused**: forgotten. Nothing was created, so the re-rendered form keeps
  the same id and a corrected submit may create.

The guarantee, as it is: a double-click, a resubmit or the back button lands on
the one task the first submit created. A create whose outcome Lens never
learned can still be duplicated if the operator starts again while it lands,
and a Lens restart between a submit and its resubmit forgets the id — the map
is memory, bounded by count (:data:`MAX_REMEMBERED_REQUESTS`), oldest settled
entry first, with no TTL.

The coordinator knows nothing about Lithos: the caller's ``create`` places the
call and classifies how it ended, because what counts as "no answer" is the
transport's to say and Writes may not import it.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "MAX_REMEMBERED_REQUESTS",
    "CreateCoordinator",
    "CreateOutcome",
    "Created",
    "Dedup",
    "OutcomeUnknown",
    "Refused",
    "Settled",
]

#: How many request ids the process remembers once settled. A request id lives
#: from a form's render to its last resubmit — a back button an hour later at
#: the outside — and each entry is a few short strings.
MAX_REMEMBERED_REQUESTS = 512


@dataclass(frozen=True)
class Created:
    """The create applied: the task it minted, and Lithos's answer whole."""

    task_id: str
    title: str
    answer: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutcomeUnknown:
    """Lithos never answered: the task may exist, or may yet."""


@dataclass(frozen=True)
class Refused:
    """Lithos refused the create with this error envelope. Nothing was made."""

    envelope: Mapping[str, Any]


CreateOutcome = Created | OutcomeUnknown | Refused

#: How a submit reached its outcome: ``""`` for the one that made the call,
#: ``joined`` for one that waited on it in flight, ``remembered`` for one that
#: arrived after it settled (D2 — a span and audit attribute, never a label).
Dedup = Literal["", "joined", "remembered"]


@dataclass(frozen=True)
class Settled:
    """One submit's answer: the create's outcome and how this submit got it."""

    outcome: CreateOutcome
    dedup: Dedup = ""


class CreateCoordinator:
    """The process's request ids and what each came to (D5)."""

    def __init__(self, *, max_remembered: int = MAX_REMEMBERED_REQUESTS) -> None:
        self._max = max_remembered
        # In flight: the task making the call. Settled: its outcome. Insertion
        # order is age order, so the oldest settled entry is found first.
        self._entries: OrderedDict[str, asyncio.Task[CreateOutcome] | CreateOutcome] = (
            OrderedDict()
        )

    async def submit(
        self, request_id: str, create: Callable[[], Awaitable[CreateOutcome]]
    ) -> Settled:
        """The outcome for ``request_id``, calling ``create`` at most once."""
        entry = self._entries.get(request_id)
        if isinstance(entry, asyncio.Task):
            return Settled(await asyncio.shield(entry), "joined")
        if entry is not None:
            return Settled(entry, "remembered")
        flight = asyncio.create_task(
            self._lead(request_id, create), name=f"lens-create-{request_id}"
        )
        self._entries[request_id] = flight
        return Settled(await asyncio.shield(flight))

    def remembered(self, request_id: str) -> CreateOutcome | None:
        """The settled outcome for ``request_id``, or None."""
        entry = self._entries.get(request_id)
        return None if entry is None or isinstance(entry, asyncio.Task) else entry

    def __len__(self) -> int:
        return len(self._entries)

    async def _lead(
        self, request_id: str, create: Callable[[], Awaitable[CreateOutcome]]
    ) -> CreateOutcome:
        try:
            outcome = await create()
        except BaseException:
            # `create` classifies every way the call can end, so reaching here
            # is a Lens defect or a shutdown — after a call that may have
            # applied. Remembered as unknown: never retried under this id.
            self._settle(request_id, OutcomeUnknown())
            raise
        if isinstance(outcome, Refused):
            del self._entries[request_id]
        else:
            self._settle(request_id, outcome)
        return outcome

    def _settle(self, request_id: str, outcome: CreateOutcome) -> None:
        self._entries.pop(request_id, None)
        self._entries[request_id] = outcome
        settled = [
            key
            for key, entry in self._entries.items()
            if not isinstance(entry, asyncio.Task)
        ]
        for key in settled[: max(len(settled) - self._max, 0)]:
            del self._entries[key]
