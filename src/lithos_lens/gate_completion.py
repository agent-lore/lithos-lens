"""Complete a gate: which gates Lens offers it on, and what it records (T3 D7).

The rules the direct Complete action is decided by, as plain values, so the
three surfaces that offer it (the gate row — in the Gates section and wherever
Needs attention promotes it — the side panel and the detail page) and the route
that performs it cannot disagree:

- :func:`completes_directly` — the ONE helper over (task type, status, gate
  type) that says whether a task carries the direct **Complete** action. The
  templates ask it through one shared partial; the route asks it again at the
  pre-check read, which is the answer that binds.
- :func:`refusal_for` — why the route refuses a task that read back as open
  but is not one of those gates, as a code and the operator's sentence.
- :func:`default_outcome` — what a completion records when the operator left
  no note.

**Gates only, split by who resolves them.** There is no complete action for an
ordinary task (agents finish their own work). Of the gates, ``human`` and
``external_task`` are the ones only a person can close, so they get the direct
action; every other type — ``timer``, ``ci``, ``pr``, anything Lens does not
know — is resolved by whatever watches it, and is refused here until the
proceed-anyway confirm step (T3-W4b) gives the operator a deliberate way to
override it.

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
    "GATE_TYPE_UNSUPPORTED",
    "completes_directly",
    "default_outcome",
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

#: The two refusal codes the route answers for an open task it will not
#: complete. On the span and in the audit line; never a metric label.
NOT_A_GATE = "not_a_gate"
GATE_TYPE_UNSUPPORTED = "gate_type_unsupported"


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


def refusal_for(task_type: str, gate_type: str) -> tuple[str, str] | None:
    """``(code, sentence)`` for an open task Lens will not complete, or None.

    Only called once the pre-check read has shown the task open, so status is
    not this function's question.
    """
    if task_type != _GATE_TASK_TYPE:
        return (
            NOT_A_GATE,
            "This task isn't a gate — only gates can be completed here.",
        )
    if gate_type not in DIRECT_COMPLETE_GATE_TYPES:
        named = gate_type or "untyped"
        return (
            GATE_TYPE_UNSUPPORTED,
            f"A {named} gate is resolved by whatever watches it. "
            "Lens can't complete it yet.",
        )
    return None


def default_outcome(operator: str) -> str:
    """The outcome a completion records when the operator wrote no note.

    It names the operator, so the record says who resolved the gate and how —
    never that the wait ended on its own.
    """
    return f"Completed via Lens by {operator}"
