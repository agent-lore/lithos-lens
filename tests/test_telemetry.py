"""OpenTelemetry wiring: providers, server spans, and trace/log correlation.

Every test here runs against the REAL OTEL SDK. The packages are required
dependencies of lithos-lens, not an optional ``otel`` extra, so there is no
skip-if-absent guard and no code path CI leaves unrun — which is the point of
``test_the_otel_packages_are_required_not_optional`` below.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util.http import parse_excluded_urls

from lithos_lens.config import load_config
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.logging import JsonFormatter
from lithos_lens.telemetry import (
    TRACE_EXCLUDED_URLS,
    get_meter,
    get_tracer,
    setup_telemetry,
    shutdown_telemetry,
)
from lithos_lens.web import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
# A note the demo dataset serves, so /note/{id} takes its rendered path.
DEMO_NOTE_ID = "note-influx-plan"


def _release_otel_provider_latches() -> None:
    """Let a later `set_*_provider` take effect. Test-only.

    OTEL guards each global provider with a one-shot latch: a second
    `set_tracer_provider` logs a warning and is IGNORED. Without releasing
    them, every test after the first would assert against the FIRST test's
    exporter and pass for the wrong reason.

    The two signals keep their globals in DIFFERENT modules, and getting that
    wrong fails silently: the tracer globals live on `opentelemetry.trace`
    itself, but the meter globals live on `opentelemetry.metrics._internal` and
    are not re-exported, so `metrics._METER_PROVIDER = None` would just mint an
    unused attribute on the package and leave the real provider latched. Each
    name is READ before it is written, so a future OTEL layout change surfaces
    here as an AttributeError rather than as a stale-exporter assertion.

    This lives in the suite, not in `lithos_lens.telemetry`: resetting OTEL's
    package globals is a fact about testing OTEL, not about Lens. Lens's own
    state is cleared in full by the public `shutdown_telemetry`.
    """
    from opentelemetry import trace as trace_api
    from opentelemetry.metrics import _internal as metrics_internal
    from opentelemetry.util._once import Once

    for module, name in (
        (trace_api, "_TRACER_PROVIDER"),
        (metrics_internal, "_METER_PROVIDER"),
    ):
        getattr(module, name)
        setattr(module, name, None)
        getattr(module, f"{name}_SET_ONCE")
        setattr(module, f"{name}_SET_ONCE", Once())


@pytest.fixture
def telemetry_off() -> Iterator[None]:
    """Guarantee a clean provider state around a test that installs its own."""
    shutdown_telemetry()
    _release_otel_provider_latches()
    yield
    shutdown_telemetry()
    _release_otel_provider_latches()


@pytest.fixture
def spans(lithos_lens_config_env: Path, telemetry_off: None) -> InMemorySpanExporter:
    """Telemetry set up with an in-memory span exporter. No collector needed."""
    exporter = InMemorySpanExporter()
    setup_telemetry(load_config(lithos_lens_config_env), _test_span_exporter=exporter)
    return exporter


def _client(config_path: Path) -> TestClient:
    config = load_config(config_path)
    return TestClient(
        create_app(config, lithos_client_factory=lambda _: FakeLithosClient())
    )


def _attributes(span: ReadableSpan) -> Mapping[str, Any]:
    """A span's attributes, never None (the SDK types them as optional)."""
    return span.attributes or {}


def _server_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    """Finished spans from the HTTP server instrumentation only."""
    return [
        s for s in exporter.get_finished_spans() if _attributes(s).get("http.route")
    ]


# ── the non-optional posture ──────────────────────────────────────────


