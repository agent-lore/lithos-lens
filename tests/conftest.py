"""Shared pytest fixtures and helpers."""

import json
from collections.abc import MutableMapping
from pathlib import Path
from textwrap import dedent
from typing import Any

import anyio
import pytest
from fastapi import FastAPI

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


class _Enough(Exception):
    """Stop the stream once the frames under test have been observed."""


async def stream_frames(
    app: FastAPI, path: str, count: int, *, query: str = ""
) -> list[bytes]:
    """The first ``count`` body frames the ASGI app writes for ``path``.

    Driven against the ASGI interface directly rather than through a test
    client: both TestClient and httpx's ASGITransport read a response to
    completion, which never happens for a stream that stays open. What these
    tests are about is what the stream writes WHILE it is open.
    """
    frames: list[bytes] = []
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"lens")],
        "client": ("127.0.0.1", 12345),
        "server": ("lens", 80),
    }

    async def receive() -> dict[str, Any]:
        # The client never disconnects and never sends more: this is a GET
        # whose response is the long-lived half.
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            frames.append(message["body"])
            if len(frames) >= count:
                raise _Enough

    try:
        with anyio.fail_after(5):
            await app(scope, receive, send)
    except _Enough:
        pass
    except BaseExceptionGroup as group:
        # Starlette runs the response in a task group, so the sentinel arrives
        # wrapped. Anything else is a real failure and must not be swallowed.
        if not all(isinstance(exc, _Enough) for exc in group.exceptions):
            raise
    return frames


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
    return config_path
