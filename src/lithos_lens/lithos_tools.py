"""MCP tool-surface discovery: which tools does a server actually advertise.

Split out of ``lithos_client`` so the cursor walk and its failure contract sit
on their own: the answer drives task-graph feature detection, which reads
ABSENCE from this result, so an incomplete walk must be impossible to mistake
for a complete one.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Guard on the tools/list cursor walk; Lithos serves its surface in one page.
TOOL_LIST_MAX_PAGES = 20


class ToolListError(RuntimeError):
    """Raised when a ``tools/list`` enumeration could not be completed."""


async def collect_tool_names(session: Any) -> set[str]:
    """Collect tool names across every ``tools/list`` page.

    The MCP client does not auto-follow ``nextCursor`` (see
    scripts/dump_lithos_tools.py), so a paginating server would silently
    truncate a single-request listing.

    Raises :class:`ToolListError` unless the walk terminates on its own: a
    cursor still pending at ``TOOL_LIST_MAX_PAGES``, or one repeating instead
    of advancing, means the enumeration FAILED. Callers read absence from this
    result, so handing back the partial set would retire a tool the server does
    advertise, on a page the walk never reached.
    """
    names: set[str] = set()
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(TOOL_LIST_MAX_PAGES):
        result = await session.list_tools(cursor=cursor)
        names.update(
            str(getattr(tool, "name", "") or "")
            for tool in getattr(result, "tools", [])
        )
        cursor = getattr(result, "nextCursor", None)
        if not cursor:
            names.discard("")
            return names
        if cursor in seen:
            break
        seen.add(cursor)
    # Callers fail closed by swallowing this, so the log is the only trace.
    logger.warning(
        "tools/list did not finish within the %d-page guard; the listing is "
        "incomplete, NOT evidence that a tool is absent",
        TOOL_LIST_MAX_PAGES,
    )
    raise ToolListError(
        f"tools/list did not terminate within {TOOL_LIST_MAX_PAGES} pages"
    )
