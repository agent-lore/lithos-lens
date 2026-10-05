"""Relation sentences: how the operator says which edge to add (T3-W8, D11).

Direction is the mistake the add-dependency form exists to prevent. An edge is
``from → to`` with a type, and "``A`` blocks ``B``" is easy to draw backwards,
so the operator never picks a ``from`` or a ``to``: they pick a SENTENCE about
the task whose page they are on, and fill in the other task. This module is
that mapping, both ways, and nothing else — no Lithos call, no state:

- :data:`SENTENCES` — the three sentences T3 offers (scope cut S4), each one
  edge: "this task is blocked by ▁" and "this task blocks ▁" (``blocks``),
  and, on a gate's page only, "▁ waits on this gate" (``waits_on_gate``). No
  ``parent_child`` sentence (a parent is set when a task is created) and no
  ``discovered_from`` (provenance agents record).
- :meth:`RelationSentence.relation` — sentence + the two ids → the
  :class:`Relation` (``from`` / ``to`` / ``type``) to write;
  :func:`sentence_of` — the reverse, which is how a POST is held to a
  relation its page could have offered.
- :func:`existing_edge` — the relation's edge in a task's edge list, if it is
  already there (the upsert would otherwise replace its metadata).
- :func:`readiness` — what the relation will mean for readiness, by the
  blocker's status (F8): an open blocker holds its dependent, a completed one
  is already satisfied, a cancelled one strands it (``blocker_unsatisfiable``),
  and a waiter waits until its gate resolves — completed, or a timer gate past
  its ``ready_at``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import GATE_TASK_TYPE, TIMER_GATE_TYPE, gate_type_of
from lithos_lens.tasks import TaskRecord, parse_timestamp

__all__ = [
    "BLOCKED_BY",
    "BLOCKS",
    "SENTENCES",
    "WAITED_ON_BY",
    "Relation",
    "RelationSentence",
    "existing_edge",
    "readiness",
    "sentence_named",
    "sentence_of",
    "sentences_for",
]

BLOCKED_BY = "blocked_by"
BLOCKS = "blocks"
WAITED_ON_BY = "waited_on_by"


@dataclass(frozen=True)
class Relation:
    """One edge to write: ``from`` → ``to``, of ``type``."""

    from_task_id: str
    to_task_id: str
    type: str

    def other(self, task_id: str) -> str:
        """The endpoint that is not ``task_id``."""
        return self.to_task_id if self.from_task_id == task_id else self.from_task_id

    def is_edge(self, edge: EdgeRecord) -> bool:
        return (edge.from_task_id, edge.to_task_id, edge.type) == (
            self.from_task_id,
            self.to_task_id,
            self.type,
        )


@dataclass(frozen=True)
class RelationSentence:
    """One sentence about the focal task, and the edge it means.

    The sentence reads ``<first> <verb> <second>``: the focal task is the
    first term when ``focal_first`` and the second otherwise. ``focal_is_from``
    says which END of the edge the focal task is — the part an operator gets
    wrong, and the reason the sentence exists.
    """

    key: str
    verb: str
    edge_type: str
    focal_first: bool
    focal_is_from: bool
    gate_only: bool = False

    @property
    def label(self) -> str:
        """The sentence with its blank, as the form's select offers it."""
        if self.focal_first:
            return f"This task {self.verb} ▁"
        return f"▁ {self.verb} this {'gate' if self.gate_only else 'task'}"

    def relation(self, focal_id: str, other_id: str) -> Relation:
        if self.focal_is_from:
            return Relation(focal_id, other_id, self.edge_type)
        return Relation(other_id, focal_id, self.edge_type)

    def terms(
        self, focal: TaskRecord, other: TaskRecord
    ) -> tuple[TaskRecord, TaskRecord]:
        """The sentence's two terms in reading order, for restating it."""
        return (focal, other) if self.focal_first else (other, focal)


#: The sentences, in the order the form lists them.
SENTENCES: tuple[RelationSentence, ...] = (
    RelationSentence(
        key=BLOCKED_BY,
        verb="is blocked by",
        edge_type="blocks",
        focal_first=True,
        focal_is_from=False,
    ),
    RelationSentence(
        key=BLOCKS,
        verb="blocks",
        edge_type="blocks",
        focal_first=True,
        focal_is_from=True,
    ),
    RelationSentence(
        key=WAITED_ON_BY,
        verb="waits on",
        edge_type="waits_on_gate",
        focal_first=False,
        focal_is_from=True,
        gate_only=True,
    ),
)


