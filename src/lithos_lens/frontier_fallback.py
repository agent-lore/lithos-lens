"""The dashboard's no-frontier fallback: the flat render and its verdict.

Split out of ``frontier.py`` when the T1 slices pushed it past the 800-line
god-module ceiling. The seam is T1-S12's own subject: ``frontier.py`` owns the
happy path — the join over a ``frontier_reads.ReadGeneration`` — while what to
render when *there is no usable frontier right now* lives here.

One condition produces the flat render: a frontier read that did not answer.
That covers an outage and, since the pre-0.4 detection was withdrawn (2026-08-24,
REQUIREMENTS §4.1), a server that never had those tools — Lens reports the
failed read rather than diagnosing a version it has no reason to guess at.
"""

from __future__ import annotations

from collections.abc import Sequence

from lithos_lens.tasks import SectionName, SectionRow, TaskRecord


def flat_open_sections(
    open_tasks: Sequence[TaskRecord],
) -> dict[SectionName, tuple[SectionRow, ...]]:
    """Partition open tasks with no usable frontier: one flat ``open`` section.

    Without the frontier there is no honest way to say ready vs blocked, so
    Lens says neither — every open row lands in one list with its claim chips
    (the 0.1.0 dashboard's behavior). No task-type filter either: the epic and
    gate treatments are graph features, and this is the board without one. The
    claims-unknown contract is unchanged: a row whose claims came back ``None``
    must still say "claims unknown" rather than a confident "unclaimed".
    """
    return {
        "open": tuple(
            SectionRow(
                task=task,
                claims=task.claims or (),
                claims_unknown=task.claims is None,
            )
            for task in open_tasks
        ),
        "in_progress": (),
        "ready": (),
        "blocked": (),
        "claims_unknown": (),
        "unclassified": (),
    }
