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
from opentelemetry.metrics import Observation

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


# Observable-gauge sources, keyed by metric name.
#
# Gauges here are OBSERVABLE (callback at collection) rather than synchronous
# (`.set()` at each transition), and the difference is not stylistic. A
# synchronous gauge reports only a value set SINCE THE LAST COLLECTION, so one
# whose value changes at transitions stops being exported once the transitions
# stop: the collector expires the series, and a healthy Lens that has simply
# been connected for five minutes reads as ABSENT. Absent is indistinguishable
# from not deployed, which is precisely the confusion these gauges were seeded
# to prevent. Measured against the live stack: `lens_lithos_session_up` vanished
# roughly five minutes after connecting while the process stayed up and its
# counters kept exporting.
#
# The indirection through this dict rather than a closure over `read` is what
# makes RE-registration work. `_instrument` caches by name, so a second
# transport or hub in the same process would not rebuild the gauge, and a
# closure would leave the FIRST instance's callback installed -- reporting a
# dead object's state forever. Last registration wins instead.
_observers: dict[str, Callable[[], float]] = {}


def _observable(name: str, description: str, read: Callable[[], float]) -> None:
    """Register ``read`` as the source of the observable gauge ``name``."""

    _observers[name] = read

    def observe(_options: Any) -> list[Observation]:
        source = _observers.get(name)
        return [] if source is None else [Observation(source())]

    _instrument(
        name,
        lambda meter: meter.create_observable_gauge(
            name, callbacks=[observe], description=description
        ),
    )


# ── Lithos transport ──────────────────────────────────────────────────


def lithos_tool_calls() -> Any:
    """Counter of Lithos MCP tool calls, by tool and terminal outcome.

    Labels: ``tool`` (the MCP tool name -- bounded by Lens's own client
    surface, roughly twenty), ``outcome`` in ``ok`` | ``timeout`` |
    ``tool_error`` | ``transport_error`` | ``cancelled``.

    The failures are kept apart because they call for different responses.
    ``timeout`` means LENS gave up, so a rise points at the deadline or at
    load rather than at the tool. ``tool_error`` means Lithos answered "no".
    ``transport_error`` means the socket failed. ``cancelled`` means the caller
    went away -- a browser disconnect, or a sibling task torn down -- and it
    exists because ``CancelledError`` inherits ``BaseException``: without its
    own clause it lands in the success bucket, and the success rate then reads
    highest exactly when Lens is dropping the most work.
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

    Labels: ``acquired`` in ``true`` | ``false``.

    This is the signal that does not exist today in any form. Queue time is
    folded into the call deadline, so a saturated gate and a slow Lithos look
    identical from outside -- both present as slow pages. Separating them is
    the difference between "Lithos is struggling" and "Lens is admitting more
    concurrent work than it can pass through".

    ``acquired`` is what keeps that true under load. The deadline SPANS the
    queue, so a caller can be shed while still waiting and never reach the
    body; recording only on a successful acquire would leave this histogram
    quiet at exactly the moment queueing was doing all the damage. ``false``
    is the waiting time of a call that was shed rather than served.
    """
    return _instrument(
        "lens_lithos_call_queue_wait_seconds",
        lambda meter: meter.create_histogram(
            "lens_lithos_call_queue_wait_seconds",
            unit="s",
            description="Seconds a Lithos tool call spent waiting at the call gate.",
        ),
    )


def register_lithos_session_up(read: Callable[[], float]) -> None:
    """Gauge: 1 while the MCP session is established, 0 while it is not.

    No labels. ``read`` is called at every metric collection, so it must be
    cheap and synchronous -- an attribute read, not a lock or a round trip.

    This is the MCP TOOL session (`MCPTransport`), and it is not the event
    stream. Lens holds two independent connections to Lithos -- MCP-over-SSE
    for tool calls, and the `/events` SSE stream for the live board -- and they
    can disagree: tool calls can be healthy while event delivery is
    reconnecting, which presents to an operator as a board that renders but
    never updates. `lens_event_stream_up` is the one that tracks the `events`
    health field; reading this gauge for that would answer the wrong question.

    OBSERVABLE rather than synchronous, and registered rather than set. An
    earlier version set an exact value at each transition, on the reasoning
    that a callback needs a registration guard and module state while a `.set()`
    needs neither. That reasoning was wrong in the way that matters: a
    synchronous gauge only reports a value set since the last collection, so a
    session that came up and then simply STAYED up stopped being exported, and
    the series expired out of Prometheus while the process was perfectly
    healthy. Seeding to 0 does distinguish "never connected" from "not
    deployed" at startup, but only until the first collection after the last
    transition -- which is not when anyone is looking. The callback holds the
    real invariant: the gauge is whatever the transport's state says it is,
    at the moment the question is asked.
    """
    _observable(
        "lens_lithos_session_up",
        "1 while the Lithos MCP session is established, 0 otherwise.",
        read,
    )