def test_the_otel_packages_are_required_not_optional() -> None:
    """The SDK is a hard dependency, so `make check` exercises the live path.

    Asserted against pyproject rather than by importing: an import test passes
    either way, since an extra that happens to be installed imports fine. What
    must not come back is the optional-extra posture the sibling services use,
    where the enabled path is only covered on machines that opted in.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    required = " ".join(pyproject["project"]["dependencies"])
    for package in (
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-http",
        "opentelemetry-instrumentation-fastapi",
        "opentelemetry-instrumentation-httpx",
    ):
        assert package in required, f"{package} must stay a required dependency"

    optional = pyproject["project"].get("optional-dependencies", {})
    assert "otel" not in optional, (
        "telemetry must not move behind an optional extra: an enabled-path "
        "test that skips when a package is absent is not a test"
    )


def test_telemetry_disabled_installs_no_provider(
    lithos_lens_config_env: Path, telemetry_off: None
) -> None:
    """`enabled = false` is the one escape hatch and it installs nothing."""
    from dataclasses import replace

    config = load_config(lithos_lens_config_env)
    config = replace(config, telemetry=replace(config.telemetry, enabled=False))
    setup_telemetry(config, _test_span_exporter=InMemorySpanExporter())

    with get_tracer().start_as_current_span("ignored"):
        assert not trace.get_current_span().get_span_context().is_valid


def test_spans_are_created_with_no_exporter_configured(
    lithos_lens_config_env: Path, telemetry_off: None
) -> None:
    """No endpoint is NOT no telemetry: ids still exist, so logs correlate.

    This is the default posture on a machine with no collector running. The
    provider is installed and spans get real ids; they simply go nowhere.
    """
    setup_telemetry(load_config(lithos_lens_config_env))

    with get_tracer().start_as_current_span("no-exporter"):
        assert trace.get_current_span().get_span_context().is_valid


# ── the reset seam itself ─────────────────────────────────────────────


def test_a_second_setup_reaches_the_second_exporter(
    lithos_lens_config_env: Path, telemetry_off: None
) -> None:
    """Guards the trap `_release_otel_provider_latches` exists for.

    OTEL latches each global provider once; a second `set_tracer_provider` is
    warned about and IGNORED. If the latch is not released — or is released on
    the wrong module, which fails silently — every test after the first would
    assert against the FIRST test's exporter and pass for the wrong reason.
    """
    config = load_config(lithos_lens_config_env)

    first = InMemorySpanExporter()
    setup_telemetry(config, _test_span_exporter=first)
    with get_tracer().start_as_current_span("first"):
        pass

    shutdown_telemetry()
    _release_otel_provider_latches()

    second = InMemorySpanExporter()
    setup_telemetry(config, _test_span_exporter=second)
    with get_tracer().start_as_current_span("second"):
        pass

    assert [s.name for s in first.get_finished_spans()] == ["first"]
    assert [s.name for s in second.get_finished_spans()] == ["second"]


def test_the_meter_is_readable_without_a_collector(
    lithos_lens_config_env: Path, telemetry_off: None
) -> None:
    """The metric seam PR 2 builds on: instruments read back in-process."""
    reader = InMemoryMetricReader()
    setup_telemetry(load_config(lithos_lens_config_env), _test_metric_reader=reader)

    get_meter().create_counter("lens_test_probe_total").add(3, {"outcome": "ok"})

    data = reader.get_metrics_data()
    assert data is not None
    points = [
        point
        for resource_metric in (data.resource_metrics or [])
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
        if metric.name == "lens_test_probe_total"
        for point in metric.data.data_points
        if isinstance(point, NumberDataPoint)
    ]
    assert [(p.value, (p.attributes or {})["outcome"]) for p in points] == [(3, "ok")]


# ── server spans ──────────────────────────────────────────────────────


def test_a_request_produces_a_server_span_carrying_the_resource(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    """A page render is traced, and the span carries the identity the shared
    dashboards key on: service.name, service.version, deployment.environment."""
    with _client(lithos_lens_config_env) as client:
        assert client.get(f"/note/{DEMO_NOTE_ID}").status_code == 200

    (span,) = _server_spans(spans)
    assert _attributes(span)["http.route"] == "/note/{knowledge_id}"
    resource = span.resource.attributes
    assert resource["service.name"] == "lithos-lens"
    assert resource["deployment.environment"] == "test"
    assert resource["service.version"] not in ("", "unknown")


def test_ids_in_the_path_never_reach_the_span_name(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    """Cardinality: Tempo's metrics generator turns span names into Prometheus
    series, so a name carrying a note id would mint one series per note. Three
    distinct ids must collapse to one name and one route."""
    with _client(lithos_lens_config_env) as client:
        for note_id in ("note-influx-plan", "note-influx-rollback", "no-such-note"):
            client.get(f"/note/{note_id}")

    observed = _server_spans(spans)
    assert len(observed) == 3
    assert {_attributes(s)["http.route"] for s in observed} == {"/note/{knowledge_id}"}
    assert len({s.name for s in observed}) == 1
    for span in observed:
        assert "note-influx" not in span.name
        for value in _attributes(span).values():
            assert value != "note-influx-rollback"


def test_health_and_static_are_not_traced(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    """/health is polled by the container healthcheck and by every page render;
    tracing it is constant volume carrying no information."""
    with _client(lithos_lens_config_env) as client:
        assert client.get("/health").status_code == 200
        client.get("/static/lens.css")

    assert _server_spans(spans) == []


def test_the_event_stream_is_excluded_from_tracing() -> None:
    """/tasks/events lives as long as the browser tab. Its span would stay open
    for hours and sit in every latency histogram, making p95 meaningless.

    Asserted through the instrumentation's own matcher rather than by opening
    the stream — a TestClient request to it would never return.
    """
    excluded = parse_excluded_urls(TRACE_EXCLUDED_URLS)
    assert excluded.url_disabled("/tasks/events")
    assert excluded.url_disabled("/health")
    assert excluded.url_disabled("/static/lens.css")
    # The pages the exclusions must NOT swallow.
    assert not excluded.url_disabled("/tasks")
    assert not excluded.url_disabled(f"/note/{DEMO_NOTE_ID}")


# ── trace <-> log correlation ─────────────────────────────────────────


def _formatted(record: logging.LogRecord) -> str:
    return JsonFormatter().format(record)


def _record() -> logging.LogRecord:
    return logging.LogRecord(
        name="lithos_lens.web",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="something happened",
        args=(),
        exc_info=None,
    )


def test_a_log_written_inside_a_span_carries_the_trace_id(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    """The Loki -> Tempo jump: a log line names the trace it came from."""
    setup_telemetry(load_config(lithos_lens_config_env))

    with get_tracer().start_as_current_span("correlated"):
        context = trace.get_current_span().get_span_context()
        payload = _formatted(_record())

    assert f'"trace_id":"{context.trace_id:032x}"' in payload
    assert f'"span_id":"{context.span_id:016x}"' in payload


def test_a_log_written_outside_a_span_omits_the_trace_fields(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    """Omitted, not zeroed: an absent field reads as "outside a request", where
    a zeroed one looks like a real trace that leads nowhere."""
    payload = _formatted(_record())

    assert "trace_id" not in payload
    assert "span_id" not in payload


def test_a_second_setup_reaches_the_second_metric_reader(
    lithos_lens_config_env: Path, telemetry_off: None
) -> None:
    """The meter half of the latch guard, and the reason it is separate.

    The two signals keep their globals in DIFFERENT modules -- tracer state on
    `opentelemetry.trace`, meter state on `opentelemetry.metrics._internal`.
    Releasing the tracer latch says nothing about the meter, so this asserts
    the meter independently: without it, a reset that clears only the tracer
    would leave every metric assertion after the first reading the first
    test's reader.
    """
    config = load_config(lithos_lens_config_env)

    first = InMemoryMetricReader()
    setup_telemetry(config, _test_metric_reader=first)
    get_meter().create_counter("lens_test_first_total").add(1)

    shutdown_telemetry()
    _release_otel_provider_latches()

    second = InMemoryMetricReader()
    setup_telemetry(config, _test_metric_reader=second)
    get_meter().create_counter("lens_test_second_total").add(1)

    assert _metric_names(second) == {"lens_test_second_total"}


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    assert data is not None
    return {
        metric.name
        for resource_metric in (data.resource_metrics or [])
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
    }
