"""Expanding a note in place (K2 D16): the pass after the capped base view.

``?focus=A&expand=B&expand=D`` draws every typed edge the filters show at an
**expanded** note, as at the focus, with their far endpoints. The pass runs
after the base typed graph is capped and the focus's one-hop layers are
loaded, against that combined visible set, and reads nothing: it is a lookup
over the snapshot's endpoint index (``edges_of``) under the filters (``shows``,
which keeps the ``edge=`` / ``pin=`` exemption).

- **Order.** Requests apply in URL order; duplicates and the focus are
  dropped. One whose note is not drawn when its turn comes is **unreached**
  and adds nothing.
- **Two counts.** The cap counts the focus and the distinct endpoints of drawn
  typed edges, as the base does; layer-only notes do not count. Visual
  additions count notes absent from the combined visible set. Promoting a
  visible layer-only note to a typed endpoint adds to the cap count and adds
  no visible note — it keeps its base place (hop 1, no ``via``).
- **Refused** on its own: a request that would take the cap count over the
  cap adds nothing, the rest is drawn, and later requests are still tried.
- **Eligibility** for every visible note — what expanding it would add to the
  final view, and whether that fits — is computed here, so the panel, the text
  and the payload state one answer, never ``nodes + undrawn``.
- **Collapse** removes a request and every request whose note it first drew
  (``via``), transitively, even when another branch still reaches that note.
  What the remaining requests draw is the same pass re-run without them, so
  the links that remove a request know which selections survive.

The view model carries these records (:mod:`lithos_lens.knowledge_graph_view`);
the assembly calls the pass (:mod:`lithos_lens.knowledge_graph`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from lithos_lens.knowledge_edges import KnowledgeEdge

#: What became of one ``expand=`` request.
ExpansionState = Literal["applied", "unreached", "refused"]

#: Whether a drawn note can be expanded now: it is the focus, already
#: expanded, has every filtered typed edge drawn (``complete``), would go over
#: the cap, or is ``available``.
NodeExpansionState = Literal["available", "focus", "expanded", "complete", "over_cap"]

EdgesOf = Callable[[str], Sequence[KnowledgeEdge]]
Shows = Callable[[KnowledgeEdge], bool]


@dataclass(frozen=True)
class ExpansionStep:
    """One request, in URL order, and what it added to the view.

    An unapplied step adds nothing. A refused one carries what it would have
    done: ``would_count`` notes towards the ``cap``, ``would_add_nodes`` of
    them newly visible.
    """

    id: str
    state: ExpansionState
    added_nodes: int = 0
    added_edges: int = 0
    would_count: int = 0
    would_add_nodes: int = 0
    cap: int = 0


@dataclass(frozen=True)
class NodeExpansion:
    """A drawn note's expansion eligibility against the final view.

    ``undrawn_nodes`` / ``undrawn_edges`` are the newly visible notes and the
    filtered typed edges expanding it would add (0 for the focus and an
    expanded note); ``would_count`` is the cap count it would give, or the
    current count when no expansion is needed.
    """

    state: NodeExpansionState
    would_count: int
    cap: int
    undrawn_nodes: int = 0
    undrawn_edges: int = 0


@dataclass(frozen=True)
class ExpansionCollapse:
    """Removing one request: the requests that go with it (it and its
    transitive ``via`` dependants, in URL order), and the notes and typed
    edges the remaining requests draw — whether a selection survives."""

    removed: tuple[str, ...]
    nodes: frozenset[str] = frozenset()
    edges: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExpansionWalk:
    """The typed graph after the requests: base plus every applied step."""

    hops: Mapping[str, int]
    edges: tuple[KnowledgeEdge, ...]
    steps: tuple[ExpansionStep, ...] = ()
    #: The request that first drew each note it added; base notes, promoted
    #: layer-only notes included, have none.
    via: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    #: Layer-only notes still visible only through their layer.
    layer_only: frozenset[str] = frozenset()

    @property
    def expanded(self) -> frozenset[str]:
        return frozenset(step.id for step in self.steps if step.state == "applied")

    @property
    def visible(self) -> frozenset[str]:
        return frozenset(self.hops) | self.layer_only


@dataclass(frozen=True)
class KnowledgeExpansion(ExpansionWalk):
    """The walk, plus every visible note's eligibility and each request's
    collapse."""

    nodes: Mapping[str, NodeExpansion] = field(
        default_factory=lambda: MappingProxyType({})
    )
    collapses: Mapping[str, ExpansionCollapse] = field(
        default_factory=lambda: MappingProxyType({})
    )


def _pending(
    edges_of: EdgesOf, shows: Shows, node_id: str, drawn: Mapping[str, KnowledgeEdge]
) -> list[KnowledgeEdge]:
    """``node_id``'s filtered typed edges not drawn yet, each once."""
    pending: dict[str, KnowledgeEdge] = {}
    for edge in edges_of(node_id):
        if shows(edge) and edge.edge_id not in drawn:
            pending.setdefault(edge.edge_id, edge)
    return list(pending.values())


def _ends(edges: Iterable[KnowledgeEdge]) -> dict[str, None]:
    """The edges' endpoints, distinct, in edge order."""
    return dict.fromkeys(end for edge in edges for end in edge.endpoints)


def requests_for(focus: str, requests: Iterable[str]) -> tuple[str, ...]:
    """The requests as applied: first occurrences in order, blanks and the
    focus dropped."""
    return tuple(dict.fromkeys(r for r in requests if r and r != focus))