def register_event_stream_up(read: Callable[[], float]) -> None:
    """Gauge: 1 while the Lithos `/events` SSE stream is live, 0 otherwise.

    No labels. This is the connection behind the `events: live | reconnecting |
    disabled` health field, and it is a DIFFERENT connection from the MCP tool
    session `lens_lithos_session_up` tracks. Both are needed: a board that
    renders correctly but stops updating is the two disagreeing.

    `disabled` and `reconnecting` both read 0 -- the operator question this
    answers is "are events flowing", and neither state answers yes. Which of
    the two it is stays in the health endpoint and the log.

    Observable for the reason given on `register_lithos_session_up`: a stream
    that stays live stops emitting transitions, and a synchronous gauge stops
    being exported with them.
    """
    _observable(
        "lens_event_stream_up",
        "1 while the Lithos /events SSE stream is live, 0 otherwise.",
        read,
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

    Labels: ``type``, mapped through ``METRIC_EVENT_TYPES`` at the recording
    site -- anything else becomes ``other``.

    Mapped rather than trusted. ``normalize_event`` does allowlist upstream
    types, but ``publish`` has other callers: fake-Lithos app mode exposes
    ``POST /tasks/events/publish``, which builds a ``LensEvent`` straight from
    request JSON and never passes through normalization. Since fake mode can be
    run with an OTLP endpoint configured, taking ``event.type`` on trust would
    let an arbitrary request mint an arbitrary Prometheus series -- unbounded
    cardinality reachable from outside the process, which is the failure this
    module's cardinality rule exists to prevent.
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


def register_event_subscribers(read: Callable[[], float]) -> None:
    """Gauge: SSE subscribers currently attached to the hub.

    No labels. ``read`` returns the authoritative ``len(subscribers)`` at
    collection time rather than a running tally, so the gauge cannot drift out
    of step with reality after a missed unsubscribe -- and a missed unsubscribe
    is exactly the condition an operator would be reading this to diagnose.

    Graphed against ``MAX_EVENT_SUBSCRIBERS`` this shows headroom before the
    hub starts answering 503. Observable for the reason given on
    `register_lithos_session_up`: a stable subscriber count is still a fact
    worth exporting, and a synchronous gauge exports only changes.
    """
    _observable(
        "lens_event_subscribers",
        "SSE subscribers currently attached to the event hub.",
        read,
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


# ── Knowledge surface (K1 PRD, task cdce170a) ─────────────────────────


def knowledge_note_renders() -> Any:
    """Counter of note-page renders by terminal outcome.

    Labels: ``outcome`` in ``rendered`` | ``not_found`` | ``error`` |
    ``offline``.

    ``not_found`` is separated from ``error`` because they are different
    problems wearing the same 200: a dead wiki-link someone should fix, versus
    Lithos failing to answer. The page looks similar either way, so the log and
    this counter are the only places the difference survives.
    """
    return _instrument(
        "lens_knowledge_note_renders_total",
        lambda meter: meter.create_counter(
            "lens_knowledge_note_renders_total",
            description="Knowledge note renders by terminal outcome.",
        ),
    )


def knowledge_related_duration() -> Any:
    """Histogram of seconds spent loading a note's related panel.

    No labels.

    The panel is the expensive half of a note render -- one `lithos_related`
    call plus a bounded `lithos_read` fan-out -- so this is where a slow note
    page is explained. Separate from the request span's own duration, which
    cannot say which half was slow.
    """
    return _instrument(
        "lens_knowledge_related_duration_seconds",
        lambda meter: meter.create_histogram(
            "lens_knowledge_related_duration_seconds",
            unit="s",
            description="Seconds spent loading a note's related panel.",
        ),
    )


def knowledge_related_fanout() -> Any:
    """Histogram of `lithos_read` calls spent resolving related-panel titles.

    No labels.

    Distribution rather than a total, because the question it answers is
    whether `related_title_fanout_cap` is set anywhere near reality. A p95 that
    sits ON the cap means notes are being silently truncated; one far below it
    means the cap is costing nothing and the latency is elsewhere.
    """
    return _instrument(
        "lens_knowledge_related_fanout",
        lambda meter: meter.create_histogram(
            "lens_knowledge_related_fanout",
            description="Backend reads spent resolving related-panel titles.",
        ),
    )


def knowledge_searches() -> Any:
    """Counter of `/knowledge` landing requests by branch.

    Labels: ``mode`` in ``search`` (hybrid `lithos_search`) | ``browse``
    (recency list, tagged or bare) | ``offline`` | ``error``.

    The raw query string is deliberately absent from this LABEL: one series per
    distinct search is unbounded cardinality driven by unauthenticated input,
    which is the failure the module's cardinality rule exists to prevent. Mode
    and count answer the operational question without it.

    It is NOT absent from the trace. The HTTP instrumentation records the
    request target, query string included, and that is kept -- bounded by
    `MAX_LOGGED_VALUE_CHARS` in `telemetry._bound_request_attributes`. On a
    span it costs no series and genuinely helps read a trace; the constraint
    there is volume, not cardinality.
    """
    return _instrument(
        "lens_knowledge_searches_total",
        lambda meter: meter.create_counter(
            "lens_knowledge_searches_total",
            description="Knowledge landing requests by branch.",
        ),
    )


def knowledge_resolves() -> Any:
    """Counter of wiki-link resolutions by the arm that decided.

    Labels: ``outcome`` in ``uuid`` | ``path`` | ``title`` | ``disambiguated``
    | ``unresolved`` | ``empty`` | ``offline`` -- `ResolveOutcome.via`, which
    exists so this distinction survives at all.

    `kind` alone would collapse the first three into `redirect`, and the
    difference is the whole signal: a corpus resolving entirely by `uuid` is
    working for a different reason than one resolving by `title`, and only the
    second is evidence the wiki-link convention is being used as intended. A
    rising `unresolved` share is the corpus growing dead links.
    """
    return _instrument(
        "lens_knowledge_resolves_total",
        lambda meter: meter.create_counter(
            "lens_knowledge_resolves_total",
            description="Wiki-link resolutions by the arm that decided.",
        ),
    )
