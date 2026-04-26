"""Application state and startup/shutdown orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from lithos_lens.config import LithosLensConfig
from lithos_lens.events import EventHub, EventStatus
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
        self, config: LithosLensConfig, lithos_client: LithosClientProtocol
    ) -> None:
        self.config = config
        self.lithos_client = lithos_client
        self.events = EventHub(config.events)
        self.health = HealthSnapshot(llm="disabled" if not config.llm.enabled else "ok")
        self._last_health_probe_at = 0.0

    async def startup(self) -> None:
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