def walk_expansions(
    edges_of: EdgesOf,
    shows: Shows,
    *,
    focus: str,
    hops: Mapping[str, int],
    edges: Iterable[KnowledgeEdge],
    layer_ids: Iterable[str],
    requests: Iterable[str],
    cap: int,
) -> ExpansionWalk:
    """Apply ``requests`` in order to the base typed graph (``hops``,
    ``edges``) with the focus's layer-only notes visible (``layer_ids``)."""
    placed = dict(hops)
    drawn = {edge.edge_id: edge for edge in edges}
    layer_only = dict.fromkeys(i for i in layer_ids if i not in placed)
    via: dict[str, str] = {}
    steps: list[ExpansionStep] = []
    for root in requests_for(focus, requests):
        if root not in placed and root not in layer_only:
            steps.append(ExpansionStep(root, "unreached"))
            continue
        new_edges = _pending(edges_of, shows, root, drawn)
        new_typed = [end for end in _ends(new_edges) if end not in placed]
        new_visible = [end for end in new_typed if end not in layer_only]
        count = len(placed) + len(new_typed)
        if count > cap:
            steps.append(
                ExpansionStep(
                    root,
                    "refused",
                    would_count=count,
                    would_add_nodes=len(new_visible),
                    cap=cap,
                )
            )
            continue
        hop = placed.get(root, 1)
        drawn.update((edge.edge_id, edge) for edge in new_edges)
        for end in new_typed:
            if end in layer_only:
                del layer_only[end]
                placed[end] = 1
            else:
                placed[end] = hop + 1
                via[end] = root
        steps.append(ExpansionStep(root, "applied", len(new_visible), len(new_edges)))
    return ExpansionWalk(
        hops=MappingProxyType(placed),
        edges=tuple(drawn.values()),
        steps=tuple(steps),
        via=MappingProxyType(via),
        layer_only=frozenset(layer_only),
    )


def _eligibility(
    edges_of: EdgesOf, shows: Shows, walk: ExpansionWalk, focus: str, cap: int
) -> dict[str, NodeExpansion]:
    drawn = {edge.edge_id: edge for edge in walk.edges}
    count = len(walk.hops)
    expanded = walk.expanded
    nodes: dict[str, NodeExpansion] = {}
    for node_id in (*walk.hops, *sorted(walk.layer_only)):
        if node_id == focus or node_id in expanded:
            state = "focus" if node_id == focus else "expanded"
            nodes[node_id] = NodeExpansion(state, count, cap)
            continue
        pending = _pending(edges_of, shows, node_id, drawn)
        new_typed = [end for end in _ends(pending) if end not in walk.hops]
        would = count + len(new_typed)
        nodes[node_id] = NodeExpansion(
            "complete" if not pending else "over_cap" if would > cap else "available",
            would,
            cap,
            undrawn_nodes=sum(1 for end in new_typed if end not in walk.layer_only),
            undrawn_edges=len(pending),
        )
    return nodes


def dependants(walk: ExpansionWalk, root: str) -> tuple[str, ...]:
    """``root`` and every request whose note a removed request first drew,
    transitively, in URL order. An unapplied request drew nothing, so it
    removes only itself."""
    removed = {root}
    grew = True
    while grew:
        grew = False
        for step in walk.steps:
            if step.id not in removed and walk.via.get(step.id) in removed:
                removed.add(step.id)
                grew = True
    return tuple(step.id for step in walk.steps if step.id in removed)


def expand_typed(
    edges_of: EdgesOf,
    shows: Shows,
    *,
    focus: str,
    hops: Mapping[str, int],
    edges: Iterable[KnowledgeEdge],
    layer_ids: Iterable[str],
    requests: Iterable[str],
    cap: int,
    pin: str = "",
    plain: Shows | None = None,
) -> KnowledgeExpansion:
    """:func:`walk_expansions`, every visible note's eligibility, and what
    removing each request leaves drawn.

    ``pin`` is the edge ``shows`` exempts (``plain`` is the same filters
    without it). Its exemption holds only where the walk draws it: a walk
    that does not — the edge refused with its step, or out of reach — is
    the plain walk, the drawing a link that drops the undrawn pin loads.
    Exempt, it could still have refused a step plain filters let through.
    A discarded pin is gone from every link the view emits, so its collapse
    previews are plain too.
    """
    edges, layer_ids = tuple(edges), tuple(layer_ids)

    def walk(chosen: Iterable[str]) -> tuple[ExpansionWalk, Shows]:
        chosen = tuple(chosen)
        done = run(chosen, shows)
        if pin and plain is not None and all(e.edge_id != pin for e in done.edges):
            return run(chosen, plain), plain
        return done, shows

    def run(chosen: Iterable[str], by: Shows) -> ExpansionWalk:
        return walk_expansions(
            edges_of,
            by,
            focus=focus,
            hops=hops,
            edges=edges,
            layer_ids=layer_ids,
            requests=chosen,
            cap=cap,
        )

    final, final_shows = walk(requests)
    discarded = final_shows is not shows
    collapses: dict[str, ExpansionCollapse] = {}
    for step in final.steps:
        removed = dependants(final, step.id)
        kept = tuple(s.id for s in final.steps if s.id not in removed)
        rest = run(kept, final_shows) if discarded else walk(kept)[0]
        collapses[step.id] = ExpansionCollapse(
            removed, rest.visible, frozenset(edge.edge_id for edge in rest.edges)
        )
    return KnowledgeExpansion(
        hops=final.hops,
        edges=final.edges,
        steps=final.steps,
        via=final.via,
        layer_only=final.layer_only,
        nodes=MappingProxyType(_eligibility(edges_of, final_shows, final, focus, cap)),
        collapses=MappingProxyType(collapses),
    )
