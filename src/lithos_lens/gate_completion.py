"""Complete a gate: which gates Lens offers it on, and what it records (T3 D7).

The rules the Complete action is decided by, as plain values, so the three
surfaces that offer it (the gate row — in the Gates section and wherever Needs
attention promotes it — the side panel and the detail page) and the route that
performs it cannot disagree:

- :func:`completes_directly` — the ONE helper over (task type, status, gate
  type) that says whether a task carries the direct **Complete** action. The
  templates ask it through one shared partial; the route asks it again at the
  pre-check read, which is the answer that binds.
- :func:`proceeds_anyway` — its counterpart: an open gate that is completed
  only through the **Proceed anyway** confirm page (T3-W4b).
- :func:`refusal_for` — why the route refuses a task that read back as open
  but is not a gate, as a code and the operator's sentence.
- :func:`default_outcome` — what a completion records when the operator left
  no note.

**Gates only, split by who resolves them.** There is no complete action for an
ordinary task (agents finish their own work). Of the gates, ``human`` and
``external_task`` are the ones only a person can close, so they get the direct
action; every other type — ``timer``, ``ci``, ``pr``, anything Lens does not
know — is resolved by whatever watches it. Completing one of those by hand is
an override of a machine wait: allowed (completing is the only way to release
its waiters; cancelling strands them), but only behind the confirm page, whose
form alone carries :data:`PROCEED_ANYWAY_CONFIRMATION`. The record says it was
an override (:func:`default_outcome`), never that the wait ended on its own.

**The word is "Complete", never "Approve".** Completing a gate means what its
author says it means — for a loom needs-human gate it is the retry gesture —
so the action shows the gate's description beside it and puts no verdict in
the operator's mouth. The route keeps §5C.7's ``/approve`` path.
"""

from __future__ import annotations

__all__ = [
    "DIRECT_COMPLETE_GATE_TYPES",
    "MAX_NOTE_LENGTH",
    "NOT_A_GATE",
    "PROCEED_ANYWAY_CONFIRMATION",
    "completes_directly",
    "default_outcome",
    "is_override",
    "proceeds_anyway",
    "refusal_for",
]

#: The gate types a person resolves, which carry the one-step Complete action.
DIRECT_COMPLETE_GATE_TYPES: frozenset[str] = frozenset({"human", "external_task"})

#: The task type a completion requires. Spelled here rather than imported from
#: the graph layer: this module is the write surface's, and the value is
#: Lithos's own vocabulary, not a Lens decision.
_GATE_TASK_TYPE = "gate"

#: Longest note accepted as a completion's outcome. A ONE-LINE note (D7): it
#: is stored on the task and shown on every surface that shows an outcome, so
#: it is bounded like any other value that arrives in a request and is echoed
#: back into pages.
MAX_NOTE_LENGTH = 500

#: The refusal code the route answers for an open task that is not a gate. On
#: the span and in the audit line; never a metric label.
NOT_A_GATE = "not_a_gate"

#: The value the confirm page's form — and only that form — posts as
#: ``confirm``. A plain value, not a token: it guards against a mis-click on a
#: surface that never showed the operator what they were overriding, not
#: against a client that means to forge it (out of the operational model).
PROCEED_ANYWAY_CONFIRMATION = "proceed-anyway"

#: How much of a peer-written gate type the default outcome repeats. The value
#: is stored on the task in Lithos, so it is bounded like any other text Lens
#: writes on the operator's behalf.
_GATE_TYPE_CAP = 80


def completes_directly(task_type: str, status: str, gate_type: str) -> bool:
    """Whether a task carries the direct Complete action, right now.

    An OPEN gate of a person-resolved type. Status is part of the question: a
    completed gate has nothing to complete, and offering the action on it would
    only produce a conflict page.
    """
    return (
        task_type == _GATE_TASK_TYPE
        and status == "open"
        and gate_type in DIRECT_COMPLETE_GATE_TYPES
    )


def proceeds_anyway(task_type: str, status: str, gate_type: str) -> bool:
    """Whether a task is completed only through the Proceed anyway page, now.

    An OPEN gate of any type a person does not resolve: ``timer``, ``ci``,
    ``pr`` — and any type Lens does not know, which takes the cautious path
    rather than the one-click one.
    """
    return status == "open" and is_override(task_type, gate_type)


def is_override(task_type: str, gate_type: str) -> bool:
    """Whether completing this task overrides a wait a person does not own.

    A gate of any type but the person-resolved ones. Not a gate at all is no
    override: there is nothing to complete (:func:`refusal_for`).
    """
    return task_type == _GATE_TASK_TYPE and gate_type not in DIRECT_COMPLETE_GATE_TYPES


def refusal_for(task_type: str) -> tuple[str, str] | None:
    """``(code, sentence)`` for an open task Lens will not complete, or None.

    Only called once the pre-check read has shown the task open, so status is
    not this function's question. Every gate type is completable — a
    machine-owned one behind its confirmation — so the one refusal left is a
    task that is not a gate.
    """
    if task_type != _GATE_TASK_TYPE:
        return (
            NOT_A_GATE,
            "This task isn't a gate — only gates can be completed here.",
        )
    return None


def default_outcome(operator: str, gate_type: str) -> str:
    """The outcome a completion records when the operator wrote no note.

    It names the operator, so the record says who resolved the gate and how —
    never that the wait ended on its own. An override says so, and names the
    type of the wait that had not resolved.
    """
    if gate_type in DIRECT_COMPLETE_GATE_TYPES:
        return f"Completed via Lens by {operator}"
    named = gate_type[:_GATE_TYPE_CAP] or "untyped"
    return (
        f"Completed early via Lens by {operator} — proceed anyway; "
        f"{named} gate had not resolved"
    )
