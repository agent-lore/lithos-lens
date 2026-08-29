"""The seam between LithosClient and the MCP transport it owns.

The transport (session worker, reconnect, the per-call deadline and
process-wide gate, result decoding) moved out of ``lithos_client`` when the
module's god-module exception was discharged. Its BEHAVIOUR is covered where it
always was — ``test_tasks_graph_reads.py`` drives the deadline, the gate and
the decoder through the real client. What moved, and so what is pinned here, is
where the bounds are SET: they are constructor arguments on the transport, and
``lithos_client``'s module constants are what fills them in. A split that
quietly let the transport fall back to its own defaults would leave every one
of those tests passing while the documented knob did nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lithos_lens import lithos_client
from lithos_lens.config import LithosConfig
from lithos_lens.lithos_client import LithosClient, LithosToolError


class _HangingClient(LithosClient):
    """A client whose tool call never answers, so only the deadline ends it."""

    def __init__(self) -> None:
        super().__init__(LithosConfig())

    async def _call_tool_oneshot(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> Any:
        await asyncio.Event().wait()


def test_the_deadline_the_transport_enforces_is_the_clients_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lithos_client.CALL_TIMEOUT_S`` is the documented deadline — the figure
    ``task_links`` and ``task_detail`` cite when they explain why a stalled
    read costs a row rather than a page. It lives one module away from the
    ``asyncio.wait_for`` that applies it, so pin the VALUE, not just that some
    deadline fires: the transport's own default would time out too, 300x later,
    and every existing timeout assertion would still pass.
    """
    monkeypatch.setattr(lithos_client, "CALL_TIMEOUT_S", 0.05)
    client = _HangingClient()

    async def _driver() -> None:
        try:
            await client.stats()
        finally:
            await client.close()

    with pytest.raises(LithosToolError) as excinfo:
        asyncio.run(_driver())

    assert excinfo.value.code == "timeout"
    assert "0.05s" in str(excinfo.value)


class _CountingClient(LithosClient):
    """Records how many tool calls are in flight at once."""

    def __init__(self) -> None:
        super().__init__(LithosConfig())
        self.in_flight = 0
        self.peak_in_flight = 0

    async def _call_tool_oneshot(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> Any:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)  # yield, so calls allowed to overlap do
            return None  # an empty result decodes to {}
        finally:
            self.in_flight -= 1


def test_the_gate_the_transport_holds_is_sized_by_the_clients_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same seam, the other bound. The existing process-wide-gate test asserts
    against the UNPATCHED constant, so it cannot tell a gate sized from this
    module apart from one sized from the transport's own default — it passes
    either way as long as 16 is the number. Narrowing the constant first makes
    the two answers differ.
    """
    monkeypatch.setattr(lithos_client, "MAX_CONCURRENT_TOOL_CALLS", 2)
    client = _CountingClient()

    async def _driver() -> None:
        try:
            await asyncio.gather(*(client.stats() for _ in range(8)))
        finally:
            await client.close()

    asyncio.run(_driver())

    assert client.peak_in_flight > 1  # still concurrent, so the bound below bites
    assert client.peak_in_flight <= 2
