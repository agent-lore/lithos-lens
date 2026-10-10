"""The browser SSE streams: `GET /tasks/events` and `GET /knowledge/events`.

One body behind both routes. Each subscribes a queue on the hub for its own
stream (`EventHub.subscribe(stream=...)`), so the hub's scope filter decides
what reaches it: task and system frames on the dashboard's stream, knowledge
frames on the graph page's, `lens.refresh` on both. Everything else is
identical by construction — the connected frame, the keepalive, the
unsubscribe on the way out and the 503 at the subscriber ceiling. Neither
replays to the browser: `Last-Event-ID` and the `lens.refresh` backstop are
the hub's, against Lithos (SPECIFICATION §5.8).
"""

from __future__ import annotations

import asyncio
from asyncio import CancelledError
from collections.abc import AsyncIterator

from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from lithos_lens.errors import EventSubscriberLimit
from lithos_lens.events import EventHub, EventStream, LensEvent

# How long the event stream waits for an event before emitting a comment frame.
# The stream otherwise blocks on ``queue.get()`` forever and only discovers a
# departed client when it next WRITES — which, in a quiet period, is never. A
# slept laptop or a dropped NAT mapping would then park a subscriber and its
# queue for the life of the process. The keepalive is what makes a dead peer
# surface: the write fails, the generator unwinds, and ``unsubscribe`` runs.
SSE_KEEPALIVE_S = 20.0


def event_stream_response(hub: EventHub, stream: EventStream) -> Response:
    """The open stream for one browser on ``stream``, or a 503 at capacity."""
    try:
        queue = hub.subscribe(stream=stream)
    except EventSubscriberLimit:
        # EventSource treats any non-200 as a failed connection: it fires
        # `error` and does NOT retry, which is exactly the handoff wanted
        # here — tasks.js arms its polling fallback on that event, so the
        # refused tab gets a slower board instead of the process getting
        # one more queue it has already said it cannot afford.
        #
        # No log line here: the hub already records the refusal, and it
        # does so RATE-LIMITED. A warning per refusal would hand the same
        # unbounded-log-write back to whoever is opening the connections.
        return PlainTextResponse(
            "Lens is at event-stream capacity. This page will poll instead.",
            status_code=503,
        )
    return StreamingResponse(
        _frames(hub, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _frames(hub: EventHub, queue: asyncio.Queue[LensEvent]) -> AsyncIterator[str]:
    try:
        yield 'event: lens.status\ndata: {"status":"connected"}\n\n'
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_S)
            except TimeoutError:
                # A comment frame: ignored by EventSource, but a WRITE,
                # which is the only way this end learns the peer left.
                yield ": keepalive\n\n"
                continue
            yield event.as_sse()
    except CancelledError:
        raise
    finally:
        hub.unsubscribe(queue)
