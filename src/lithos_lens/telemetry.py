"""OpenTelemetry setup for Lithos Lens.

Unlike the sibling services (``lithos``, ``influx``), the OTEL packages here are
**required** dependencies, not an optional ``otel`` extra. There is therefore no
``_HAS_OTEL`` import guard and there are no no-op stub classes: those exist in
the siblings only to survive a missing install, and half of what they buy is a
code path CI never runs. ``make check`` exercises the real SDK.

What ``config.telemetry.enabled`` still governs is **export**, never whether the
instrumentation is compiled in:

* enabled + an endpoint       -> OTLP/HTTP to the collector
* enabled + ``console_fallback`` -> spans and metrics to stdout
* enabled + neither           -> providers installed, nothing exported. Spans
  are still created, so ``trace_id`` reaches the log (see
  :class:`lithos_lens.logging.JsonFormatter`) and requests stay correlatable
  on a machine with no collector running.
* disabled                    -> the one escape hatch; no providers at all.

The endpoint is the base collector URL (e.g. ``http://localhost:4318``); the
per-signal ``/v1/traces``, ``/v1/metrics`` and ``/v1/logs`` paths are derived,
and the standard ``OTEL_EXPORTER_OTLP_*_ENDPOINT`` variables override.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

from lithos_lens.logging import MAX_LOGGED_VALUE_CHARS

if TYPE_CHECKING:
    from fastapi import FastAPI

    from lithos_lens.config import LithosLensConfig

logger = logging.getLogger(__name__)

# Instrumentation scope name for every tracer and meter Lens creates. One scope
# keeps the emitted `otel_scope_name` label stable across modules.
INSTRUMENTATION_SCOPE = "lithos_lens"

# Request paths kept OUT of the trace, as a comma-joined regex list for
# `opentelemetry-instrumentation-fastapi`.
#
# These coincide with web.py's `_UNMETERED_EXACT` / `_UNMETERED_PREFIXES` today,
# but the reasons differ and the lists are deliberately not derived from each
# other:
#
# * `/health` is polled by the container healthcheck AND by every page render
#   (health.refresh_interval_s, default 30s). Tracing it is constant volume
#   carrying no information.
# * `/tasks/events` is an SSE stream that lives as long as the browser tab. Its
#   span would stay open for hours and sit in every latency histogram, making
#   p95 meaningless. The stream is measured with its own counters and a
#   subscriber gauge instead.
# * `/static/` is served from disk with no Lithos call.
TRACE_EXCLUDED_URLS = "/health,/tasks/events,/static/"

_initialized = False
_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None
_logger_provider: Any = None
_log_handler: logging.Handler | None = None


def setup_telemetry(
    config: LithosLensConfig,
    *,
    _test_span_exporter: Any = None,
    _test_metric_reader: Any = None,
    _test_log_exporter: Any = None,
) -> None:
    """Install the tracer, meter and (when exporting) log providers.

    Idempotent: a second call while initialized is a no-op, so the uvicorn
    factory calling this per worker is safe.

    ``_test_span_exporter`` / ``_test_metric_reader`` / ``_test_log_exporter``
    are the test seams (``InMemorySpanExporter`` / ``InMemoryMetricReader`` /
    ``InMemoryLogRecordExporter``), letting the suite assert on real spans,
    instruments and log records with no collector running.
    """
    global _initialized, _tracer_provider, _meter_provider, _logger_provider
    global _log_handler

    if _initialized:
        return
    if not config.telemetry.enabled:
        logger.debug("telemetry disabled in config; no providers installed")
        return

    resource = Resource.create(
        {
            "service.name": config.telemetry.service_name,
            "service.version": _service_version(),
            "deployment.environment": config.environment,
        }
    )
    endpoint = _resolve_endpoint(config)

    _tracer_provider = TracerProvider(resource=resource)
    span_processor = _build_span_processor(
        endpoint, config, test_exporter=_test_span_exporter
    )
    if span_processor is not None:
        _tracer_provider.add_span_processor(span_processor)
    trace.set_tracer_provider(_tracer_provider)

    metric_reader = _build_metric_reader(
        endpoint, config, test_reader=_test_metric_reader
    )
    if metric_reader is not None:
        _meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader]
        )
        metrics.set_meter_provider(_meter_provider)

    _logger_provider, _log_handler = _install_log_export(
        endpoint,
        resource,
        test_exporter=_test_log_exporter,
        # A span or metric seam with no log seam means "keep this process off
        # the network": a test that names an endpoint would otherwise build a
        # real OTLP log exporter and pay its retry backoff on shutdown -- seven
        # seconds per test, for a pipeline the test is not looking at.
        offline=_test_log_exporter is None
        and (_test_span_exporter is not None or _test_metric_reader is not None),
    )

    _initialized = True
    # Report per SIGNAL, not just traces. The three pipelines are configured
    # independently (OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_ENDPOINT each
    # override the base), so a deployment exporting only metrics is exporting
    # -- and a single `otel_exporting` flag derived from the span processor
    # would tell its operator the opposite while the data flowed.
    exporting = [
        signal
        for signal, active in (
            ("traces", span_processor is not None),
            ("metrics", metric_reader is not None),
            ("logs", _logger_provider is not None),
        )
        if active
    ]
    logger.info(
        "telemetry initialized",
        extra={
            "lens_event": "lens.telemetry.initialized",
            "otel_endpoint": endpoint or "",
            "otel_environment": config.environment,
            "otel_exporting": bool(exporting),
            "otel_exporting_signals": exporting,
        },
    )


def shutdown_telemetry() -> None:
    """Flush and tear down every provider. **Terminal: process-exit only.**

    Clears everything this module owns -- providers flushed and dropped, the
    OTLP log handler detached from the root logger, ``_initialized`` cleared --
    so calling it is always safe, including when setup never ran.

    It does NOT restore the ability to start telemetry again, and no caller
    should assume it does. OTEL latches each global provider the first time it
    is set: after this returns, a second :func:`setup_telemetry` builds fresh
    providers, logs ``Overriding of current TracerProvider is not allowed``,
    and the SDK keeps the ORIGINAL ones -- so spans would go to the discarded
    provider's exporter and quietly never arrive. Releasing those latches means
    writing to ``opentelemetry``'s module globals, which is not something a
    service should do to a library at runtime.

    That is not a limitation in practice: the only caller is the ``atexit``
    hook registered by ``main.create_app_from_config``, and Lens has no
    in-process restart path. The test suite, which does need repeated setup,
    releases the latches itself -- see ``_release_otel_provider_latches`` in
    ``tests/test_telemetry.py``, and
    ``test_shutdown_alone_does_not_permit_a_working_restart``, which pins this
    contract so it cannot be misread as supported.
    """
    global _initialized, _tracer_provider, _meter_provider, _logger_provider
    global _log_handler

    for provider in (_tracer_provider, _meter_provider, _logger_provider):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:  # pragma: no cover - shutdown must not mask an exit
            logger.warning("telemetry provider shutdown failed", exc_info=True)
    if _log_handler is not None:
        logging.getLogger().removeHandler(_log_handler)
    _tracer_provider = None
    _meter_provider = None
    _logger_provider = None
    _log_handler = None
    _initialized = False


def get_tracer(name: str = INSTRUMENTATION_SCOPE) -> trace.Tracer:
    """A tracer. Before :func:`setup_telemetry` this is the API's no-op."""
    return trace.get_tracer(name)


