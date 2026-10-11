"""The knowledge graph's scope picker (K2 D11, story 17): what the unscoped
`/knowledge/graph` offers, from the edge-table snapshot alone.

The page renders it with no ``focus``, ``type`` or ``namespace``, and the
knowledge landing reads it for its browse link and count, so it is the graph
data layer's own model rather than either route module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lithos_lens.knowledge_edges import (
    EdgeFacets,
    EdgeTable,
    EdgeTableRefusal,
    EdgeTableSnapshot,
)

#: The picker's namespace table shows this many rows; the rest are behind a
#: disclosure (PRD D11).
PICKER_TOP_NAMESPACES = 20


@dataclass(frozen=True)
class KnowledgeGraphPicker:
    """What the unscoped page offers (PRD D11, story 17).

    ``facets`` is ``None`` when the snapshot gave none: the table over its
    bound (``refused``) or unreadable (``unavailable``). The page then offers
    only a typed-in ``type=`` / ``namespace=`` scope.
    """

    facets: EdgeFacets | None = None
    as_of: datetime | None = None
    stale: bool = False
    refused: EdgeTableRefusal | None = None
    unavailable: bool = False

    @property
    def top_namespaces(self) -> tuple[tuple[str, int], ...]:
        rows = tuple(self.facets.namespaces.items()) if self.facets else ()
        return rows[:PICKER_TOP_NAMESPACES]

    @property
    def more_namespaces(self) -> tuple[tuple[str, int], ...]:
        rows = tuple(self.facets.namespaces.items()) if self.facets else ()
        return rows[PICKER_TOP_NAMESPACES:]


async def load_picker(table: EdgeTable) -> KnowledgeGraphPicker:
    """The picker from the snapshot: its facets, or why there are none."""
    try:
        state = await table.read()
    except Exception:
        return KnowledgeGraphPicker(unavailable=True)
    if isinstance(state, EdgeTableSnapshot):
        return KnowledgeGraphPicker(state.facets, state.as_of, state.stale)
    return KnowledgeGraphPicker(refused=state, as_of=state.as_of)
