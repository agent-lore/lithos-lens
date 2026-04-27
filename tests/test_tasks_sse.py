"""Milestone 2 task event normalization tests."""

from __future__ import annotations

import asyncio

import pytest

from lithos_lens.config import EventsConfig, LithosConfig
from lithos_lens.events import EventHub, LensEvent, parse_lithos_sse_frame


def test_lithos_task_event_is_normalized_with_original_event_id() -> None:
    event = parse_lithos_sse_frame(
        [
            "id: evt-1",
            "event: task.claimed",
            'data: {"task_id":"task-1","agent":"agent-a","aspect":"docs"}',
        ]
    )

    assert event is not None
    assert event.id == "evt-1"
    assert event.type == "task.claimed"
    assert event.task_id == "task-1"
    assert event.payload["agent"] == "agent-a"
    assert event.requires_refresh is True


def test_non_task_events_are_ignored() -> None:
    event = parse_lithos_sse_frame(
        [
            "id: evt-2",
            "event: note.created",
            'data: {"id":"note-1"}',
        ]
    )

    assert event is None


@pytest.mark.anyio
async def test_event_hub_fans_out_to_browser_subscribers() -> None:
    hub = EventHub(EventsConfig(enabled=False), LithosConfig())
    first = hub.subscribe()
    second = hub.subscribe()
    event = LensEvent(
        id="evt-3",
        type="finding.posted",
        task_id="task-1",
        payload={"finding_id": "finding-1"},
    )

    await hub.publish(event)

    assert await asyncio.wait_for(first.get(), timeout=0.1) == event
    assert await asyncio.wait_for(second.get(), timeout=0.1) == event
    hub.unsubscribe(first)
    hub.unsubscribe(second)
