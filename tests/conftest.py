"""Shared pytest fixtures."""

from pathlib import Path
from textwrap import dedent

import pytest


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
    monkeypatch.setenv("LITHOS_LENS_LLM_ENABLED", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_MODEL", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_PROVIDER", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_API_KEY", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_BASE_URL", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_EXTRA_HEADERS_JSON", "")
    monkeypatch.setenv("LITHOS_LENS_LLM_MAX_TOKENS", "")
    monkeypatch.setenv("LITHOS_LENS_OTEL_ENABLED", "")
    return config_path
