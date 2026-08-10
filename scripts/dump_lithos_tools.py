#!/usr/bin/env python3
"""Dump the live Lithos server's tool schemas into the vendored contracts dir.

Writes ``tests/contracts/_tools_snapshot.json`` — an advisory, diffable map of
EVERY live tool's description and input schema — so a hermetic (containerised)
agent can author a new client method against the real request schema instead
of inventing one (issue #31). The underscore prefix keeps it out of the
contract coverage check; the per-tool ``tests/contracts/<tool>.json`` files
remain the authoritative response-shape contracts.

Usage: uv run python scripts/dump_lithos_tools.py [--url URL] [--out PATH]
       (URL defaults to $LITHOS_URL, then http://localhost:8765)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "tests" / "contracts" / "_tools_snapshot.json"
DEFAULT_URL = "http://localhost:8765"
MCP_SSE_PATH = "/sse"


async def list_all_tools(session: object) -> list[object]:
    """Collect tools across every tools/list page.

    The MCP client does not auto-follow ``nextCursor``; a paginating server
    would silently truncate a single-request listing, so every consumer of
    the tool surface (this script and the live contract-verification test)
    goes through this cursor loop.
    """
    tools: list[object] = []
    cursor: str | None = None
    while True:
        result = await session.list_tools(cursor=cursor)  # type: ignore[attr-defined]
        tools.extend(result.tools)
        cursor = getattr(result, "nextCursor", None)
        if not cursor:
            return tools


async def _dump(url: str) -> dict[str, dict[str, object]]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    endpoint = f"{url.rstrip('/')}/{MCP_SSE_PATH.strip('/')}"
    async with (
        sse_client(endpoint) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        tools = await list_all_tools(session)
    return {
        tool.name: {  # type: ignore[attr-defined]
            "description": tool.description or "",  # type: ignore[attr-defined]
            "inputSchema": tool.inputSchema or {},  # type: ignore[attr-defined]
        }
        for tool in tools
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("LITHOS_URL", "").strip() or DEFAULT_URL,
        help=f"Lithos base URL (default: $LITHOS_URL, then {DEFAULT_URL})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"snapshot path (default: {DEFAULT_OUT.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args(argv)

    try:
        tools = asyncio.run(_dump(args.url))
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit
        print(f"error: could not list tools from {args.url}: {exc}", file=sys.stderr)
        return 1

    snapshot = {
        "_comment": (
            "Advisory dump of the live Lithos tools/list surface (refresh via "
            "`make contracts-snapshot`). NOT a contract file — response-shape "
            "contracts live in the sibling <tool>.json files."
        ),
        "url": args.url,
        "tools": tools,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so an interrupted run can't leave a truncated snapshot.
    tmp = args.out.with_name(args.out.name + ".tmp")
    tmp.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(args.out)
    sys.stdout.write(f"wrote {len(tools)} tool schemas to {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
