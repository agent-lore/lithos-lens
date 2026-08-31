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
from lithos_lens.events import MAX_EVENT_SUBSCRIBERS, EventHub, LensEvent
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


# ── cardinality ───────────────────────────────────────────────────────


async def test_no_metric_label_carries_an_unbounded_value(
    metric_reader: InMemoryMetricReader,
) -> None:
    """The rule the catalog states, enforced rather than trusted.

    Tempo's metrics generator turns these into Prometheus series, so a task id
    or a note id in a label mints one series per entity. Every label emitted
    here must come from a bounded set: a tool name off Lens's own client
    surface, an outcome enum, or an event type off Lens's allowlist.
    """

    async def ok(name: str, arguments: dict[str, Any]) -> Any:
        return _Result()

    await _transport(ok).call_tool("lithos_task_get", {"task_id": "unbounded-id-42"})
    hub = _hub()
    hub.subscribe()
    await hub.publish(LensEvent(id="ev-9", type="task.created", task_id="task-77"))

    allowed = {
        "tool": {"lithos_task_get"},
        "outcome": {"ok", "timeout", "tool_error", "transport_error", "admitted"},
        "type": {"task.created"},
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


# ── the instrument cache ──────────────────────────────────────────────


def test_recording_after_shutdown_does_not_write_a_log_line_per_call(
    metric_reader: InMemoryMetricReader, caplog: pytest.LogCaptureFixture
) -> None:
    """Instruments are cached per provider, and this is why.

    Every metric here sits on a hot path -- one per event delivered, one per
    Lithos call. Resolving `get_meter()` on each of them means that once the
    provider is shut down, the SDK logs "A shutdown MeterProvider can not
    provide a Meter" EVERY time: an unbounded log write driven by upstream
    event rate, at exactly the moment nothing is watching. That is the shape
    the event hub's rate limiting exists to prevent elsewhere, reintroduced by
    the instrumentation meant to observe it.

    Caught by the existing SSE suite, which asserts exact `caplog` record
    counts and started seeing dozens of these.
    """
    from lithos_lens import metrics
    from lithos_lens.telemetry import shutdown_telemetry

    metrics.events_delivered().add(1)
    shutdown_telemetry()

    with caplog.at_level("WARNING"):
        for _ in range(25):
            metrics.events_delivered().add(1)

    noisy = [r for r in caplog.records if "shutdown" in r.getMessage().lower()]
    assert noisy == [], f"{len(noisy)} log lines from 25 post-shutdown records"


def test_a_new_provider_gets_new_instruments(
    lithos_lens_config_env: Any, telemetry_off: None
) -> None:
    """The cache is keyed on provider IDENTITY, not on a "built already" flag.

    A flag would leave the second reader empty while measurements kept landing
    in the first -- silently, which is precisely the trap filed against
    lithos.telemetry as task 41de9716.
    """
    from lithos_lens import metrics
    from lithos_lens.config import load_config
    from lithos_lens.telemetry import setup_telemetry, shutdown_telemetry
    from tests.conftest import _release_otel_provider_latches

    config = load_config(lithos_lens_config_env)

    first = InMemoryMetricReader()
    setup_telemetry(config, _test_metric_reader=first)
    metrics.events_delivered().add(1)

    shutdown_telemetry()
    _release_otel_provider_latches()

    second = InMemoryMetricReader()
    setup_telemetry(config, _test_metric_reader=second)
    metrics.events_delivered().add(1)

    assert metric_points(second, "lens_events_delivered_total"), (
        "the second reader saw nothing -- instruments are still bound to the "
        "first provider"
    )
