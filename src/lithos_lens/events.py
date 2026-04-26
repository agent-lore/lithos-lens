"""Shared event subscription state.

Milestone 0 provides the lifecycle skeleton and status reporting. Milestone 2
wires this to Lithos's `/events` SSE stream and browser re-broadcast routes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from lithos_lens.config import EventsConfig

EventStatus = Literal["live", "reconnecting", "disabled"]


@dataclass
class EventHub:
    config: EventsConfig
    status: EventStatus = "disabled"

    async def start(self) -> None:
        if not self.config.enabled:
            self.status = "disabled"
            return
        self.status = "reconnecting"

    async def stop(self) -> None:
        self.status = "disabled"
