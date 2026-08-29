"""Admission control: what a saturated process refuses, and what it must not.

The bound exists because Lens takes unauthenticated requests across the
trusted-network boundary. These tests pin the two halves that make it correct
rather than merely present: it REFUSES a render past the cap instead of
queueing, and it never meters the event stream — a parked browser is not a
render, and metering it would refuse real work while the backend sat idle.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lithos_lens import web
from lithos_lens.config import load_config
from lithos_lens.web import create_app
from tests.conftest import stream_frames


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


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("/health", "REQUIREMENTS §4 makes this the container health check"),
        ("/static/lens.css", "the admitted page needs its own assets"),
    ],
)
def test_saturation_does_not_take_out_the_probe_or_the_assets(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    why: str,
) -> None:
    """Refusing either of these makes saturation worse rather than survivable.

    A 503 on /health tells the orchestrator a merely-busy container is
    unhealthy, so it restarts it: the load spike the cap exists to survive
    becomes a restart loop. A 503 on /static leaves a page whose HTML was
    admitted unstyled and inert — a slot spent to produce a broken result.
    Neither does any Lithos work, which is what the budget is for.
    """
    monkeypatch.setattr(web, "MAX_CONCURRENT_RENDERS", 0)
    app = create_app(load_config(lithos_lens_config_env))

    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 503, "the cap is in force"
        assert client.get(path).status_code == 200, why


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

    frames = await stream_frames(app, "/tasks/events", 1)

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

    frames = await stream_frames(app, "/tasks/events", 2)

    assert frames[1] == b": keepalive\n\n"
