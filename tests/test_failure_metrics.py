"""Metrics for the things that actually go wrong: the Lithos call funnel, the
event hub, and admission control.

Every assertion reads a real instrument back through an in-memory metric
reader, so these cover the recording path rather than the call site's intent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from lithos_lens.config import EventsConfig, LithosConfig
from lithos_lens.errors import EventSubscriberLimit
from lithos_lens.events import (
    LENS_REFRESH_EVENT,
    MAX_EVENT_SUBSCRIBERS,
    EventHub,
    LensEvent,
)
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.mcp_transport import MCPTransport
from tests.conftest import metric_points, metric_value

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── Lithos call funnel ────────────────────────────────────────────────


class _Result:
    """The duck type `decode_tool_result` reads: `.content[0].text`, `.isError`."""

    def __init__(self, text: str = "{}", *, is_error: bool = False) -> None:
        self.content = [type("Block", (), {"text": text})()]
        self.isError = is_error


def _transport(oneshot: Any, **kwargs: Any) -> MCPTransport:
    """A transport with no session worker, so `_invoke` uses the oneshot path.

    That path runs the real gate, the real deadline and the real decoder, which
    is what the instrumentation wraps -- so these exercise the production
    funnel, not a stand-in for it.
    """
    return MCPTransport(LithosConfig(), oneshot=oneshot, **kwargs)


async def test_a_successful_call_is_counted_and_timed(
    metric_reader: InMemoryMetricReader,
) -> None:
    async def ok(name: str, arguments: dict[str, Any]) -> Any:
        return _Result('{"status": "ok"}')

    await _transport(ok).call_tool("lithos_task_list", {})

    counted = metric_value(
        metric_reader,
        "lens_lithos_tool_calls_total",
        tool="lithos_task_list",
        outcome="ok",
    )
    assert counted.value == 1
    (timed,) = metric_points(metric_reader, "lens_lithos_tool_duration_seconds")
    assert timed.count == 1
    assert dict(timed.attributes or {}) == {"tool": "lithos_task_list"}


async def test_a_timeout_is_counted_apart_from_other_failures(
    metric_reader: InMemoryMetricReader,
) -> None:
    """`timeout` means Lens gave up, not that Lithos answered. Folding it in
    with tool_error would point an operator at the tool when the deadline or
    the load is what moved."""

    async def hangs(name: str, arguments: dict[str, Any]) -> Any:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    with pytest.raises(LithosToolError) as excinfo:
        await _transport(hangs, call_timeout_s=0.05).call_tool("lithos_read", {})

    assert excinfo.value.code == "timeout"
    assert (
        metric_value(
            metric_reader,
            "lens_lithos_tool_calls_total",
            tool="lithos_read",
            outcome="timeout",
        ).value
        == 1
    )


async def test_a_coded_refusal_counts_as_a_tool_error(
    metric_reader: InMemoryMetricReader,
) -> None:
    """Lithos answering "no" is not the transport failing, and the two want
    different responses from whoever is reading the graph."""

    async def refuses(name: str, arguments: dict[str, Any]) -> Any:
        return _Result("task_not_found", is_error=True)

    with pytest.raises(LithosToolError):
        await _transport(refuses).call_tool("lithos_task_get", {})

    assert (
        metric_value(
            metric_reader,
            "lens_lithos_tool_calls_total",
            tool="lithos_task_get",
            outcome="tool_error",
        ).value
        == 1
    )


async def test_a_transport_failure_is_counted_separately(
    metric_reader: InMemoryMetricReader,
) -> None:
    async def explodes(name: str, arguments: dict[str, Any]) -> Any:
        raise ConnectionResetError("socket went away")

    with pytest.raises(ConnectionResetError):
        await _transport(explodes).call_tool("lithos_stats", {})

    assert (
        metric_value(
            metric_reader,
            "lens_lithos_tool_calls_total",
            tool="lithos_stats",
            outcome="transport_error",
        ).value
        == 1
    )


async def test_time_queued_at_the_call_gate_is_measured(
    metric_reader: InMemoryMetricReader,
) -> None:
    """The signal that does not exist today in any form.

    Queue time is folded into the call deadline, so a saturated gate and a slow
    Lithos are indistinguishable from outside -- both are just slow pages. With
    a gate of one and a slow first call, the second call's wait must show up as
    time spent queued, not merely as time spent calling.
    """
    release = asyncio.Event()

    async def slow(name: str, arguments: dict[str, Any]) -> Any:
        await release.wait()
        return _Result()

    transport = _transport(slow, max_concurrent_calls=1)
    first = asyncio.create_task(transport.call_tool("lithos_read", {}))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(transport.call_tool("lithos_read", {}))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(first, second)

    (queued,) = metric_points(metric_reader, "lens_lithos_call_queue_wait_seconds")
    assert queued.count == 2
    # One call walked straight through, the other waited behind it.
    assert queued.max > 0.01


async def test_a_call_shed_while_still_queued_is_measured(
    metric_reader: InMemoryMetricReader,
) -> None:
    """The saturation case, which recording only after a successful acquire
    silently omitted.

    The outer deadline spans the queue, so under hard saturation a caller is
    shed while STILL WAITING and never reaches the body. Recording only on
    acquisition therefore left the histogram quiet at exactly the moment
    queueing was doing all the damage -- inverting the signal's purpose.

    The waiting call is cancelled directly rather than left to time out.
    ``call_timeout_s`` is per-TRANSPORT, so a deadline short enough to shed the
    queued call also sheds the one holding the gate, which then releases it and
    lets the "queued" call straight through -- a test that would pass against
    the bug about half the time. Cancellation exercises the same
    ``except BaseException`` path around the acquire, deterministically.
    """
    hold = asyncio.Event()

    async def slow(name: str, arguments: dict[str, Any]) -> Any:
        await hold.wait()
        return _Result()

    transport = _transport(slow, max_concurrent_calls=1, call_timeout_s=30)
    holder = asyncio.create_task(transport.call_tool("lithos_read", {}))
    await asyncio.sleep(0.02)

    queued = asyncio.create_task(transport.call_tool("lithos_read", {}))
    await asyncio.sleep(0.05)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    shed = [
        point
        for point in metric_points(metric_reader, "lens_lithos_call_queue_wait_seconds")
        if dict(point.attributes or {}) == {"acquired": "false"}
    ]
    assert shed and shed[0].count == 1, "the shed call recorded no queue wait"
    assert shed[0].sum > 0.01, "the recorded wait is not the wait that happened"

    hold.set()
    await holder


async def test_a_cancelled_call_is_not_counted_as_a_success(
    metric_reader: InMemoryMetricReader,
) -> None:
    """`CancelledError` inherits BaseException, so it slips past
    `except Exception` and the finally block would record `outcome="ok"`.

    A browser disconnect or a sibling task being torn down would then inflate
    the success rate -- the metric reading healthiest exactly when Lens is
    dropping the most work.
    """
    gate = asyncio.Event()

    async def blocks(name: str, arguments: dict[str, Any]) -> Any:
        await gate.wait()
        return _Result()

    task = asyncio.create_task(_transport(blocks).call_tool("lithos_stats", {}))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    outcomes = {
        dict(point.attributes or {}).get("outcome")
        for point in metric_points(metric_reader, "lens_lithos_tool_calls_total")
    }
    assert outcomes == {"cancelled"}


async def test_the_mcp_session_gauge_starts_at_zero(
    metric_reader: InMemoryMetricReader,
) -> None:
    """Seeded before the first connect attempt.

    A gauge that only appears on the first transition is ABSENT during the
    outage an operator is most likely to be investigating, and absent reads as
    "not deployed" rather than "down".
    """

    async def ok(name: str, arguments: dict[str, Any]) -> Any:
        return _Result()

    _transport(ok)

    assert metric_value(metric_reader, "lens_lithos_session_up").value == 0


# ── Event hub ─────────────────────────────────────────────────────────


def _hub() -> EventHub:
    return EventHub(EventsConfig(enabled=False), LithosConfig())


def _event(event_type: str = "task.created") -> LensEvent:
    return LensEvent(id="e1", type=event_type, task_id="t1")


async def test_published_and_delivered_are_counted_separately(
    metric_reader: InMemoryMetricReader,
) -> None:
    """Their ratio is the mean fan-out, which is what decides whether the
    subscriber ceiling is close to mattering."""
    hub = _hub()
    hub.subscribe()
    hub.subscribe()

    await hub.publish(_event())

    assert (
        metric_value(
            metric_reader, "lens_events_published_total", type="task.created"
        ).value
        == 1
    )
    assert metric_value(metric_reader, "lens_events_delivered_total").value == 2


async def test_a_full_subscriber_queue_is_counted_not_only_rate_limited(
    metric_reader: InMemoryMetricReader,
) -> None:
    """The counter is the point of this whole layer.

    `RateLimitedWarning` is right for the log -- an upstream misbehaving must
    not choose how fast Lens writes to the operator's log -- and it costs the
    RATE: `occurrences` is a running total, not something to graph or alert on.
    Ten drops must therefore be ten increments, even though the log saw one
    line.
    """
    hub = _hub()
    hub.subscribe(maxsize=1)

    for _ in range(11):
        await hub.publish(_event())

    dropped = metric_value(
        metric_reader, "lens_events_dropped_total", reason="subscriber_queue_full"
    )
    assert dropped.value == 10  # the first one fit in the queue


async def test_refusing_a_subscriber_past_the_ceiling_is_counted(
    metric_reader: InMemoryMetricReader,
) -> None:
    hub = _hub()
    for _ in range(MAX_EVENT_SUBSCRIBERS):
        hub.subscribe()

    with pytest.raises(EventSubscriberLimit):
        hub.subscribe()

    assert (
        metric_value(
            metric_reader, "lens_events_dropped_total", reason="subscriber_limit"
        ).value
        == 1
    )


async def test_the_subscriber_gauge_tracks_the_real_count_in_both_directions(
    metric_reader: InMemoryMetricReader,
) -> None:
    """Set from `len(self._subscribers)`, not incremented, so it cannot drift
    after a missed unsubscribe -- which is the condition an operator would be
    reading this gauge to diagnose."""
    hub = _hub()
    first = hub.subscribe()
    hub.subscribe()
    hub.unsubscribe(first)

    assert metric_value(metric_reader, "lens_event_subscribers").value == 1


async def test_the_event_stream_gauge_tracks_a_different_connection(
    metric_reader: InMemoryMetricReader,
) -> None:
    """Lens holds TWO connections to Lithos and they can disagree.

    `lens_lithos_session_up` is the MCP tool session; this is the `/events`
    stream behind the `events` health field. Tool calls healthy while event
    delivery reconnects presents to an operator as a board that renders but
    never updates -- invisible if only one of the two is graphed.
    """
    hub = _hub()
    assert metric_value(metric_reader, "lens_event_stream_up").value == 0

    hub._set_status("live")
    assert metric_value(metric_reader, "lens_event_stream_up").value == 1

    hub._set_status("reconnecting")
    assert metric_value(metric_reader, "lens_event_stream_up").value == 0


async def test_an_event_without_a_task_id_is_counted_as_dropped(
    metric_reader: InMemoryMetricReader,
) -> None:
    """Task-scoped types carry a task id or they are dropped; the counter is
    what makes a rise in that visible as a rate.

    The other two out-of-hub reasons (`oversized_frame`,
    `content_encoding_refused`) live inside the SSE reader and are asserted in
    `test_tasks_sse.py`, where the stream that drives them already exists.
    """
    from lithos_lens.events import normalize_lithos_event

    assert (
        normalize_lithos_event(event_id="e1", event_type="task.created", payload={})
        is None
    )

    assert (
        metric_value(
            metric_reader, "lens_events_dropped_total", reason="no_task_id"
        ).value
        == 1
    )


# ── Admission control ─────────────────────────────────────────────────


def test_admitted_and_refused_requests_are_counted(
    metric_reader: InMemoryMetricReader, lithos_lens_config_env: Any
) -> None:
    """Saturation is visible today only as users receiving 503s, which is the
    last moment anyone wants to learn about it."""
    from fastapi.testclient import TestClient

    from lithos_lens.config import load_config
    from lithos_lens.fake_lithos import FakeLithosClient
    from lithos_lens.web import create_app

    app = create_app(
        load_config(lithos_lens_config_env),
        lithos_client_factory=lambda _: FakeLithosClient(),
    )
    with TestClient(app) as client:
        assert client.get("/tasks").status_code == 200

    assert (
        metric_value(
            metric_reader, "lens_render_admissions_total", outcome="admitted"
        ).value
        >= 1
    )


def test_a_refused_request_is_counted_as_refused(
    metric_reader: InMemoryMetricReader, lithos_lens_config_env: Any
) -> None:
    """The branch that matters, which the admitted case does not reach.

    Saturation is visible today only as users receiving 503s. A counter that is
    only ever incremented on the happy path would report full health right up
    to the moment the board stops answering.
    """
    from fastapi.testclient import TestClient

    from lithos_lens import web as web_module
    from lithos_lens.config import load_config
    from lithos_lens.fake_lithos import FakeLithosClient

    # A ceiling of zero: the gate is locked before any request arrives, so the
    # refusal path is taken deterministically rather than by racing the server.
    original = web_module.MAX_CONCURRENT_RENDERS
    web_module.MAX_CONCURRENT_RENDERS = 0
    try:
        app = web_module.create_app(
            load_config(lithos_lens_config_env),
            lithos_client_factory=lambda _: FakeLithosClient(),
        )
        with TestClient(app) as client:
            assert client.get("/tasks").status_code == 503
    finally:
        web_module.MAX_CONCURRENT_RENDERS = original

    assert (
        metric_value(
            metric_reader, "lens_render_admissions_total", outcome="refused"
        ).value
        == 1
    )


# ── cardinality ───────────────────────────────────────────────────────


async def test_a_caller_chosen_event_type_cannot_mint_a_series(
    metric_reader: InMemoryMetricReader,
) -> None:
    """`publish` is public and normalization is not on all of its paths.

    Fake-Lithos app mode exposes `POST /tasks/events/publish`, which builds a
    LensEvent straight from request JSON, and fake mode can be run with an OTLP
    endpoint configured. Taking `event.type` on trust would therefore let an
    arbitrary REQUEST mint an arbitrary Prometheus series -- unbounded
    cardinality reachable from outside the process. Driven through the public
    hub surface, which is the surface that route uses.
    """
    hub = _hub()
    hub.subscribe()

    await hub.publish(LensEvent(id="x", type="caller-chosen-type-12345", task_id="t1"))

    assert (
        metric_value(metric_reader, "lens_events_published_total", type="other").value
        == 1
    )
    assert not [
        point
        for point in metric_points(metric_reader, "lens_events_published_total")
        if "caller-chosen" in str(dict(point.attributes or {}).get("type", ""))
    ]


async def test_lens_synthesized_events_keep_their_own_label(
    metric_reader: InMemoryMetricReader,
) -> None:
    """`lens.refresh` is Lens's own synthetic type and is not in the upstream
    allowlist, so a naive allowlist check would bucket it as `other` and hide
    the refresh rate behind whatever else landed there."""
    hub = _hub()
    hub.subscribe()

    await hub.publish(LensEvent(id="r", type=LENS_REFRESH_EVENT, task_id=""))

    assert (
        metric_value(
            metric_reader, "lens_events_published_total", type=LENS_REFRESH_EVENT
        ).value
        == 1
    )


async def test_no_metric_label_carries_an_unbounded_value(
    metric_reader: InMemoryMetricReader,
) -> None:
    """The rule the catalogue states, enforced rather than trusted.

    Tempo's metrics generator turns these into Prometheus series, so a task id
    or a note id in a label mints one series per entity. Every label emitted
    must come from a bounded set -- and the adversarial values below are the
    ones a caller could actually choose.
    """

    async def ok(name: str, arguments: dict[str, Any]) -> Any:
        return _Result()

    await _transport(ok).call_tool("lithos_task_get", {"task_id": "unbounded-id-42"})
    hub = _hub()
    hub.subscribe()
    await hub.publish(
        LensEvent(id="ev-9", type="attacker-chosen-99", task_id="task-77")
    )

    allowed = {
        "tool": {"lithos_task_get"},
        "outcome": {
            "ok",
            "timeout",
            "tool_error",
            "transport_error",
            "cancelled",
            "admitted",
            "refused",
        },
        "type": {"other"},
        "acquired": {"true", "false"},
        "reason": set(),
    }
    data = metric_reader.get_metrics_data()
    assert data is not None
    seen = 0
    for resource_metric in data.resource_metrics or []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                for point in metric.data.data_points:
                    for key, value in dict(point.attributes or {}).items():
                        seen += 1
                        assert key in allowed, f"{metric.name}: unexpected label {key}"
                        assert value in allowed[key], (
                            f"{metric.name}: label {key}={value!r} is not from a "
                            "bounded set -- ids and free text belong on spans"
                        )
    assert seen, "no labelled points were recorded; the assertion proved nothing"
