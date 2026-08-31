"""Shared pytest fixtures and helpers."""

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

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
