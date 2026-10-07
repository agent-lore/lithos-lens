"""The knowledge-edge vocabulary: the known-type table, direction and legend.

``edges.type`` is an unvalidated TEXT column upstream (lithos ``edge_store.py``
at 0.6.0 @ d2c49bb), so Lens keeps the table of types it knows (K2 PRD D5) and
draws anything else as stored. The table's direction and symmetry are facts
read from the Lithos source and cited in ``tests/contracts/lithos_edge_list.json``,
not re-derived here:

- ``contradicts`` and ``analogy_to`` are symmetric; inference stores them
  ``from_id <= to_id`` (``lcma/edge_inference.py`` ``SYMMETRIC_RELATIONS``);
- ``related_to`` is symmetric; citation reinforcement and task consolidation
  alike store it ``from_id <= to_id`` (``cognitive_memory.reinforce_between``,
  ``lcma/enrich.py``);
- that ordering is a writer habit, not a storage rule: ``lithos_edge_upsert``
  keeps a caller's endpoints as given, so a symmetric row may be stored
  ``from_id > to_id``. Symmetry is read from the type alone, never from the
  endpoint order;
- ``supports``, ``refines``, ``is_example_of`` and ``depends_on`` take the
  adjudicator's direction verdict (``edge_inference.edge_endpoints``);
- ``derived_from`` is the frontmatter provenance projection, derived → source
  (``provenance.py``).

An unknown type keeps its stored direction — an arrowhead from ``from_id`` to
``to_id`` — and is labelled with its raw type, so a type Lithos starts writing
tomorrow is shown rather than dropped or guessed at.

Pure: no client, no clock. The snapshot (:mod:`lithos_lens.knowledge_edges`)
holds the rows; this module only says how a row reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

Stroke = Literal["solid", "dotted", "dashed"]


class EdgeDirection(Enum):
    """How an edge's two endpoints relate."""

    #: ``from_id`` → ``to_id`` means something; drawn with an arrowhead.
    DIRECTED = "directed"
    #: Endpoint order means nothing (usually ``from_id <= to_id``, but
    #: ``lithos_edge_upsert`` keeps a caller's order); no arrowhead.
    SYMMETRIC = "symmetric"
    #: An unknown type: drawn as stored, arrowhead included, and its panel
    #: says "direction as recorded".
    AS_RECORDED = "as_recorded"

    @property
    def has_arrowhead(self) -> bool:
        return self is not EdgeDirection.SYMMETRIC


@dataclass(frozen=True)
class KnownEdgeType:
    """One row of the known-type table (K2 PRD D5)."""

    name: str
    direction: EdgeDirection
    stroke: Stroke
    #: The legend's plain-language line: "A supports B: A is evidence for B".
    legend: str

    @property
    def css_class(self) -> str:
        return f"kedge-{self.name.replace('_', '-')}"


#: The known types in the PRD's order (live counts, most first, with
#: ``derived_from`` and ``contradicts`` after), which is also legend order.
KNOWN_KNOWLEDGE_EDGE_TYPES: tuple[KnownEdgeType, ...] = (
    KnownEdgeType(
        "supports",
        EdgeDirection.DIRECTED,
        "solid",
        "A supports B: A is evidence for B",
    ),
    KnownEdgeType(
        "related_to",
        EdgeDirection.SYMMETRIC,
        "solid",
        "A related to B: the two were cited together (no direction)",
    ),
    KnownEdgeType(
        "analogy_to",
        EdgeDirection.SYMMETRIC,
        "solid",
        "A analogy to B: the two are analogous (no direction)",
    ),
    KnownEdgeType(
        "refines",
        EdgeDirection.DIRECTED,
        "solid",
        "A refines B: A narrows or improves on B",
    ),
    KnownEdgeType(
        "is_example_of",
        EdgeDirection.DIRECTED,
        "solid",
        "A is an example of B: A is an instance of B",
    ),
    KnownEdgeType(
        "depends_on",
        EdgeDirection.DIRECTED,
        "solid",
        "A depends on B: A relies on B",
    ),
    KnownEdgeType(
        "derived_from",
        EdgeDirection.DIRECTED,
        "dotted",
        "A derived from B: A was derived from the source B (declared in frontmatter)",
    ),
    KnownEdgeType(
        "contradicts",
        EdgeDirection.SYMMETRIC,
        "dashed",
        "A contradicts B: the two disagree (no direction); dashed until resolved",
    ),
)

