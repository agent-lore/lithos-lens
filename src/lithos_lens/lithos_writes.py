"""The five Lithos task-write methods, on the protocol and on the real client.

The same shape as every read in :mod:`lithos_lens.lithos_client` — build the
arguments, place the call, raise on the error envelope, normalize the payload —
with two twists: the arguments come from the shared builders in
:mod:`lithos_lens.task_writes`, so the fake's write log and this wire cannot
drift, and ``agent`` is a parameter rather than ``self._config.agent_id``
because a write is attributed to the OPERATOR, not to Lens (T3 D3). The write
funnel is their only caller.

Kept out of ``lithos_client`` so that module stays under the 800-line ceiling
once T3's agent reads land beside it (``docs/architecture.toml`` [budgets]).
:class:`LithosWriteProtocol` is a base of ``LithosClientProtocol`` and
:class:`LithosWriteMethods` a base of ``LithosClient``, so callers see one
surface. The contract guardrail scans this module's ``self._call_tool`` calls
alongside the client's.
"""

from __future__ import annotations

from typing import Any, Protocol

from lithos_lens import task_writes
from lithos_lens.mcp_transport import raise_for_error


class LithosWriteProtocol(Protocol):
    """The writes (T3): called only by the write funnel, never by a read path."""

    async def task_complete(
        self, task_id: str, *, agent: str, outcome: str = ""
    ) -> task_writes.TaskCompleteResult: ...

    async def task_reopen(
        self, task_id: str, *, agent: str
    ) -> task_writes.TaskReopenResult: ...

    async def task_cancel(
        self, task_id: str, *, agent: str, reason: str = ""
    ) -> task_writes.TaskCancelResult: ...

    async def task_create(
        self,
        *,
        title: str,
        agent: str,
        description: str = "",
        tags: tuple[str, ...] | list[str] = (),
        metadata: dict[str, Any] | None = None,
        task_type: str = "task",
        depends_on: tuple[str, ...] | list[str] = (),
        parent_task_id: str = "",
    ) -> task_writes.TaskCreateResult: ...

    async def task_edge_upsert(
        self,
        *,
        from_task_id: str,
        to_task_id: str,
        edge_type: str,
        agent: str,
        metadata: dict[str, Any] | None = None,
    ) -> task_writes.TaskEdgeUpsertResult: ...


class LithosWriteMethods:
    """The five writes, placed through the host client's ``_call_tool``."""

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Overridden by ``LithosClient``, which places the call on its transport."""
        raise NotImplementedError

    async def task_complete(
        self, task_id: str, *, agent: str, outcome: str = ""
    ) -> task_writes.TaskCompleteResult:
        """Complete an open task; answers ``task_not_found`` if it is neither."""
        payload = await self._call_tool(
            "lithos_task_complete",
            task_writes.complete_arguments(task_id, agent=agent, outcome=outcome),
        )
        raise_for_error(payload)
        return task_writes.normalize_task_complete(payload)

    async def task_reopen(
        self, task_id: str, *, agent: str
    ) -> task_writes.TaskReopenResult:
        """Return a completed or cancelled task to open."""
        payload = await self._call_tool(
            "lithos_task_reopen", task_writes.reopen_arguments(task_id, agent=agent)
        )
        raise_for_error(payload)
        return task_writes.normalize_task_reopen(payload)

    async def task_cancel(
        self, task_id: str, *, agent: str, reason: str = ""
    ) -> task_writes.TaskCancelResult:
        """Cancel an open task, releasing every claim on it."""
        payload = await self._call_tool(
            "lithos_task_cancel",
            task_writes.cancel_arguments(task_id, agent=agent, reason=reason),
        )
        raise_for_error(payload)
        return task_writes.normalize_task_cancel(payload)

    async def task_create(
        self,
        *,
        title: str,
        agent: str,
        description: str = "",
        tags: tuple[str, ...] | list[str] = (),
        metadata: dict[str, Any] | None = None,
        task_type: str = "task",
        depends_on: tuple[str, ...] | list[str] = (),
        parent_task_id: str = "",
    ) -> task_writes.TaskCreateResult:
        """Create a task, epic or gate, with its predecessors and parent."""
        payload = await self._call_tool(
            "lithos_task_create",
            task_writes.create_arguments(
                title=title,
                agent=agent,
                description=description,
                tags=tags,
                metadata=metadata,
                task_type=task_type,
                depends_on=depends_on,
                parent_task_id=parent_task_id,
            ),
        )
        raise_for_error(payload)
        return task_writes.normalize_task_create(payload)

    async def task_edge_upsert(
        self,
        *,
        from_task_id: str,
        to_task_id: str,
        edge_type: str,
        agent: str,
        metadata: dict[str, Any] | None = None,
    ) -> task_writes.TaskEdgeUpsertResult:
        """Insert a typed relation, or replace an existing one's metadata."""
        payload = await self._call_tool(
            "lithos_task_edge_upsert",
            task_writes.edge_upsert_arguments(
                from_task_id=from_task_id,
                to_task_id=to_task_id,
                edge_type=edge_type,
                agent=agent,
                metadata=metadata,
            ),
        )
        raise_for_error(payload)
        return task_writes.normalize_task_edge_upsert(payload)
