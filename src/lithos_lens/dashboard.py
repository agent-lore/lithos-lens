"""The assembled dashboard view model: ``DashboardData`` and its counters.

Split out of ``tasks.py`` for the reason the other T1 seams were (the 800-line
god-module ceiling in ``docs/architecture.toml``), and for one more: the board
now carries the Gates section, whose row and group models live in ``gates.py``
— which itself reads ``TaskRecord`` out of ``tasks.py``. Holding the assembled
board here keeps every dependency pointing one way (``tasks`` -> ``gates`` ->
``dashboard``) instead of making the record module and the gate module import
each other.

``frontier.load_dashboard`` is the only producer; the ``tasks/dashboard.html``
template is the only consumer.
"""

from __future__ import annotations

from dataclasses import dataclass

from lithos_lens.gates import GateGroup, GateRow
from lithos_lens.tasks import (
    OPEN_SECTIONS,
    AgentRecord,
    EpicRollup,
    SectionName,
    SectionRow,
    TaskFilters,
)


@dataclass(frozen=True)
class TaskSummary:
    # Rows in the Needs-attention list. They are promoted out of their home
    # section, so this count never overlaps the section counts below.
    attention: int = 0
    in_progress: int = 0
    ready: int = 0
    blocked: int = 0
    # Open gates rendered in the Gates section. Never part of the workable
    # partition (Lithos excludes gates from both frontiers), so this count
    # overlaps none of the three above.
    gates: int = 0
    # Rows whose claims came back None (unknown): visible in their own group,
    # deliberately NOT counted as workable Ready/Blocked.
    claims_unknown: int = 0
    unclassified: int = 0
    open_total: int = 0
    # Claims held by the rows rendered In progress. Deliberately NOT the
    # Lithos-wide lithos_stats.open_claims: this sits under the In-progress
    # count on the situation card, which is filtered (and can be epic-scoped),
    # so a server-wide figure would contradict the number above it.
    active_claims: int = 0
    recent_completed: int = 0
    recent_cancelled: int = 0
    agents: int = 0


@dataclass(frozen=True)
class DashboardData:
    filters: TaskFilters
    summary: TaskSummary
    sections: dict[SectionName, tuple[SectionRow, ...]]
    agents: tuple[AgentRecord, ...]
    frontier_limit: int
    open_total: int
    # The project universe for the filter dropdown: the UNION of both
    # conventions' slugs over the loaded snapshot (§5B.1), so no project is
    # invisible to its own view.
    projects: tuple[str, ...] = ()
    # The Gates section (§5.2.3), human-first then oldest-first, one group per
    # gate type. Kept beside ``sections`` rather than inside it: a gate row
    # carries gate chrome (type badge, waiter count, countdown), not the
    # claim/blocker chrome of a task row.
    gate_groups: tuple[GateGroup, ...] = ()
    # The earliest still-future timer-gate ``ready_at`` on the page, or "".
    # Lithos emits no event when a timer lapses, so the board self-refreshes
    # once at this instant (PRD story 6).
    next_gate_ready_at: str = ""
    reconciliation_pending: bool = False
    truncated: bool = False
    # True when these filters hide part of the corpus from the sections, so
    # every per-view signal below (truncation, reconciliation, claims-unknown,
    # emptiness) describes the filtered subset rather than the whole system.
    filters_narrowed: bool = False
    # The subset of ``filters_narrowed`` that hides OPEN rows (tag/agent/
    # project/epic, never status). The Needs-attention stripe reads this one:
    # ``?status=open`` hides only terminal sections, so it cannot invalidate a
    # claim about the open board.
    open_side_narrowed: bool = False
    # True when the open rows render in the flat ``open`` section instead of
    # the workable three, because a frontier read did not answer (§14). Half a
    # frontier is not a classification.
    open_flat: bool = False
    # Open rows the graph deliberately rolls up rather than rendering: epics,
    # which roll up to their children. Gates are NOT among them since T1-S4 —
    # they render in ``gate_groups`` (or, when one has waited too long, in
    # Needs attention). Counted so the board can SAY so — an open row
    # that exists and renders nowhere must not read as an empty tracker, and
    # must not sit under an affirmative health claim. Always 0 in the flat
    # fallback, where every open row renders.
    rolled_up_open: int = 0
    # True when Lithos answered every read successfully and returned nothing
    # for this view: no open tasks, and nothing resolved inside the ``since``
    # window. Distinguishes "there is nothing here" from "your filters hid
    # everything", which the per-section empty lines already say. Deliberately
    # NOT a claim about the corpus — the terminal reads are windowed by
    # ``since``, so work resolved before it is invisible to this flag and the
    # panel it drives has to name the window.
    nothing_to_show: bool = False
    errors: tuple[str, ...] = ()
    # One rollup per open epic, in open-snapshot (newest-first) order.
    epics: tuple[EpicRollup, ...] = ()
    # The epic id the sections are actually scoped to — empty when no ``?epic=``
    # was asked for OR when the requested epic is no longer an open epic, which
    # the template explains instead of rendering a silently empty board.
    epic_scope: str = ""

    @property
    def gates(self) -> tuple[GateRow, ...]:
        """Every gate row on the page, in rendered order.

        Derived from :attr:`gate_groups` rather than stored beside it, so the
        count on the situation card and the rows under the heading can never
        describe different sets.
        """
        return tuple(row for group in self.gate_groups for row in group.rows)

    @property
    def scoped_epic(self) -> EpicRollup | None:
        """The epic chip the board is scoped to, if any (the template's handle
        on it — e.g. to explain a confirmed-childless epic's empty board)."""
        return next((epic for epic in self.epics if epic.selected), None)

    @property
    def rolled_up_only(self) -> bool:
        """True when the open side is empty ONLY because rows were rolled up.

        The degenerate case is a tracker holding nothing but epics: every open
        section renders empty, ``nothing_to_show`` is False (the open read did
        return rows), and without this the board would show an empty board
        under "All systems healthy". Drives the explanatory panel that names
        the rolled-up rows instead.
        """
        if not self.rolled_up_open:
            return False
        return not any(self.sections.get(section) for section in OPEN_SECTIONS)

    @property
    def healthy(self) -> bool:
        """True when this load carries no degraded signal to report.

        Drives the "All systems healthy" stripe: every read succeeded, the
        frontier was complete (no truncation) and self-consistent, claims came
        back for every row, and the graph tools are present. T1-S3 extends this
        with the needs-attention rules, whose emptiness is the other half of
        the same statement.

        Since T1-S3 this is also the Needs-attention stripe's gate, so it
        additionally requires an EMPTY attention list: rows the severity rules
        promoted are precisely "something to report". It reads
        ``open_side_narrowed`` rather than ``filters_narrowed`` because a
        status filter hides no open row.

        Withheld when the open side rendered nothing but rolled-up rows exist
        (see :attr:`rolled_up_only`): the stripe would be the only thing on an
        empty board, asserting health over work the operator cannot see.

        Withheld on a narrowed view. Truncation, reconciliation and
        claims-unknown are all measured over the rows the filters left, so on a
        filtered board they cannot support the stripe's system-wide claim — a
        ``?tag=`` in a shared link would otherwise turn a degraded system into
        an affirmative "all healthy". The warning banners are per-view
        statements and keep rendering under any filter.
        """
        return (
            not self.open_side_narrowed
            and not self.rolled_up_only
            and not self.sections.get("attention")
            and not self.open_flat
            and not self.errors
            and not self.truncated
            and not self.reconciliation_pending
            and not self.sections.get("claims_unknown")
        )