_BY_NAME = {known.name: known for known in KNOWN_KNOWLEDGE_EDGE_TYPES}
_ORDER = {known.name: index for index, known in enumerate(KNOWN_KNOWLEDGE_EDGE_TYPES)}

#: The class every unknown type draws with: neutral grey, arrowhead.
UNKNOWN_EDGE_CSS_CLASS = "kedge-unknown"


class _TypedEdge(Protocol):
    @property
    def type(self) -> str: ...

    @property
    def conflict_state(self) -> str | None: ...


@dataclass(frozen=True)
class EdgeStyle:
    """How one edge row draws: the class, the stroke, the arrowhead, a label.

    ``label`` is empty for a known type in its ordinary state. It carries the
    raw type for an unknown one, and the resolution (``superseded`` …) for a
    resolved ``contradicts``, which draws muted rather than red.
    """

    css_class: str
    stroke: Stroke
    arrowhead: bool
    label: str = ""


@dataclass(frozen=True)
class LegendLine:
    """One legend entry: a type present in the drawn graph and what it means."""

    type: str
    css_class: str
    line: str
    known: bool = True


def known_edge_type(name: str) -> KnownEdgeType | None:
    """The table row for ``name``, or ``None`` for a type Lens does not know."""
    return _BY_NAME.get(name)


def direction_of(edge: _TypedEdge) -> EdgeDirection:
    """The edge's direction: the table's for a known type, as recorded otherwise."""
    known = _BY_NAME.get(edge.type)
    return known.direction if known is not None else EdgeDirection.AS_RECORDED


def edge_style(edge: _TypedEdge) -> EdgeStyle:
    """How ``edge`` draws (K2 PRD D5).

    A ``contradicts`` row is unresolved while its ``conflict_state`` is NULL
    — the column stays NULL until ``lithos_conflict_resolve`` writes one of
    ``accepted_dual`` / ``superseded`` / ``refuted`` / ``merged`` — and only
    then is it drawn dashed red; a resolved one is muted and labelled with
    its resolution.
    """
    known = _BY_NAME.get(edge.type)
    if known is None:
        return EdgeStyle(
            css_class=UNKNOWN_EDGE_CSS_CLASS,
            stroke="solid",
            arrowhead=True,
            label=edge.type,
        )
    arrowhead = known.direction.has_arrowhead
    if known.name == "contradicts":
        if edge.conflict_state is None:
            return EdgeStyle(
                f"{known.css_class} kedge-unresolved", known.stroke, arrowhead
            )
        return EdgeStyle(
            f"{known.css_class} kedge-resolved",
            known.stroke,
            arrowhead,
            label=edge.conflict_state,
        )
    return EdgeStyle(known.css_class, known.stroke, arrowhead)


def legend(types: Iterable[str]) -> tuple[LegendLine, ...]:
    """One line per type PRESENT, known types in table order, unknown after.

    Exactly the types given, each once: a legend line for a type the graph
    does not draw is noise (the T2 rule). Unknown types follow the known ones,
    by name, each saying it is drawn as recorded.
    """
    present = set(types)
    known = sorted(
        (name for name in present if name in _BY_NAME), key=_ORDER.__getitem__
    )
    unknown = sorted(name for name in present if name not in _BY_NAME)
    return tuple(
        LegendLine(name, _BY_NAME[name].css_class, _BY_NAME[name].legend)
        for name in known
    ) + tuple(
        LegendLine(
            name,
            UNKNOWN_EDGE_CSS_CLASS,
            f"A {name} B: a type Lens does not know, drawn as recorded",
            known=False,
        )
        for name in unknown
    )
