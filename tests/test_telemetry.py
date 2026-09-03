"""OpenTelemetry wiring: providers, server spans, and trace/log correlation.

Every test here runs against the REAL OTEL SDK. The packages are required
dependencies of lithos-lens, not an optional ``otel`` extra, so there is no
skip-if-absent guard and no code path CI leaves unrun — which is the point of
``test_the_otel_packages_are_required_not_optional`` below.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util.http import parse_excluded_urls

from lithos_lens import metrics
from lithos_lens.config import load_config
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.logging import MAX_LOGGED_VALUE_CHARS, JsonFormatter
from lithos_lens.telemetry import (
    HISTOGRAM_BUCKETS,
    TRACE_EXCLUDED_URLS,
    get_meter,
    get_tracer,
    setup_telemetry,
    shutdown_telemetry,
)
from lithos_lens.web import create_app
from tests.conftest import _release_otel_provider_latches, metric_value

REPO_ROOT = Path(__file__).resolve().parents[1]
# A note the demo dataset serves, so /note/{id} takes its rendered path.
DEMO_NOTE_ID = "note-influx-plan"


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


def test_shutdown_alone_does_not_permit_a_working_restart(
    lithos_lens_config_env: Path, telemetry_off: None
) -> None:
    """`shutdown_telemetry` is TERMINAL, and this pins that so it cannot be
    misread as supported.

    Setup -> shutdown -> setup, with NO latch release in between, which is all
    a production caller could do. OTEL keeps the first provider, so the second
    setup's exporter never receives anything. Asserting the limitation is the
    point: the failure mode it guards against is a future caller adding an
    in-process restart path, seeing no error, and losing every span from the
    restart onward to a discarded provider.

    The companion tests either side of this one show the same sequence WITH
    `_release_otel_provider_latches` working correctly -- so together they
    locate the behaviour precisely in the latch, not in Lens's own teardown.
    """
    config = load_config(lithos_lens_config_env)

    first = InMemorySpanExporter()
    setup_telemetry(config, _test_span_exporter=first)
    shutdown_telemetry()

    second = InMemorySpanExporter()
    setup_telemetry(config, _test_span_exporter=second)
    with get_tracer().start_as_current_span("after-restart"):
        pass

    assert second.get_finished_spans() == (), (
        "shutdown_telemetry() now permits a working restart -- if that is "
        "deliberate, update its docstring, which documents the opposite"
    )


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


def test_a_request_produces_no_incidental_spans(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    """No ASGI `http send` / `http receive` children.

    The instrumentation emits those by default, which made every request four
    spans rather than one. That is not merely storage: Tempo's metrics
    generator mints a Prometheus series per distinct span name, so
    `GET /note/{knowledge_id} http send` was being tracked alongside the route
    itself. Found against the live stack, not in this suite.

    Asserted by NAME rather than by count, because one deliberate child span
    exists: `lens.knowledge.related` is a phase within the note render with its
    own backend calls, so it earns a name (see `knowledge_routes`). A count
    assertion could not tell that apart from the noise this guards against.
    """
    with _client(lithos_lens_config_env) as client:
        assert client.get(f"/note/{DEMO_NOTE_ID}").status_code == 200

    names = sorted(span.name for span in spans.get_finished_spans())
    assert names == ["GET /note/{knowledge_id}", "lens.knowledge.related"], names


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


# ── endpoint resolution ───────────────────────────────────────────────
#
# The chain is four deep and only the middle two are obvious:
#   LITHOS_LENS_OTEL_ENDPOINT  (Lens env override, applied by the config loader)
#   > [lithos-lens.telemetry] endpoint
#   > OTEL_EXPORTER_OTLP_ENDPOINT   (the standard variable, lowest priority)
#   > nothing exported
# Asserted through the `lens.telemetry.initialized` record rather than by
# reaching for the private resolver, so these stay public-surface tests.


def _resolved_endpoint(config_path: Path, caplog: pytest.LogCaptureFixture) -> str:
    """The endpoint `setup_telemetry` actually resolved, read off its own event.

    Both test seams are supplied so no real OTLP exporter is built. That is not
    only speed: without them the log-export handler attaches to the root logger
    at NOTSET, every record in the process is queued as an OTLP log record, and
    shutdown spends seven seconds retrying them against a host that does not
    exist. The endpoint is resolved and logged before any exporter is chosen,
    so this still exercises the real resolution path.
    """
    with caplog.at_level(logging.INFO, logger="lithos_lens.telemetry"):
        setup_telemetry(
            load_config(config_path),
            _test_span_exporter=InMemorySpanExporter(),
            _test_metric_reader=InMemoryMetricReader(),
        )
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "lens_event", "") == "lens.telemetry.initialized"
    ]
    return record.__dict__["otel_endpoint"]


def _write_endpoint(config_path: Path, endpoint: str) -> None:
    config_path.write_text(
        config_path.read_text()
        + f'\n[lithos-lens.telemetry]\nendpoint = "{endpoint}"\n'
    )


def test_no_endpoint_anywhere_exports_nothing(
    lithos_lens_config_env: Path,
    telemetry_off: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default on a machine with no collector. Providers still install."""
    assert _resolved_endpoint(lithos_lens_config_env, caplog) == ""