def get_current_span() -> trace.Span:
    """The span the current request is already inside.

    Lens's knowledge routes each do ONE unit of work, so wrapping a handler in
    a child span would nest ~1:1 with the FastAPI server span and mint a second
    Prometheus series per route through Tempo's metrics generator — the same
    duplication removed in #71 for the ASGI `http send` children, for the same
    reason. The information belongs on the request's own span, where a trace
    reader is already looking. A child span earns its place when a handler
    grows a phase worth timing separately from the request.
    """
    return trace.get_current_span()


def get_meter(name: str = INSTRUMENTATION_SCOPE) -> metrics.Meter:
    """A meter. Before :func:`setup_telemetry` this is the API's no-op."""
    return metrics.get_meter(name)


def instrument_app(app: FastAPI) -> None:
    """Attach HTTP server and client instrumentation to ``app``.

    Auto-instrumentation rather than a hand-rolled middleware, for one reason
    that matters at this scale: **route-template cardinality**. Lens routes on
    ids (``/note/{knowledge_id}``, ``/tasks/{task_id}``), and Tempo's
    metrics-generator turns span names into Prometheus series. Naming a span
    after the concrete path would mint one series per note id. The
    instrumentation names spans and sets ``http.route`` from the route
    TEMPLATE, which is the bounded form.

    Also gives Lens the semantic-convention attributes
    (``http.request.method``, ``http.response.status_code``) that
    ``service-health.json`` and the service graph key on, so Lens appears there
    without a dashboard of its own.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        server_request_hook=_bound_request_attributes,
        excluded_urls=TRACE_EXCLUDED_URLS,
        # Drop the ASGI `http send` / `http receive` child spans. Verified
        # against the live stack: they made every request FOUR spans instead of
        # one, and Tempo's metrics generator turns each distinct span name into
        # its own Prometheus series -- so `GET /note/{knowledge_id} http send`
        # was minted alongside the route itself, multiplying both trace storage
        # and series count to record that an ASGI response went out in three
        # chunks. No operator question is answered by that.
        exclude_spans=["send", "receive"],
    )
    # httpx instrumentation is process-wide, not per-app, and re-instrumenting
    # warns and no-ops. The suite builds many apps in one process, so ask
    # first rather than emit a warning per app.
    httpx_instrumentor = HTTPXClientInstrumentor()
    if not httpx_instrumentor.is_instrumented_by_opentelemetry:
        httpx_instrumentor.instrument()


# Span attributes carrying the raw request target, which includes the query
# string. The instrumentation sets these from the ASGI scope.
_URL_ATTRIBUTES = ("http.target", "http.url", "url.full", "url.query")


def _bound_request_attributes(span: Any, scope: dict[str, Any]) -> None:
    """Apply the log-value ceiling to URL-bearing span attributes.

    Same reason `JsonFormatter` bounds them, on a path that did not inherit it.
    A query string is unbounded input on a route with no authentication, and
    the instrumentation copies the request target onto the span verbatim -- so
    the 47 KB query that `logging.py` describes writing a 47 KB log line was
    also shipping a 47 KB span attribute to the collector.

    Bounded rather than stripped: the query string is genuinely useful when
    reading a trace (which filters produced this dashboard render), and unlike
    a metric label it costs no series -- Tempo stores it per-trace. What it
    must not do is carry unbounded volume.
    """
    if span is None or not getattr(span, "is_recording", lambda: False)():
        return
    attributes = getattr(span, "attributes", None) or {}
    for name in _URL_ATTRIBUTES:
        value = attributes.get(name)
        if isinstance(value, str) and len(value) > MAX_LOGGED_VALUE_CHARS:
            span.set_attribute(name, _truncated_url(value))


def _truncated_url(value: str) -> str:
    return f"{value[:MAX_LOGGED_VALUE_CHARS]}…[truncated, {len(value)} chars]"


def _resolve_endpoint(config: LithosLensConfig) -> str:
    """The base OTLP collector URL, config first then the standard env var."""
    configured = config.telemetry.endpoint.strip()
    if configured:
        return configured
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()


def _signal_endpoint(base: str, signal: str) -> str:
    """Build the OTLP/HTTP endpoint for ``signal`` from a base collector URL."""
    base = base.rstrip("/")
    if base.endswith(f"/v1/{signal}"):
        return base
    return f"{base}/v1/{signal}"


def _signal_override(signal: str) -> str:
    """A per-signal endpoint from the standard env var, if set."""
    return os.environ.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT", "").strip()


def _build_span_processor(
    endpoint: str, config: LithosLensConfig, *, test_exporter: Any
) -> Any:
    """The span processor to install, or ``None`` to create-and-drop spans."""
    if test_exporter is not None:
        return SimpleSpanProcessor(test_exporter)

    traces_endpoint = _signal_override("traces") or (
        _signal_endpoint(endpoint, "traces") if endpoint else ""
    )
    if traces_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        return BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint))
    if config.telemetry.console_fallback:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return SimpleSpanProcessor(ConsoleSpanExporter())
    # No exporter: spans are still created and still carry ids, which is what
    # puts trace_id on every log line. Nothing leaves the process.
    return None


def _build_metric_reader(
    endpoint: str, config: LithosLensConfig, *, test_reader: Any
) -> Any:
    """The metric reader to install, or ``None`` for no metrics pipeline."""
    if test_reader is not None:
        return test_reader

    metrics_endpoint = _signal_override("metrics") or (
        _signal_endpoint(endpoint, "metrics") if endpoint else ""
    )
    if metrics_endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        return PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=metrics_endpoint),
            export_interval_millis=config.telemetry.export_interval_ms,
        )
    if config.telemetry.console_fallback:
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

        return PeriodicExportingMetricReader(
            ConsoleMetricExporter(),
            export_interval_millis=config.telemetry.export_interval_ms,
        )
    return None


def _install_log_export(
    endpoint: str,
    resource: Resource,
    *,
    test_exporter: Any = None,
    offline: bool = False,
) -> tuple[Any, logging.Handler | None]:
    """Export Python logs over OTLP so they reach Loki with the same resource.

    Additive: the JSON handler on stdout stays, so ``docker logs`` is unchanged
    whether or not a collector is reachable.
    """
    logs_endpoint = (
        ""
        if offline
        else (
            _signal_override("logs")
            or (_signal_endpoint(endpoint, "logs") if endpoint else "")
        )
    )
    if test_exporter is None and not logs_endpoint:
        return None, None

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        BatchLogRecordProcessor,
        SimpleLogRecordProcessor,
    )

    from lithos_lens.logging import BoundedRecordFilter

    provider = LoggerProvider(resource=resource)
    if test_exporter is not None:
        provider.add_log_record_processor(SimpleLogRecordProcessor(test_exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )

        provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=logs_endpoint))
        )
    set_logger_provider(provider)
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    # This handler takes no formatter, so `MAX_LOGGED_VALUE_CHARS` -- applied
    # centrally in JsonFormatter -- does not reach it. Without the filter an
    # oversized request line is exported to the collector verbatim.
    handler.addFilter(BoundedRecordFilter())
    logging.getLogger().addHandler(handler)
    return provider, handler


def _service_version() -> str:
    """Lens's version from package metadata, never hardcoded."""
    try:
        from importlib.metadata import version

        return version("lithos-lens")
    except Exception:
        return "unknown"
