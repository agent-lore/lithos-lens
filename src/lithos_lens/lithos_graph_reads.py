"""The knowledge-graph reads (K2), on the protocol and on the real client.

``lithos_edge_list`` — the one call the edge-table snapshot
(:mod:`lithos_lens.knowledge_edges`) is fetched with, and the filtered read a
table over the bound still serves. Same shape as every read in
:mod:`lithos_lens.lithos_client`: build the arguments, place the call, raise on
the error envelope, normalize the payload — the rows through
:func:`~lithos_lens.knowledge_edges.normalize_edge_list`, which the fake
serves its dataset through too.

Mixed in rather than written into ``lithos_client`` on the
:mod:`lithos_lens.lithos_writes` precedent: that module sits under the 800-line
ceiling (``docs/architecture.toml`` [budgets]) with no room for a K2 surface,
and the graph's reads are a surface of their own — K3's ``lithos_node_stats``
lands here too. :class:`LithosGraphReadProtocol` is a base of
``LithosClientProtocol`` and :class:`LithosGraphReadMethods` a base of
``LithosClient``, so callers see one surface. The contract guardrail scans this
module's ``self._call_tool`` calls alongside the client's.
"""

from __future__ import annotations

from typing import Any, Protocol

from lithos_lens.knowledge_edges import KnowledgeEdge, normalize_edge_list
from lithos_lens.mcp_transport import raise_for_error


class LithosGraphReadProtocol(Protocol):
    """The reads the knowledge graph is assembled from."""

    async def edge_list(
        self,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        type: str | None = None,
        namespace: str | None = None,
    ) -> tuple[KnowledgeEdge, ...]: ...


class LithosGraphReadMethods:
    """The graph reads, placed through the host client's ``_call_tool``."""

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Overridden by ``LithosClient``, which places the call on its transport."""
        raise NotImplementedError

    async def edge_list(
        self,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        type: str | None = None,
        namespace: str | None = None,
    ) -> tuple[KnowledgeEdge, ...]:
        """Edge-table rows via ``lithos_edge_list``, filtered by exact match.

        Only the filters given are sent, so the snapshot's unfiltered fetch
        sends ``{}`` and answers the whole table — no limit, offset or order
        upstream. Lens never sends an id prefix: upstream resolves a 6+-char
        one on ``from_id``/``to_id`` and refuses an ambiguous one with
        ``ambiguous_id_prefix``, which raises here like any envelope.
        """
        filters = {
            "from_id": from_id,
            "to_id": to_id,
            "type": type,
            "namespace": namespace,
        }
        arguments = {key: value for key, value in filters.items() if value is not None}
        payload = await self._call_tool("lithos_edge_list", arguments)
        raise_for_error(payload)
        return normalize_edge_list(payload)