def test_the_standard_otel_variable_is_the_lowest_priority_fallback(
    lithos_lens_config_env: Path,
    telemetry_off: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With nothing Lens-specific set, the ecosystem-standard variable is used
    -- so a container that already exports it for lithos/influx needs no
    Lens-specific configuration."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://standard:4318")

    assert _resolved_endpoint(lithos_lens_config_env, caplog) == "http://standard:4318"


def test_the_config_key_beats_the_standard_variable(
    lithos_lens_config_env: Path,
    telemetry_off: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deliberate Lens setting outranks an ambient one inherited from the
    environment -- otherwise a shell that happens to export the standard
    variable would silently redirect a configured deployment."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://standard:4318")
    _write_endpoint(lithos_lens_config_env, "http://configured:4318")

    assert (
        _resolved_endpoint(lithos_lens_config_env, caplog) == "http://configured:4318"
    )


def test_the_lens_env_override_beats_the_config_key(
    lithos_lens_config_env: Path,
    telemetry_off: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same direction as every other LITHOS_LENS_* override (README: env var ->
    config file -> default), so a container can retarget its collector without
    editing the mounted config."""
    monkeypatch.setenv("LITHOS_LENS_OTEL_ENDPOINT", "http://override:4318")
    _write_endpoint(lithos_lens_config_env, "http://configured:4318")

    assert _resolved_endpoint(lithos_lens_config_env, caplog) == "http://override:4318"


def test_the_shared_fixture_neutralizes_an_inherited_collector(
    lithos_lens_config_env: Path,
) -> None:
    """Guards the suite itself, not the app.

    Telemetry is on by default, so a developer or CI runner with an OTLP
    endpoint exported would turn nominally-hermetic tests into a live exporter
    and ship test spans, metrics and LOG RECORDS to a real backend. The fixture
    must blank every variable `setup_telemetry` consults -- including the
    signal-specific ones, which take precedence over the base.
    """
    import os

    for name in (
        "LITHOS_LENS_OTEL_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ):
        assert os.environ.get(name, "") == "", f"{name} leaks into the test suite"


# ── the OTLP log path is bounded too ──────────────────────────────────


def _exported_logs(config_path: Path, message: str, **extra: object) -> Any:
    """Emit one record through the real OTLP log pipeline, in memory."""
    exporter = InMemoryLogRecordExporter()
    setup_telemetry(load_config(config_path), _test_log_exporter=exporter)
    logging.getLogger("lithos_lens.probe").warning(message, extra=extra)
    return exporter.get_finished_logs()[-1].log_record


def test_the_exported_log_body_is_bounded(
    lithos_lens_config_env: Path, telemetry_off: None
) -> None:
    """`MAX_LOGGED_VALUE_CHARS` is applied in JsonFormatter, which the OTLP
    handler does not run -- it reads `record.getMessage()` directly.

    Without a bound this is the log-volume problem JsonFormatter's own comment
    describes, relocated to a path that costs collector storage rather than
    local log history: an unauthenticated request with a 47 KB query string
    writes a 47 KB uvicorn.access record, and that record is exported verbatim.
    """
    record = _exported_logs(lithos_lens_config_env, "x" * 5000)

    body = str(record.body)
    assert len(body) < 5000
    assert len(body) <= MAX_LOGGED_VALUE_CHARS + 64  # + the truncation marker
    assert "truncated, 5000 chars" in body


def test_exported_log_attributes_are_bounded(
    lithos_lens_config_env: Path, telemetry_off: None
) -> None:
    """Extras are the likelier carrier: structured fields hold the raw query
    string, tag lists and ids. Bounding only the message would leave the same
    volume travelling one field over."""
    record = _exported_logs(
        lithos_lens_config_env, "short message", lens_query="q" * 5000
    )

    attributes = record.attributes or {}
    assert len(str(attributes["lens_query"])) <= MAX_LOGGED_VALUE_CHARS + 64


def test_bounding_the_export_does_not_shorten_the_stdout_record(
    lithos_lens_config_env: Path, telemetry_off: None
) -> None:
    """The bound is a per-handler REPLACEMENT, not an edit of the shared record.

    An in-place mutation would shorten what every other handler receives, and
    which one saw the original would depend on the order they happen to run in.
    JsonFormatter must still see the full value and apply its own bound.
    """
    exporter = InMemoryLogRecordExporter()
    setup_telemetry(load_config(lithos_lens_config_env), _test_log_exporter=exporter)

    record = logging.LogRecord(
        name="lithos_lens.probe",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="z" * 5000,
        args=(),
        exc_info=None,
    )
    logging.getLogger("lithos_lens.probe").handle(record)

    assert len(record.getMessage()) == 5000, "the original record was mutated"
    assert "truncated, 5000 chars" in JsonFormatter().format(record)


def test_the_initialized_event_reports_every_active_signal(
    lithos_lens_config_env: Path,
    telemetry_off: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A metrics-only deployment IS exporting.

    The three pipelines are configured independently, so a flag derived from
    the span processor alone would tell the operator of a metrics-only or
    logs-only deployment that nothing was being exported while the data flowed.
    """
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://collector:4318/v1/metrics"
    )
    with caplog.at_level(logging.INFO, logger="lithos_lens.telemetry"):
        setup_telemetry(load_config(lithos_lens_config_env))

    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "lens_event", "") == "lens.telemetry.initialized"
    ]
    assert record.__dict__["otel_exporting_signals"] == ["metrics"]
    assert record.__dict__["otel_exporting"] is True


