"""How a value is SPELLED in a template: the Jinja filter vocabulary.

Three filters and the constant behind one of them — ``display_date``,
``short_id`` and ``format_tag`` (registered in ``web.py``) — plus nothing else.
They share a responsibility that is not ``tasks.py``'s: that module defines the
task domain (records, sections, the filter/URL grammar the board is built
from), while these define how one already-decided value is rendered for a human
to read. A template asking "how is this spelled?" and a module answering "what
is a task?" are two questions, and this file is the first one.

Split out of ``tasks.py`` (2026-09-21) when the short-id work grew it past the
``max_module_lines`` stop-loss. ``docs/architecture.toml`` records the decision:
the seam was named a round earlier, and this discharges it rather than arguing
the ceiling up. It stays inside the **Tasks** component — the same
responsibility, one module further down — and imports ``parse_date`` from
``tasks.py``, which imports nothing back.
"""

from __future__ import annotations

from lithos_lens.tasks import parse_date

# How much of a task id Lens shows beside a title -- NOT a Lens choice: 8 is the
# prefix loom's gate and log lines, findings, PR bodies and the ROADMAP already
# type, and Lithos resolves any unambiguous prefix of 6 or more back to the
# whole id. Showing the same 8 is what lets a row, a gate or a graph entry be
# matched by eye to a line written somewhere else.
#
# `static/tasks.js` and `static/graph.js` each carry a copy for the two surfaces
# no template renders (the optimistic row, a graph search hit). They cut on a
# CODE POINT boundary, as `task_id[:8]` does over a Python str, and
# `tests/test_short_id.py` holds all three to the same prefix.
SHORT_ID_CHARS = 8


def short_id(task_id: str) -> str:
    """The id prefix the rest of the ecosystem names this task by.

    An id shorter than the prefix is returned WHOLE, not padded: it is already
    its own prefix, and there is nothing to elide.
    """
    return task_id[:SHORT_ID_CHARS]


def format_display_date(value: str) -> str:
    parsed = parse_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed else value


def format_tag(tag: str) -> str:
    if ":" not in tag:
        return tag
    key, value = tag.split(":", 1)
    return f"{key}: {value}"
