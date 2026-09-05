"""Application state and startup/shutdown orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from lithos_lens.config import LithosLensConfig
from lithos_lens.events import EventHub, EventStatus
from lithos_lens.graph_cache import GraphCache
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
