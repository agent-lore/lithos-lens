"""What a panel may say about a graph it did not assemble (D7/D8/D10, T2-A7).

The graph page draws one assembly and the panel beside it reads another: a
clicked panel rebuilds the scope after the page did, and the canvas
deliberately does not re-lay-out under the operator (D8). Everything here
exists to keep the two honest about each other:

- :func:`impact_fingerprint` is the identity of one drawn ANSWER, in two halves
  — the picture, and the material M is counted from — so a panel that finds one
  of them moved knows which of its claims survive (:func:`canvas_holds`).
- :func:`lower_bound_nodes` is D8's lower-bound fact, answered for every node
  at once because the payload carries it per node.
- :class:`CanvasNotes` is the client's own account of what it is drawing,
  stated back on the panel request — the only authority for a picture this
  process no longer holds.

The figures themselves are ``graph_impact``'s; nothing here counts anything.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from lithos_lens.graph_cycles import CycleSignal
from lithos_lens.graph_scope import EDGE_UNKNOWN, TaskGraphScope
from lithos_lens.task_filtering import task_projects
from lithos_lens.tasks import DEFAULT_PROJECT_TAG_KEY

#: Between a fingerprint's two halves — the canvas and the answer counted over
#: it (:func:`impact_fingerprint`). A character no hex digest can contain, so
#: the join is unambiguous however the two values move.
FINGERPRINT_SEPARATOR = "."


@dataclass(frozen=True)
class CanvasNotes:
    """What the CLIENT says it is drawing around the focus (D7/D8).

    The panel states two things about the picture beside it — "what the canvas
    lights is a lower bound" and "on the longest chain (k of n)" — and those
    are facts about the graph ON SCREEN, which is the static payload the page
    loaded with (D8 forbids an auto re-layout). The panel's own assembly is a
    later read: it can hold a node this one does not, a chain this one does not
    run, or nothing at all when the scope has since been refused or failed. So
    for a panel fetched by the client, the client states them — it is the only
    party that knows what is drawn — and they are rendered instead of anything
    this rebuild would say (round-4 correctness f-006).

    Both facts come from the SERVER either way: the lower bound rides in the
    payload per node (:func:`lower_bound_nodes`) and the position is read off
    the chain the payload ships (``graph_view.active_chain_payload``), so the
    browser restates D7 and D8 rather than reimplementing them.

    ``stated`` separates "the client described its canvas" from "it described a
    canvas with nothing to note": a page rendering its own ``focus=`` describes
    nothing, and its assembly IS the drawn graph, so it keeps its own answer.
    """

    stated: bool = False
    relations_exact: bool = True
    chain_position: int = 0
    chain_length: int = 0


#: The client's ``bound=`` values — a lit set that is a lower bound, or whole.
BOUND_LOWER = "lower"
BOUND_EXACT = "exact"
#: Between the two halves of ``chain=<position>:<length>``.
CHAIN_SEPARATOR = ":"


def parse_canvas_notes(bound: str | None, chain: str | None) -> CanvasNotes:
    """Read ``bound=lower|exact`` and ``chain=<k>:<n>`` off a panel request.

    ``bound`` is what makes the description a description: a request without it
    states nothing and the panel answers from its own assembly, which is what
    every host that is not the graph canvas does. A malformed ``chain`` is read
    as "not on the chain" rather than refused — the position is an annotation,
    and dropping the line beats printing a position out of a broken pair.
    """
    if bound not in (BOUND_LOWER, BOUND_EXACT):
        return CanvasNotes()
    position, _, length = (chain or "").partition(CHAIN_SEPARATOR)
    try:
        step, steps = int(position), int(length)
    except ValueError:
        step, steps = 0, 0
    if step < 1 or steps < step:
        step, steps = 0, 0
    return CanvasNotes(
        stated=True,
        relations_exact=bound == BOUND_EXACT,
        chain_position=step,
        chain_length=steps,
    )


def impact_fingerprint(
    scope: TaskGraphScope,
    signal: CycleSignal,
    *,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> str:
    """The identity of one assembled ANSWER — everything D10's figures rest on.

    TWO digests joined by :data:`FINGERPRINT_SEPARATOR`, because a panel that
    no longer reproduces the page's answer has two different things to say
    depending on WHICH half moved (round-3 correctness f-006):

    - the **canvas** half — the node set (id, the status the count reads, the
      completeness that turns a status into ``unknown`` and marks an unread
      edge list, the ghost kind that decides whether it is drawn at all) and
      the edge set (endpoints, type and the state the active projection is read
      from). This is the PICTURE: what is drawn, what N is walked over, which
      nodes light under a focus, where the longest chain runs — so while it
      holds, the panel's statements ABOUT that picture are still true of what
      the operator is looking at, whatever else moved (:func:`_stale_impact`).
    - the **answer** half — the project slugs coverage is matched by, kept
      apart by the convention that carries each; the coverage set; each read's
      outcome; the blocked rows for the nodes this graph holds, by blocker kind,
      predecessor, type and status; and the projectless set. This is M's
      material, read fresh on every panel with no cache under it.

    The answer half is what makes this a fingerprint of the ANSWER rather than
    of the picture. An eventless edge upsert elsewhere in the fleet (ROADMAP
    gap #1) can add a second blocker to a dependent while every edge entry this
    scope reads stays warm: the drawn graph is then byte-identical and M has
    still moved from 1 to 0 (round-1 correctness f-001). Nothing else
    downstream would notice, so it is caught here or not at all — and, the
    halves being compared separately, catching it costs the FIGURES and not the
    notes beside them.

    What is deliberately NOT in it: titles, claims, blocker MESSAGES, error
    reasons — text that moves no figure and no class on the canvas. A
    fingerprint that changed on every heartbeat would withhold the line
    permanently rather than when it is actually wrong. Rows for tasks outside
    this graph are left out for the same reason: a scoped read legitimately
    names them, and no figure here is counted over them.

    Order is imposed on every part, because none of it arrives ordered: the
    blocked rows are folded out of two concurrent reads per project and their
    order follows whichever answered first (``graph_cycles._signal``).

    Each half is 16 hex digits (:func:`_digest`, where the encoding is argued):
    the pair travels in a URL and is compared to a value Lens produced itself
    in the same process, so this is a change detector, not a defence against a
    forged one. The separator is safe where one between FIELDS would not be —
    a hex digest cannot contain it.
    """
    return FINGERPRINT_SEPARATOR.join(
        (
            _digest(_canvas_material(scope)),
            _digest(_answer_material(scope, signal, tag_key)),
        )
    )


def canvas_holds(drawn: str, given: str) -> bool:
    """Whether two fingerprints name the same PICTURE (D8's canvas).

    What a stale panel asks before deciding what to withhold: the figures
    belong to the whole answer, the notes beside them only to the drawing
    (round-3 correctness f-006). A ``given`` that is not a fingerprint Lens
    emitted — invented, or truncated by a hand-edited URL — answers False and
    so withholds everything, the default the whole comparison has.
    """
    left, right = _canvas_half(drawn), _canvas_half(given)
    return bool(left) and left == right


def _canvas_half(fingerprint: str) -> str:
    parts = fingerprint.split(FINGERPRINT_SEPARATOR)
    return parts[0] if len(parts) == 2 and all(parts) else ""


def _digest(material: list[object]) -> str:
    """One half's material, canonically encoded and hashed.

    Canonical JSON rather than fields joined on a separator: task ids are
    arbitrary non-empty strings (§5.1) that nothing normalises control
    characters out of, so a chosen separator is one an id may legitimately
    CONTAIN — a digest over joined fields then reads ``a -> b<sep>c`` and
    ``a<sep>b -> c`` as the same edge, letting a moved graph pass the check and
    print the wrong count (round-2 correctness f-003). JSON's own escaping is
    what makes the encoding injective: every value stays a distinct element.
    """
    encoded = json.dumps(material, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _canvas_material(scope: TaskGraphScope) -> list[object]:
    """The drawn picture: what N is walked over and what focus mode classes."""
    return [
        sorted(
            [node.id, node.status, node.completeness, node.ghost_kind]
            for node in scope.nodes
        ),
        sorted(
            [edge.from_task_id, edge.to_task_id, edge.type, edge.state]
            for edge in scope.edges
        ),
    ]


def _answer_material(
    scope: TaskGraphScope, signal: CycleSignal, tag_key: str
) -> list[object]:
    """M's material: whose blocked fact was read, how, and what it said."""
    held = set(scope.node_ids)
    return [
        sorted(
            [
                node.id,
                # Per CONVENTION, never unioned. Coverage belongs to the READ
                # (``graph_cycles.read_covers``): a complete ``project=<slug>``
                # response establishes the absence only of tasks carrying that
                # slug in ``metadata.project``, a ``tags=`` one only of tasks
                # carrying the tag. So the same slug rewritten from one
                # convention to the other — an edit that moves no id, no status
                # and no edge — can turn a covered dependent into an uncovered
                # one and M from a figure into a withheld line, and a digest
                # over the union would call the two graphs the same.
                sorted(
                    task_projects(node.task, convention="metadata", tag_key=tag_key)
                ),
                sorted(task_projects(node.task, convention="tag", tag_key=tag_key)),
            ]
            for node in scope.nodes
        ),
        sorted(signal.coverage),
        sorted(
            # The OUTCOME, not the reason: "this read cannot establish absence"
            # is the whole of what M asks of it (``graph_cycles.read_covers``).
            [read.project, read.by, read.truncated, bool(read.error), read.unmade]
            for read in signal.reads
        ),
        sorted(
            [
                record.task.id,
                sorted(
                    [blocker.kind, blocker.task_id, blocker.type, blocker.status]
                    for blocker in record.blockers
                ),
            ]
            for record in signal.blocked
            if record.task.id in held
        ),
        sorted(signal.projectless),
    ]


def lower_bound_nodes(scope: TaskGraphScope) -> frozenset[str]:
    """Every node whose focus view is a LOWER BOUND of what surrounds it (D8).

    Two ways a lit set is not the whole of a node's neighbourhood, and D8
    states both: the SCOPE is incomplete — an unreadable edge list anywhere is
    evidence that the projection Lens can see is not all of it, wherever the
    gap turns out to be — or an ``unknown`` edge touches that node's own
    neighbourhood, a relation Lens cannot classify in either direction. The
    first is a property of the scope, so it answers for every node at once; the
    second is walked.

    The walk is symmetric where :func:`_downstream` is not, because the canvas
    lights ancestors AND descendants (D8) — which makes the answer a property
    of the whole CONNECTED COMPONENT over active dependency edges, and so one
    pass answers for every node rather than one pass per node. The page needs
    it that way: the client states this fact back when it asks for the panel of
    a node it is drawing (:class:`CanvasNotes`), so every node carries it in
    the payload, not only the focused one (round-4 correctness f-006).
    """
    if scope.incomplete:
        return frozenset(scope.node_ids)
    neighbours: dict[str, list[str]] = {}
    unknown_at: set[str] = set()
    for edge in scope.edges:
        if not edge.dependency:
            continue
        if edge.active:
            neighbours.setdefault(edge.from_task_id, []).append(edge.to_task_id)
            neighbours.setdefault(edge.to_task_id, []).append(edge.from_task_id)
        elif edge.state == EDGE_UNKNOWN:
            unknown_at.update((edge.from_task_id, edge.to_task_id))
    bounded: set[str] = set()
    placed: set[str] = set()
    for node in scope.nodes:
        if node.id in placed:
            continue
        component = {node.id}
        queue = [node.id]
        while queue:
            for other in neighbours.get(queue.pop(0), ()):
                if other not in component:
                    component.add(other)
                    queue.append(other)
        placed |= component
        if component & unknown_at:
            bounded |= component
    return frozenset(bounded)
