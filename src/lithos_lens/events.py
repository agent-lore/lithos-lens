"""Shared Lithos event subscription and browser fan-out."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from lithos_lens.config import EventsConfig, LithosConfig

logger = logging.getLogger(__name__)

EventStatus = Literal["live", "reconnecting", "disabled"]

TASK_EVENT_TYPES = {
    "task.created",
    "task.claimed",
    "task.released",
    "task.completed",
    "task.cancelled",
    "finding.posted",
}
SPARSE_EVENT_TYPES = {
    "task.created",
    "task.claimed",
    "task.released",
    "task.completed",
    "task.cancelled",
    "finding.posted",
}


@dataclass(frozen=True)
class LensEvent:
    id: str
    type: str
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    requires_refresh: bool = True

    def as_sse(self) -> str:
        data = json.dumps(
            {
                "id": self.id,
                "type": self.type,
                "task_id": self.task_id,
                "payload": self.payload,
                "requires_refresh": self.requires_refresh,
            },
            separators=(",", ":"),
        )
        return f"id: {self.id}\nevent: {self.type}\ndata: {data}\n\n"


@dataclass
class EventHub:
    config: EventsConfig
    lithos: LithosConfig
    status: EventStatus = "disabled"

    def __post_init__(self) -> None:
        self._subscribers: set[asyncio.Queue[LensEvent]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if not self.config.enabled:
            self.status = "disabled"
            return
        if self._task is not None and not self._task.done():
            return
        self.status = "reconnecting"
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="lithos-events")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        self.status = "disabled"
        for queue in list(self._subscribers):
            self._subscribers.discard(queue)

    def subscribe(self, *, maxsize: int = 100) -> asyncio.Queue[LensEvent]:
        queue: asyncio.Queue[LensEvent] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[LensEvent]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event: LensEvent) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "lens event subscriber queue full",
                    extra={"event_type": event.type, "event_id": event.id},
                )

    async def _run(self) -> None:
        backoff = self.config.reconnect_backoff_ms or (1000,)
        attempt = 0
        while not self._stop.is_set():
            try:
                self.status = "live"
                async for event in _stream_lithos_events(self.lithos):
                    attempt = 0
                    await self.publish(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.info("lithos event stream disconnected", exc_info=True)
            self.status = "reconnecting"
            delay_ms = backoff[min(attempt, len(backoff) - 1)]
            attempt += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay_ms / 1000)
            except TimeoutError:
                continue


async def _stream_lithos_events(lithos: LithosConfig) -> AsyncIterator[LensEvent]:
    endpoint = _events_url(lithos)
    timeout = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream("GET", endpoint) as response,
    ):
        response.raise_for_status()
        frame: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                if frame:
                    event = parse_lithos_sse_frame(frame)
                    if event is not None:
                        yield event
                    frame = []
                continue
            if line.startswith(":"):
                continue
            frame.append(line)


def parse_lithos_sse_frame(lines: list[str]) -> LensEvent | None:
    event_id = ""
    event_type = "message"
    data_lines: list[str] = []
    for line in lines:
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            event_id = value
        elif field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)

    if event_type not in TASK_EVENT_TYPES:
        return None
    try:
        payload = json.loads("\n".join(data_lines)) if data_lines else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    event = normalize_lithos_event(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
    )
    return event


def normalize_lithos_event(
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> LensEvent | None:
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        return None
    return LensEvent(
        id=event_id or f"{event_type}:{task_id}",
        type=event_type,
        task_id=task_id,
        payload=payload,
        requires_refresh=event_type in SPARSE_EVENT_TYPES,
    )


def _events_url(lithos: LithosConfig) -> str:
    path = lithos.sse_events_path.strip("/")
    base = f"{lithos.url.rstrip('/')}/{path}"
    params = urlencode({"types": ",".join(sorted(TASK_EVENT_TYPES))})
    return f"{base}?{params}"
