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

# How long a "the frontier tools are missing" verdict is trusted before Lens
# probes for them again. Long enough that a genuinely pre-0.4 server is not
# re-probed on every render (the PRD's reason for remembering the answer at
# all), short enough that the verdict cannot become permanent: the same
# symptom is produced by an outage or by the tools being withheld from this
# client, and neither should cost the operator the graph surface until a
# restart.
GRAPH_REPROBE_INTERVAL_S = 300.0


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
        self.health = HealthSnapshot(llm="disabled" if not config.llm.enabled else "ok")
        self._last_health_probe_at = 0.0
        # Task-graph feature detection (T1 story 27): when a frontier read
        # fails because the TOOL is missing, the verdict is remembered until
        # this deadline and then re-probed (see GRAPH_REPROBE_INTERVAL_S).
        self._graph_unavailable_until = 0.0

    @property
    def graph_available(self) -> bool:
        """Whether to attempt the task-graph frontier reads on this render.

        False only inside the re-probe window opened by
        :meth:`note_graph_unavailable`; the next render after it lapses probes
        again, so an upgrade — or a transient failure that merely looked like
        version skew — heals without a restart.
        """
        return monotonic() >= self._graph_unavailable_until

    def note_graph_unavailable(self) -> None:
        """Record that a probe found the frontier tools missing."""
        self._graph_unavailable_until = monotonic() + GRAPH_REPROBE_INTERVAL_S

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
