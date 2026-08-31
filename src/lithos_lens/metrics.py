"""Named metric instruments, one function per instrument.

Same shape as ``influx.metrics``: call sites do ``metrics.lithos_tool_calls()
.add(1, {...})`` rather than constructing instruments themselves, so every
name, unit and label set is declared once and reviewable in one file. The
meter caches by name, so repeated calls return the same instrument.

**Names are Prometheus-native** -- snake_case, ``_total`` on counters,
``_seconds`` on durations -- rather than the dotted OTEL style ``lithos`` uses.
The collector's Prometheus exporter is what the shared dashboards read, and
dotted names arrive mangled: ``lithos.knowledge.write_duration_ms`` carrying
``unit="ms"`` renders as ``otel_lithos_knowledge_write_duration_ms_milliseconds``
because the unit is appended to a name that already stated it. Naming natively
avoids that. Span names stay dotted (``lens.lithos.call_tool``) -- both sibling
services agree there, and Tempo does not mangle them.

**Cardinality rule.** Every label here is a bounded set: a tool name from
Lens's own client surface, an outcome enum, an event type from Lens's
allowlist. Never a task id, note id, search query or raw path -- Tempo's
metrics generator turns these into Prometheus series, and one series per note
id degrades Prometheus rather than informing anyone. Unbounded values belong on
spans, which are stored per-trace and never become series.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from opentelemetry import metrics as _metrics_api

from lithos_lens.telemetry import get_meter

# Instruments, cached per meter provider.
#
# Not an optimization. Every function below is called on the hot path -- once
# per event delivered, once per Lithos call -- and `get_meter()` resolves
# through the global provider each time. Two consequences without a cache:
# the SDK re-validates an instrument name per delivered event, and, once the
# provider has been shut down, EVERY call logs "A shutdown MeterProvider can
# not provide a Meter". That last one is the failure this cache exists for: it
# turns process teardown, or any post-shutdown activity, into one log line per
# event -- an unbounded write at exactly the moment nothing is watching, which
# is the shape the event hub's rate limiting exists to prevent elsewhere.
#
# Keyed on provider IDENTITY rather than a boolean "already built" flag, so a
# test that installs a fresh provider gets fresh instruments instead of ones
# wired to the previous test's reader. A stale-instrument cache would fail
# silently, which is the exact trap documented in Lithos task 41de9716.
_provider: Any = None
_instruments: dict[str, Any] = {}


def _instrument(name: str, build: Callable[[Any], Any]) -> Any:
    """The cached instrument for ``name``, rebuilt when the provider changes."""
    global _provider, _instruments

    provider = _metrics_api.get_meter_provider()
    if provider is not _provider:
        _provider = provider
        _instruments = {}
    cached = _instruments.get(name)
    if cached is None:
        cached = build(get_meter())
        _instruments[name] = cached
    return cached


# ── Lithos transport ──────────────────────────────────────────────────


def lithos_tool_calls() -> Any:
    """Counter of Lithos MCP tool calls, by tool and terminal outcome.

    Labels: ``tool`` (the MCP tool name -- bounded by Lens's own client
    surface, roughly twenty), ``outcome`` in ``ok`` | ``timeout`` |
    ``tool_error`` | ``transport_error``.

    ``timeout`` is broken out from the other failures on purpose: it is the one
    outcome that means Lens gave up rather than Lithos answering, so a rise in
    it points at the deadline or at load, not at the tool.
    """
    return _instrument(
        "lens_lithos_tool_calls_total",
        lambda meter: meter.create_counter(
            "lens_lithos_tool_calls_total",
            description="Lithos MCP tool calls by tool and terminal outcome.",
        ),
    )


def lithos_tool_duration() -> Any:
    """Histogram of Lithos tool-call latency in seconds, including queue time.

    Labels: ``tool``.

    Queue time is inside the measurement for the same reason it is inside the
    deadline (see ``MCPTransport.call_tool``): a queued call has not answered
    yet, and a caller waiting behind the gate is waiting. Use
    ``lens_lithos_call_queue_wait_seconds`` to tell the two halves apart.
    """
    return _instrument(
        "lens_lithos_tool_duration_seconds",
        lambda meter: meter.create_histogram(
            "lens_lithos_tool_duration_seconds",
            unit="s",
            description=(
                "Lithos tool-call latency including time queued at the call gate."
            ),
        ),
    )


def lithos_call_queue_wait() -> Any:
    """Histogram of seconds spent blocked on the process-wide call gate.

    No labels.

    This is the signal that does not exist today in any form. Queue time is
    folded into the call deadline, so a saturated gate and a slow Lithos look
    identical from outside -- both present as slow pages. Separating them is
    the difference between "Lithos is struggling" and "Lens is admitting more
    concurrent work than it can pass through".
    """
    return _instrument(
        "lens_lithos_call_queue_wait_seconds",
        lambda meter: meter.create_histogram(
            "lens_lithos_call_queue_wait_seconds",
            unit="s",
            description="Seconds a Lithos tool call spent waiting at the call gate.",
        ),
    )


def lithos_session_up() -> Any:
    """Gauge: 1 while the MCP session is established, 0 while it is not.

    No labels.

    A SYNCHRONOUS gauge set from the authoritative value at each transition,
    not an observable gauge with a callback. The callback form needs a
    registration guard and module state to survive being installed twice (the
    shape `lithos.telemetry.register_sse_active_clients_observer` has); setting
    an exact value at the two places the state actually changes needs neither,
    and cannot drift the way an incremented counter would.

    This is what turns the `events: "live" | "reconnecting"` field -- readable
    today only by fetching /health by hand -- into something graphable and
    alertable.
    """
    return _instrument(
        "lens_lithos_session_up",
        lambda meter: meter.create_gauge(
            "lens_lithos_session_up",
            description="1 while the Lithos MCP session is established, 0 otherwise.",
        ),
    )


def lithos_reconnects() -> Any:
    """Counter of MCP session losses that began a reconnect.

    No labels.

    Pairs with ``lens_lithos_session_up``: the gauge says whether Lens is
    connected now, this says how hard it has been working to stay that way. A
    session that is up but reconnecting every minute reads as healthy on the
    gauge alone.
    """
    return _instrument(
        "lens_lithos_reconnects_total",
        lambda meter: meter.create_counter(
            "lens_lithos_reconnects_total",
            description="MCP session losses that started a reconnect.",
        ),
    )


# ── Event hub ─────────────────────────────────────────────────────────


def events_published() -> Any:
    """Counter of events accepted from upstream and fanned out.

    Labels: ``type`` -- safe as a label only because ``normalize_event`` drops
    anything outside ``CONSUMED_EVENT_TYPES``, so the value comes from Lens's
    own allowlist rather than from whatever upstream decides to emit.
    """
    return _instrument(
        "lens_events_published_total",
        lambda meter: meter.create_counter(
            "lens_events_published_total",
            description="Events accepted from upstream and fanned out to subscribers.",
        ),
    )


def events_delivered() -> Any:
    """Counter of per-subscriber event deliveries.

    No labels. Divided by ``lens_events_published_total`` this gives the mean
    fan-out, which is the number that decides whether the subscriber ceiling is
    close to mattering.
    """
    return _instrument(
        "lens_events_delivered_total",
        lambda meter: meter.create_counter(
            "lens_events_delivered_total",
            description="Event deliveries to individual subscriber queues.",
        ),
    )


def events_dropped() -> Any:
    """Counter of events Lens refused or discarded, by reason.

    Labels: ``reason`` in ``no_task_id`` | ``oversized_frame`` |
    ``subscriber_queue_full`` | ``content_encoding_refused`` |
    ``subscriber_limit``.

    These are the same five conditions ``RateLimitedWarning`` reports to the
    log, and the counter exists because that rate limiting -- correct for logs,
    since an upstream misbehaving must not choose how fast Lens writes to the
    operator's log -- necessarily discards the rate. ``occurrences`` and
    ``suppressed_since_last`` are running totals, not something graphable or
    alertable. A counter has no such constraint: it is cheap per occurrence.
    The log line keeps the human-readable detail; this carries the rate.
    """
    return _instrument(
        "lens_events_dropped_total",
        lambda meter: meter.create_counter(
            "lens_events_dropped_total",
            description="Events refused or discarded by Lens, by reason.",
        ),
    )


def event_subscribers() -> Any:
    """Gauge: SSE subscribers currently attached to the hub.

    No labels. Set from ``len(self._subscribers)`` -- the authoritative value --
    rather than incremented, so it cannot drift out of step with reality after
    a missed unsubscribe.

    Graphed against ``MAX_EVENT_SUBSCRIBERS`` this shows headroom before the
    hub starts answering 503.
    """
    return _instrument(
        "lens_event_subscribers",
        lambda meter: meter.create_gauge(
            "lens_event_subscribers",
            description="SSE subscribers currently attached to the event hub.",
        ),
    )


# ── Saturation ────────────────────────────────────────────────────────


def render_admissions() -> Any:
    """Counter of metered requests by admission outcome.

    Labels: ``outcome`` in ``admitted`` | ``refused``.

    Refusals are observable today only as users receiving 503s. Graphed against
    ``MAX_CONCURRENT_RENDERS`` this shows how much headroom remains before that
    starts happening.
    """
    return _instrument(
        "lens_render_admissions_total",
        lambda meter: meter.create_counter(
            "lens_render_admissions_total",
            description="Metered requests by admission-control outcome.",
        ),
    )
