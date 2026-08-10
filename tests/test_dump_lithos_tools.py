"""Unit tests for scripts/dump_lithos_tools.py.

The cursor-following ``list_all_tools`` helper is exercised over a fake
session, so a Lithos server that starts paginating tools/list cannot silently
truncate the snapshot or the live contract-verification sweep.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dump_lithos_tools import (  # noqa: E402 # pyright: ignore[reportMissingImports]
    list_all_tools,
)


class _PagedSession:
    """Fake MCP session serving tools/list in cursor-linked pages."""

    def __init__(self, pages: dict[str | None, tuple[list[str], str | None]]) -> None:
        self._pages = pages
        self.cursors_seen: list[str | None] = []

    async def list_tools(self, cursor: str | None = None) -> SimpleNamespace:
        self.cursors_seen.append(cursor)
        names, next_cursor = self._pages[cursor]
        return SimpleNamespace(
            tools=[SimpleNamespace(name=name) for name in names],
            nextCursor=next_cursor,
        )


def test_list_all_tools_follows_cursors_across_pages() -> None:
    session = _PagedSession(
        {
            None: (["lithos_read", "lithos_list"], "page-2"),
            "page-2": (["lithos_stats"], None),
        }
    )
    tools = asyncio.run(list_all_tools(session))
    assert [tool.name for tool in tools] == [  # type: ignore[attr-defined]
        "lithos_read",
        "lithos_list",
        "lithos_stats",
    ]
    assert session.cursors_seen == [None, "page-2"]


def test_list_all_tools_single_page_without_cursor() -> None:
    session = _PagedSession({None: (["lithos_read"], None)})
    tools = asyncio.run(list_all_tools(session))
    assert len(tools) == 1
    assert session.cursors_seen == [None]
