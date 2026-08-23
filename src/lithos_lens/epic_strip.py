"""The epic rollup strip: one progress chip per open epic, and its scope.

Split out of ``frontier.py`` when T1-S5 landed on top of the S9/S10/S12 slices
and pushed it past the 800-line god-module ceiling. The seam is S5's own
subject: ``frontier.py`` owns the ready/blocked join over the open snapshot,
while everything about EPICS — the recursive children fan-out, the progress
chips it reduces to, and the ``?epic=`` descendant scope those chips drive —
lives here.

The fan-out is the one dashboard read whose call count follows the corpus (one
subtree read per open epic), which is why the batching and its resource bound
sit in this module rather than being spread through the assembly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import NamedTuple, Protocol, cast

from lithos_lens.tasks import EpicRollup, TaskRecord

# Only ``task``-typed descendants count as work (mirrors ``frontier``): nested
# epics and gates are structure, and counting a sub-epic double-counts its own
# children.
WORKABLE_TASK_TYPE = "task"


class EpicChildrenClient(Protocol):
    """The narrow client surface the strip needs.

    Declared here rather than imported from ``frontier`` so the dependency runs
    one way: ``frontier`` imports the strip, never the reverse. Its wider
    ``FrontierLithosClient`` satisfies this structurally.
    """

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]: ...

    async def task_get(self, task_id: str) -> TaskRecord: ...


# Epics roll up into the strip (one progress chip each) rather than appearing
# in any section; the strip is built from their recursive children.
EPIC_TASK_TYPE = "epic"

# Concurrency bound for the epic-strip fan-out. Story 8 wants a chip for EVERY
# open epic, so the read count follows the corpus and cannot be capped; what is
# bounded instead is how much of it is in flight. Each batch is gathered and
# immediately reduced to rollups, so at most this many reads contend for the
# shared MCP session and at most this many subtrees are resident at once — the
# resource dimensions a cap would have covered, without dropping an epic.
# An internal constant rather than public config, for the same reason as
# knowledge.RELATED_RENDER_CAP: a safety net, not a dial operators tune.
EPIC_FANOUT_BATCH = 8


def build_epic_rollup(
    epic: TaskRecord,
    children: Sequence[TaskRecord],
) -> EpicRollup:
    """Roll one epic's recursive children up into its progress chip.

    ``children`` is the ``lithos_task_children(recursive=True,
    include_closed=True)`` answer, so it is the whole subtree in every status.
    Progress counts only WORKABLE descendants and drops cancelled ones from the
    denominator — see :class:`~lithos_lens.tasks.EpicRollup` for why — while the
    scope set keeps EVERY descendant id, whatever its type or status. This pure
    function always reports the full set; ``_load_children_batch`` is where it
    is dropped for the epics whose set nothing will read.
    """
    workable = [task for task in children if task.task_type == WORKABLE_TASK_TYPE]
    done = sum(1 for task in workable if task.status == "completed")
    cancelled = sum(1 for task in workable if task.status == "cancelled")
    return EpicRollup(
        task=epic,
        done=done,
        total=len(workable) - cancelled,
        cancelled=cancelled,
        descendant_ids=frozenset(task.id for task in children if task.id),
    )


async def load_epic_rollups(
    lithos: EpicChildrenClient,
    snapshot: Sequence[TaskRecord],
    *,
    selected: str,
) -> EpicStrip:
    """Fan ``lithos_task_children`` out over EVERY open epic in the snapshot.

    One recursive, closed-inclusive call per epic — story 8 wants a chip for
    each open epic, so nothing is dropped. This is the one read that cannot
    join the main gather (the epic ids come from the open list it fetches) and
    the one whose call count follows the corpus rather than a server-side
    limit, so it runs one ``EPIC_FANOUT_BATCH``-sized batch at a time, each
    reduced to rollups by ``_load_children_batch`` before the next is issued.
    Per-subtree truncation is deliberately NOT applied: a clipped subtree would
    report a wrong ``5/8`` and silently shrink the scope.

    ``failed`` is set when ANY call failed; a failed epic is dropped rather
    than shown with a wrong count, and the caller turns the flag into the usual
    load-error banner. A selected epic that answers with an EMPTY subtree is
    ambiguous (childless vs. closed since the open read), so it is confirmed
    with ``_is_open_epic`` before its empty scope is honored.
    """
    epics = [task for task in snapshot if task.task_type == EPIC_TASK_TYPE]
    if not epics:
        return EpicStrip((), False)
    rollups: list[EpicRollup] = []
    failed = False
    for start in range(0, len(epics), EPIC_FANOUT_BATCH):
        batch_rollups, batch_failed = await _load_children_batch(
            lithos, epics[start : start + EPIC_FANOUT_BATCH], selected=selected
        )
        rollups.extend(batch_rollups)
        failed = failed or batch_failed
    return await _resolve_empty_selection(lithos, tuple(rollups), failed)


async def _load_children_batch(
    lithos: EpicChildrenClient,
    batch: Sequence[TaskRecord],
    *,
    selected: str,
) -> tuple[list[EpicRollup], bool]:
    """Read one batch of epics' subtrees concurrently and reduce them to chips.

    A separate frame on purpose: the bulky part — the raw ``task_children``
    responses — is local to it and released when it RETURNS, so the caller
    never holds a finished batch's subtrees (nor the loop variables pointing
    into them) while the next batch is in flight. That, with the batch size, is
    what bounds how many subtrees are resident at once.

    What survives the frame is one small ``EpicRollup`` per epic. Even there,
    ``descendant_ids`` is kept ONLY for the selected epic: those reads are
    ``include_closed=True``, so retaining every epic's set would hold an id for
    every task ever closed under every epic, for the whole render — and nothing
    reads the set of an epic that is not the ``?epic=`` scope.
    """
    results = await asyncio.gather(
        *(
            lithos.task_children(epic.id, recursive=True, include_closed=True)
            for epic in batch
        ),
        return_exceptions=True,
    )
    rollups: list[EpicRollup] = []
    failed = False
    for epic, result in zip(batch, results, strict=True):
        if isinstance(result, BaseException):
            failed = True
            continue
        rollup = build_epic_rollup(epic, cast(list[TaskRecord], result))
        is_selected = bool(selected) and epic.id == selected
        rollups.append(
            replace(
                rollup,
                selected=is_selected,
                descendant_ids=rollup.descendant_ids if is_selected else frozenset(),
            )
        )
    return rollups, failed


async def _resolve_empty_selection(
    lithos: EpicChildrenClient,
    rollups: tuple[EpicRollup, ...],
    failed: bool,
) -> EpicStrip:
    """Decide what an EMPTY subtree under the selected epic means.

    ``lithos_task_children`` answers ``[]`` both for a childless open epic —
    which must scope to an empty board, the descendant set really is empty —
    and for an epic that closed between the open read and this one, whose chip
    and scope are stale. The two want opposite handling, so the tie is broken
    by re-reading the epic itself: confirmed still-open epic keeps its (empty)
    scope; anything else drops the stale chip so the board falls back unscoped
    with the "scope not applied" banner. Nothing else is touched — a selected
    epic WITH descendants needs no second read.
    """
    selected = next(
        (rollup for rollup in rollups if rollup.selected and not rollup.descendant_ids),
        None,
    )
    if selected is None or await _is_open_epic(lithos, selected.task.id):
        return EpicStrip(rollups, failed)
    return EpicStrip(
        tuple(rollup for rollup in rollups if rollup is not selected), failed
    )


async def _is_open_epic(lithos: EpicChildrenClient, task_id: str) -> bool:
    """Whether ``task_id`` is (still) an open epic, per one ``lithos_task_get``.

    False for every other answer — the coded not-found error a deleted task
    raises, a resolved status, an unexpected type, or a failed read. All of
    them mean Lens cannot vouch for the chip, and the caller's fallback (an
    unscoped board with the banner) is the same in each case, so they are not
    told apart here: ``frontier`` is Foundation and must not import the client
    to inspect its error codes.
    """
    try:
        task = await lithos.task_get(task_id)
    except Exception:
        return False
    return task.status == "open" and task.task_type == EPIC_TASK_TYPE


class EpicStrip(NamedTuple):
    """One epic-strip load: the chips plus the read-failure flag."""

    rollups: tuple[EpicRollup, ...]
    failed: bool


def epic_scope_ids(epics: Sequence[EpicRollup]) -> frozenset[str] | None:
    """The selected epic's descendant ids, or ``None`` when nothing is scoped.

    ``None`` (no scope) and an empty frozenset (a confirmed childless epic —
    a real scope that renders an empty board) are deliberately different
    answers; see ``_resolve_empty_selection`` and ``matches_filters``.
    """
    return next((epic.descendant_ids for epic in epics if epic.selected), None)
