"""Shared pytest fixtures and helpers."""

import json
from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from lithos_lens.config import load_config
from lithos_lens.telemetry import setup_telemetry, shutdown_telemetry

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"


def load_contract(tool: str) -> dict[str, Any]:
    """Load a vendored Lithos tool contract (tests/contracts/<tool>.json).

    The contracts are the authoritative payload shapes for every Lithos tool
    the client calls — see tests/contracts/README.md and issue #31. Tests that
    need a canonical payload must load it from here, never restate it inline.
    """
    payload = json.loads((CONTRACTS_DIR / f"{tool}.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def lithos_lens_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a minimal lithos-lens.toml and point ``LITHOS_LENS_CONFIG`` at it.

    Env-var overrides are cleared so a developer's local ``.env`` cannot
    silently inject values via ``load_dotenv``.
    """
    data_dir = tmp_path / "data"
    config_path = tmp_path / "lithos-lens.toml"
    config_path.write_text(
        dedent(
            f"""
            [lithos-lens]
            environment = "test"
            greeting = "Hello"

            [lithos-lens.storage]
            data_dir = "{data_dir}"

            [lithos-lens.logging]
            level = "info"

            [lithos-lens.lithos]
            url = "http://lithos.test"
            mcp_sse_path = "/sse"
            sse_events_path = "/events"
            agent_id = "lithos-lens-test"
            """
        )
    )
    monkeypatch.setenv("LITHOS_LENS_CONFIG", str(config_path))
    monkeypatch.setenv("LITHOS_LENS_ENVIRONMENT", "")
    monkeypatch.setenv("LITHOS_LENS_DATA_DIR", "")
    monkeypatch.setenv("LITHOS_LENS_LOG_LEVEL", "")
    monkeypatch.setenv("LITHOS_LENS_LITHOS_URL", "")
    monkeypatch.setenv("LITHOS_LENS_MCP_SSE_PATH", "")
    monkeypatch.setenv("LITHOS_LENS_AGENT_ID", "")
    monkeypatch.setenv("LITHOS_LENS_TASKS_VISIBLE_CAP", "")
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", "")
    monkeypatch.setenv("LITHOS_LENS_TASKS_GATE_WAITING_ATTENTION_HOURS", "")
    monkeypatch.setenv("LITHOS_LENS_TASKS_CLAIM_EXPIRING_SOON_MINUTES", "")
    monkeypatch.setenv("LITHOS_LENS_TASKS_STALE_OPEN_AGE_DAYS", "")
    monkeypatch.setenv("LITHOS_LENS_TASKS_UNCLAIMED_READY_AGE_MINUTES", "")
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_ENABLED", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_MODEL", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_PROVIDER", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_API_KEY", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_BASE_URL", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_EXTRA_HEADERS_JSON", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_MAX_TOKENS", "")
    monkeypatch.setenv("LITHOS_LENS_OTEL_ENABLED", "")
    # Every OTLP endpoint `setup_telemetry` consults, Lens's own and the
    # standard OTEL ones. Not merely tidiness: telemetry is ON by default now,
    # so a developer or CI runner with OTEL_EXPORTER_OTLP_ENDPOINT exported
    # would turn nominally-hermetic tests into a live exporter and ship test
    # spans, metrics and LOG RECORDS to whatever collector that names. The
    # signal-specific variables take precedence over the base one, so clearing
    # only the base would leave the hole open.
    monkeypatch.setenv("LITHOS_LENS_OTEL_ENDPOINT", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "")
    return config_path


# ── telemetry fixtures, shared by every suite that asserts on OTEL ──


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

    # Clearing the globals is NOT enough, and the shortfall is silent.
    #
    # With `_METER_PROVIDER` set to None, `get_meter_provider()` falls back to
    # the module-level PROXY provider -- which caches the real provider it was
    # last pointed at and keeps delegating to it. So the next `get_meter()`
    # reaches the SHUT-DOWN provider and logs "A shutdown MeterProvider can not
    # provide a Meter", once per call, into whatever test is running by then.
    # That is how this was found: unrelated SSE tests asserting exact caplog
    # counts started seeing stray records.
    #
    # Same failure shape as the reset bug filed against lithos.telemetry
    # (task 41de9716) -- a reset that looks complete, leaves no error, and
    # quietly keeps the previous object alive.
    for proxy, attribute in (
        (getattr(metrics_internal, "_PROXY_METER_PROVIDER", None), "_meters"),
        (getattr(trace_api, "_PROXY_TRACER_PROVIDER", None), None),
    ):
        if proxy is None:  # pragma: no cover - layout differs across versions
            continue
        for field in vars(proxy):
            if field.startswith("_real_"):
                setattr(proxy, field, None)
        if attribute is not None and hasattr(proxy, attribute):
            getattr(proxy, attribute).clear()


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


@pytest.fixture
def metric_reader(
    lithos_lens_config_env: Path, telemetry_off: None
) -> InMemoryMetricReader:
    """Telemetry set up with an in-memory metric reader. No collector needed."""
    reader = InMemoryMetricReader()
    setup_telemetry(load_config(lithos_lens_config_env), _test_metric_reader=reader)
    return reader


def metric_points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    """Every data point recorded under ``name``, across all label sets."""
    data = reader.get_metrics_data()
    assert data is not None
    return [
        point
        for resource_metric in (data.resource_metrics or [])
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def metric_value(reader: InMemoryMetricReader, name: str, **labels: str) -> Any:
    """The single point recorded under ``name`` with exactly ``labels``."""
    matching = [
        point
        for point in metric_points(reader, name)
        if dict(point.attributes or {}) == labels
    ]
    assert len(matching) == 1, (
        f"expected one {name} point with {labels}, got "
        f"{[dict(p.attributes or {}) for p in metric_points(reader, name)]}"
    )
    return matching[0]