# ── Histogram resolution ──────────────────────────────────────────────


def test_every_histogram_has_explicit_buckets() -> None:
    """A histogram left on the SDK defaults cannot report a real percentile.

    The defaults are ``(0, 5, 10, 25, ... 10000)`` and are shaped for
    MILLISECONDS. Lens records seconds, so every healthy observation lands in
    the single bucket ``(0, 5]`` and ``histogram_quantile`` interpolates inside
    it -- p50 2.5s, p95 4.75s, whatever the real latency was. This walks the
    instrument catalogue rather than a hand-kept list, so a histogram added
    later without a view fails here instead of shipping a plausible-looking
    number nobody can tell is fabricated.
    """
    import ast

    source = Path("src/lithos_lens/metrics.py").read_text()
    declared: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "create_histogram" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            declared.add(first.value)

    assert declared, "found no histograms to check -- the AST walk is wrong"
    assert declared <= set(HISTOGRAM_BUCKETS), (
        f"histograms with no explicit buckets: "
        f"{sorted(declared - set(HISTOGRAM_BUCKETS))}"
    )
    assert set(HISTOGRAM_BUCKETS) <= declared, (
        f"buckets for instruments that no longer exist: "
        f"{sorted(set(HISTOGRAM_BUCKETS) - declared)}"
    )


def test_a_sub_second_call_is_not_reported_as_seconds(
    metric_reader: InMemoryMetricReader,
) -> None:
    """The observation has to land in a bucket that can distinguish it.

    130ms is roughly what a real Lithos call costs. Under the SDK defaults it
    shares the bucket ``(0, 5]`` with everything else sub-5-second, so the only
    percentile derivable from it is an interpolation across five seconds. Here
    it must land in ``(0.1, 0.25]``, which is narrow enough for a percentile to
    mean something.
    """
    metrics.lithos_tool_duration().record(0.13, {"tool": "lithos_read"})

    point = metric_value(
        metric_reader, "lens_lithos_tool_duration_seconds", tool="lithos_read"
    )
    bounds = list(point.explicit_bounds)
    counts = list(point.bucket_counts)
    occupied = [i for i, c in enumerate(counts) if c]

    assert len(occupied) == 1
    index = occupied[0]
    assert bounds[index] == 0.25
    assert bounds[index - 1] == 0.1


def test_the_fanout_cap_sits_on_a_bucket_boundary(
    metric_reader: InMemoryMetricReader,
) -> None:
    """ "Is the cap being hit" is only answerable if the cap is an edge.

    With the default boundaries the buckets jump 10 -> 25, so a fan-out of
    exactly 20 -- a note whose related panel was truncated -- is
    indistinguishable from one of 11, and a p95 anywhere in that range is
    interpolated. 19 and 20 are both boundaries here, so a truncated note lands
    in ``(19, 20]`` on its own.
    """
    metrics.knowledge_related_fanout().record(20)
    metrics.knowledge_related_fanout().record(11)

    point = metric_value(metric_reader, "lens_knowledge_related_fanout")
    bounds = list(point.explicit_bounds)
    counts = list(point.bucket_counts)
    at_cap = counts[bounds.index(20)]
    below = counts[bounds.index(12)]

    assert at_cap == 1, "the fan-out sitting ON the cap needs its own bucket"
    assert below == 1
