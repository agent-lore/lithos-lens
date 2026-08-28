"""Admission control: what a saturated process refuses, and what it must not.

The bound exists because Lens takes unauthenticated requests across the
trusted-network boundary. These tests pin the two halves that make it correct
rather than merely present: it REFUSES a render past the cap instead of
queueing, and it never meters the event stream — a parked browser is not a
render, and metering it would refuse real work while the backend sat idle.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lithos_lens import web
from lithos_lens.config import load_config
from lithos_lens.web import create_app


class _Enough(Exception):
    """Stop the stream once the frames under test have been observed."""


async def _stream_frames(app: FastAPI, path: str, count: int) -> list[bytes]:
    """The first ``count`` body frames the ASGI app writes for ``path``.

    Driven against the ASGI interface directly rather than through a test
    client: both TestClient and httpx's ASGITransport read a response to
    completion, which never happens for a stream that stays open. What these
    tests are about is what the stream writes WHILE it is open.
    """
    frames: list[bytes] = []
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"lens")],
        "client": ("127.0.0.1", 12345),
        "server": ("lens", 80),
    }

    async def receive() -> dict[str, Any]:
        # The client never disconnects and never sends more: this is a GET
        # whose response is the long-lived half.
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            frames.append(message["body"])
            if len(frames) >= count:
                raise _Enough

    try:
        with anyio.fail_after(5):
            await app(scope, receive, send)
    except _Enough:
        pass
    except BaseExceptionGroup as group:
        # Starlette runs the response in a task group, so the sentinel arrives
        # wrapped. Anything else is a real failure and must not be swallowed.
        if not all(isinstance(exc, _Enough) for exc in group.exceptions):
            raise
    return frames


def test_a_saturated_process_refuses_a_render_rather_than_queueing_it(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At capacity the answer is an immediate 503, not a growing queue.

    A cap of zero saturates the process by construction, so the refusal is
    exercised without racing two requests against each other.
    """
    monkeypatch.setattr(web, "MAX_CONCURRENT_RENDERS", 0)
    app = create_app(load_config(lithos_lens_config_env))

    with TestClient(app) as client:
        response = client.get("/tasks")

    assert response.status_code == 503
    assert "capacity" in response.text.lower()


@pytest.mark.anyio
async def test_the_event_stream_is_never_metered_by_admission_control(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this split exists for.

    An SSE connection does no Lithos work and is held for as long as a tab is
    open, so counting it against the render budget lets N open tabs consume N
    slots permanently — 503ing every page and /health while the backend is
    idle. The same saturated process that refuses a render must still connect
    a subscriber here.
    """
    monkeypatch.setattr(web, "MAX_CONCURRENT_RENDERS", 0)
    app = create_app(load_config(lithos_lens_config_env))

    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 503

    frames = await _stream_frames(app, "/tasks/events", 1)

    assert b"lens.status" in frames[0]


@pytest.mark.anyio
async def test_a_quiet_stream_still_writes_so_a_departed_client_is_noticed(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this the stream blocks on ``queue.get()`` forever.

    A connection only discovers its peer left when it WRITES, and with no
    events it never would — so a slept laptop or a dropped NAT mapping would
    park a subscriber and its queue for the life of the process. The comment
    frame is that write. It carries no data, so no client behaviour depends on
    it; what depends on it is the server noticing.
    """
    monkeypatch.setattr(web, "SSE_KEEPALIVE_S", 0.05)
    app = create_app(load_config(lithos_lens_config_env))

    frames = await _stream_frames(app, "/tasks/events", 2)

    assert frames[1] == b": keepalive\n\n"
