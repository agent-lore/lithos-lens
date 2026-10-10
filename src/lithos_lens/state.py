"""Application state and startup/shutdown orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from lithos_lens.config import LithosLensConfig
from lithos_lens.events import EventHub, EventStatus
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.graph_cache import GraphCache, graph_fanout_gate
from lithos_lens.knowledge_edges import EdgeTable
from lithos_lens.knowledge_facts import NoteFactsCache
from lithos_lens.lithos_client import LithosClientProtocol, LithosHealth

logger = logging.getLogger(__name__)

LLMHealth = Literal["disabled", "ok", "error"]


@dataclass
class HealthSnapshot:
    lithos: LithosHealth = "unreachable"
    events: EventStatus = "disabled"
    llm: LLMHealth = "disabled"

    @property
    def status(self) -> str:
        return "ok" if self.lithos == "ok" and self.llm != "error" else "degraded"


class AppState:
    def __init__(
        self,
        config: LithosLensConfig,
        lithos_client: LithosClientProtocol,
        *,
        events: EventHub | None = None,
    ) -> None:
        self.config = config
        self.lithos_client = lithos_client
        # An injected hub (fake-Lithos app mode passes its hermetic
        # FakeEventHub) replaces the real upstream-dialing one.
        self.events = (
            events if events is not None else EventHub(config.events, config.lithos)
        )
        # The per-task edge cache every graph surface reads through, and the
        # hub's eviction hook wired to it. It lives HERE, beside the hub,
        # because both halves of its invalidation are process-wide: one entry
        # per task shared by every scope, evicted by the one event stream.
        # Wired after construction so an injected hub (fake mode's hermetic
        # FakeEventHub) gets the same treatment as the real one.
        self.graph_cache = GraphCache(ttl_s=config.graph.cache_ttl_s)
        self.events.graph_cache = self.graph_cache
        # The knowledge graph's two caches (K2 D2, D4), here beside the task
        # graph's for the same reason: each is one process-wide instance —
        # the edge-table snapshot every graph page, panel and note banner
        # reads, and the note facts every node draws with — that the hub's
        # knowledge events patch before each fan-out, wired below the same
        # way as the graph cache (S7). The facts reads share the task graph's
        # fan-out gate, so the two graphs together hold one share of the
        # Lithos session.
        knowledge = config.knowledge
        client = lithos_client
        self.edge_table = EdgeTable(
            lambda edge_type, namespace: client.edge_list(
                type=edge_type, namespace=namespace
            ),
            ttl_s=knowledge.graph_edge_table_ttl_s,
            max_edges=knowledge.graph_edge_table_max_edges,
        )
        self.note_facts = NoteFactsCache(
            lambda note_id: client.read_note(note_id, max_length=1),
            graph_fanout_gate,
            ttl_s=knowledge.graph_note_facts_ttl_s,
            fanout_cap=knowledge.graph_title_fanout_cap,
        )
        self.events.edge_table = self.edge_table
        self.events.note_facts = self.note_facts
        # Fake-Lithos app mode's writes announce themselves the way the real
        # server does, so that mode exercises write -> event -> SSE -> board
        # end to end. Wired HERE, with the graph cache, for the same reason:
        # this is where the hub the process actually publishes through is
        # settled, and the client was handed over before it existed.
        if isinstance(self.lithos_client, FakeLithosClient):
            self.lithos_client.events = self.events
        self.health = HealthSnapshot(llm="disabled" if not config.llm.enabled else "ok")
        self._last_health_probe_at = 0.0

    async def startup(self) -> None:
        await self.lithos_client.startup()
        self.health.lithos = await self.lithos_client.health()
        self._last_health_probe_at = monotonic()
        if self.health.lithos == "ok":
            registered = await self.lithos_client.register_agent()
            if not registered:
                logger.info("startup registration did not complete")
        await self.events.start()
        self.health.events = self.events.status

    async def shutdown(self) -> None:
        await self.events.stop()
        self.health.events = self.events.status
        await self.lithos_client.close()

    async def refresh_health(self) -> HealthSnapshot:
        now = monotonic()
        if now - self._last_health_probe_at >= self.config.health.refresh_interval_s:
            self.health.lithos = await self.lithos_client.health()
            self._last_health_probe_at = now
        self.health.events = self.events.status
        self.health.llm = "disabled" if not self.config.llm.enabled else self.health.llm
        return self.health