def sentences_for(task: TaskRecord) -> tuple[RelationSentence, ...]:
    """The sentences a task's page offers: the gate one only on a gate.

    Tested on ``task_type`` itself — a gate with no ``metadata.gate_type`` is
    still a gate, and is still the only thing a waiter can wait on.
    """
    is_gate = task.task_type == GATE_TASK_TYPE
    return tuple(
        sentence for sentence in SENTENCES if is_gate or not sentence.gate_only
    )


def sentence_named(key: str, task: TaskRecord) -> RelationSentence | None:
    """The sentence ``key`` names, if ``task``'s page offers it."""
    return next((s for s in sentences_for(task) if s.key == key), None)


def sentence_of(relation: Relation, task: TaskRecord) -> RelationSentence | None:
    """The sentence on ``task``'s page that means ``relation``, or None.

    None for a relation that does not touch the task, a type no sentence
    writes, and the gate sentence on a task that is not a gate. ``blocks`` is
    offered from either end, so the end the task is on decides. A
    self-relation is left to Lithos, which refuses it (``self_edge``).
    """
    for sentence in sentences_for(task):
        if sentence.edge_type != relation.type:
            continue
        focal = relation.from_task_id if sentence.focal_is_from else relation.to_task_id
        if focal == task.id:
            return sentence
    return None


def existing_edge(edges: Sequence[EdgeRecord], relation: Relation) -> EdgeRecord | None:
    """The relation's edge among ``edges`` (one task's list), if present."""
    return next((edge for edge in edges if relation.is_edge(edge)), None)


def _name(task: TaskRecord) -> str:
    return f"“{task.title or task.id}”"


def _timer_elapsed(gate: TaskRecord, now: datetime) -> bool:
    if gate_type_of(gate) != TIMER_GATE_TYPE:
        return False
    ready_at = parse_timestamp(str(gate.metadata.get("ready_at") or ""))
    return ready_at is not None and ready_at <= now


def readiness(
    relation: Relation,
    source: TaskRecord,
    target: TaskRecord,
    *,
    now: datetime | None = None,
) -> str:
    """What the relation will mean for readiness, as one or two sentences.

    ``source`` and ``target`` are the edge's ``from`` and ``to`` tasks as just
    read. The blocker's status decides the wording (F8); a dependent that is
    itself resolved waits on nothing now, and is told so.
    """
    now = now or datetime.now(UTC)
    blocker, dependent = _name(source), _name(target)
    if relation.type == "waits_on_gate":
        if source.status == "completed":
            meaning = (
                f"{blocker} is already completed, so {dependent} does not wait on it."
            )
        elif source.status == "cancelled":
            meaning = (
                f"{blocker} is cancelled, so {dependent} will be blocked permanently "
                "until the gate is reopened."
            )
        elif _timer_elapsed(source, now):
            meaning = (
                f"{blocker} is a timer gate already past its ready time, so "
                f"{dependent} does not wait on it."
            )
        elif gate_type_of(source) == TIMER_GATE_TYPE:
            meaning = (
                f"{dependent} will not be ready until {blocker} resolves — when it "
                "is completed or its ready time passes."
            )
        else:
            meaning = (
                f"{dependent} will not be ready until {blocker} resolves — when it "
                "is completed."
            )
    elif source.status == "completed":
        meaning = (
            f"{blocker} is already completed, so this adds no wait: {dependent}'s "
            "readiness does not change."
        )
    elif source.status == "cancelled":
        meaning = (
            f"{blocker} is cancelled, so {dependent} will be blocked permanently — "
            "its blocker can never be satisfied — until that task is reopened."
        )
    else:
        meaning = f"{dependent} will not be ready until {blocker} completes."
    if target.status in ("completed", "cancelled"):
        meaning += (
            f" {dependent} is already {target.status}, so it waits on nothing now; "
            "the relation applies if it is reopened."
        )
    return meaning
